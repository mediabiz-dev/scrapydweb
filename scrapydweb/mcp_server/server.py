# coding: utf-8
from contextlib import nullcontext
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from ..__version__ import __version__
from . import operations
from .metrics import get_mcp_metrics


INSTRUCTIONS = """\
ScrapydWeb manages a cluster of Scrapyd nodes that run Scrapy spiders.
Call list_nodes first: the other tools take a node by its name or 1-based index.
- deploy_project packages a project folder in SCRAPY_PROJECTS_DIR on the ScrapydWeb server
  (see list_deployable_projects) and adds it to the nodes. It never uploads files from your machine.
- list_timer_tasks and fire_timer_task run the scheduled spider runs (timer tasks) right away.
- list_jobs finds the jobs of the nodes. Pass the node, project, spider and job of a job to
  get_job_stats for its stats, search_job_log to grep its log, and get_job_items_link for the link
  to the items it exported. stop_job stops a pending or running job.
"""

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

Node = Annotated[str | int, Field(description="Name or 1-based index of the node, see list_nodes.")]
Nodes = Annotated[list[str | int] | None, Field(
    description="Names or 1-based indexes of the nodes, see list_nodes. Defaults to all nodes.")]
Project = Annotated[str, Field(description="Project of the job.")]
Spider = Annotated[str, Field(description="Spider of the job.")]
Job = Annotated[str, Field(description="ID of the job, without the extension of its log file.")]
Limit = Annotated[int, Field(ge=1, le=operations.MAX_PAGE_SIZE, description="Max number of results to return.")]
Offset = Annotated[int, Field(
    ge=0, description="Number of results to skip, pass next_offset of the previous call to get the next page.")]
TailMb = Annotated[float | None, Field(
    gt=0, description="Only scan the last N megabytes of the log, handy for big or running jobs. "
                      "Ignored for gzipped logs. Defaults to scanning the whole log.")]


