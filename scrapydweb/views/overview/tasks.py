# coding: utf-8
from datetime import datetime, timedelta
import json
import logging
import traceback

from flask import Blueprint, flash, render_template, request, send_file, url_for
from sqlalchemy import and_, case, func

from ..operations.schedule import ScheduleRunView
from ...common import chunks, handle_metadata
from ...models import Task, TaskResult, TaskJobResult, db
from ...utils.scheduler import scheduler
from ...vars import SCHEDULER_STATE_DICT, STATE_PAUSED, STATE_RUNNING, TIMER_TASKS_HISTORY_LOG
from ..baseview import BaseView


apscheduler_logger = logging.getLogger('apscheduler')
metadata = dict(per_page=handle_metadata().get('tasks_per_page', 100))

bp = Blueprint('tasks', __name__, url_prefix='/')


def get_latest_task_job_results(task_result_ids):
    latest_ids = []
    for chunk in chunks(task_result_ids):
        latest_ids.extend(i for (i, ) in db.session.query(func.max(TaskJobResult.id)).filter(
            TaskJobResult.task_result_id.in_(chunk)).group_by(TaskJobResult.task_result_id))
    latest_task_job_results = {}
    for chunk in chunks(latest_ids):
        for task_job_result in TaskJobResult.query.filter(TaskJobResult.id.in_(chunk)):
            latest_task_job_results[task_job_result.task_result_id] = task_job_result
    return latest_task_job_results


@bp.route('/tasks/history/')
def history():
    return send_file(TIMER_TASKS_HISTORY_LOG, mimetype='text/plain', cache_timeout=0)


