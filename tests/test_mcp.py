# coding: utf-8
import asyncio
import base64
from datetime import datetime
import gzip
import os
from shutil import copy, rmtree
import socket
import time

import pytest

pytest.importorskip('mcp')

from flask import url_for
import httpx2
from logparser import __version__ as LOGPARSER_VERSION
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver.exceptions import ToolError

from scrapydweb.mcp_server import create_asgi_app, operations, start_mcp_server
from scrapydweb.mcp_server.auth import BasicAuthMiddleware
from scrapydweb.metrics import init_metrics
from scrapydweb.models import Task, db
from scrapydweb.utils.check_app_config import check_mcp_config
from scrapydweb.vars import jobs_table_map
from scrapydweb.views.operations.deploy import get_modification_time
from tests.utils import (cst, find_samples, get_metric_samples, req, req_single_scrapyd, sleep,
                         upload_file_deploy)


MCP_USERNAME = 'mcp-user'
MCP_PASSWORD = 'mcp-password'
TOOLS = ['list_nodes', 'list_deployable_projects', 'deploy_project', 'list_timer_tasks', 'fire_timer_task',
         'list_jobs', 'stop_job', 'get_job_stats', 'search_job_log', 'get_job_items_link']


def basic_auth(username, password):
    return 'Basic %s' % base64.b64encode(('%s:%s' % (username, password)).encode('utf-8')).decode('ascii')


def run(app, func, *args, **kwargs):
    with app.app_context():
        return func(app, *args, **kwargs)


def get_free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture
def mcp_url(app):
    app.config.update(MCP_BIND='127.0.0.1', MCP_PORT=get_free_port(),
                      MCP_USERNAME=MCP_USERNAME, MCP_PASSWORD=MCP_PASSWORD)
    server = start_mcp_server(app)
    assert server.started
    yield 'http://127.0.0.1:%s/mcp' % app.config['MCP_PORT']
    server.should_exit = True


@pytest.fixture
def demo_job(app):
    project_path = os.path.join(app.config['LOCAL_SCRAPYD_LOGS_DIR'], 'mcp_demo')
    spider_path = os.path.join(project_path, cst.SPIDER)
    os.makedirs(spider_path, exist_ok=True)
    copy(os.path.join(cst.ROOT_DIR, 'data', cst.DEMO_LOG), os.path.join(spider_path, 'mcp_job.log'))
    yield dict(node=1, project='mcp_demo', spider=cst.SPIDER, job='mcp_job')
    rmtree(project_path, ignore_errors=True)


