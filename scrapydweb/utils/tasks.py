# coding: utf-8
from collections import OrderedDict
import json

from sqlalchemy import func

from ..common import chunks
from ..models import Task, TaskResult, db
from .scheduler import scheduler


CRON_FIELDS = ['year', 'month', 'day', 'week', 'day_of_week', 'hour', 'minute', 'second']


def query_timer_tasks(project=None, spider=None, name=None, limit=None, offset=0):
    """Return (total, rows) of the timer tasks matching the filters, ordered by id. limit=None returns all of them."""
    query = Task.query
    if project:
        query = query.filter_by(project=project)
    if spider:
        query = query.filter_by(spider=spider)
    if name:
        query = query.filter_by(name=name)
    total = query.count()
    query = query.order_by(Task.id).offset(offset)
    tasks = (query.limit(limit) if limit is not None else query).all()

    latest_ids = []
    for chunk in chunks(task.id for task in tasks):
        latest_ids.extend(i for (i, ) in db.session.query(func.max(TaskResult.id)).filter(
            TaskResult.task_id.in_(chunk)).group_by(TaskResult.task_id))
    latest_results = {}
    for chunk in chunks(latest_ids):
        latest_results.update((r.task_id, r) for r in TaskResult.query.filter(TaskResult.id.in_(chunk)))
    apscheduler_jobs = {job.id: job for job in scheduler.get_jobs(jobstore='default')}

    rows = []
    for task in tasks:
        job = apscheduler_jobs.get(str(task.id))
        if not job:
            state = 'finished'
        elif job.next_run_time:
            state = 'scheduled'
        else:
            state = 'paused'
        latest = latest_results.get(task.id)
        rows.append(OrderedDict(
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
    return total, rows