# https://apscheduler.readthedocs.io/en/latest/userguide.html
# https://apscheduler.readthedocs.io/en/latest/modules/schedulers/base.html#module-apscheduler.schedulers.base
# https://apscheduler.readthedocs.io/en/latest/modules/job.html#apscheduler.job.Job
class TasksView(BaseView):
    metadata = metadata

    def __init__(self):
        super(TasksView, self).__init__()

        self.task_id = self.view_args['task_id']  # <int:task_id>, 0 ok, -1 fail
        self.task_result_id = self.view_args['task_result_id']  # <int:task_result_id>

        self.flash = request.args.get('flash', '')
        self.per_page = request.args.get('per_page', default=self.metadata['per_page'], type=int)
        if self.per_page != self.metadata['per_page']:
            self.metadata['per_page'] = self.per_page
            handle_metadata('tasks_per_page', self.per_page)
            self.logger.debug("Change per_page to %s", self.metadata['per_page'])
        self.page = request.args.get('page', default=1, type=int)

        # If self.task is defined before handle_metadata('tasks_per_page', self.per_page)
        # Instance <Task at 0x58140f0> is not bound to a Session;
        # attribute refresh operation cannot proceed
        # (Background on this error at: http://sqlalche.me/e/bhk3)
        self.task = Task.query.get(self.task_id) if self.task_id else None
        self.kwargs = {}
        self.template = ''

    def dispatch_request(self, **kwargs):
        if self.flash:
            flash(self.flash, self.INFO)

        # http://flask-sqlalchemy.pocoo.org/2.3/queries/#queries-in-views
        # Use get_or_404 to ensure that url_for the Stats page works
        # task = Task.query.get_or_404(self.task_id)
        if self.task_id and not self.task:
            message = "Task #%s not found" % self.task_id
            self.logger.error(message)
            return render_template(self.template_fail, node=self.node, message=message)

        if self.task_id and self.task_result_id:
            self.template = 'scrapydweb/task_job_results.html'
            self.query_task_job_results()
        elif self.task_id:
            self.template = 'scrapydweb/task_results.html'
            self.query_task_results()
        else:
            self.template = 'scrapydweb/tasks.html'
            self.query_tasks()
        return render_template(self.template, **self.kwargs)

    def query_tasks(self):
        if self.scheduler.state == STATE_PAUSED:
            flash("Click the DISABLED button to enable the scheduler for timer tasks. ", self.WARN)
        self.remove_apscheduler_job_without_task()

        # https://stackoverflow.com/questions/43103585/python-flask-sqlalchemy-pagination
        # https://blog.miguelgrinberg.com/post/the-flask-mega-tutorial-part-ix-pagination
        # http://flask-sqlalchemy.pocoo.org/2.3/api/#flask_sqlalchemy.BaseQuery.paginate
        # paginate(page=None, per_page=None, error_out=True, max_per_page=None)
        # tasks = Task.query.all()
        tasks = Task.query.order_by(Task.id.desc()).paginate(
            page=self.page, per_page=self.per_page, error_out=False)
        self.process_tasks(tasks)

        # default-sort in Vue would cause background color of the buttons flash once
        # :default-sort="{prop: 'status', order: 'descending'}"
        tasks.items.sort(key=lambda task: task.status, reverse=True)

        if self.scheduler.state == STATE_RUNNING:
            scheduler_action_button = 'ENABLED'
            url_scheduler_action = url_for('tasks.xhr', node=self.node, action='disable')
        else:
            scheduler_action_button = 'DISABLED'
            url_scheduler_action = url_for('tasks.xhr', node=self.node, action='enable')

        self.kwargs = dict(
            node=self.node,
            tasks=tasks,
            url_add_task=url_for('schedule', node=self.node, add_task='True'),
            scheduler_action_button=scheduler_action_button,
            url_scheduler_action=url_scheduler_action,
            url_tasks_history=url_for('tasks.history')
        )

    def remove_apscheduler_job_without_task(self):
        # In case the task is remove from database while its apscheduler_job is still running
        apscheduler_job_id_set = set([j.id for j in self.scheduler.get_jobs(jobstore='default')])  # type(j.id): str
        task_id_set = set([str(i) for (i, ) in db.session.query(Task.id)])  # type(Task.id): int
        for i in apscheduler_job_id_set.difference(task_id_set):
            self.scheduler.remove_job(i, jobstore='default')
            msg = "apscheduler_job #{id} removed since task #{id} not exist. ".format(id=i)
            apscheduler_logger.error(msg)
            flash(msg, self.WARN)

    def process_tasks(self, tasks):
        with db.session.no_autoflush:  # To avoid in place updating
            task_ids = [task.id for task in tasks.items]
            task_result_stats = {}
            for chunk in chunks(task_ids):
                for task_id, run_times, fail_times, latest_id in db.session.query(
                        TaskResult.task_id, func.count(TaskResult.id),
                        func.sum(case([(TaskResult.fail_count > 0, 1)], else_=0)),
                        func.max(TaskResult.id)).filter(TaskResult.task_id.in_(chunk)).group_by(TaskResult.task_id):
                    task_result_stats[task_id] = (run_times, int(fail_times or 0), latest_id)
            latest_task_results = {}
            for chunk in chunks([stats[2] for stats in task_result_stats.values()]):
                for task_result in TaskResult.query.filter(TaskResult.id.in_(chunk)):
                    latest_task_results[task_result.task_id] = task_result
            latest_task_job_results = get_latest_task_job_results(
                [task_result.id for task_result in latest_task_results.values()
                 if task_result.fail_count == 0 and task_result.pass_count == 1])
            apscheduler_jobs = {}
            for apscheduler_job in self.scheduler.get_jobs():
                apscheduler_jobs.setdefault(apscheduler_job.id, apscheduler_job)

            # for task in tasks:  # TypeError: 'Pagination' object is not iterable  # tasks.item: list
            for index, task in enumerate(tasks.items, (tasks.page - 1) * tasks.per_page + 1):
                # Columns: Name | Prev run result | Task results
                task.index = index
                task.name = task.name or ''
                task.timezone = task.timezone or self.scheduler.timezone
                task.create_time = self.remove_microsecond(task.create_time)
                task.update_time = self.remove_microsecond(task.update_time)
                task.run_times, fail_times, __ = task_result_stats.get(task.id, (0, 0, None))
                task.url_task_results = url_for('tasks', node=self.node, task_id=task.id)
                if task.run_times > 0:
                    task.fail_times = fail_times
                    latest_task_result = latest_task_results[task.id]
                    if latest_task_result.fail_count == 0 and latest_task_result.pass_count == 1:
                        task_job_result = latest_task_job_results.get(latest_task_result.id)
                        task.prev_run_result = task_job_result.result[-19:]  # task_N_2019-01-01T00_00_01
                        task.url_prev_run_result = url_for('log', node=task_job_result.node_name, opt='stats',
                                                           project=task.project, spider=task.spider,
                                                           job=task_job_result.result)
                    else:
                        # 'FAIL 0, PASS 0' if execute_task() has not finished
                        task.prev_run_result = 'FAIL %s, PASS %s' % (latest_task_result.fail_count,
                                                                     latest_task_result.pass_count)
                        task.url_prev_run_result = url_for('tasks', node=self.node,
                                                           task_id=task.id, task_result_id=latest_task_result.id)
                else:
                    task.fail_times = 0
                    task.prev_run_result = self.NA
                    task.url_prev_run_result = task.url_task_results
                # Columns: Status | Actions | Next run time
                task.url_edit = url_for('schedule', node=self.node, task_id=task.id)
                # PostgreSQL 8.3 removes implicit casts
                # sqlalchemy.exc.ProgrammingError: (psycopg2.ProgrammingError) operator does not exist: character varying = integer
                # LINE 3: WHERE apscheduler_jobs.id = 2
                # HINT:  No operator matches the given name and argument types. You might need to add explicit type casts.
                # [SQL: 'SELECT apscheduler_jobs.job_state \nFROM apscheduler_jobs \nWHERE apscheduler_jobs.id = %(id_1)s'] [parameters: {'id_1': 2}]
                # (Background on this error at: http://sqlalche.me/e/f405)
                apscheduler_job = apscheduler_jobs.get(str(task.id))  # Return type: Job or None
                if apscheduler_job:
                    self.logger.debug("apscheduler_job %s: %s", apscheduler_job.name, apscheduler_job)
                    if apscheduler_job.next_run_time:
                        task.status = 'Running'
                        action = 'pause'
                        if self.scheduler.state == STATE_PAUSED:
                            task.next_run_time = "Click DISABLED button first. "
                        else:
                            # TypeError: argument of type 'datetime.datetime' is not iterable
                            task.next_run_time = str(apscheduler_job.next_run_time)  # '2019-01-01 00:00:01+08:00'
                        task.url_fire = url_for('tasks.xhr', node=self.node, action='fire', task_id=task.id)
                    else:
                        task.status = 'Paused'
                        action = 'resume'
                        task.next_run_time = self.NA
                        task.url_fire = ''
                    task.url_status = url_for('tasks.xhr', node=self.node, action=action, task_id=task.id)
                    task.action = 'Stop'
                    task.url_action = url_for('tasks.xhr', node=self.node, action='remove', task_id=task.id)
                else:
                    task.status = 'Finished'
                    task.url_status = task.url_task_results  # '',  'javascript:;'
                    task.action = 'Delete'
                    task.url_action = url_for('tasks.xhr', node=self.node, action='delete', task_id=task.id)
                    task.url_recreate = url_for('tasks.xhr', node=self.node, action='recreate', task_id=task.id)
                    task.next_run_time = self.NA
                    task.url_fire = ''

    def query_task_results(self):
        task_results = TaskResult.query.filter_by(task_id=self.task_id).order_by(
            TaskResult.id.desc()).paginate(page=self.page, per_page=self.per_page, error_out=False)
        # In case that execute_task() has not finished or selected_nodes is modified
        with_job = all([task_result.fail_count + task_result.pass_count == 1 for task_result in task_results.items])

        with db.session.no_autoflush:
            if with_job:
                latest_task_job_results = get_latest_task_job_results(
                    [task_result.id for task_result in task_results.items])
            for index, task_result in enumerate(task_results.items,
                                                (task_results.page - 1) * task_results.per_page + 1):
                task_result.index = index
                if with_job:  # To show task_job_result in task_results.html
                    self.template = 'scrapydweb/task_results_with_job.html'
                    task_job_result = latest_task_job_results.get(task_result.id)
                    task_result.task_job_result_id = task_job_result.id
                    task_result.run_time = self.remove_microsecond(task_job_result.run_time)
                    task_result.node_name = task_job_result.node_name
                    task_result.server = task_job_result.server
                    task_result.status_code = task_job_result.status_code
                    task_result.status = task_job_result.status
                    task_result.result = task_job_result.result
                    if task_job_result.status == self.OK:
                        task_result.url_stats = url_for('log', node=task_job_result.node_name, opt='stats',
                                                        project=self.task.project, spider=self.task.spider,
                                                        job=task_job_result.result)
                    else:
                        task_result.url_stats = ''  # 'javascript:;'
                else:
                    task_result.execute_time = self.remove_microsecond(task_result.execute_time)
                    task_result.url_task_job_results = url_for('tasks', node=self.node,
                                                               task_id=self.task_id, task_result_id=task_result.id)
                task_result.url_action = url_for('tasks.xhr', node=self.node, action='delete',
                                                 task_id=self.task.id, task_result_id=task_result.id)

        self.kwargs = dict(
            node=self.node,
            task_id=self.task_id,
            task=self.task,
            task_results=task_results,
            url_tasks=url_for('tasks', node=self.node),
        )

    def query_task_job_results(self):
        # https://docs.sqlalchemy.org/en/latest/core/sqlelement.html#sqlalchemy.sql.expression.asc
        task_job_results = TaskJobResult.query.filter_by(task_result_id=self.task_result_id).order_by(
            TaskJobResult.node_name.asc()).paginate(page=self.page, per_page=self.per_page, error_out=False)
        with db.session.no_autoflush:
            for index, task_job_result in enumerate(task_job_results.items,
                                                    (task_job_results.page - 1) * task_job_results.per_page + 1):
                task_job_result.index = index
                task_job_result.run_time = self.remove_microsecond(task_job_result.run_time)
                if task_job_result.status == self.OK:
                    task_job_result.url_stats = url_for('log', node=task_job_result.node_name, opt='stats',
                                                        project=self.task.project, spider=self.task.spider,
                                                        job=task_job_result.result)
                    task_job_result.url_clusterreports = url_for('clusterreports', node=self.node,
                                                                 project=self.task.project, spider=self.task.spider,
                                                                 job=task_job_result.result)
                else:
                    task_job_result.url_stats = ''  # 'javascript:;'
                    task_job_result.url_clusterreports = ''

        self.kwargs = dict(
            node=self.node,
            task_id=self.task_id,
            task_result_id=self.task_result_id,
            task=self.task,
            task_job_results=task_job_results,
            url_tasks=url_for('tasks', node=self.node),
            url_task_results=url_for('tasks', node=self.node, task_id=self.task_id),
        )


