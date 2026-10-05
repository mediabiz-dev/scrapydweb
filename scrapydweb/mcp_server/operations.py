# coding: utf-8
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from configparser import Error as ScrapyCfgParseError
from contextlib import contextmanager
from datetime import datetime
import glob
import json
import os
import re
from shutil import copyfile, rmtree
from subprocess import CalledProcessError, TimeoutExpired
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
import zlib

from flask import url_for
from logparser import __version__ as LOGPARSER_VERSION
from mcp.server.mcpserver.exceptions import ToolError
import regex as regex_lib
import requests
from sqlalchemy import func

from ..common import chunks, get_response_from_view, session
from ..models import Task, TaskJobResult, TaskResult, db
from ..servers import find_by_name, scrapyd_auth
from ..utils.scheduler import scheduler
from ..vars import (DEPLOY_PATH, LEGAL_NAME_PATTERN, SCHEDULER_STATE_DICT, STATE_PAUSED, STRICT_NAME_PATTERN,
                    jobs_table_map)
from ..views.operations.deploy import get_modification_time, run_pre_deploy_hook
from ..views.operations.scrapyd_deploy import _build_egg, get_config
from ..views.operations.utils import slot


SAFE_NAME_PATTERN = re.compile(r'^[\w.-]+$')
JOB_STATUSES = ['pending', 'running', 'finished']
CRON_FIELDS = ['year', 'month', 'day', 'week', 'day_of_week', 'hour', 'minute', 'second']

JOB_STATUS_MAP = {'0': 'pending', '1': 'running', '2': 'finished'}

MAX_WAIT_SECONDS = 120
FORCE_STOP_INTERVAL = 2
SUBPROCESS_TIMEOUT = 600
REGEX_TIMEOUT = 1
MAX_LINE_CHARS = 1000
MAX_RAW_LINE_BYTES = 1024 * 1024
MAX_SCAN_BYTES = 2 * 1024 ** 3
MAX_RESPONSE_CHARS = 200000
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500
MAX_PARSE_LOG_BYTES = 256 * 1024 ** 2

LOG_LEVELS_WITH_DETAILS = ['critical_logs', 'error_logs', 'warning_logs']
STATS_SUMMARY_KEYS = ['first_log_time', 'latest_log_time', 'runtime', 'pages', 'items',
                      'shutdown_reason', 'finish_reason', 'last_update_time']


def clip(text, limit=MAX_LINE_CHARS):
    if len(text) <= limit:
        return text
    return '%s... [%s more chars]' % (text[:limit], len(text) - limit)


def check_page(limit, offset):
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ToolError("limit should be between 1 and %s" % MAX_PAGE_SIZE)
    if offset < 0:
        raise ToolError("offset should not be negative")


def page_info(total, limit, offset):
    # next_offset is None on the last page
    return OrderedDict(total=total, offset=offset,
                       next_offset=offset + limit if offset + limit < total else None)


def web_auth(app):
    if app.config.get('ENABLE_AUTH', False):
        return (str(app.config.get('USERNAME', '')), str(app.config.get('PASSWORD', '')))
    return None


def get_view_url(app, endpoint, **kwargs):
    with app.test_request_context():
        return url_for(endpoint, **kwargs)


def check_names(**kwargs):
    for key, value in kwargs.items():
        if not SAFE_NAME_PATTERN.match(value or '') or value in ['.', '..']:
            raise ToolError("Invalid %s: %r, it may only contain letters, digits, '_', '-' and '.'" % (key, value))


def resolve_node(app, node):
    servers = app.config['SCRAPYD_SERVER_OBJECTS']
    node = str(node).strip()
    index = int(node) if node.isdigit() else find_by_name(servers, node)
    if not 0 < index <= len(servers):
        raise ToolError("Node %r not found, call list_nodes to get the names and indexes of the nodes" % node)
    return index, servers[index - 1]


def resolve_nodes(app, nodes):
    if not nodes:
        return list(enumerate(app.config['SCRAPYD_SERVER_OBJECTS'], 1))
    resolved = OrderedDict()
    for node in nodes:
        index, server = resolve_node(app, node)
        resolved[index] = server
    return list(resolved.items())


