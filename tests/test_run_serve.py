# coding: utf-8
import asyncio
import threading

from flask import request as flask_request
import pytest

from scrapydweb.metrics import SERVER_METRICS_KEY, init_metrics
from scrapydweb.run import KEEP_ALIVE_TIMEOUT, create_asgi_app, serve
from tests.utils import find_samples as find, get_metric_samples as get_samples


uvicorn = pytest.importorskip('uvicorn')
a2wsgi = pytest.importorskip('a2wsgi')


@pytest.fixture
def served(app, monkeypatch):
    app.config.update(DEBUG=False, WSGI_SERVER='uvicorn', WSGI_THREADS=128)
    calls = {}

    def fake_uvicorn_run(server):
        calls['uvicorn'] = server.config

    def fake_flask_run(**kwargs):
        calls['werkzeug'] = kwargs

    monkeypatch.setattr(uvicorn.Server, 'run', fake_uvicorn_run)
    monkeypatch.setattr(app, 'run', fake_flask_run)
    return calls


def test_serve_with_uvicorn(app, served):
    serve(app)
    assert 'werkzeug' not in served
    config = served['uvicorn']
    assert isinstance(config.app, a2wsgi.WSGIMiddleware)
    assert config.app.executor._max_workers == 128
    assert (config.host, config.port) == (app.config['SCRAPYDWEB_BIND'], app.config['SCRAPYDWEB_PORT'])
    # The requests beyond WSGI_THREADS should queue up instead of getting 503
    assert config.limit_concurrency is None
    # Longer than the 2 minutes Caddy keeps the idle connections to reuse them, which would answer 502 otherwise
    assert config.timeout_keep_alive == KEEP_ALIVE_TIMEOUT > 120
    assert config.ssl_certfile is None


def test_serve_with_uvicorn_in_https(app, served):
    serve(app, ('cert.pem', 'cert.key'))
    config = served['uvicorn']
    assert (config.ssl_certfile, config.ssl_keyfile) == ('cert.pem', 'cert.key')


@pytest.mark.parametrize('settings', [dict(WSGI_SERVER='werkzeug'), dict(DEBUG=True)])
def test_serve_with_werkzeug(app, served, settings):
    app.config.update(settings)
    serve(app, ('cert.pem', 'cert.key'))
    assert 'uvicorn' not in served
    assert served['werkzeug'] == dict(host=app.config['SCRAPYDWEB_BIND'], port=app.config['SCRAPYDWEB_PORT'],
                                      ssl_context=('cert.pem', 'cert.key'), use_reloader=False)


async def request(asgi_app, path, method='GET', chunks=(b'',), headers=()):
    messages = [{'type': 'http.request', 'body': chunk, 'more_body': i < len(chunks) - 1}
                for i, chunk in enumerate(chunks)]
    sent = []

    async def receive():
        return messages.pop(0) if messages else {'type': 'http.disconnect'}

    async def send(message):
        sent.append(message)

    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': method,
             'scheme': 'http', 'path': path, 'raw_path': path.encode(), 'query_string': b'', 'root_path': '',
             'headers': [(b'host', b'localhost')] + list(headers), 'client': ('127.0.0.1', 12345),
             'server': ('localhost', 5000)}
    await asgi_app(scope, receive, send)
    body = b''.join(message.get('body', b'') for message in sent if message['type'] == 'http.response.body')
    return sent[0]['status'], body


@pytest.mark.parametrize('headers', [[(b'content-length', b'11')], [(b'transfer-encoding', b'chunked')]])
def test_request_body(app, headers):
    @app.route('/test_request_body', methods=['POST'])
    def echo():
        return flask_request.get_data()

    assert asyncio.run(request(create_asgi_app(app), '/test_request_body', method='POST',
                               chunks=(b'hello', b' world'), headers=headers)) == (200, b'hello world')


def test_server_metrics_queue(app):
    app.config.update(ENABLE_METRICS=True, WSGI_THREADS=1)
    init_metrics(app)
    release = threading.Event()

    @app.route('/test_server_metrics_queue')
    def block():
        release.wait(10)
        return 'ok'

    asgi_app = create_asgi_app(app)
    server_metrics = app.extensions[SERVER_METRICS_KEY]

    async def burst():
        requests = [asyncio.ensure_future(request(asgi_app, '/test_server_metrics_queue')) for __ in range(3)]
        for __ in range(500):
            if server_metrics.inflight == 3 and server_metrics.running == 1:
                break
            await asyncio.sleep(0.01)
        # One request runs in the only thread, the other two wait for it instead of failing
        samples = get_samples(app.test_client())
        assert find(samples, 'scrapydweb_http_requests_inflight') == [3]
        assert find(samples, 'scrapydweb_http_requests_queued') == [2]
        assert find(samples, 'scrapydweb_http_threads') == [1]
        release.set()
        return await asyncio.gather(*requests)

    assert asyncio.run(burst()) == [(200, b'ok')] * 3
    assert (server_metrics.inflight, server_metrics.running) == (0, 0)
    samples = get_samples(app.test_client())
    assert find(samples, 'scrapydweb_http_requests_inflight') == [0]
    assert find(samples, 'scrapydweb_http_requests_queued') == [0]


def test_no_server_metrics_without_enable_metrics(app):
    app.config.update(ENABLE_METRICS=False)
    assert isinstance(create_asgi_app(app), a2wsgi.WSGIMiddleware)
    assert SERVER_METRICS_KEY not in app.extensions
