import asyncio
import contextlib
import os
from pathlib import Path
import tempfile
import unittest
from render_tunnel import Relay, validate


class ConfigurationTests(unittest.TestCase):
    def env(self):
        return {'TAILSCALE_AUTHKEY': 'test-only', 'TAILSCALE_DB_HOST': '100.90.80.70',
                'DATABASE_URL': 'postgresql+psycopg://reader:test@127.0.0.1:15432/document_search',
                'SESSION_SECRET': 'x' * 40}

    def test_local_forward_only(self):
        self.assertEqual(validate(self.env()), ('100.90.80.70', 8000))
        for url in ['postgresql+psycopg://u:p@localhost:5433/document_search',
                    'postgresql+psycopg://u:p@public.example:15432/document_search']:
            env = self.env(); env['DATABASE_URL'] = url
            with self.assertRaises(ValueError): validate(env)

    def test_only_tailscale_ipv4(self):
        for host in ['127.0.0.1', '109.1.2.3', '100.63.0.1', '100.128.0.1', 'invalid']:
            env = self.env(); env['TAILSCALE_DB_HOST'] = host
            with self.assertRaises(ValueError): validate(env)

    def test_errors_do_not_print_password(self):
        env = self.env(); env['DATABASE_URL'] = 'bad://secret-password'
        with self.assertRaises(ValueError) as cm: validate(env)
        self.assertNotIn('secret-password', str(cm.exception))


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        binary = Path(self.temp.name) / 'fake-tailscale'
        # Real TCP relay through a subprocess, replacing only Tailscale transport.
        binary.write_text('''#!/usr/bin/env python3
import socket, sys, threading
s = socket.create_connection((sys.argv[-2], int(sys.argv[-1])))
def send():
    while b := sys.stdin.buffer.read1(65536): s.sendall(b)
    s.shutdown(socket.SHUT_WR)
t = threading.Thread(target=send, daemon=True); t.start()
while b := s.recv(65536):
    sys.stdout.buffer.write(b); sys.stdout.buffer.flush()
s.close()
''')
        binary.chmod(0o700)
        async def echo(reader, writer):
            try:
                while b := await reader.read(65536):
                    writer.write(b); await writer.drain()
                writer.write(b'FINAL'); await writer.drain()
            finally:
                writer.close(); await writer.wait_closed()
        self.echo = await asyncio.start_server(echo, '127.0.0.1', 0)
        remote_port = self.echo.sockets[0].getsockname()[1]
        self.relay = Relay('127.0.0.1', str(binary), remote_port)
        self.server = await asyncio.start_server(self.relay.handle, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close(); await self.server.wait_closed()
        await self.relay.close()
        self.echo.close(); await self.echo.wait_closed()
        self.temp.cleanup()

    async def exchange(self, payload):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        writer.write(payload); await writer.drain(); writer.write_eof()
        result = await asyncio.wait_for(reader.read(), 5)
        writer.close(); await writer.wait_closed()
        self.assertEqual(result, payload + b'FINAL')

    async def test_binary_data_and_half_close(self):
        await self.exchange(bytes(range(256)) * 1024)

    async def test_concurrent_connections(self):
        await asyncio.gather(*(self.exchange(str(i).encode() * 10000) for i in range(4)))

    async def test_shutdown_closes_idle_connections(self):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        writer.write(b'test'); await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(4), 5), b'test')
        await self.relay.close()
        self.assertEqual(await asyncio.wait_for(reader.read(), 5), b'')
        writer.close(); await writer.wait_closed()
        self.assertFalse(self.relay.tasks)


if __name__ == '__main__': unittest.main()