def node_info(index, server):
    return OrderedDict(node=index, name=server.name)


def run_on_nodes(app, func_, nodes):
    def run(args):
        with app.app_context():
            return func_(*args)

    with ThreadPoolExecutor(max_workers=min(len(nodes), 20) or 1) as executor:
        return list(executor.map(run, nodes))


def list_nodes(app):
    return OrderedDict(nodes=[
        OrderedDict(node=index, name=server.name, server='%s:%s' % (server.ip, server.port), group=server.group)
        for index, server in enumerate(app.config['SCRAPYD_SERVER_OBJECTS'], 1)
    ])


def find_scrapy_projects(app):
    projects_dir = app.config.get('SCRAPY_PROJECTS_DIR', '')
    if not projects_dir:
        return OrderedDict()
    scrapy_cfg_list = sorted(glob.glob(os.path.join(projects_dir, '*', 'scrapy.cfg')), key=lambda x: x.lower())
    return OrderedDict((os.path.basename(os.path.dirname(i)), os.path.dirname(i)) for i in scrapy_cfg_list)


def get_project_name(project_path, folder):
    try:
        return get_config(os.path.join(project_path, 'scrapy.cfg')).get('deploy', 'project') or folder
    except ScrapyCfgParseError:
        return folder


def get_version(project_path):
    return datetime.fromtimestamp(get_modification_time(project_path)).strftime('%Y-%m-%dT%H_%M_%S')


def list_deployable_projects(app):
    return OrderedDict(
        scrapy_projects_dir=app.config.get('SCRAPY_PROJECTS_DIR', ''),
        projects=[OrderedDict(folder=folder, project=get_project_name(path, folder), version=get_version(path))
                  for folder, path in find_scrapy_projects(app).items()]
    )


def deploy_project(app, folder=None, nodes=None, project=None, version=None):
    projects = find_scrapy_projects(app)
    if not projects:
        raise ToolError("No Scrapy projects found in SCRAPY_PROJECTS_DIR: %r"
                        % app.config.get('SCRAPY_PROJECTS_DIR', ''))
    if not folder:
        if len(projects) > 1:
            raise ToolError("Pass the folder of the project to deploy, one of: %s" % ', '.join(projects))
        folder = list(projects)[0]
    elif folder not in projects:
        raise ToolError("Folder %r not found in SCRAPY_PROJECTS_DIR on the ScrapydWeb server, use one of: %s"
                        % (folder, ', '.join(projects)))
    project_path = projects[folder]
    selected_nodes = resolve_nodes(app, nodes)

    project = re.sub(STRICT_NAME_PATTERN, '_', project or get_project_name(project_path, folder))
    version = re.sub(LEGAL_NAME_PATTERN, '-', version or get_version(project_path))

    try:
        run_pre_deploy_hook(project_path, project, version, timeout=SUBPROCESS_TIMEOUT)
    except TimeoutExpired as err:
        raise ToolError("Fail to run the pre_deploy_hook of %s: %s" % (folder, err))
    eggname = '%s_%s.egg' % (project, version)
    eggpath = os.path.join(DEPLOY_PATH, eggname)
    try:
        egg, tmpdir = _build_egg(os.path.join(project_path, 'scrapy.cfg'), timeout=SUBPROCESS_TIMEOUT)
    except (ScrapyCfgParseError, CalledProcessError, TimeoutExpired) as err:
        raise ToolError("Fail to build the egg of %s: %s" % (folder, err))
    copyfile(egg, eggpath)
    rmtree(tmpdir)
    with open(eggpath, 'rb') as f:
        slot.add_egg(eggname, f.read())

    auth = web_auth(app)

    def deploy(index, server):
        url = get_view_url(app, 'deploy.xhr', node=index, eggname=eggname, project=project, version=version)
        js = get_response_from_view(url, auth=auth, as_json=True)
        result = node_info(index, server)
        result.update((k, v) for k, v in js.items() if k in ['status', 'status_code', 'spiders', 'message'])
        return result

    results = run_on_nodes(app, deploy, selected_nodes)
    return OrderedDict(
        folder=folder,
        project=project,
        version=version,
        deployed=sum(r.get('status') == 'ok' for r in results),
        failed=sum(r.get('status') != 'ok' for r in results),
        nodes=results,
    )


