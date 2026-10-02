# coding: utf-8
import glob
import io
import json
import locale
import os
import platform
import re
from shutil import rmtree, copy
import sys
import time
import zipfile

from flask import url_for
from prometheus_client.parser import text_string_to_metric_families
from six import string_types

from logparser import __version__ as logparser_version
from scrapydweb.vars import DATABASE_PATH, LEGAL_NAME_PATTERN, STATS_PATH, setup_logfile


class Constant(object):
    PROJECT = 'ScrapydWeb_demo'
    VERSION = '2018-01-01T01_01_01'
    SPIDER = 'test'
    JOBID = '2018-01-01T01_01_02'

    FAKE_PROJECT = 'FAKE_PROJECT'
    FAKE_VERSION = 'FAKE_VERSION'
    FAKE_SPIDER = 'FAKE_SPIDER'
    FAKE_JOBID = 'FAKE_JOBID'

    NA = 'N/A'
    OK = 'ok'
    ERROR = 'error'
    BIGINT = 9876543210
    DEFAULT_LATEST_VERSION = 'default: the latest version'
    STRICT_NAME_PATTERN = re.compile(r'[^0-9A-Za-z_]')
    DEMO_JOBID = 'ScrapydWeb_demo'
    DEMO_LOG = 'ScrapydWeb_demo.log'
    DEMO_JSON = 'ScrapydWeb_demo.json'
    DEMO_UNFINISHED_LOG = 'ScrapydWeb_demo_unfinished.log'
    DEMO_UNFINISHED_JSON = 'ScrapydWeb_demo_unfinished.json'

    ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
    LOGPARSER_VERSION = logparser_version

    # ?flash=Add task #1 (Chinese 中文) successfully, next run at 2019-01-01 00:00:01.176468+08:00. Reload this page
    TASK_NEXT_RUN_TIME_PATTERN = re.compile(r"[ ]task #(\d+).+?next run at (.+?)\.[ ]")

    # default UA: werkzeug/0.14.1
    HEADERS_DICT = dict(
        Chrome={'User-Agent': 'Chrome'},
        iPad={'User-Agent': 'iPad'},
        iPhone={'User-Agent': 'iPhone'},
        Android={'User-Agent': 'Android'},
        IE={'User-Agent': 'msie'},
        EDGE={'User-Agent': 'EDGE'},
    )

    SCRAPY_CFG_DICT = dict(
        demo_only_scrapy_cfg='No module named',  # Result from Scrapyd server
        demo_without_scrapy_cfg='scrapy.cfg not found',

        scrapy_cfg_no_settings_default='No section: &#39;settings&#39;',
        scrapy_cfg_no_section_settings='File contains no section headers.',
        scrapy_cfg_no_option_default='No option &#39;default&#39; in section: &#39;settings&#39;',
        scrapy_cfg_no_option_default_equal='contains parsing errors',
        scrapy_cfg_no_option_default_value='returned non-zero exit status',

        scrapy_cfg_no_deploy_project='',
        scrapy_cfg_no_section_deploy='',
        scrapy_cfg_no_option_project='',
        scrapy_cfg_no_option_project_equal='contains parsing errors',
        scrapy_cfg_no_option_project_value='',
    )

    VIEW_TITLE_MAP = {
        'servers': 'Monitor and control',
        'jobs': 'Get the list of jobs',
        'tasks': 'Get the list of timer tasks',

        'deploy': 'Add a version to a project',
        'schedule': 'Schedule a spider run',
        'projects': 'Get the list of projects uploaded',

        'logs': 'Directory listing for /logs/',
        'parse.upload': 'Upload a scrapy logfile to parse',
        'settings': 'default_settings.py'
    }

    (_language_code, _encoding) = locale.getdefaultlocale()
    WINDOWS_NOT_CP936 = platform.system() == 'Windows' and _encoding != 'cp936'


cst = Constant()


def get_text(response):
    return response.get_data(as_text=True)


