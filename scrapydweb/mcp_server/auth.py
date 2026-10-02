# coding: utf-8
import base64
import binascii
import hmac
import json


class BasicAuthMiddleware(object):
    def __init__(self, app, username, password, realm='ScrapydWeb MCP'):
        self.app = app
        self.username = username.encode('utf-8')
        self.password = password.encode('utf-8')
        self.realm = realm

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan' or self.is_authorized(scope):
            await self.app(scope, receive, send)
        elif scope['type'] == 'websocket':
            await receive()
            await send({'type': 'websocket.close', 'code': 1008})
        else:
            body = json.dumps({'error': 'Unauthorized'}).encode('utf-8')
            await send({
                'type': 'http.response.start',
                'status': 401,
                'headers': [
                    (b'content-type', b'application/json'),
                    (b'content-length', str(len(body)).encode('latin-1')),
                    (b'www-authenticate', ('Basic realm="%s"' % self.realm).encode('latin-1')),
                ],
            })
            await send({'type': 'http.response.body', 'body': body})

    def is_authorized(self, scope):
        for name, value in scope.get('headers', []):
            if name.lower() != b'authorization':
                continue
            scheme, __, credentials = value.partition(b' ')
            if scheme.lower() != b'basic':
                return False
            try:
                decoded = base64.b64decode(credentials.strip(), validate=True)
            except (binascii.Error, ValueError):
                return False
            username, sep, password = decoded.partition(b':')
            username_ok = hmac.compare_digest(username, self.username)
            password_ok = hmac.compare_digest(password, self.password)
            return bool(sep) and username_ok and password_ok
        return False