def list_timer_tasks(app, project=None, spider=None, limit=DEFAULT_PAGE_SIZE, offset=0):
    check_page(limit, offset)
    query = Task.query
    if project:
        query = query.filter_by(project=project)
    if spider:
        query = query.filter_by(spider=spider)
    total = query.count()
    tasks = query.order_by(Task.id).offset(offset).limit(limit).all()

    latest_ids = [i for (i, ) in db.session.query(func.max(TaskResult.id)).filter(
        TaskResult.task_id.in_([task.id for task in tasks])).group_by(TaskResult.task_id)]
    latest_results = {}
    for chunk in chunks(latest_ids):
        latest_results.update((r.task_id, r) for r in TaskResult.query.filter(TaskResult.id.in_(chunk)))
    apscheduler_jobs = {job.id: job for job in scheduler.get_jobs(jobstore='default')}

    result = []
    for task in tasks:
        job = apscheduler_jobs.get(str(task.id))
        if not job:
            state = 'finished'
        elif job.next_run_time:
            state = 'scheduled'
        else:
            state = 'paused'
        latest = latest_results.get(task.id)
        result.append(OrderedDict(
            task_id=task.id,
            name=task.name,
            project=task.project,
            version=task.version,
            spider=task.spider,
            nodes=json.loads(task.selected_node_names),
            cron=OrderedDict((k, getattr(task, k)) for k in CRON_FIELDS),
            timezone=task.timezone,
            state=state,
            next_run_time=str(job.next_run_time) if job and job.next_run_time else None,
            last_run=OrderedDict(
                task_result_id=latest.id,
                execute_time=str(latest.execute_time),
                pass_count=latest.pass_count,
                fail_count=latest.fail_count,
            ) if latest else None,
        ))
    return OrderedDict(scheduler=SCHEDULER_STATE_DICT[scheduler.state], **page_info(total, limit, offset),
                       tasks=result)


def get_task_run(task_result_id):
    rows = TaskJobResult.query.filter_by(task_result_id=task_result_id).order_by(TaskJobResult.id).all()
    return [OrderedDict(node=r.node_name, server=r.server, status=r.status, run_time=str(r.run_time),
                        result=clip(r.result)) for r in rows]


def fire_timer_task(app, task_id, wait_seconds=0):
    last_result_id = db.session.query(func.max(TaskResult.id)).filter(TaskResult.task_id == task_id).scalar() or 0
    url = get_view_url(app, 'tasks.xhr', node=1, action='fire', task_id=task_id)
    js = get_response_from_view(url, auth=web_auth(app), as_json=True)
    if js.get('status') != 'ok':
        raise ToolError("Fail to fire task #%s: %s" % (task_id, js.get('message', js)))

    result = OrderedDict(task_id=task_id, status='fired')
    if scheduler.state == STATE_PAUSED:
        result['warning'] = ("The scheduler for timer tasks is paused, "
                             "the task would not run until it's resumed in the Timer Tasks page.")
    if wait_seconds <= 0:
        result['tip'] = "Call list_timer_tasks later to check out the result of this run."
        return result

    deadline = time.time() + min(wait_seconds, MAX_WAIT_SECONDS)
    while time.time() < deadline:
        time.sleep(1)
        db.session.rollback()
        task_result = (TaskResult.query.filter(TaskResult.task_id == task_id, TaskResult.id > last_result_id)
                       .order_by(TaskResult.id.desc()).first())
        if task_result and task_result.pass_count + task_result.fail_count > 0:
            result.update(task_result_id=task_result.id, pass_count=task_result.pass_count,
                          fail_count=task_result.fail_count, nodes=get_task_run(task_result.id))
            return result
    result['tip'] = ("The run didn't finish within %s seconds, "
                     "call list_timer_tasks later to check out its result." % min(wait_seconds, MAX_WAIT_SECONDS))
    return result


