# coding: utf-8
from contextlib import contextmanager
from timeit import default_timer

from prometheus_client import Counter, Histogram

from ..metrics import REGISTRY_KEY


MCP_METRICS_KEY = 'mcp_metrics'
# The tools take from milliseconds, like list_nodes, to minutes, like deploy_project or fire_timer_task
TOOL_DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)


class McpMetrics(object):
    def __init__(self, registry):
        self.http_requests = Counter('scrapydweb_mcp_http_requests', "HTTP requests to the MCP server",
                                     ['status'], registry=registry)
        self.tool_calls = Counter('scrapydweb_mcp_tool_calls', "Calls of the MCP tool",
                                  ['tool', 'status'], registry=registry)
        self.tool_duration = Histogram('scrapydweb_mcp_tool_duration_seconds', "Duration of the calls of the MCP tool",
                                       ['tool'], buckets=TOOL_DURATION_BUCKETS, registry=registry)

    @contextmanager
    def track_tool(self, tool):
        start = default_timer()
        status = 'error'
        try:
            yield
            status = 'ok'
        finally:
            self.tool_duration.labels(tool).observe(max(default_timer() - start, 0))
            self.tool_calls.labels(tool, status).inc()


def get_mcp_metrics(app):
    # None if ENABLE_METRICS is False, or init_metrics() has not been called yet
    registry = app.extensions.get(REGISTRY_KEY)
    if registry is None:
        return None
    # Only once per app, since a registry rejects the metrics registered twice
    if MCP_METRICS_KEY not in app.extensions:
        app.extensions[MCP_METRICS_KEY] = McpMetrics(registry)
    return app.extensions[MCP_METRICS_KEY]


class MetricsMiddleware(object):
    def __init__(self, app, metrics):
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return

        async def send_with_metrics(message):
            if message['type'] == 'http.response.start':
                self.metrics.http_requests.labels(str(message['status'])).inc()
            await send(message)

        await self.app(scope, receive, send_with_metrics)
