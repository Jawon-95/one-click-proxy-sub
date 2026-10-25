"""Authenticated, TLS-wrapped HTTP forward proxy for native browser proxy APIs.

Runs independently of Xray. Only public destinations are reachable. Every DNS
answer is checked, then a validated numeric address is dialled to prevent DNS
rebinding. Credentials are derived per enabled subscription, never from SSH.
"""
import base64
from contextlib import contextmanager
import hashlib
import hmac
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import select
import socket
import sqlite3
import ssl
import threading
import time
from urllib.parse import urlsplit

MAX_CONNECTIONS = 128
MAX_BODY = 16 * 1024 * 1024
HOP_HEADERS = {'connection', 'proxy-connection', 'proxy-authorization',
               'proxy-authenticate', 'keep-alive', 'te', 'trailer', 'upgrade', 'transfer-encoding'}


def credentials(row, config):
    secret = config['browser_proxy']['secret']
    username = f'browser_{row["id"]}'
    password = hmac.new(secret.encode(), f'{row["id"]}:{row["path"]}'.encode(), hashlib.sha256).hexdigest()
    return username, password


def subscription(row, config):
    options = config.get('browser_proxy', {})
    if not options.get('enabled'):
        raise ValueError('浏览器专用代理尚未启用，请先由服务器管理员启用。')
    username, password = credentials(row, config)
    host = options.get('host', config['origin_ip'])
    return {'version': 1, 'name': 'Singapore Private', 'updatedAt': int(time.time()),
            'nodes': [{'id': 'singapore-origin', 'name': '新加坡 · HTTPS 专线',
                       'scheme': 'https', 'host': host, 'port': options.get('port', 8443),
                       'username': username, 'password': password}],
            'note': '浏览器专用 HTTPS 入口。原 Trojan/CDN 节点保留在原订阅格式中。'}


def authenticate(value, config, db):
    if not config.get('browser_proxy', {}).get('enabled') or not value or len(value) > 1024:
        return False
    try:
        scheme, encoded = value.split(' ', 1)
        if scheme.lower() != 'basic':
            return False
        username, password = base64.b64decode(encoded, validate=True).decode('utf-8').split(':', 1)
        match = re.fullmatch(r'browser_([1-9][0-9]{0,12})', username)
        if not match or not re.fullmatch(r'[a-f0-9]{64}', password):
            return False
        with db() as conn:
            row = conn.execute('SELECT id,path FROM subscriptions WHERE id=? AND enabled=1', (int(match[1]),)).fetchone()
        return row is not None and hmac.compare_digest(credentials(row, config)[1], password)
    except (ValueError, UnicodeError, TypeError, sqlite3.Error):
        return False


def destinations(host, port, origin_ip, own_port):
    if not host or not 1 <= port <= 65535 or any(c in host for c in '\r\n\x00'):
        raise ValueError('Invalid destination')
    # Prevent forwarding to this proxy itself, even through a public IP.
    if host == origin_ip and port == own_port:
        raise ValueError('Proxy recursion is not allowed')
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not answers:
        raise ValueError('No destination addresses')
    result = []
    for family, kind, proto, _, address in answers:
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global or ip.is_multicast or (ip.version == 6 and ip.ipv4_mapped is not None):
            raise ValueError('Private/reserved destinations are not allowed')
        if str(ip) == origin_ip and port == own_port:
            raise ValueError('Proxy recursion is not allowed')
        result.append((family, kind, proto, address))
    return result


def dial(host, port, config):
    options = config['browser_proxy']
    addresses = destinations(host, port, config['origin_ip'], options.get('port', 8443))
    last = None
    for family, kind, proto, address in addresses:
        remote = socket.socket(family, kind, proto)
        remote.settimeout(12)
        try:
            remote.connect(address)
            return remote
        except OSError as exc:
            last = exc
            remote.close()
    raise last or OSError('Connection failed')