def request_scrapyd(server, path, data=None, **params):
    url = '%s/%s' % (server.url(), path)
    if data is None:
        r = session.get(url, params=params, auth=scrapyd_auth(server), timeout=30)
    else:
        r = session.post(url, data=data, auth=scrapyd_auth(server), timeout=30)
    r.raise_for_status()
    js = r.json()
    if js.get('status') != 'ok':
        raise ValueError(js.get('message', js))
    return js


def refresh_jobs_table(app, index):
    # The same as the auto-reload of the Jobs page: records the jobs listed by Scrapyd, with their pages and items
    url = get_view_url(app, 'jobs', node=index)
    js = get_response_from_view(url, auth=web_auth(app), data={}, as_json=True)
    if js.get('status') == 'error':
        message = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', str(js.get('message', '')))).strip()
        raise ValueError(clip(message, 300))


def str_or_none(value):
    return str(value) if value is not None else None


def list_jobs(app, nodes=None, status='running', project=None, spider=None, limit=DEFAULT_PAGE_SIZE, offset=0):
    if status not in JOB_STATUSES + ['all']:
        raise ToolError("status should be one of: %s" % ', '.join(JOB_STATUSES + ['all']))
    check_page(limit, offset)
    status_codes = {v: k for k, v in JOB_STATUS_MAP.items()}

    def fetch(index, server):
        errors = []
        try:
            refresh_jobs_table(app, index)
        except Exception as err:
            errors.append("Fail to refresh the jobs from Scrapyd, the jobs of this node may be out of date: %s: %s"
                          % (err.__class__.__name__, err))
        Job = jobs_table_map.get(index)
        if Job is None:
            return 0, [], errors + ["No jobs table for this node"]
        db.session.rollback()  # To see the rows committed by the refresh
        query = Job.query.filter_by(deleted='0')
        if status != 'all':
            query = query.filter_by(status=status_codes[status])
        if project:
            query = query.filter_by(project=project)
        if spider:
            query = query.filter_by(spider=spider)
        total = query.count()
        # Enough rows of each node to fill the page after merging the nodes
        records = query.order_by(Job.status.asc(), Job.start.desc(), Job.id.asc()).limit(offset + limit).all()
        jobs = [OrderedDict(
            node=index, name=server.name, status=JOB_STATUS_MAP.get(r.status),
            project=r.project, spider=r.spider, job=r.job, pid=r.pid,
            start_time=str_or_none(r.start), end_time=str_or_none(r.finish), runtime=r.runtime,
            pages=r.pages, items=r.items, update_time=str_or_none(r.update_time)
        ) for r in records]
        return total, jobs, errors

    def fetch_or_error(index, server):
        try:
            total, jobs, errors = fetch(index, server)
        except Exception as err:
            total, jobs, errors = 0, [], ['%s: %s' % (err.__class__.__name__, err)]
        error = None
        if errors:
            error = node_info(index, server)
            error['error'] = ' '.join(errors)
        return total, jobs, error

    results = run_on_nodes(app, fetch_or_error, resolve_nodes(app, nodes))
    jobs = [job for __, jobs_, __ in results for job in jobs_]
    # A stable order across the pages: by status, then the latest started first, pending ones in the queue order
    jobs.sort(key=lambda job: job['start_time'] or '', reverse=True)
    jobs.sort(key=lambda job: JOB_STATUSES.index(job['status']))
    return OrderedDict(
        **page_info(sum(total for total, __, __ in results), limit, offset),
        jobs=jobs[offset:offset + limit],
        errors=[error for __, __, error in results if error],
    )


def get_job_status(server, project, job):
    js = request_scrapyd(server, 'listjobs.json', project=project)
    for status in JOB_STATUSES:
        if any(job_.get('id') == job for job_ in js.get(status, [])):
            return status
    return None