def req(app, client, view='', kws=None, url='', data=None,
        headers=None, content_type='multipart/form-data',
        ins=None, nos=None, jskws=None, jskeys=None, location=None, mobileui=False,
        single_scrapyd=False, set_to_second=False, save=''):
    if single_scrapyd:
        set_single_scrapyd(app, set_to_second)

    if view == 'tasks.xhr' and (kws or {}).get('action') == 'dump':
        return check_js(dump_task_data(app, kws.get('task_id')), jskws=jskws, jskeys=jskeys)

    with app.test_request_context():
        if not url:
            url = url_for(view, **kws)
        if data is not None:
            response = client.post(url, headers=headers, data=data, content_type=content_type)
        else:
            response = client.get(url, headers=headers)
        if save:
            with io.open('%s.html' % save, 'wb') as f:
                f.write(response.data)
        text = get_text(response)
        try:
            # js = response.get_json()
            js = json.loads(text)
        except (TypeError, ValueError) as err:  # issubclass(JSONDecodeError, ValueError)
            js = {}
            print(time.ctime(), 'text not js:', text)
        else:
            print(time.ctime(), "js: %s" % json.dumps(js, sort_keys=True, indent=4, ensure_ascii=False))
        try:
            if isinstance(ins, string_types):
                try:
                    print("ins: %s" % ins)
                except:  # For compatibility with Win10 Python2
                    print("ins: %s" % repr(ins))
                assert ins in text, "%s is not found in %s" % (ins, text)
            elif isinstance(ins, list):
                for i in ins:
                    try:
                        print("ins: %s" % i)
                    except:
                        print("ins: %s" % repr(i))
                    assert i in text, "%s is not found in %s" % (i, text)
            elif ins:
                raise TypeError("The argument 'ins' should be either a string or a list")

            if isinstance(nos, string_types):
                print("nos: %s" % nos)
                assert nos not in text, "%s is found in %s" % (nos, text)
            elif isinstance(nos, list):
                for n in nos:
                    print("nos: %s" % n)
                    assert n not in text, "%s is found in %s" % (n, text)
            elif nos:
                raise TypeError("The argument 'nos' should be either a string or a list")

            if location:
                print("response.headers['Location']: %s" % response.headers['Location'])
                print("location: %s" % location)
                try:
                    assert response.headers['Location'].endswith(location)
                except AssertionError:
                    assert location in response.headers['Location']

            if jskws:
                for k, v in jskws.items():
                    print("jskws: %s = %s" % (k, v))
                    try:
                        assert js[k] == v
                    except AssertionError:
                        # v is an element of js[k] or a substring of js[k]
                        assert v in js[k]

            if jskeys:
                if isinstance(jskeys, string_types):
                    print("jskeys: %s" % jskeys)
                    assert jskeys in js.keys()
                elif isinstance(jskeys, list):
                    for k in jskeys:
                        print("jskeys: %s" % k)
                        assert k in js.keys()
                elif jskeys:
                    raise TypeError("The argument 'jskeys' should be either a string or a list")

            if mobileui:
                assert 'Desktop version' in text
            else:
                assert 'Desktop version' not in text
        except:
            with io.open('response.html', 'wb') as f:
                f.write(response.data)
            raise

        return text, js


def check_js(js, jskws=None, jskeys=None):
    print(time.ctime(), "js: %s" % json.dumps(js, sort_keys=True, indent=4, ensure_ascii=False))
    for k, v in (jskws or {}).items():
        print("jskws: %s = %s" % (k, v))
        try:
            assert js[k] == v
        except AssertionError:
            # v is an element of js[k] or a substring of js[k]
            assert v in js[k]
    for k in [jskeys] if isinstance(jskeys, string_types) else (jskeys or []):
        print("jskeys: %s" % k)
        assert k in js.keys()
    return json.dumps(js), js