def create_mcp_server(app):
    mcp = MCPServer(name='scrapydweb', version=__version__, instructions=INSTRUCTIONS)
    metrics = get_mcp_metrics(app)

    # Each tool is named after the function of operations it runs
    def run(func, *args, **kwargs):
        with app.app_context(), (metrics.track_tool(func.__name__) if metrics else nullcontext()):
            return func(app, *args, **kwargs)

    @mcp.tool(annotations=READ_ONLY)
    def list_nodes() -> dict[str, Any]:
        """List the Scrapyd nodes managed by ScrapydWeb, with their names, 1-based indexes and groups."""
        return run(operations.list_nodes)

    @mcp.tool(annotations=READ_ONLY)
    def list_deployable_projects() -> dict[str, Any]:
        """List the Scrapy projects that deploy_project can package: the folders in SCRAPY_PROJECTS_DIR
        on the ScrapydWeb server, with the project name and version that deploy_project uses by default."""
        return run(operations.list_deployable_projects)

    @mcp.tool(annotations=WRITE)
    def deploy_project(
        folder: Annotated[str | None, Field(
            description="Folder of the project in SCRAPY_PROJECTS_DIR, see list_deployable_projects. "
                        "May be omitted if there is only one project.")] = None,
        nodes: Nodes = None,
        project: Annotated[str | None, Field(
            description="Project name on Scrapyd. Defaults to [deploy] project in scrapy.cfg, "
                        "or the folder name.")] = None,
        version: Annotated[str | None, Field(
            description="Version to add. Defaults to the latest modification time of the project folder, "
                        "like the Deploy page.")] = None,
    ) -> dict[str, Any]:
        """Package a Scrapy project from SCRAPY_PROJECTS_DIR on the ScrapydWeb server into an egg,
        like the Auto packaging of the Deploy page, and add it as a new version to all or some nodes.
        Returns the result of each node."""
        return run(operations.deploy_project, folder=folder, nodes=nodes, project=project, version=version)

    @mcp.tool(annotations=READ_ONLY)
    def list_timer_tasks(
        project: Annotated[str | None, Field(description="Only the tasks of this project.")] = None,
        spider: Annotated[str | None, Field(description="Only the tasks of this spider.")] = None,
        limit: Limit = operations.DEFAULT_PAGE_SIZE,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """List the timer tasks (scheduled spider runs) with their cron schedule, nodes, state
        (scheduled, paused or finished), next run time, and the result of their last run.
        Returns a page of the tasks by ID: total is the number of matching tasks,
        next_offset is null on the last page."""
        return run(operations.list_timer_tasks, project=project, spider=spider, limit=limit, offset=offset)

    @mcp.tool(annotations=WRITE)
    def fire_timer_task(
        task_id: Annotated[int, Field(description="ID of the timer task, see list_timer_tasks.")],
        wait_seconds: Annotated[int, Field(
            ge=0, le=operations.MAX_WAIT_SECONDS,
            description="Wait up to this many seconds for the run to finish and return the job "
                        "started on each node. 0 returns right after firing.")] = 0,
    ) -> dict[str, Any]:
        """Fire a timer task now, like the Fire button of the Timer Tasks page: it runs the spider
        on all nodes of the task once, without changing its schedule. Paused tasks can't be fired."""
        return run(operations.fire_timer_task, task_id, wait_seconds=wait_seconds)

    @mcp.tool(annotations=READ_ONLY)
    def list_jobs(
        nodes: Nodes = None,
        status: Annotated[str, Field(
            description="One of 'running', 'pending', 'finished' or 'all'.")] = 'running',
        project: Annotated[str | None, Field(description="Only the jobs of this project.")] = None,
        spider: Annotated[str | None, Field(description="Only the jobs of this spider.")] = None,
        limit: Limit = operations.DEFAULT_PAGE_SIZE,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """List the jobs of the nodes recorded by ScrapydWeb, the same as the Jobs page, running ones by default.
        Includes the finished jobs that Scrapyd no longer lists. Each node is refreshed from Scrapyd first.
        Returns their project, spider, job ID, pid, start/end time, runtime, pages and items.
        Returns a page of the jobs, pending first, then running and finished ones, the latest started first:
        total is the number of matching jobs, next_offset is null on the last page."""
        return run(operations.list_jobs, nodes=nodes, status=status, project=project, spider=spider,
                   limit=limit, offset=offset)

    @mcp.tool(annotations=DESTRUCTIVE)
    def stop_job(
        node: Node,
        project: Project,
        job: Job,
        force: Annotated[bool, Field(
            description="Send a second cancel so that Scrapy shuts down right away instead of letting "
                        "the requests in progress finish, like the ForceStop button.")] = False,
        wait_seconds: Annotated[int, Field(
            ge=0, le=operations.MAX_WAIT_SECONDS,
            description="Wait up to this many seconds for a running job to finish. "
                        "0 returns right after stopping.")] = 0,
    ) -> dict[str, Any]:
        """Stop a job on a node, like the Stop button of the Jobs page: a pending job is removed from the
        queue, a running one gets closed by Scrapy. prevstate in the result is the state of the job before,
        null if it was neither pending nor running."""
        return run(operations.stop_job, node, project, job, force=force, wait_seconds=wait_seconds)

    @mcp.tool(annotations=READ_ONLY)
    def get_job_stats(
        node: Node,
        project: Project,
        spider: Spider,
        job: Job,
        include_log_details: Annotated[bool, Field(
            description="Include the latest critical, error and warning log lines.")] = True,
        include_tail: Annotated[bool, Field(description="Include the tail of the log.")] = False,
    ) -> dict[str, Any]:
        """Get the stats of a job, like the Stats page: pages and items scraped, runtime,
        finish reason, counts of the log levels, latest matches and the Scrapy stats dump."""
        return run(operations.get_job_stats, node, project, spider, job,
                   include_log_details=include_log_details, include_tail=include_tail)

    @mcp.tool(annotations=READ_ONLY)
    def search_job_log(
        node: Node,
        project: Project,
        spider: Spider,
        job: Job,
        pattern: Annotated[str | None, Field(
            description="Text to look for, or a regex if regex is true. Required unless whole_log is true.")] = None,
        regex: Annotated[bool, Field(description="Treat pattern as a Python regex.")] = False,
        case_sensitive: bool = False,
        context_lines: Annotated[int, Field(
            ge=0, le=20, description="Lines to include before and after each match.")] = 0,
        max_matches: Annotated[int, Field(ge=1, le=500)] = 50,
        tail_mb: TailMb = None,
        whole_log: Annotated[bool, Field(
            description="Instead of searching, return log_url: the link to the whole log, the same as the Source "
                        "button of the Jobs page. The pattern options are ignored.")] = False,
    ) -> dict[str, Any]:
        """Search the log of a job for lines matching a pattern, returning them with their line numbers,
        or the link to the whole log with whole_log. The log is streamed from the Scrapyd node, so big logs work too.
        With auth_embedded true, the link carries the login of the node: share it only with the user who asked."""
        return run(operations.search_job_log, node, project, spider, job, pattern, regex=regex,
                   case_sensitive=case_sensitive, context_lines=context_lines,
                   max_matches=max_matches, tail_mb=tail_mb, whole_log=whole_log)

    @mcp.tool(annotations=READ_ONLY)
    def get_job_items_link(node: Node, project: Project, spider: Spider, job: Job) -> dict[str, Any]:
        """Get the link to the items exported by a job, the same as the Items button of the Jobs page:
        a zipped CSV in the archive once the job is finished, or the CSV being written while it runs.
        The file isn't downloaded, the result only tells whether it's there and its size.
        With auth_embedded true, the link carries the login of the node: share it only with the user who asked."""
        return run(operations.get_job_items_link, node, project, spider, job)

    return mcp
