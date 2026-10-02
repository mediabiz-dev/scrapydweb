# coding: utf-8
from concurrent.futures import ThreadPoolExecutor
import logging

from prometheus_client import CollectorRegistry, GCCollector, PlatformCollector, ProcessCollector
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_flask_exporter import PrometheusMetrics
import requests
from sqlalchemy import case, func

from .__version__ import __version__
from .common import session
from .models import Task, TaskResult, db
from .servers import scrapyd_auth
from .utils.scheduler import scheduler
from .vars import STATE_RUNNING


logger = logging.getLogger(__name__)

# Less than the default scrape_timeout of Prometheus, which is 10s
SCRAPYD_TIMEOUT = 5
MAX_WORKERS = 20
JOB_STATES = ['pending', 'running', 'finished']
TASK_STATES = ['scheduled', 'paused', 'finished']
TASK_LABELS = ['task_id', 'task']
# For the MCP server to register its metrics as well, see scrapydweb/mcp_server/metrics.py
REGISTRY_KEY = 'prometheus_registry'


def init_metrics(app):
    # A registry per app instead of the global one, which allows registering the same metrics only once
    registry = CollectorRegistry()
    for collector in (ProcessCollector, PlatformCollector, GCCollector):
        collector(registry=registry)
    # Group by endpoint since the paths contain the names of projects, spiders, jobs and so on
    metrics = PrometheusMetrics(app, registry=registry, group_by='endpoint', excluded_paths='^/static/')
    metrics.info('scrapydweb_info', "ScrapydWeb version", version=__version__)
    registry.register(ScrapydWebCollector(app))
    app.extensions[REGISTRY_KEY] = registry
    return metrics


def get_daemonstatus(server):
    try:
        r = session.get('%s/daemonstatus.json' % server.url(), auth=scrapyd_auth(server), timeout=SCRAPYD_TIMEOUT)
        r.raise_for_status()
        js = r.json()
    except (requests.RequestException, ValueError) as err:
        logger.debug("Fail to get daemonstatus.json of %s: %s", server, err)
        return None
    return js if isinstance(js, dict) and js.get('status') == 'ok' else None


class ScrapydWebCollector(object):
    def __init__(self, app):
        self.app = app

    def collect(self):
        yield from self.collect_scrapyd_servers()
        with self.app.app_context():
            yield from self.collect_timer_tasks()

    def collect_scrapyd_servers(self):
        up = GaugeMetricFamily('scrapydweb_scrapyd_up', "Whether the Scrapyd server answers daemonstatus.json",
                               labels=['node', 'group'])
        jobs = GaugeMetricFamily('scrapydweb_scrapyd_jobs', "Jobs of the Scrapyd server by state",
                                 labels=['node', 'group', 'state'])
        servers = self.app.config.get('SCRAPYD_SERVER_OBJECTS', [])
        if servers:
            with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(servers))) as executor:
                for server, status in zip(servers, executor.map(get_daemonstatus, servers)):
                    labels = [server.name or '%s:%s' % (server.ip, server.port), server.group]
                    up.add_metric(labels, 1 if status else 0)
                    if status:
                        for state in JOB_STATES:
                            jobs.add_metric(labels + [state], status.get(state, 0))
        yield up
        yield jobs

    def collect_timer_tasks(self):
        yield GaugeMetricFamily('scrapydweb_scheduler_running', "Whether the scheduler of timer tasks is running",
                                value=1 if scheduler.state == STATE_RUNNING else 0)

        states = GaugeMetricFamily('scrapydweb_timer_tasks', "Timer tasks by state", labels=['state'])
        runs = CounterMetricFamily('scrapydweb_timer_task_runs', "Runs of the timer task", labels=TASK_LABELS)
        failed_runs = CounterMetricFamily('scrapydweb_timer_task_failed_runs',
                                          "Runs of the timer task that fail to run the job on some node",
                                          labels=TASK_LABELS)
        last_run = GaugeMetricFamily('scrapydweb_timer_task_last_run_timestamp_seconds',
                                     "When the timer task ran last time, in seconds since the epoch",
                                     labels=TASK_LABELS)

        task_result_stats = {}
        for task_id, run_times, fail_times, latest_time in db.session.query(
                TaskResult.task_id, func.count(TaskResult.id),
                func.sum(case([(TaskResult.fail_count > 0, 1)], else_=0)),
                func.max(TaskResult.execute_time)).group_by(TaskResult.task_id):
            task_result_stats[task_id] = (run_times, int(fail_times or 0), latest_time)
        apscheduler_jobs = {job.id: job for job in scheduler.get_jobs(jobstore='default')}

        state_counts = dict.fromkeys(TASK_STATES, 0)
        for task_id, name in db.session.query(Task.id, Task.name).order_by(Task.id):
            job = apscheduler_jobs.get(str(task_id))
            if not job:
                state = 'finished'
            elif job.next_run_time:
                state = 'scheduled'
            else:
                state = 'paused'
            state_counts[state] += 1

            labels = [str(task_id), name or '']
            run_times, fail_times, latest_time = task_result_stats.get(task_id, (0, 0, None))
            runs.add_metric(labels, run_times)
            failed_runs.add_metric(labels, fail_times)
            if latest_time:
                # execute_time is naive local time, the same as what timestamp() assumes
                last_run.add_metric(labels, latest_time.timestamp())
        for state, count in state_counts.items():
            states.add_metric([state], count)
        yield states
        yield runs
        yield failed_runs
        yield last_run