# The app's test-only 'tasks.xhr' dump action (TasksXhrView.dump_task_data) still reads the
# 'selected_nodes' column, renamed to 'selected_node_names' in 77dcc15, so it crashes for any
# existing task. Build the same payload here from the Task row and its apscheduler job instead,
# mapping the stored node names back to node numbers like the Run Spider page does.
def dump_task_data(app, task_id):
    from scrapydweb.models import Task
    from scrapydweb.servers import names_to_nodes
    from scrapydweb.utils.scheduler import scheduler

    js = dict(action='dump', task_id=task_id, task_result_id=None, status=cst.OK)
    with app.app_context():
        task = Task.query.get(task_id) if task_id else None
        apscheduler_job = scheduler.get_job(str(task_id)) if task_id else None
        if not task:
            js['status'] = cst.ERROR
            if apscheduler_job:
                js['data'] = dict(apscheduler_job=task_id)
                js['message'] = "apscheduler_job #%s found. " % task_id
            else:
                js['data'] = None
                js['message'] = "apscheduler_job #%s not found. " % task_id
            js['message'] += "Task #%s not found. " % task_id
            return js
        data = dict((k, v) for k, v in vars(task).items() if not k.startswith('_'))
        data['settings_arguments'] = json.loads(data['settings_arguments'])
        data['selected_nodes'] = names_to_nodes(app.config['SCRAPYD_SERVER_OBJECTS'],
                                                json.loads(data.pop('selected_node_names')))
        data['create_time'] = str(data['create_time'])
        data['update_time'] = str(data['update_time'])
        js['data'] = data
        if not apscheduler_job:
            data['apscheduler_job'] = None
            js['tip'] = "apscheduler_job #{id} not found. Task #{id} found. ".format(id=task_id)
            return js
        trigger = apscheduler_job.trigger
        data['apscheduler_job'] = dict(
            id=apscheduler_job.id,
            name=apscheduler_job.name,
            kwargs=apscheduler_job.kwargs,
            misfire_grace_time=apscheduler_job.misfire_grace_time,
            coalesce=apscheduler_job.coalesce,
            max_instances=apscheduler_job.max_instances,
            next_run_time=str(apscheduler_job.next_run_time) if apscheduler_job.next_run_time else None,
        )
        data['apscheduler_job']['trigger'] = dict((f.name, str(f)) for f in trigger.fields)
        data['apscheduler_job']['trigger'].update(dict(
            start_date=str(trigger.start_date) if trigger.start_date else None,
            end_date=str(trigger.end_date) if trigger.end_date else None,
            timezone=str(trigger.timezone) if trigger.timezone else None,
            jitter=trigger.jitter,
        ))
        js['tip'] = "apscheduler_job #{id} found. Task #{id} found. ".format(id=task_id)
    return js


def req_single_scrapyd(*args, **kwargs):
    kwargs.update(single_scrapyd=True)
    return req(*args, **kwargs)


# The views read SCRAPYD_SERVER_OBJECTS (see conftest.py), so keep it in step with the per-server lists
SERVER_CONFIG_KEYS = ['SCRAPYD_SERVERS', 'SCRAPYD_SERVER_OBJECTS', 'SCRAPYD_SERVERS_AUTHS',
                      'SCRAPYD_SERVERS_GROUPS', 'SCRAPYD_SERVERS_PUBLIC_URLS']


def set_single_scrapyd(app, set_to_second=False):
    if len(app.config['SCRAPYD_SERVERS']) > 1:
        index = -1 if set_to_second else 0
        for key in SERVER_CONFIG_KEYS:
            if key in app.config:
                app.config[key] = [app.config[key][index]]
        app.config['SCRAPYD_SERVERS_AMOUNT'] = 1


def switch_scrapyd(app):
    if len(app.config['SCRAPYD_SERVERS']) > 1:
        for key in SERVER_CONFIG_KEYS:
            if key in app.config:
                app.config[key] = app.config[key][::-1]


def get_metric_samples(client):
    response = client.get('/metrics')
    assert response.status_code == 200
    return [sample for family in text_string_to_metric_families(response.get_data(as_text=True))
            for sample in family.samples]


def find_samples(samples, name, **labels):
    return [sample.value for sample in samples
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items())]


def sleep(seconds=10):
    print(time.ctime(), "Sleep %ss" % seconds)
    time.sleep(seconds)


def replace_file_content(filepath, old, new):
    with io.open(filepath, 'r+', encoding='utf-8') as f:
        content = f.read()
        f.seek(0)
        f.write(content.replace(old, new))
        print("replace %s to %s in %s" % (old, new, filepath))


