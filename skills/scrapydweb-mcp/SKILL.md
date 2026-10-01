---
name: scrapydweb-mcp
description: ScrapydWeb MCP tools. Use when deploying a Scrapy project, firing a timer task, checking which spiders are running, stopping a job, or investigating a job's stats, log or items.
---

# ScrapydWeb MCP

The `scrapydweb` tools drive **production** Scrapyd nodes: a deploy replaces the code the spiders run, a fired task starts real crawls, a stopped job loses the rest of its run. This covers chaining the tools and reading their results; [Tools](#tools) lists every tool with its parameters.

If the tools are missing, the MCP server isn't connected; setup is in the "MCP Server" section of the ScrapydWeb README.

## Deploy a project

1. `list_deployable_projects` shows each folder with the project name and version a deploy uses by default. The egg is built on the ScrapydWeb host from that folder, after its `pre_deploy_hook` if it has one. Your working copy isn't involved.
2. Name the target nodes (all by default), the project and the version to the user, and get their go-ahead.
3. `deploy_project`. Done when every targeted node reports `status: ok`. Report each failed node with its `message`.

The default version is the folder's latest modification time. Redeploying unchanged code reuses that version and replaces its egg on the nodes; pass `version` to keep the previous one.

## Fire a timer task

1. `list_timer_tasks`, then pick the task by name or spider. Only a `scheduled` task can be fired. If the task is `paused`, or `scheduler` isn't `STATE_RUNNING`, tell the user instead: it has to be resumed in the Timer Tasks page first.
2. Name the task and its nodes to the user, and get their go-ahead.
3. `fire_timer_task` with `wait_seconds` around 60. Done when the result lists every node of the task: `status: ok` with the new job ID in `result`, or the error that node returned. If the run outlasts the wait, `last_run` in `list_timer_tasks` shows it later.

## Stop a job

1. `list_jobs` for the node, project and job ID. Only a `pending` or `running` job can be stopped.
2. Name the job and its node to the user, and get their go-ahead.
3. `stop_job` with `wait_seconds` around 60. Done when `status` is `finished` for a job that was running, or `prevstate` is `pending` for one that was removed from the queue. `prevstate: null` means it wasn't pending or running anymore.

A plain stop lets Scrapy finish the requests in progress, which can take a while with a big `DOWNLOAD_DELAY`. If it's still `running` after the wait, offer `force=true`: that shuts the spider down right away, without the usual cleanup.

## Investigate a job

A job is identified by its node, project, spider and job ID. Take all four from `list_jobs` (`status='all'`, filtered by `project`) or from the user. Runs started by a timer task have job IDs like `task_<task name>_<timestamp>`.

1. `get_job_stats` first. It gives the finish reason, pages and items, counts per log level and, when `source` is `logparser`, the latest error and warning lines. With `source: report` the node had no LogParser file, so those lines have to come from the log.
2. `search_job_log` for specifics. Search plain text by default and switch to `regex` only for real patterns. Pass `tail_mb` for running or very large jobs. When a `tip` says the search stopped at a limit, narrow the pattern. When the user wants the log itself, pass `whole_log=true` for `log_url`, the link of the Source button of the Jobs page; hand it over the same way as an items link.
3. `get_job_items_link` when the user wants the scraped items. It returns a link to hand over, not the items: a zipped CSV once the job has finished, or the CSV still being written while it runs.

Done when the answer rests on evidence: quote the stats values or log lines behind it.

## Reading results

- `list_jobs` and `list_timer_tasks` return a page of 100 by default. When `next_offset` isn't null there are more: narrow the call with `project`, `spider` or `status` first, and only pass `offset=next_offset` when you really need the rest. `total` is the number of matches.
- `list_jobs` only knows what Scrapyd remembers: pending, running and recently finished jobs. `get_job_items_link` also finds older jobs, through the Jobs page history.
- `auth_embedded: true` on an items or log link means it carries the node's login, which grants the whole Scrapyd API. Hand it only to the user who asked, and keep it out of commits, issues, docs and summaries.
- `available: false` on an items link means the file isn't where the Items button of the Jobs page would point either. That name is guessed from the job's start minute and the country code in its job ID, so find the real file in the directory listing given in `notes`.

## Tools

`node` is a node name or 1-based index from `list_nodes`; `nodes` is a list of them and defaults to all nodes. A job is `node`, `project`, `spider` and `job` (its ID, without the log file extension).

| Tool | Effect | Parameters (default) |
|---|---|---|
| `list_nodes` | read | none |
| `list_deployable_projects` | read | none |
| `deploy_project` | write | `folder` (required when there are several projects), `nodes`, `project` (`[deploy] project` in `scrapy.cfg`, or the folder name), `version` (the folder's latest modification time) |
| `list_timer_tasks` | read | `project`, `spider`, `limit` (100, max 500), `offset` (0) |
| `fire_timer_task` | write | `task_id` (required), `wait_seconds` (0, max 120) |
| `list_jobs` | read | `nodes`, `status` (`running`; or `pending`, `finished`, `all`), `project`, `spider`, `limit` (100, max 500), `offset` (0) |
| `stop_job` | destructive | `node`, `project`, `job` (all required, no `spider`), `force` (false), `wait_seconds` (0, max 120) |
| `get_job_stats` | read | the job, `include_log_details` (true), `include_tail` (false) |
| `search_job_log` | read | the job, `pattern` (required unless `whole_log`), `whole_log` (false, returns `log_url` instead of matches), `regex` (false), `case_sensitive` (false), `context_lines` (0, max 20), `max_matches` (50, max 500), `tail_mb` (whole log; ignored for gzipped logs) |
| `get_job_items_link` | read | the job |