class TasksXhrView(BaseView):

    def __init__(self):
        super(TasksXhrView, self).__init__()

        self.action = self.view_args['action']  # pause|resume|remove|delete|dump|fire
        self.task_id = self.view_args['task_id']  # <int:task_id>
        self.task_result_id = self.view_args['task_result_id']  # <int:task_result_id>

        self.task: Task = Task.query.get(self.task_id) if self.task_id else None
        self.apscheduler_job = self.scheduler.get_job(str(self.task_id)) if self.task_id else None  # Return type: Job|None
        self.js = dict(action=self.action, task_id=self.task_id, task_result_id=self.task_result_id, url=request.url)
        # self.logger.warning(self.js)

    def dispatch_request(self, **kwargs):
        try:
            self.generate_response()
        except Exception as err:
            self.logger.error(traceback.format_exc())
            db.session.rollback()
            self.js['status'] = 'exception'
            self.js['message'] = str(err)
        else:
            self.js.setdefault('status', self.OK)
        finally:
            self.logger.debug(self.js)
            return self.json_dumps(self.js, as_response=True)

    def generate_response(self):
        if self.action in ['disable', 'enable']:  # ENABLE|DISABLE the scheduler
            self.enable_disable_scheduler()
        elif self.action == 'delete':  # delete a task_result|task
            if self.task_result_id:
                self.delete_task_result()
            elif self.task_id:
                self.delete_task()
            else:
                self.delete_outdated_task_results()
        elif self.action == 'dump':  # For test only
            self.dump_task_data()
        elif self.action == 'fire':  # update next_run_time
            self.fire_task()
        elif self.action == 'list':  # For test only
            self.list_tasks_or_results()
        elif self.action == 'recreate':
            self.recreate_task()
        else:  # pause|resume|remove a apscheduler_job
            self.handle_apscheduler_job()

    def enable_disable_scheduler(self):
        # scheduler.running: a shortcut for scheduler.state != STATE_STOPPED
        # if self.scheduler.state == STATE_RUNNING:
        if self.action == 'disable':
            self.scheduler.pause()
        else:  # 'enable'
            self.scheduler.resume()
        handle_metadata('scheduler_state', self.scheduler.state)
        self.js['tip'] = "Scheduler after '%s': %s" % (self.action, SCHEDULER_STATE_DICT[self.scheduler.state])

    def delete_task_result(self):
        task_result = TaskResult.query.get(self.task_result_id)
        # In case that execute_task() has not finished
        # if task_result and (task_result.pass_count or task_result.fail_count):
        if task_result:
            db.session.delete(task_result)
            db.session.commit()
            self.js['tip'] = "task_result #%s deleted. " % self.task_result_id
        else:
            self.js['status'] = self.ERROR
            self.js['message'] = "task_result #%s not found. " % self.task_result_id

    def delete_task(self):
        # Actually, the 'delete a task' button is available only when  apscheduler_job is None
        if request.args.get('ignore_apscheduler_job') == 'True':  # For test only
            self.js['tip'] = "Ignore apscheduler_job #%s. " % self.task_id
        else:
            if self.apscheduler_job:
                self.apscheduler_job.remove()
                self.js['tip'] = "apscheduler_job #%s removed. " % self.task_id
            else:
                self.js['tip'] = "apscheduler_job #%s not found. " % self.task_id
        if self.task:
            db.session.delete(self.task)
            db.session.commit()
            msg = "Task #%s deleted. " % self.task_id
            apscheduler_logger.warning(msg)
            self.js['tip'] += msg
        else:
            self.js['status'] = self.ERROR
            self.js['message'] = self.js.pop('tip') + "Task #%s not found. " % self.task_id

    def fire_task(self):
        if not self.apscheduler_job:
            self.js['status'] = self.ERROR
            self.js['message'] = "apscheduler_job #{0} not found, check if task #{0} is finished. ".format(self.task_id)
            return
        elif not self.apscheduler_job.next_run_time:
            self.js['status'] = self.ERROR
            self.js['message'] = "apscheduler_job #%s is paused, resume it first. " % self.task_id
            return
        self.apscheduler_job.modify(next_run_time=datetime.now())
        self.js['tip'] = "Reload this page several seconds later to check out the fire result. "
        self.js['url_jump'] = url_for('tasks', node=self.node, task_id=self.task_id)

    def handle_apscheduler_job(self):
        if not self.apscheduler_job:
            self.js['status'] = self.ERROR
            self.js['message'] = "apscheduler_job #%s not found. " % self.task_id
            return
        if self.action == 'pause':
            self.apscheduler_job.pause()
        elif self.action == 'resume':
            self.apscheduler_job.resume()
        else:  # 'Stop' button to 'remove'
            self.apscheduler_job.remove()
        self.js['tip'] = u"apscheduler_job #{task_id} after '{action}': {apscheduler_job}".format(
            task_id=self.task_id, action=self.action, apscheduler_job=self.scheduler.get_job(str(self.task_id)))

    def recreate_task(self):
        # NOTE: this is a horrible hack
        # We fake-instantiate the class and then fill out as little as possible for it to work
        # This is needed due to rampant use of global variables and basically no view-logic decoupling
        run_view = ScheduleRunView.__new__(ScheduleRunView)
        run_view.logger = self.logger
        run_view.scheduler = scheduler
        run_view.task = self.task
        # This is copied and converted from db_process_task and needs to be kept in sync
        run_view.task_data = {'name': self.task.name, 'trigger': self.task.trigger, 'year': self.task.year, 'month': self.task.month, 'day': self.task.day, 'week': self.task.week,
                              'day_of_week': self.task.day_of_week, 'hour': self.task.hour, 'minute': self.task.minute, 'second': self.task.second, 'start_date': self.task.start_date,
                              'end_date': self.task.end_date, 'timezone': self.task.timezone, 'jitter': self.task.jitter, 'misfire_grace_time': self.task.misfire_grace_time,
                              'coalesce': self.task.coalesce, 'max_instances': self.task.max_instances}

        run_view._action = 'add'  # add|add_fire|add_pause
        run_view.task_id = self.task_id
        run_view.to_update_task = True
        run_view.task.update_time = datetime.now()
        run_view.add_update_task()

    def dump_task_data(self):
        if not self.task:
            self.js['status'] = self.ERROR
            if self.apscheduler_job:  # For test only
                self.js['data'] = dict(apscheduler_job=self.task_id)
                self.js['message'] = "apscheduler_job #%s found. " % self.task_id
            else:
                self.js['data'] = None
                self.js['message'] = "apscheduler_job #%s not found. " % self.task_id
            self.js['message'] += "Task #%s not found. " % self.task_id
            return
        # print(vars(self.task))
        self.js['data'] = dict((k, v) for k, v in vars(self.task).items() if not k.startswith('_'))
        self.js['data']['settings_arguments'] = json.loads(self.js['data']['settings_arguments'])
        self.js['data']['selected_nodes'] = json.loads(self.js['data']['selected_nodes'])
        self.js['data']['create_time'] = str(self.js['data']['create_time'])
        self.js['data']['update_time'] = str(self.js['data']['update_time'])
        if not self.apscheduler_job:
            self.js['data']['apscheduler_job'] = None
            self.js['tip'] = "apscheduler_job #{id} not found. Task #{id} found. ".format(id=self.task_id)
            return
        # print(self.apscheduler_job.__slots__)
        # ('_scheduler', '_jobstore_alias', 'id', 'trigger', 'executor', 'func', 'func_ref', 'args', 'kwargs',
        #  'name', 'misfire_grace_time', 'coalesce', 'max_instances', 'next_run_time')
        self.js['data']['apscheduler_job'] = dict(
            id=self.apscheduler_job.id,
            name=self.apscheduler_job.name,
            kwargs=self.apscheduler_job.kwargs,
            misfire_grace_time=self.apscheduler_job.misfire_grace_time,
            coalesce=self.apscheduler_job.coalesce,
            max_instances=self.apscheduler_job.max_instances,
            next_run_time=str(self.apscheduler_job.next_run_time) if self.apscheduler_job.next_run_time else None,
        )
        self.js['data']['apscheduler_job']['trigger'] = dict((f.name, str(f))
                                                             for f in self.apscheduler_job.trigger.fields)
        start_date = self.apscheduler_job.trigger.start_date
        self.js['data']['apscheduler_job']['trigger'].update(dict(
            start_date=str(start_date) if start_date else None,
            end_date=str(self.apscheduler_job.trigger.end_date) if self.apscheduler_job.trigger.end_date else None,
            timezone=str(self.apscheduler_job.trigger.timezone) if self.apscheduler_job.trigger.timezone else None,
            jitter=self.apscheduler_job.trigger.jitter,
        ))
        self.js['tip'] = "apscheduler_job #{id} found. Task #{id} found. ".format(id=self.task_id)

    def list_tasks_or_results(self):
        if self.task_id and self.task_result_id:
            records = TaskJobResult.query.filter_by(task_result_id=self.task_result_id).all()
        elif self.task_id:
            records = TaskResult.query.filter_by(task_id=self.task_id).all()
        else:
            records = Task.query.all()
        self.js['ids'] = [i.id for i in records]

    def delete_outdated_task_results(self):
        # The condition equals to: pass_count != 0 or fail_count != 0
        condition = ~and_(TaskResult.pass_count == 0, TaskResult.fail_count == 0)

        if self.KEEP_TASK_RESULT_LIMIT:
            count_before = TaskResult.query.count()
            task_results = TaskResult.query.filter(condition).order_by(
                TaskResult.execute_time.desc()).offset(self.KEEP_TASK_RESULT_LIMIT).all()
            for task_result in task_results:
                self.logger.debug("delete TaskResult: %s" % task_result)
                db.session.delete(task_result)
            db.session.commit()
            count_after = TaskResult.query.count()
            self.logger.info("KEEP_TASK_RESULT_LIMIT: %s, total TaskResult: from %s to %s" % (
                self.KEEP_TASK_RESULT_LIMIT, count_before, count_after))
            self.js.update(amount_limit=dict(KEEP_TASK_RESULT_LIMIT=self.KEEP_TASK_RESULT_LIMIT,
                                             count_before=count_before, count_after=count_after))

        if self.KEEP_TASK_RESULT_WITHIN_DAYS:
            count_before = TaskResult.query.count()
            # timedelta(days=0, seconds=0, microseconds=0, milliseconds=0, minutes=0, hours=0, weeks=0)
            n_days_ago = datetime.now() - timedelta(days=self.KEEP_TASK_RESULT_WITHIN_DAYS)

            task_results = TaskResult.query.filter(TaskResult.execute_time <= n_days_ago, condition).all()
            for task_result in task_results:
                self.logger.debug("delete TaskResult: %s" % task_result)
                db.session.delete(task_result)
            db.session.commit()

            count_after = TaskResult.query.count()
            self.logger.info("KEEP_TASK_RESULT_WITHIN_DAYS: %s, total TaskResult: from %s to %s" % (
                self.KEEP_TASK_RESULT_WITHIN_DAYS, count_before, count_after))
            self.js.update(day_limit=dict(KEEP_TASK_RESULT_WITHIN_DAYS=self.KEEP_TASK_RESULT_WITHIN_DAYS,
                                          count_before=count_before, count_after=count_after))
