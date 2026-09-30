# coding: utf-8
import logging
import threading
import time

from mcp.server.transport_security import TransportSecuritySettings
import uvicorn

from .auth import BasicAuthMiddleware
from .server import create_mcp_server


logger = logging.getLogger(__name__)

MAX_CONCURRENT_REQUESTS = 16


def create_asgi_app(app):
    allowed_hosts = app.config.get('MCP_ALLOWED_HOSTS', [])
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(allowed_hosts),
        allowed_hosts=allowed_hosts,
        allowed_origins=app.config.get('MCP_ALLOWED_ORIGINS', []),
    )
    starlette_app = create_mcp_server(app).streamable_http_app(
        stateless_http=True, json_response=True, transport_security=transport_security)
    return BasicAuthMiddleware(starlette_app, app.config['MCP_USERNAME'], app.config['MCP_PASSWORD'])


def start_mcp_server(app, timeout=10):
    kwargs = {}
    if app.config.get('ENABLE_HTTPS', False):
        kwargs.update(ssl_certfile=app.config['CERTIFICATE_FILEPATH'], ssl_keyfile=app.config['PRIVATEKEY_FILEPATH'])
    config = uvicorn.Config(create_asgi_app(app), host=app.config['MCP_BIND'], port=app.config['MCP_PORT'],
                            log_config=None, limit_concurrency=MAX_CONCURRENT_REQUESTS, **kwargs)
    server = uvicorn.Server(config)

    def serve():
        try:
            server.run()
        except BaseException:
            logger.exception("The MCP server stopped")

    thread = threading.Thread(target=serve, name='mcp-server', daemon=True)
    thread.start()

    deadline = time.time() + timeout
    while not server.started and thread.is_alive() and time.time() < deadline:
        time.sleep(0.1)
    if server.started:
        logger.info("MCP server running at %s://%s:%s/mcp", 'https' if kwargs else 'http',
                    app.config['MCP_BIND'], app.config['MCP_PORT'])
    else:
        logger.error("Fail to start the MCP server on %s:%s", app.config['MCP_BIND'], app.config['MCP_PORT'])
    return server
