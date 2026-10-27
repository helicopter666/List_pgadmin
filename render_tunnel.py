"""Render -> loopback TCP relay -> Tailscale userspace -> Mac PostgreSQL."""
import asyncio
import contextlib
import ipaddress
import os
from pathlib import Path
import signal
import sys
import tempfile
from urllib.parse import urlsplit
import uuid

LOCAL_PORT = 15432
SOCKET = '/tmp/docsearch-tailscaled.sock'


def validate(env):
    required = ('TAILSCALE_AUTHKEY', 'TAILSCALE_DB_HOST', 'DATABASE_URL', 'SESSION_SECRET')
    for key in required:
        if not env.get(key, '').strip():
            raise ValueError(f'Не задана переменная {key}')
    host = env['TAILSCALE_DB_HOST'].strip()
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError('TAILSCALE_DB_HOST должен содержать IPv4 Mac из Tailscale') from None
    if addr.version != 4 or addr not in ipaddress.ip_network('100.64.0.0/10'):
        raise ValueError('Нужен IPv4 Tailscale из диапазона 100.64.0.0/10')
    try:
        url = urlsplit(env['DATABASE_URL'])
        correct = (url.scheme == 'postgresql+psycopg'
                   and url.hostname == '127.0.0.1' and url.port == LOCAL_PORT
                   and bool(url.username) and bool(url.password)
                   and url.path == '/document_search')
    except ValueError:
        correct = False
    if not correct:
        raise ValueError('DATABASE_URL должен вести на 127.0.0.1:15432/document_search через psycopg')
    if len(env['SESSION_SECRET']) < 32:
        raise ValueError('SESSION_SECRET должен содержать минимум 32 символа')
    web_port = int(env.get('PORT', '8000'))
    if not 1 <= web_port <= 65535 or web_port == LOCAL_PORT:
        raise ValueError('Некорректный HTTP PORT')
    return host, web_port


async def stop_process(proc):
    if proc is None or proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()


async def pump(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, BrokenPipeError, OSError):
        pass


class Relay:
    def __init__(self, host, binary='tailscale', remote_port=5433):
        self.host, self.binary, self.remote_port = host, binary, remote_port
        self.tasks = set()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        proc = None
        try:
            child_env = dict(os.environ)
            for key in ('TAILSCALE_AUTHKEY', 'DATABASE_URL', 'SESSION_SECRET'):
                child_env.pop(key, None)
            proc = await asyncio.create_subprocess_exec(
                self.binary, '--socket=' + SOCKET, 'nc', self.host, str(self.remote_port),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=child_env)
            # Preserve half-close: PostgreSQL can still send its final response
            # after the client closes its sending side.
            await asyncio.gather(pump(reader, proc.stdin), pump(proc.stdout, writer))
        except asyncio.CancelledError:
            raise
        except (OSError, ConnectionError):
            print('Соединение с Mac через Tailscale не установлено', flush=True)
        finally:
            await stop_process(proc)
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()
            self.tasks.discard(task)

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def bootstrap(env):
    # Write auth key to a private temporary file, never to command arguments.
    fd, name = tempfile.mkstemp(prefix='docsearch-auth-', dir='/tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(env['TAILSCALE_AUTHKEY'])
        child_env = dict(env)
        for key in ('TAILSCALE_AUTHKEY', 'DATABASE_URL', 'SESSION_SECRET'):
            child_env.pop(key, None)
        proc = await asyncio.create_subprocess_exec(
            'tailscale', '--socket=' + SOCKET, 'up', '--auth-key=file:' + name,
            '--hostname=docsearch-render-' + uuid.uuid4().hex[:8],
            '--accept-dns=false', '--timeout=60s',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            env=child_env)
        try:
            rc = await asyncio.wait_for(proc.wait(), 65)
        finally:
            await stop_process(proc)
        if rc:
            raise RuntimeError('Вход Tailscale не выполнен: проверь ключ, срок действия и одобрение устройства')
    finally:
        Path(name).unlink(missing_ok=True)


async def check_database():
    # The parent loop must stay active while the subprocess uses the TCP relay.
    # No URL or database exception text is printed, because it can contain secrets.
    probe = """import os
import sys
try:
    from sqlalchemy import create_engine, text
    e = create_engine(os.environ['DATABASE_URL'], connect_args={'connect_timeout': 8})
    with e.connect() as c:
        c.execute(text('SELECT 1'))
    e.dispose()
except Exception:
    sys.exit(1)
"""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, '-c', probe,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        return await asyncio.wait_for(proc.wait(), 15) == 0
    except asyncio.TimeoutError:
        return False
    finally:
        await stop_process(proc)


async def main():
    env = dict(os.environ)
    host, web_port = validate(env)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    daemon = web = server = relay = None
    pending = []
    try:
        daemon_env = dict(env)
        for key in ('TAILSCALE_AUTHKEY', 'DATABASE_URL', 'SESSION_SECRET'):
            daemon_env.pop(key, None)
        daemon = await asyncio.create_subprocess_exec(
            'tailscaled', '--tun=userspace-networking', '--state=mem:',
            '--socket=' + SOCKET, env=daemon_env)
        for _ in range(100):
            if daemon.returncode is not None:
                raise RuntimeError('tailscaled завершился до запуска')
            if Path(SOCKET).exists():
                break
            await asyncio.sleep(.1)
        else:
            raise RuntimeError('Не появился сокет tailscaled')
        await bootstrap(env)
        print('Tailscale подключён; проверяем PostgreSQL на Mac', flush=True)
        relay = Relay(host)
        server = await asyncio.start_server(relay.handle, '127.0.0.1', LOCAL_PORT)
        for _ in range(3):
            if stop.is_set():
                return
            if await check_database():
                break
            await asyncio.sleep(2)
        else:
            raise RuntimeError('Нет подключения к PostgreSQL: проверь Mac, Serve, права Tailscale и пароль БД')
        print('PostgreSQL на Mac доступен; запускаем сайт', flush=True)
        web_env = dict(env)
        web_env.pop('TAILSCALE_AUTHKEY', None)
        web = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0',
            '--port', str(web_port), '--workers', '1', env=web_env)
        pending = [asyncio.create_task(stop.wait()), asyncio.create_task(daemon.wait()),
                   asyncio.create_task(web.wait())]
        await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        if not stop.is_set():
            raise RuntimeError('Сайт или tailscaled завершился; контейнер должен быть перезапущен')
    finally:
        if server:
            server.close()
            await server.wait_closed()
        await stop_process(web)
        if relay:
            await relay.close()
        await stop_process(daemon)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (ValueError, RuntimeError, asyncio.TimeoutError) as exc:
        print('Ошибка запуска: ' + str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