def stop_job(app, node, project, job, force=False, wait_seconds=0):
    check_names(project=project, job=job)
    index, server = resolve_node(app, node)
    data = dict(project=project, job=job)
    try:
        prevstate = request_scrapyd(server, 'cancel.json', data=data).get('prevstate')
        if force and prevstate == 'running':
            time.sleep(FORCE_STOP_INTERVAL)
            request_scrapyd(server, 'cancel.json', data=data)
    except (requests.RequestException, ValueError) as err:
        raise ToolError("Fail to stop job %s/%s on node %s: %s: %s"
                        % (project, job, server.name, err.__class__.__name__, err))

    result = node_info(index, server)
    result.update(project=project, job=job, force=force, prevstate=prevstate)
    if prevstate is None:
        result['tip'] = ("The job was neither pending nor running on the node, it may have finished already. "
                         "Call list_jobs with status='all' to find it.")
        return result
    if prevstate == 'pending':
        result['tip'] = "The job was removed from the queue of the node before it started."
        return result

    wait_seconds = min(wait_seconds, MAX_WAIT_SECONDS)
    status = 'running'
    deadline = time.time() + wait_seconds
    while wait_seconds > 0 and time.time() < deadline:
        time.sleep(1)
        try:
            status = get_job_status(server, project, job)
        except (requests.RequestException, ValueError) as err:
            result['notes'] = ["Fail to check the status of the job: %s: %s" % (err.__class__.__name__, err)]
            break
        if status != 'running':
            break
    if wait_seconds > 0:
        result['status'] = status
    if status == 'running':
        still_running = " It's still running after %s seconds." % wait_seconds if wait_seconds > 0 else ''
        if force:
            result['tip'] = ("Scrapy is shutting down the spider.%s "
                             "Call list_jobs later to check that it has finished." % still_running)
        else:
            result['tip'] = ("Scrapy closes the spider gracefully, letting the requests in progress finish.%s "
                             "Call list_jobs later to check that it has finished, or stop it again with force=true "
                             "to shut it down right away." % still_running)
    return result


