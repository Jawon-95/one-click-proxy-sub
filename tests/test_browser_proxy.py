import base64
from contextlib import contextmanager
import ipaddress
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'app'))
import browser_proxy as proxy


class BrowserProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'db.sqlite'
        with self.db() as conn:
            conn.execute('CREATE TABLE subscriptions(id INTEGER PRIMARY KEY,path TEXT,enabled INTEGER)')
            conn.execute("INSERT INTO subscriptions VALUES (1,'/subscribe/fixture',1)")
        self.config = {'origin_ip': '203.0.113.10', 'browser_proxy': {'enabled': True, 'port': 8443, 'secret': 'fixture-only-secret'}}
        self.row = {'id': 1, 'path': '/subscribe/fixture'}
        self.user, self.password = proxy.credentials(self.row, self.config)
        self.auth = 'Basic '+base64.b64encode(f'{self.user}:{self.password}'.encode()).decode()

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path); conn.row_factory = sqlite3.Row
        try:
            with conn: yield conn
        finally: conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_credentials_and_revocation(self):
        self.assertTrue(proxy.authenticate(self.auth, self.config, self.db))
        self.assertFalse(proxy.authenticate('Basic garbage', self.config, self.db))
        self.assertFalse(proxy.authenticate(self.auth+'x', self.config, self.db))
        unicode_auth = 'Basic '+base64.b64encode(f'{self.user}:非ASCII'.encode()).decode()
        self.assertFalse(proxy.authenticate(unicode_auth, self.config, self.db))
        with self.db() as conn: conn.execute('UPDATE subscriptions SET enabled=0')
        self.assertFalse(proxy.authenticate(self.auth, self.config, self.db))

    def test_browser_subscription_is_separate_from_trojan(self):
        value = proxy.subscription(self.row, self.config)
        self.assertEqual(value['nodes'][0]['scheme'], 'https')
        self.assertEqual(value['nodes'][0]['port'], 8443)
        self.assertNotIn('trojan', json.dumps(value).lower().split('note')[0])
        self.assertEqual(len(value['nodes']), 1)
        self.config['browser_proxy']['enabled'] = False
        with self.assertRaises(ValueError): proxy.subscription(self.row, self.config)

    def test_unavailable_database_denies_authentication(self):
        @contextmanager
        def unavailable():
            raise sqlite3.OperationalError('database unavailable')
            yield
        self.assertFalse(proxy.authenticate(self.auth, self.config, unavailable))

    def test_dns_rebinding_private_and_proxy_loop_rejected(self):
        for ip in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fe80::1', '::ffff:8.8.8.8'):
            answer = [(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))]
            with patch.object(socket, 'getaddrinfo', return_value=answer), self.assertRaises(ValueError):
                proxy.destinations('fixture.example', 443, self.config['origin_ip'], 8443)
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))
        with patch.object(socket, 'getaddrinfo', return_value=[public, private]), self.assertRaises(ValueError):
            proxy.destinations('rebind.example', 443, self.config['origin_ip'], 8443)
        with self.assertRaises(ValueError): proxy.destinations(self.config['origin_ip'], 8443, self.config['origin_ip'], 8443)

    def test_real_connect_requires_auth_and_relays_opaque_bytes(self):
        server = proxy.Server(('127.0.0.1', 0), self.config, self.db)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with socket.create_connection(server.server_address) as client:
                client.sendall(b'CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n')
                self.assertIn(b'407', client.recv(4096))
            local, upstream = socket.socketpair()
            with patch.object(proxy, 'dial', return_value=upstream):
                with socket.create_connection(server.server_address) as client:
                    client.settimeout(5); local.settimeout(5)
                    # Send the first tunneled bytes in the same TCP write as
                    # CONNECT: Chrome may do this for an authenticated proxy.
                    early = b'opaque-client-bytes'
                    client.sendall(f'CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\nProxy-Authorization: {self.auth}\r\n\r\n'.encode() + early)
                    response = b''
                    while b'\r\n\r\n' not in response: response += client.recv(4096)
                    self.assertIn(b'200 Connection Established', response)
                    self.assertEqual(local.recv(100), early)
                    local.sendall(b'opaque-server-bytes'); self.assertEqual(client.recv(100), b'opaque-server-bytes')
            local.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(5)

    def test_plain_http_forwards_without_exposing_proxy_credentials(self):
        server = proxy.Server(('127.0.0.1', 0), self.config, self.db)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        target, upstream = socket.socketpair()
        observed = []
        def origin():
            data = b''
            while b'\r\n\r\n' not in data: data += target.recv(4096)
            observed.append(data)
            target.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 7\r\nConnection: close\r\n\r\nfixture')
            target.close()
        target_thread = threading.Thread(target=origin, daemon=True); target_thread.start()
        try:
            with patch.object(proxy, 'dial', return_value=upstream):
                with socket.create_connection(server.server_address) as client:
                    client.sendall(f'GET http://example.com/path HTTP/1.1\r\nHost: example.com\r\nProxy-Authorization: {self.auth}\r\n\r\n'.encode())
                    response = b''
                    while chunk := client.recv(4096): response += chunk
                    self.assertTrue(response.endswith(b'fixture'))
                    self.assertIn(b'GET /path HTTP/1.1', observed[0])
                    self.assertNotIn(b'Proxy-Authorization', observed[0])
                    self.assertNotIn(self.password.encode(), observed[0])
        finally:
            server.shutdown(); server.server_close(); thread.join(5); target_thread.join(5)


if __name__ == '__main__': unittest.main()