def relay(client, upstream, idle=300):
    """Bounded buffers and select, including decrypted TLS bytes already buffered."""
    client.setblocking(False)
    upstream.setblocking(False)
    buffers = {client: bytearray(), upstream: bytearray()}
    peers = {client: upstream, upstream: client}
    deadline = time.monotonic() + idle
    while time.monotonic() < deadline:
        readable = [s for s in peers if len(buffers[peers[s]]) < 262144]
        writable = [s for s in peers if buffers[s]]
        ready, writers, _ = select.select(readable, writable, [], 1)
        if isinstance(client, ssl.SSLSocket) and client.pending() and client in readable and client not in ready:
            ready.append(client)
        for stream in ready:
            try:
                data = stream.recv(65536)
                if not data:
                    # Flush data already read before closing a normal HTTP response.
                    destination = peers[stream]
                    destination.settimeout(10)
                    if buffers[destination]:
                        destination.sendall(buffers[destination])
                    return
                buffers[peers[stream]].extend(data)
                deadline = time.monotonic() + idle
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                pass
        for stream in writers:
            try:
                count = stream.send(buffers[stream])
                del buffers[stream][:count]
                deadline = time.monotonic() + idle
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'PrivateProxy'
    sys_version = ''
    # CONNECT clients may pipeline the first TLS bytes with the request.  The
    # default buffered reader can consume those bytes before relay() gets the
    # underlying socket, so keep request parsing unbuffered.
    rbufsize = 0

    def log_message(self, *_):
        pass  # Do not log browsing URLs, credentials or tunnel contents.

    def reply(self, status, body=b'', headers=None):
        self.close_connection = True
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'no-store')
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def authorized(self):
        return authenticate(self.headers.get('Proxy-Authorization'), self.server.config, self.server.db)

    def challenge(self):
        self.reply(407, headers={'Proxy-Authenticate': 'Basic realm="Private browser proxy", charset="UTF-8"'})

    def do_CONNECT(self):
        if not self.authorized():
            return self.challenge()
        try:
            target = urlsplit('//' + self.path)
            if target.path or target.query or target.fragment or target.username or not target.port:
                return self.reply(400)
            upstream = dial(target.hostname, target.port, self.server.config)
        except ValueError:
            return self.reply(403)
        except OSError:
            return self.reply(502)
        self.close_connection = True
        with upstream:
            self.send_response(200, 'Connection Established')
            self.end_headers()
            self.wfile.flush()
            try:
                relay(self.connection, upstream)
            except (OSError, TimeoutError):
                pass

    def forward(self):
        if not self.authorized():
            return self.challenge()
        url = urlsplit(self.path)
        if url.scheme != 'http' or not url.hostname or url.username or url.fragment:
            return self.reply(400)
        if self.headers.get('Transfer-Encoding'):
            return self.reply(501)
        if len(self.headers.get_all('Content-Length', [])) > 1:
            return self.reply(400)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 <= length <= MAX_BODY:
                return self.reply(413)
            sock = dial(url.hostname, url.port or 80, self.server.config)
        except ValueError:
            return self.reply(403)
        except OSError:
            return self.reply(502)
        connection = HTTPConnection(url.hostname, url.port or 80, timeout=30)
        connection.sock = sock
        try:
            extra_hop = {p.strip().lower() for p in self.headers.get('Connection', '').split(',')}
            headers = {name: value for name, value in self.headers.items() if name.lower() not in HOP_HEADERS | extra_hop}
            headers['Connection'] = 'close'
            route = url.path or '/'
            if url.query:
                route += '?' + url.query
            body = bytearray()
            while len(body) < length:
                chunk = self.rfile.read(min(65536, length - len(body)))
                if not chunk:
                    return self.reply(400)
                body.extend(chunk)
            connection.request(self.command, route, body=bytes(body) if length else None, headers=headers)
            response = connection.getresponse()
            self.close_connection = True
            self.send_response(response.status, response.reason)
            for name, value in response.getheaders():
                if name.lower() not in HOP_HEADERS:
                    self.send_header(name, value)
            self.send_header('Connection', 'close')
            self.end_headers()
            while chunk := response.read(65536):
                self.wfile.write(chunk)
        except (OSError, TimeoutError):
            self.close_connection = True
        finally:
            connection.close()

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = forward


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, config, db, tls=None):
        self.config, self.db, self.tls = config, db, tls
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(address, Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(False):
            return self.shutdown_request(request)
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        secure = request
        try:
            request.settimeout(15)
            if self.tls:
                secure = self.tls.wrap_socket(request, server_side=True)
            self.finish_request(secure, address)
        except (OSError, TimeoutError):
            pass
        finally:
            self.shutdown_request(secure)
            self.slots.release()


def main():
    config = json.loads(Path(os.environ.get('PROXY_CONFIG', '/etc/proxy-manager/config.json')).read_text())
    options = config['browser_proxy']
    if not options.get('enabled'):
        raise SystemExit('Browser proxy not enabled')

    @contextmanager
    def db():
        # The service is deliberately run with ProtectSystem=strict.  Opening
        # URI read-only mode prevents this proxy from changing panel rows.
        # The service still permits SQLite WAL shared-memory lock coordination
        # in the data directory; the rest of the filesystem stays read-only.
        database = Path(config['data_dir']) / 'panel.sqlite3'
        connection = sqlite3.connect(f'{database.as_uri()}?mode=ro', uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    if options.get('fronted_by_xray'):
        Server(('127.0.0.1', options.get('internal_port', 18080)), config, db).serve_forever()
    else:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.set_alpn_protocols(['http/1.1'])
        context.load_cert_chain(options['certificate'], options['private_key'])
        Server(('0.0.0.0', options.get('port', 8443)), config, db, context).serve_forever()


if __name__ == '__main__':
    main()
