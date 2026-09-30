# coding: utf-8
from datetime import datetime

import pytest

from scrapydweb.__version__ import __version__
from scrapydweb.metrics import init_metrics
from scrapydweb.models import Task, TaskResult, db
from scrapydweb.utils.scheduler import scheduler
from scrapydweb.vars import STATE_RUNNING
from scrapydweb.views.operations.execute_task import execute_task
from tests.utils import find_samples as find, get_metric_samples as get_samples


@pytest.fixture
def metrics_client(app):
    app.config['ENABLE_METRICS'] = True
    init_metrics(app)
    return app.test_client()


def test_metrics_disabled(client):
    assert client.get('/metrics').status_code == 404


def test_http_metrics(metrics_client):
    metrics_client.get('/')
    metrics_client.get('/static/v%s/css/style.css' % __version__.replace('.', ''))
    samples = get_samples(metrics_client)
    assert find(samples, 'scrapydweb_info')
    assert find(samples, 'flask_http_request_total')
    assert find(samples, 'flask_http_request_duration_seconds_count', endpoint='index')
    assert not find(samples, 'flask_http_request_duration_seconds_count', endpoint='static')
    assert all('path' not in sample.labels for sample in samples)
    assert find(samples, 'process_cpu_seconds_total')


def test_scrapyd_metrics(app, metrics_client):
    samples = get_samples(metrics_client)
    servers = app.config['SCRAPYD_SERVER_OBJECTS']
    assert len(find(samples, 'scrapydweb_scrapyd_up')) == len(servers) == 2
    for server in servers:
        node = server.name or '%s:%s' % (server.ip, server.port)
        is_up = server.ip == '127.0.0.1'
        assert find(samples, 'scrapydweb_scrapyd_up', node=node, group=server.group) == [1 if is_up else 0]
        for state in ['pending', 'running', 'finished']:
            assert len(find(samples, 'scrapydweb_scrapyd_jobs', node=node, state=state)) == (1 if is_up else 0)


def test_timer_task_metrics(app, metrics_client):
    last_execute_time = datetime(2036, 1, 1, 0, 1, 0)
    with app.app_context():
        task = Task(name='metrics_task', trigger='cron', project='metrics_project', version='v', spider='s',
                    jobid='j', settings_arguments='{}', selected_node_names='[]', year='2036', month='*', day='*',
                    week='*', day_of_week='*', hour='*', minute='*', second='0', jitter=0, coalesce='True',
                    max_instances=1)
        db.session.add(task)
        db.session.commit()
        task_id = task.id
        db.session.add_all([
            TaskResult(task_id=task_id, execute_time=datetime(2036, 1, 1, 0, 0, 0), pass_count=1, fail_count=0),
            TaskResult(task_id=task_id, execute_time=last_execute_time, pass_count=0, fail_count=1),
        ])
        db.session.commit()
    labels = dict(task_id=str(task_id), task='metrics_task')

    try:
        samples = get_samples(metrics_client)
        assert find(samples, 'scrapydweb_scheduler_running') == [1 if scheduler.state == STATE_RUNNING else 0]
        assert find(samples, 'scrapydweb_timer_task_runs_total', **labels) == [2]
        assert find(samples, 'scrapydweb_timer_task_failed_runs_total', **labels) == [1]
        assert find(samples, 'scrapydweb_timer_task_last_run_timestamp_seconds', **labels) == [
            last_execute_time.timestamp()]
        finished = find(samples, 'scrapydweb_timer_tasks', state='finished')[0]
        paused = find(samples, 'scrapydweb_timer_tasks', state='paused')[0]
        assert finished >= 1

        # A job without next_run_time is paused, so it never runs
        scheduler.add_job(func=execute_task, kwargs=dict(task_id=task_id), trigger='cron', year='2036',
                          id=str(task_id), jobstore='default', next_run_time=None)
        samples = get_samples(metrics_client)
        assert find(samples, 'scrapydweb_timer_tasks', state='finished') == [finished - 1]
        assert find(samples, 'scrapydweb_timer_tasks', state='paused') == [paused + 1]
    finally:
        if scheduler.get_job(str(task_id), jobstore='default'):
            scheduler.remove_job(str(task_id), jobstore='default')
        with app.app_context():
            db.session.delete(Task.query.get(task_id))
            db.session.commit()