class LogStream(object):
    def __init__(self, url, response, gzipped, partial):
        self.url = url
        self.response = response
        self.gzipped = gzipped
        self.partial = partial
        self.bytes_scanned = 0
        self.lines_scanned = 0
        self.capped = False

    def chunks(self):
        if not self.gzipped:
            for chunk in self.response.iter_content(chunk_size=64 * 1024):
                yield chunk
            return
        decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
        for chunk in self.response.iter_content(chunk_size=64 * 1024):
            chunk = decompressor.decompress(chunk, MAX_RAW_LINE_BYTES)
            while True:
                yield chunk
                if not decompressor.unconsumed_tail:
                    break
                chunk = decompressor.decompress(decompressor.unconsumed_tail, MAX_RAW_LINE_BYTES)
        yield decompressor.flush()

    def lines(self):
        buffer = b''
        skip_first_line = self.partial
        for chunk in self.chunks():
            if self.bytes_scanned >= MAX_SCAN_BYTES:
                self.capped = True
                return
            self.bytes_scanned += len(chunk)
            lines = (buffer + chunk).split(b'\n')
            buffer = lines.pop()
            if len(buffer) > MAX_RAW_LINE_BYTES:
                lines.append(buffer)
                buffer = b''
            for line in lines:
                if skip_first_line:
                    skip_first_line = False
                    continue
                self.lines_scanned += 1
                yield line.decode('utf-8', errors='replace').rstrip('\r')
        if buffer and not skip_first_line:
            self.lines_scanned += 1
            yield buffer.decode('utf-8', errors='replace').rstrip('\r')

    def info(self):
        info = OrderedDict(log_url=self.url, lines_scanned=self.lines_scanned, bytes_scanned=self.bytes_scanned)
        notes = []
        if self.partial:
            notes.append("Only the tail of the log was scanned, line numbers count from where it starts.")
        if self.capped:
            notes.append("Stopped after scanning %s MB of the log, pass tail_mb to scan its end."
                         % (MAX_SCAN_BYTES // 1024 ** 2))
        if notes:
            info['notes'] = notes
        return info


def get_log_urls(app, server, project, spider, job):
    extensions = app.config.get('SCRAPYD_LOG_EXTENSIONS', None) or ['.log', '.log.gz', '.txt']
    return ['%s/logs/%s/%s/%s%s' % (server.url(), project, spider, job, ext) for ext in extensions]


@contextmanager
def open_job_log(app, node, project, spider, job, tail_mb=None):
    check_names(project=project, spider=spider, job=job)
    __, server = resolve_node(app, node)
    tried = []
    for url in get_log_urls(app, server, project, spider, job):
        headers = {}
        if tail_mb and not url.endswith('.gz'):
            headers['Range'] = 'bytes=-%s' % int(tail_mb * 1024 * 1024)
        try:
            r = session.get(url, auth=scrapyd_auth(server), headers=headers, stream=True, timeout=(10, 60))
        except requests.RequestException as err:
            tried.append('%s (%s)' % (url, err.__class__.__name__))
            continue
        if r.status_code not in [200, 206]:
            tried.append('%s (%s)' % (url, r.status_code))
            r.close()
            continue
        gzipped = url.endswith('.gz') and 'gzip' not in r.headers.get('Content-Encoding', '')
        try:
            yield LogStream(url, r, gzipped=gzipped, partial=r.status_code == 206)
        except (requests.RequestException, zlib.error) as err:
            raise ToolError("Fail to read %s: %s" % (url, err))
        finally:
            r.close()
        return
    raise ToolError("Log of job %s/%s/%s not found, tried: %s" % (project, spider, job, ', '.join(tried)))


def get_job_log_link(app, node, project, spider, job):
    # The link of the Source button of the Jobs page
    check_names(project=project, spider=spider, job=job)
    __, server = resolve_node(app, node)
    tried = []
    for url in get_log_urls(app, server, project, spider, job):
        try:
            r = session.head(url, auth=scrapyd_auth(server), timeout=10)
        except requests.RequestException as err:
            tried.append('%s (%s)' % (url, err.__class__.__name__))
            continue
        if r.status_code != 200:
            tried.append('%s (%s)' % (url, r.status_code))
            continue
        result = OrderedDict(log_url=public_link(server, urlsplit(url).path))
        if r.headers.get('Content-Length', '').isdigit():
            result['size_bytes'] = int(r.headers['Content-Length'])
        return embed_auth(app, server, result, 'log_url')
    raise ToolError("Log of job %s/%s/%s not found, tried: %s" % (project, spider, job, ', '.join(tried)))


def search_job_log(app, node, project, spider, job, pattern=None, regex=False, case_sensitive=False,
                   context_lines=0, max_matches=50, tail_mb=None, whole_log=False):
    if whole_log:
        return get_job_log_link(app, node, project, spider, job)
    if not pattern:
        raise ToolError("Pass a pattern to search for, or whole_log=true to get the link to the whole log")
    if regex:
        try:
            compiled = regex_lib.compile(pattern, 0 if case_sensitive else regex_lib.IGNORECASE)
        except regex_lib.error as err:
            raise ToolError("Invalid regex %r: %s" % (pattern, err))

        def is_match(line):
            return compiled.search(line, concurrent=True, timeout=REGEX_TIMEOUT) is not None
    else:
        needle = pattern if case_sensitive else pattern.lower()

        def is_match(line):
            return needle in (line if case_sensitive else line.lower())
    context_lines = max(0, min(context_lines, 20))
    max_matches = max(1, min(max_matches, 500))

    matches = []
    waiting_for_after = []
    before = deque(maxlen=context_lines)
    response_chars = 0
    tip = ''
    with open_job_log(app, node, project, spider, job, tail_mb) as log:
        for line in log.lines():
            clipped = clip(line)
            for match in list(waiting_for_after):
                match['after'].append(clipped)
                response_chars += len(clipped)
                if len(match['after']) == context_lines:
                    waiting_for_after.remove(match)
            if len(matches) == max_matches or response_chars > MAX_RESPONSE_CHARS:
                if not waiting_for_after:
                    limit = "max_matches=%s" % max_matches if len(matches) == max_matches else "the size limit"
                    tip = "Stopped at %s, there may be more matches below." % limit
                    break
            else:
                try:
                    matched = is_match(line)
                except TimeoutError:
                    raise ToolError("The regex took over %ss on line %s of the log, simplify it."
                                    % (REGEX_TIMEOUT, log.lines_scanned))
                if matched:
                    match = OrderedDict(line_number=log.lines_scanned, line=clipped)
                    response_chars += len(clipped)
                    if context_lines:
                        match['before'] = list(before)
                        match['after'] = []
                        response_chars += sum(len(i) for i in before)
                        waiting_for_after.append(match)
                    matches.append(match)
            before.append(clipped)
        result = log.info()
    result['matches'] = matches
    if tip:
        result['tip'] = tip
    return result


def url_with_auth(url, auth):
    # Percent-encode the credentials so that '@', ':' or '/' in them can't change the host of the URL
    parts = urlsplit(url)
    host = parts.hostname or ''
    if ':' in host:  # IPv6
        host = '[%s]' % host
    netloc = '%s:%s@%s' % (quote(str(auth[0]), safe=''), quote(str(auth[1]), safe=''), host)
    if parts.port:
        netloc += ':%s' % parts.port
    return urlunsplit(parts._replace(netloc=netloc))


def public_link(server, href):
    # The same base as the Log, Source and Items buttons of the Jobs page
    public_url = '%s/jobs' % server.public_url if server.public_url else 'http://%s:%s/jobs' % (server.ip, server.port)
    return urljoin(public_url, href)


def embed_auth(app, server, result, key):
    # Only the links handed to the agent, the Jobs page keeps relying on the browser to log in
    if not app.config.get('MCP_LINKS_WITH_AUTH', False):
        return result
    auth = scrapyd_auth(server)
    if auth and urlsplit(result[key]).scheme == 'https':
        result[key] = url_with_auth(result[key], auth)
        result['auth_embedded'] = True
    else:
        result['auth_embedded'] = False
        if auth:
            result.setdefault('notes', []).append("The login of the node is left out of the link since it isn't HTTPS.")
    return result


def get_job_items_link(app, node, project, spider, job):
    from ..views.dashboard.jobs import get_items_href

    check_names(project=project, spider=spider, job=job)
    index, server = resolve_node(app, node)
    result = node_info(index, server)
    result.update(project=project, spider=spider, job=job)

    status = start = href_items = None
    try:
        js = request_scrapyd(server, 'listjobs.json', project=project)
    except (requests.RequestException, ValueError) as err:
        js = {}
        result['notes'] = ["Fail to list the jobs of the node: %s: %s" % (err.__class__.__name__, err)]
    for status_ in JOB_STATUSES:
        for job_ in js.get(status_, []):
            if job_.get('id') == job and job_.get('spider') == spider:
                status, start = status_, (job_.get('start_time') or '')[:19]
    if status is None and jobs_table_map.get(index) is not None:
        record = jobs_table_map[index].query.filter_by(project=project, spider=spider, job=job).first()
        if record:
            status, start, href_items = JOB_STATUS_MAP.get(record.status), str(record.start or ''), record.href_items
    if status is None:
        raise ToolError("Job %s/%s/%s not found on node %s, neither by Scrapyd nor in the Jobs page, "
                        "call list_jobs to find it." % (project, spider, job, server.name))
    if status == 'pending' or not start:
        raise ToolError("Job %s/%s/%s is pending, it has no items yet." % (project, spider, job))

    href_items = href_items or get_items_href(spider, job, start, finished=status == 'finished')
    items_url = public_link(server, href_items)
    result.update(status=status, start_time=start, items_url=items_url,
                  format='zipped CSV' if items_url.endswith('.zip') else 'CSV')

    try:
        r = session.head(urljoin('%s/jobs' % server.url(), href_items), auth=scrapyd_auth(server), timeout=10)
    except requests.RequestException as err:
        result['available'] = None
        result.setdefault('notes', []).append("Fail to check the link: %s" % err.__class__.__name__)
    else:
        result['available'] = r.status_code == 200
        if r.status_code == 200 and r.headers.get('Content-Length', '').isdigit():
            result['size_bytes'] = int(r.headers['Content-Length'])
        elif r.status_code != 200:
            result.setdefault('notes', []).append(
                "The link got status code %s. Its file name comes from the start minute of the job, "
                "see the directory listing: %s" % (r.status_code, urljoin(items_url, '.')))
    return embed_auth(app, server, result, 'items_url')


def curate_stats(stats, include_log_details, include_tail):
    result = OrderedDict((k, stats.get(k)) for k in STATS_SUMMARY_KEYS if k in stats)
    latest_log_timestamp = stats.get('latest_log_timestamp')
    if latest_log_timestamp:
        result['seconds_since_latest_log'] = int(time.time() - latest_log_timestamp)
    log_categories = stats.get('log_categories') or {}
    result['log_counts'] = OrderedDict((k.replace('_logs', ''), v.get('count'))
                                       for k, v in log_categories.items())
    log_details = OrderedDict((k.replace('_logs', ''), [clip(i) for i in log_categories[k]['details']])
                              for k in LOG_LEVELS_WITH_DETAILS if log_categories.get(k, {}).get('details'))
    if include_log_details and log_details:
        result['log_details'] = log_details
    result['latest_matches'] = OrderedDict((k, clip(v)) for k, v in (stats.get('latest_matches') or {}).items()
                                           if not k.startswith('telnet_'))
    if stats.get('crawler_stats'):
        result['crawler_stats'] = stats['crawler_stats']
    if include_tail and stats.get('tail'):
        result['tail'] = clip(stats['tail'], MAX_RESPONSE_CHARS // 10)
    return result


def check_log_size(app, server, project, spider, job):
    for url in get_log_urls(app, server, project, spider, job):
        try:
            r = session.head(url, auth=scrapyd_auth(server), timeout=10)
        except requests.RequestException:
            continue
        size = r.headers.get('Content-Length', '')
        if r.status_code != 200 or not size.isdigit():
            continue
        limit = MAX_PARSE_LOG_BYTES // 10 if url.endswith('.gz') else MAX_PARSE_LOG_BYTES
        if int(size) > limit:
            raise ToolError("No stats by LogParser for this job, and its log %s is too big (%s MB) to parse here. "
                            "Use search_job_log instead." % (url, int(size) // 1024 ** 2))
        return


def get_job_stats(app, node, project, spider, job, include_log_details=True, include_tail=False):
    check_names(project=project, spider=spider, job=job)
    index, server = resolve_node(app, node)
    notes = []

    json_url = '%s/logs/%s/%s/%s.json' % (server.url(), project, spider, job)
    stats = None
    try:
        r = session.get(json_url, auth=scrapyd_auth(server), timeout=30)
        if r.status_code == 200:
            js = r.json()
            if js.get('logparser_version') == LOGPARSER_VERSION:
                stats = js
            else:
                notes.append("Ignored %s by LogParser v%s, which should be v%s"
                             % (json_url, js.get('logparser_version'), LOGPARSER_VERSION))
        else:
            notes.append("%s got status code %s" % (json_url, r.status_code))
    except (requests.RequestException, ValueError) as err:
        notes.append("%s: %s" % (json_url, err.__class__.__name__))

    if stats is not None:
        result = OrderedDict(source='logparser', stats_url=json_url)
    else:
        check_log_size(app, server, project, spider, job)
        url = get_view_url(app, 'log', node=index, opt='report', project=project, spider=spider, job=job)
        stats = get_response_from_view(url, auth=web_auth(app), as_json=True)
        if stats.get('status') == 'error' and 'pages' not in stats:
            raise ToolError("Stats of job %s/%s/%s not found on node %s. %s"
                            % (project, spider, job, server.name, ' '.join(notes)))
        result = OrderedDict(source='report')
        notes.append("Only the summary and the log counts are available without the json file by LogParser.")
    result.update(node_info(index, server))
    result.update(project=project, spider=spider, job=job)
    result.update(curate_stats(stats, include_log_details, include_tail))
    if notes:
        result['notes'] = notes
    return result