def setup_env(custom_settings):
    for file in glob.glob(os.path.join(DATABASE_PATH, '*.db')):
        os.remove(file)
        print("Removed %s" % file)

    if os.path.isdir(STATS_PATH):
        rmtree(STATS_PATH, ignore_errors=True)
        print("rmtree %s" % STATS_PATH)

    setup_logfile(delete=True)
    print("setup_logfile(delete=True)")

    if not custom_settings.get('LOCAL_SCRAPYD_LOGS_DIR', ''):
        custom_settings['LOCAL_SCRAPYD_LOGS_DIR'] = os.path.join(os.path.expanduser('~'), 'logs')
        print("Setting LOCAL_SCRAPYD_LOGS_DIR to: %s" % custom_settings['LOCAL_SCRAPYD_LOGS_DIR'])
    local_scrapyd_logs_dir = custom_settings['LOCAL_SCRAPYD_LOGS_DIR']
    if not os.path.isdir(local_scrapyd_logs_dir):
        sys.exit("custom_settings['LOCAL_SCRAPYD_LOGS_DIR'] not found: %s" % repr(local_scrapyd_logs_dir))
    else:
        logs_scrapydweb_demo = os.path.join(local_scrapyd_logs_dir, cst.PROJECT)
        if os.path.isdir(logs_scrapydweb_demo):
            rmtree(logs_scrapydweb_demo, ignore_errors=True)
            print("rmtree %s" % logs_scrapydweb_demo)

    data_folder = os.path.join(cst.ROOT_DIR, 'data')
    if os.path.isdir(data_folder):
        rmtree(data_folder, ignore_errors=True)
    sleep(3)
    with zipfile.ZipFile(os.path.join(cst.ROOT_DIR, 'data.zip'), 'r') as f:
        f.extractall(cst.ROOT_DIR)

    project_path = os.path.join(local_scrapyd_logs_dir, cst.PROJECT)
    spider_path = os.path.join(project_path, cst.SPIDER)
    for path in [project_path, spider_path]:
        if not os.path.isdir(path):
            os.mkdir(path)
    src = os.path.join(cst.ROOT_DIR, 'data', cst.DEMO_LOG)
    for filename in [cst.DEMO_LOG, cst.DEMO_UNFINISHED_LOG]:
        dst = os.path.join(spider_path, filename)
        copy(src, dst)
        print("Copied to %s from %s" % (dst, src))
        # 'finish_reason': 'finished',
        if filename == cst.DEMO_UNFINISHED_LOG:
            replace_file_content(dst, "'finish_reason'", "'finish_reason_removed'")
    stats_json_path = os.path.join(local_scrapyd_logs_dir, 'stats.json')
    demo_json_path = os.path.join(spider_path, cst.DEMO_JSON)
    demo_unfinished_json_path = os.path.join(spider_path, cst.DEMO_UNFINISHED_JSON)
    custom_settings['STATS_JSON_PATH'] = stats_json_path
    custom_settings['DEMO_JSON_PATH'] = demo_json_path
    custom_settings['DEMO_LOG_PATH'] = os.path.join(spider_path, cst.DEMO_LOG)
    for path in [stats_json_path, demo_json_path, demo_unfinished_json_path]:
        if os.path.exists(path):
            os.remove(path)
            print("Deleted: %s" % path)
    node = re.sub(LEGAL_NAME_PATTERN, '-', re.sub(r'[.:]', '_', custom_settings['_SCRAPYD_SERVER']))
    custom_settings['BACKUP_DEMO_JSON_PATH'] = os.path.join(STATS_PATH, node, cst.PROJECT, cst.SPIDER, cst.DEMO_JSON)


def upload_file_deploy(app, client, filename, project, multinode=False,
                       fail=False, redirect_project=None, alert=None):
    data = {
        'project': project,
        'version': cst.VERSION,
        'file': (os.path.join(cst.ROOT_DIR, u'data/%s' % filename), filename)
    }
    if multinode:
        data.update({'1': 'on', '2': 'on', 'checked_amount': '2'})
    with app.test_request_context():
        url = url_for('deploy.upload', node=1)
        response = client.post(url, content_type='multipart/form-data', data=data)
        text = get_text(response)
        if fail:
            assert response.status_code == 200 and "fail - ScrapydWeb" in text
        else:
            url_redirect = url_for('schedule', node=1, project=redirect_project, version=cst.VERSION)
            if multinode:
                assert response.status_code == 200 and "deploy results - ScrapydWeb" in text and url_redirect in text
            else:
                assert response.status_code == 302 and response.headers['Location'].endswith(url_redirect)

        if alert:
            assert alert in text