def asgi_call(headers=None, scope_type='http'):
    calls = []
    messages = []

    async def app(scope, receive, send):
        calls.append(scope['type'])

    async def receive():
        return {'type': 'http.request', 'body': b''}

    async def send(message):
        messages.append(message)

    scope = dict(type=scope_type, headers=[(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()])
    asyncio.run(BasicAuthMiddleware(app, MCP_USERNAME, MCP_PASSWORD)(scope, receive, send))
    return calls, messages


def test_basic_auth_middleware():
    for authorization in [None, basic_auth(MCP_USERNAME, 'wrong'), basic_auth('wrong', MCP_PASSWORD),
                          basic_auth(MCP_USERNAME, ''), 'Bearer %s' % MCP_PASSWORD, 'Basic not-base64!']:
        calls, messages = asgi_call({'Authorization': authorization} if authorization else None)
        assert calls == []
        assert messages[0]['status'] == 401
        assert (b'www-authenticate', b'Basic realm="ScrapydWeb MCP"') in messages[0]['headers']

    calls, messages = asgi_call({'Authorization': basic_auth(MCP_USERNAME, MCP_PASSWORD)})
    assert calls == ['http'] and messages == []
    calls, messages = asgi_call(scope_type='lifespan')
    assert calls == ['lifespan'] and messages == []


def test_mcp_over_http(app, mcp_url):
    async def main():
        async with httpx2.AsyncClient() as http:
            response = await http.post(mcp_url, json=dict(jsonrpc='2.0', id=1, method='tools/list'))
            assert response.status_code == 401

        headers = {'Authorization': basic_auth(MCP_USERNAME, MCP_PASSWORD)}
        async with httpx2.AsyncClient(headers=headers) as http:
            async with Client(streamable_http_client(mcp_url, http_client=http)) as client:
                fake_job = dict(node='fake-node', project='p', spider='s', job='j')
                return (await client.list_tools(),
                        await client.call_tool('list_nodes', {}),
                        await client.call_tool('get_job_stats', fake_job))

    tools, nodes, error = asyncio.run(main())
    assert sorted(tool.name for tool in tools.tools) == sorted(TOOLS)
    assert all(tool.description for tool in tools.tools)
    assert [tool.annotations.read_only_hint for tool in tools.tools if tool.name == 'list_nodes'] == [True]
    assert [tool.annotations.destructive_hint for tool in tools.tools if tool.name == 'stop_job'] == [True]
    assert not nodes.is_error
    assert [n['node'] for n in nodes.structured_content['nodes']] == [1, 2]
    assert error.is_error and "Node 'fake-node' not found" in error.content[0].text


def test_mcp_metrics(app):
    init_metrics(app)
    app.config.update(MCP_BIND='127.0.0.1', MCP_PORT=get_free_port(),
                      MCP_USERNAME=MCP_USERNAME, MCP_PASSWORD=MCP_PASSWORD)
    server = start_mcp_server(app)
    mcp_url = 'http://127.0.0.1:%s/mcp' % app.config['MCP_PORT']

    async def main():
        async with httpx2.AsyncClient() as http:
            response = await http.post(mcp_url, json=dict(jsonrpc='2.0', id=1, method='tools/list'))
            assert response.status_code == 401

        headers = {'Authorization': basic_auth(MCP_USERNAME, MCP_PASSWORD)}
        async with httpx2.AsyncClient(headers=headers) as http:
            async with Client(streamable_http_client(mcp_url, http_client=http)) as client:
                await client.call_tool('list_nodes', {})
                await client.call_tool('get_job_stats', dict(node='fake-node', project='p', spider='s', job='j'))

    try:
        asyncio.run(main())
    finally:
        server.should_exit = True

    samples = get_metric_samples(app.test_client())
    assert find_samples(samples, 'scrapydweb_mcp_http_requests_total', status='401') == [1]
    assert find_samples(samples, 'scrapydweb_mcp_http_requests_total', status='200')[0] >= 2
    assert find_samples(samples, 'scrapydweb_mcp_tool_calls_total', tool='list_nodes', status='ok') == [1]
    assert find_samples(samples, 'scrapydweb_mcp_tool_calls_total', tool='get_job_stats', status='error') == [1]
    assert find_samples(samples, 'scrapydweb_mcp_tool_duration_seconds_count', tool='list_nodes') == [1]
    # The metrics of the MCP server are created once per app, so building its app again does not fail
    create_asgi_app(app)


def test_mcp_allowed_hosts(app):
    app.config.update(MCP_BIND='127.0.0.1', MCP_PORT=get_free_port(), MCP_USERNAME=MCP_USERNAME,
                      MCP_PASSWORD=MCP_PASSWORD, MCP_ALLOWED_HOSTS=['scrapydweb.example.com'])
    server = start_mcp_server(app)
    try:
        response = httpx2.post('http://127.0.0.1:%s/mcp' % app.config['MCP_PORT'],
                               json=dict(jsonrpc='2.0', id=1, method='tools/list'),
                               headers={'Authorization': basic_auth(MCP_USERNAME, MCP_PASSWORD)})
        assert response.status_code == 421
    finally:
        server.should_exit = True


def test_list_nodes(app):
    nodes = run(app, operations.list_nodes)['nodes']
    assert [(n['node'], n['server']) for n in nodes] == [(1, '127.0.0.1:6800'), (2, 'scrapydweb-fake-domain.com:443')]
    assert run(app, operations.resolve_node, nodes[1]['name'])[0] == 2
    assert run(app, operations.resolve_node, '2')[0] == 2
    for node in [0, 3, 'fake-node']:
        with pytest.raises(ToolError, match="not found"):
            run(app, operations.resolve_node, node)


def test_deploy_project(app, client):
    projects_dir = app.config['SCRAPY_PROJECTS_DIR']
    projects = run(app, operations.list_deployable_projects)['projects']
    demo = [p for p in projects if p['folder'] == 'demo'][0]
    assert demo['project'] == 'demo'
    timestamp = get_modification_time(os.path.join(projects_dir, 'demo'))
    assert demo['version'] == datetime.fromtimestamp(timestamp).strftime('%Y-%m-%dT%H_%M_%S')

    for folder in ['../data/demo', os.path.join(projects_dir, 'demo'), '/etc', 'fake-folder']:
        with pytest.raises(ToolError, match="not found in SCRAPY_PROJECTS_DIR"):
            run(app, operations.deploy_project, folder=folder)
    with pytest.raises(ToolError, match="Pass the folder"):
        run(app, operations.deploy_project)

    project = 'demo_mcp'
    try:
        result = run(app, operations.deploy_project, folder='demo', project=project, version=cst.VERSION)
        assert (result['project'], result['version'], result['deployed'], result['failed']) == (
            project, cst.VERSION, 1, 1)
        node_1, node_2 = result['nodes']
        assert node_1['node'] == 1 and node_1['status'] == 'ok' and node_1['spiders'] > 0
        assert node_2['node'] == 2 and node_2['status'] == 'error'
        assert all('auth' not in node for node in result['nodes'])

        result = run(app, operations.deploy_project, folder='demo', nodes=['1'], project=project)
        assert result['version'] == demo['version'] and result['deployed'] == 1 and result['failed'] == 0
    finally:
        req(app, client, view='api', kws=dict(node=1, opt='delproject', project=project))


def test_list_jobs(app):
    result = run(app, operations.list_jobs, nodes=[1], status='all')
    assert result['errors'] == []
    assert all(job['node'] == 1 and job['status'] in operations.JOB_STATUSES for job in result['jobs'])

    result = run(app, operations.list_jobs)
    assert [error['node'] for error in result['errors']] == [2]

    with pytest.raises(ToolError, match="status should be one of"):
        run(app, operations.list_jobs, status='fake-status')


def test_timer_task(app, client):
    project = 'mcp_task_demo'
    upload_file_deploy(app, client, filename='ScrapydWeb_demo_no_request.egg', project=project,
                       redirect_project=project)
    req_single_scrapyd(app, client, view='tasks.xhr', kws=dict(node=1, action='enable'))
    name = 'mcp_task'
    data = dict(project=project, _version=cst.DEFAULT_LATEST_VERSION, spider=cst.SPIDER, jobid=cst.JOBID,
                year='2036', month='12', day='31', week='*', day_of_week='*', hour='2', minute='3', second='4',
                start_date='', end_date='', timezone='Asia/Shanghai', jitter='0', misfire_grace_time='600',
                coalesce='True', max_instances='1', task_id='0', action='add', trigger='cron', name=name,
                replace_existing='True')
    req_single_scrapyd(app, client, view='schedule.check', kws=dict(node=1), data=data)
    filename = '%s_%s_%s.pickle' % (project, 'default-the-latest-version', cst.SPIDER)
    with app.test_request_context():
        location = url_for('tasks', node=1)
    req_single_scrapyd(app, client, view='schedule.run', kws=dict(node=1), data=dict(filename=filename),
                       location=location)
    with app.app_context():
        task_id = Task.query.filter_by(name=name).order_by(Task.id.desc()).first().id

    try:
        tasks = run(app, operations.list_timer_tasks, project=project)
        assert tasks['scheduler'] == 'STATE_RUNNING'
        assert len(tasks['tasks']) == 1
        task = tasks['tasks'][0]
        assert (task['task_id'], task['spider'], task['state'], task['last_run']) == (
            task_id, cst.SPIDER, 'scheduled', None)
        assert task['nodes'] == ['127.0.0.1:6800'] and task['cron']['year'] == '2036'

        result = run(app, operations.fire_timer_task, task_id, wait_seconds=60)
        assert result['status'] == 'fired' and 'warning' not in result
        assert (result['pass_count'], result['fail_count']) == (1, 0)
        assert result['nodes'][0]['status'] == 'ok' and result['nodes'][0]['result'].startswith('task_%s' % name)

        task = run(app, operations.list_timer_tasks, project=project)['tasks'][0]
        assert task['last_run']['task_result_id'] == result['task_result_id']

        with pytest.raises(ToolError, match="apscheduler_job #%s not found" % cst.BIGINT):
            run(app, operations.fire_timer_task, cst.BIGINT)
    finally:
        req_single_scrapyd(app, client, view='tasks.xhr', kws=dict(node=1, action='delete', task_id=task_id))
        for __ in range(30):
            jobs = run(app, operations.list_jobs, nodes=[1], status='all', project=project)['jobs']
            if all(job['status'] == 'finished' for job in jobs):
                break
            sleep(1)
        req(app, client, view='api', kws=dict(node=1, opt='delproject', project=project))
        rmtree(os.path.join(app.config['LOCAL_SCRAPYD_LOGS_DIR'], project), ignore_errors=True)


def test_stop_job(app, client):
    project = 'mcp_stop_demo'
    # In ScrapydWeb_demo.egg: CONCURRENT_REQUESTS=1, DOWNLOAD_DELAY=10
    upload_file_deploy(app, client, filename='ScrapydWeb_demo.egg', project=project, redirect_project=project)

    def start_job():
        __, js = req(app, client, view='api', kws=dict(node=1, opt='start', project=project,
                                                       version_spider_job=cst.SPIDER))
        for __ in range(30):
            if operations.get_job_status(app.config['SCRAPYD_SERVER_OBJECTS'][0], project, js['jobid']) == 'running':
                return js['jobid']
            sleep(1)
        raise AssertionError("Job %s didn't start" % js['jobid'])

    try:
        job = start_job()
        result = run(app, operations.stop_job, 1, project, job, wait_seconds=60)
        assert (result['node'], result['job'], result['prevstate'], result['status']) == (1, job, 'running', 'finished')
        assert 'tip' not in result

        result = run(app, operations.stop_job, 1, project, job)
        assert result['prevstate'] is None and 'finished already' in result['tip']

        job = start_job()
        result = run(app, operations.stop_job, 1, project, job, force=True)
        assert result['prevstate'] == 'running' and result['force'] is True and 'status' not in result
        assert 'shutting down' in result['tip']

        with pytest.raises(ToolError, match="Invalid job"):
            run(app, operations.stop_job, 1, project, '../job')
        with pytest.raises(ToolError, match="Fail to stop job"):
            run(app, operations.stop_job, 2, project, job)
    finally:
        for __ in range(30):
            jobs = run(app, operations.list_jobs, nodes=[1], status='all', project=project)['jobs']
            if all(job_['status'] == 'finished' for job_ in jobs):
                break
            sleep(1)
        req(app, client, view='api', kws=dict(node=1, opt='delproject', project=project))
        rmtree(os.path.join(app.config['LOCAL_SCRAPYD_LOGS_DIR'], project), ignore_errors=True)


def test_search_job_log(app, demo_job):
    result = run(app, operations.search_job_log, pattern='spider closed', context_lines=1, **demo_job)
    assert result['log_url'].endswith('/logs/mcp_demo/%s/mcp_job.log' % cst.SPIDER)
    assert len(result['matches']) == 1
    match = result['matches'][0]
    assert 'Spider closed (finished)' in match['line']
    assert len(match['before']) == 1 and len(match['after']) <= 1
    assert result['lines_scanned'] >= match['line_number'] > 1

    result = run(app, operations.search_job_log, pattern=r'Crawled \(\d+\)', regex=True, max_matches=2, **demo_job)
    assert len(result['matches']) == 2 and 'max_matches=2' in result['tip']
    assert run(app, operations.search_job_log, pattern='SPIDER CLOSED', case_sensitive=True,
               **demo_job)['matches'] == []
    assert run(app, operations.search_job_log, pattern='Crawled (200)', **demo_job)['matches']

    result = run(app, operations.search_job_log, pattern='Spider closed', tail_mb=0.001, **demo_job)
    assert result['bytes_scanned'] <= 1048 and 'tail of the log' in result['notes'][0]
    assert len(result['matches']) == 1

    with pytest.raises(ToolError, match="Invalid regex"):
        run(app, operations.search_job_log, pattern='(', regex=True, **demo_job)
    with pytest.raises(ToolError, match="not found, tried"):
        run(app, operations.search_job_log, pattern='x', **dict(demo_job, job=cst.FAKE_JOBID))
    with pytest.raises(ToolError, match="Invalid job"):
        run(app, operations.search_job_log, pattern='x', **dict(demo_job, job='../../etc/passwd'))


def test_search_job_log_with_catastrophic_regex(app, demo_job):
    log_path = os.path.join(app.config['LOCAL_SCRAPYD_LOGS_DIR'], demo_job['project'], demo_job['spider'], 'redos.log')
    with open(log_path, 'w') as f:
        f.write('a' * 60 + 'b\n')
    started = time.time()
    with pytest.raises(ToolError, match="The regex took over 1s on line 1"):
        run(app, operations.search_job_log, pattern='^(a|aa)+$', regex=True, **dict(demo_job, job='redos'))
    assert time.time() - started < 10


class FakeResponse(object):
    def __init__(self, status_code=200, content=b'', headers=None, js=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.js = js

    def iter_content(self, chunk_size):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def json(self):
        return self.js

    def raise_for_status(self):
        pass

    def close(self):
        pass


def test_log_stream(monkeypatch):
    monkeypatch.setattr(operations, 'MAX_RAW_LINE_BYTES', 100)
    content = b'first\r\n' + b'x' * 250 + b'\nlast'
    log = operations.LogStream('url', FakeResponse(content=gzip.compress(content)), gzipped=True, partial=False)
    lines = list(log.lines())
    assert lines[0] == 'first' and lines[-1] == 'last'
    assert all(len(line) <= 200 for line in lines) and ''.join(lines[1:-1]) == 'x' * 250
    assert log.bytes_scanned == len(content) and log.lines_scanned == len(lines)

    log = operations.LogStream('url', FakeResponse(content=b'cut line\nsecond\n'), gzipped=False, partial=True)
    assert list(log.lines()) == ['second'] and 'tail of the log' in log.info()['notes'][0]


def test_get_job_items_link(app, client, monkeypatch):
    from scrapydweb.views.dashboard.jobs import get_items_href

    finished = dict(id='task_tubi_minibot_CA_2026-09-30T10_05_02', spider='tubi', project='demo',
                    start_time='2026-09-30 10:05:16.123456', end_time='2026-09-30 10:07:35.654321')
    running = dict(id='task_tubi_minibot_US_2026-09-30T10_06_02', spider='tubi', project='demo',
                   start_time='2026-09-30 10:06:59.000001')
    pending = dict(id='pending_job', spider='tubi', project='demo')
    available = {'http://127.0.0.1:6800/items/archive/tubi/CA/tubi_CA_20260930-10-05.csv.zip': '1234'}

    class Session(object):
        def get(self, url, params=None, **kwargs):
            assert url == 'http://127.0.0.1:6800/listjobs.json' and params == dict(project='demo')
            return FakeResponse(js=dict(status='ok', pending=[pending], running=[running], finished=[finished]))

        def head(self, url, **kwargs):
            if url in available:
                return FakeResponse(headers={'Content-Length': available[url]})
            return FakeResponse(status_code=404)

    monkeypatch.setattr(operations, 'session', Session())
    job = dict(node=1, project='demo', spider='tubi')

    result = run(app, operations.get_job_items_link, job=finished['id'], **job)
    assert (result['status'], result['start_time'], result['format']) == (
        'finished', '2026-09-30 10:05:16', 'zipped CSV')
    assert result['items_url'] == 'http://127.0.0.1:6800/items/archive/tubi/CA/tubi_CA_20260930-10-05.csv.zip'
    assert result['items_url'].endswith(get_items_href('tubi', finished['id'], '2026-09-30 10:05:16', True))
    assert result['available'] is True and result['size_bytes'] == 1234

    result = run(app, operations.get_job_items_link, job=running['id'], **job)
    assert result['items_url'] == 'http://127.0.0.1:6800/items/single_sites/tubi_US_20260930-10-06.csv'
    assert (result['status'], result['format'], result['available']) == ('running', 'CSV', False)
    assert 'http://127.0.0.1:6800/items/single_sites/' in result['notes'][0]

    app.config['SCRAPYD_SERVER_OBJECTS'][0].public_url = 'https://scrapyd.example.com'
    result = run(app, operations.get_job_items_link, job=finished['id'], **job)
    assert result['items_url'] == 'https://scrapyd.example.com/items/archive/tubi/CA/tubi_CA_20260930-10-05.csv.zip'
    assert result['available'] is True
    app.config['SCRAPYD_SERVER_OBJECTS'][0].public_url = ''

    with pytest.raises(ToolError, match="is pending"):
        run(app, operations.get_job_items_link, job=pending['id'], **job)
    with pytest.raises(ToolError, match="not found on node"):
        run(app, operations.get_job_items_link, job='fake_job', **job)
    with pytest.raises(ToolError, match="Invalid job"):
        run(app, operations.get_job_items_link, job='../fake_job', **job)

    if jobs_table_map.get(1) is None:
        req(app, client, view='jobs', kws=dict(node=1))
    Job = jobs_table_map[1]
    with app.app_context():
        db.session.add(Job(project='demo', spider='tubi', job='old_job', status='2',
                           start=datetime(2026, 9, 1, 1, 2, 3), href_items='/items/archive/tubi/CA/old.csv.zip'))
        db.session.commit()
    try:
        result = run(app, operations.get_job_items_link, job='old_job', **job)
        assert (result['status'], result['start_time']) == ('finished', '2026-09-01 01:02:03')
        assert result['items_url'] == 'http://127.0.0.1:6800/items/archive/tubi/CA/old.csv.zip'
    finally:
        with app.app_context():
            Job.query.filter_by(job='old_job').delete()
            db.session.commit()


def test_get_job_stats_from_report(app, demo_job):
    result = run(app, operations.get_job_stats, **demo_job)
    assert (result['source'], result['node'], result['job']) == ('report', 1, 'mcp_job')
    assert (result['pages'], result['items'], result['finish_reason']) == (3, 2, 'finished')
    assert set(result['log_counts']) == {'critical', 'error', 'warning', 'redirect', 'retry', 'ignore'}
    assert any('json' in note for note in result['notes'])

    with pytest.raises(ToolError, match="Stats of job .+ not found"):
        run(app, operations.get_job_stats, **dict(demo_job, job=cst.FAKE_JOBID))


def test_get_job_stats_from_logparser(app, demo_job, monkeypatch):
    stats = dict(
        logparser_version=LOGPARSER_VERSION, last_update_time='2019-01-01 00:00:10', pages=3, items=2,
        first_log_time='2019-01-01 00:00:01', latest_log_time='2019-01-01 00:00:09', latest_log_timestamp=1,
        runtime='0:00:08', shutdown_reason='N/A', finish_reason='finished', tail='the tail', datas=[[1, 2, 3, 4, 5]],
        log_categories=dict(error_logs=dict(count=1, details=['an error']), redirect_logs=dict(count=0, details=[])),
        latest_matches=dict(latest_item="{'item': 2}", telnet_password='secret'),
        crawler_stats={'item_scraped_count': 2},
    )
    log_size = {'value': '100'}

    class Session(object):
        urls = []

        def get(self, url, **kwargs):
            self.urls.append(url)
            return FakeResponse(js=stats)

        def head(self, url, **kwargs):
            return FakeResponse(headers={'Content-Length': log_size['value']})

    monkeypatch.setattr(operations, 'session', Session())
    result = run(app, operations.get_job_stats, **demo_job)
    assert Session.urls == ['http://127.0.0.1:6800/logs/mcp_demo/%s/mcp_job.json' % cst.SPIDER]
    assert result['source'] == 'logparser' and 'notes' not in result
    assert (result['pages'], result['items'], result['runtime']) == (3, 2, '0:00:08')
    assert result['log_counts'] == dict(error=1, redirect=0)
    assert result['log_details'] == dict(error=['an error'])
    assert result['latest_matches'] == dict(latest_item="{'item': 2}")
    assert result['crawler_stats'] == {'item_scraped_count': 2}
    assert result['seconds_since_latest_log'] > 0
    assert 'tail' not in result and 'datas' not in result

    result = run(app, operations.get_job_stats, include_log_details=False, include_tail=True, **demo_job)
    assert 'log_details' not in result and result['tail'] == 'the tail'

    stats['logparser_version'] = '0.0.1'
    result = run(app, operations.get_job_stats, **demo_job)
    assert result['source'] == 'report' and 'v0.0.1' in result['notes'][0]

    log_size['value'] = str(operations.MAX_PARSE_LOG_BYTES + 1)
    with pytest.raises(ToolError, match="too big"):
        run(app, operations.get_job_stats, **demo_job)


def test_check_mcp_config():
    valid = dict(ENABLE_MCP=True, MCP_BIND='0.0.0.0', MCP_PORT=5001, MCP_USERNAME='username', MCP_PASSWORD='password',
                 SCRAPYDWEB_PORT=5000)
    config = dict(valid)
    check_mcp_config(config)
    assert config['ENABLE_MCP'] is True

    for invalid in [dict(ENABLE_MCP='True'), dict(MCP_PASSWORD=''), dict(MCP_USERNAME=None), dict(MCP_PORT=5000),
                    dict(MCP_PORT='5001'), dict(MCP_PORT=70000), dict(MCP_ALLOWED_HOSTS='example.com')]:
        config = dict(valid, **invalid)
        check_mcp_config(config)
        assert config['ENABLE_MCP'] is False, invalid


def test_start_mcp_server_with_port_in_use(app):
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        s.listen()
        app.config.update(MCP_BIND='127.0.0.1', MCP_PORT=s.getsockname()[1],
                          MCP_USERNAME=MCP_USERNAME, MCP_PASSWORD=MCP_PASSWORD)
        server = start_mcp_server(app, timeout=5)
        assert not server.started
