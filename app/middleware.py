from fastapi import HTTPException
from starlette.responses import PlainTextResponse

class RequestGuards:
    def __init__(self, app, max_bytes):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        length = headers.get(b'content-length')
        if length is not None:
            try:
                over = int(length) < 0 or int(length) > self.max_bytes
            except ValueError:
                over = True
            if over:
                return await PlainTextResponse('Слишком большой запрос. Уменьшите размер файла.', status_code=413)(scope, receive, send)
        count = 0
        async def limited_receive():
            nonlocal count
            message = await receive()
            if message['type'] == 'http.request':
                count += len(message.get('body', b''))
                if count > self.max_bytes:
                    raise HTTPException(413, 'Слишком большой запрос. Уменьшите размер файла.')
            return message
        async def secure_send(message):
            if message['type'] == 'http.response.start':
                message['headers'] = list(message.get('headers', [])) + [
                    (b'x-content-type-options', b'nosniff'),
                    (b'x-frame-options', b'DENY'),
                    (b'referrer-policy', b'same-origin'),
                    (b'cache-control', b'no-store'),
                    (b'content-security-policy', b"default-src 'self'; style-src 'self'; script-src 'none'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")]
            await send(message)
        return await self.app(scope, limited_receive, secure_send)
