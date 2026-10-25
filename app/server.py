"""Small authenticated subscription manager behind Xray's TLS fallback."""
import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
from contextlib import contextmanager

import generator
import domain_control
import browser_proxy
import urllib.request

import qrcode
import qrcode.image.svg

CONFIG_PATH = Path(os.environ.get('PROXY_CONFIG', '/etc/proxy-manager/config.json'))
CONFIG = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
DATA = Path(CONFIG.get('data_dir', '/var/lib/proxy-manager'))
ASSETS = Path(__file__).resolve().parent / 'web'
ACME = Path(CONFIG.get('acme_root', '/var/lib/proxy-acme')) / '.well-known/acme-challenge'
DOMAIN = CONFIG['domain']
HOST = CONFIG['origin_ip']
BASE = 'https://' + DOMAIN
SESSION_SECONDS = 30 * 86400
FAILURES = []
FAIL_LOCK = threading.Lock()
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@contextmanager
def db():
    conn = sqlite3.connect(DATA / 'panel.sqlite3', timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def meta(key, default=''):
    with db() as conn:
        row = conn.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def set_meta(key, value):
    with db() as conn:
        conn.execute('INSERT INTO metadata(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 310000).hex()


def public_subscription(row):
    value = dict(row)
    value['url'] = BASE + value['path']
    value['clash_url'] = value['url'] + '?format=clash'
    value['enabled'] = bool(value['enabled'])
    return value


def payload(row, fmt):
    if fmt == 'browser':
        return json.dumps(browser_proxy.subscription(row, CONFIG), ensure_ascii=False).encode(), 'application/json; charset=utf-8'
    return generator.render(generator.get_snapshot(db), fmt, BASE + row['path'])


def target_format(query, user_agent=''):
    explicit = query.get('target', [''])[0].lower()
    legacy = query.get('format', [''])[0].lower()
    if not explicit and legacy == 'raw':
        return 'plain'
    value = explicit or legacy
    if value and value != 'auto':
        return {'base64': 'raw', 'v2ray': 'raw', 'v2rayn': 'raw', 'shadowrocket': 'raw', 'yaml': 'clash', 'mihomo': 'clash'}.get(value, value)
    ua = user_agent.lower()
    if any(x in ua for x in ('clash', 'mihomo', 'stash')):
        return 'clash'
    return 'surge' if 'surge' in ua else 'raw'


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32
    slots = threading.BoundedSemaphore(48)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            request.settimeout(15)
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'Service'
    sys_version = ''

    def log_message(self, *_):
        pass

    def reply(self, code, body, kind='application/json; charset=utf-8', headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store, private')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        # The old panel probes the newly validated HTTPS origin before navigating to it.
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' https:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header('Connection', 'close')
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def error(self, code, message):
        return self.reply(code, {'error': message})

    def body(self):
        if self.headers.get_content_type() != 'application/json':
            raise ValueError('请使用 JSON 请求')
        if self.headers.get('Transfer-Encoding'):
            raise ValueError('不支持此请求格式')
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= 262144:
            raise ValueError('请求大小不正确')
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError('请求数据不正确')
        return data

    def session(self):
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            token = cookie['proxy_session'].value
        except Exception:
            return None
        if len(token) > 128:
            return None
        with db() as conn:
            return conn.execute('SELECT * FROM sessions WHERE token_hash=? AND expires>?',
                (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()

    def valid_origin(self):
        origin = self.headers.get('Origin')
        if origin is not None and origin not in (BASE, 'https://' + HOST):
            return False
        return self.headers.get('Sec-Fetch-Site') not in ('cross-site',)

    def do_HEAD(self):
        self.dispatch('GET')

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def do_PATCH(self):
        self.dispatch('PATCH')

    def do_DELETE(self):
        self.dispatch('DELETE')

    def dispatch(self, method):
        try:
            return self.route(method)
        except (ValueError, json.JSONDecodeError) as exc:
            return self.error(400, str(exc))
        except sqlite3.IntegrityError:
            return self.error(409, '这个订阅路径已存在，请换一个。')
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as exc:
            print('Request failed:', type(exc).__name__, flush=True)
            return self.error(500, '服务暂时不可用，请稍后重试。')

    def route(self, method):
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        query = urllib.parse.parse_qs(url.query)
        match = re.fullmatch(r'/\.well-known/acme-challenge/([A-Za-z0-9_-]{1,256})', path)
        if match and method == 'GET':
            try:
                return self.reply(200, (ACME / match[1]).read_bytes(), 'text/plain')
            except FileNotFoundError:
                pass
        if self.server.server_port != int(os.environ.get('PROXY_PRIVATE_PORT', '8080')):
            return self.error(404, 'Not found')
        if CONFIG.get('retired_domains'):
            host = self.headers.get('Host', '').lower().rstrip('.')
            if host not in (DOMAIN, DOMAIN+':443'):
                return self.error(421, '此域名已停用，请使用当前管理域名。')
        if method == 'GET' and (path == '/' or path.startswith('/assets/')):
            relative = 'index.html' if path == '/' else path.removeprefix('/assets/')
            asset = (ASSETS / relative).resolve()
            if not asset.is_relative_to(ASSETS.resolve()) or not asset.is_file():
                return self.error(404, 'Not found')
            mime = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
                    '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml'}.get(asset.suffix)
            if not mime:
                return self.error(404, 'Not found')
            return self.reply(200, asset.read_bytes(), mime)
        if method == 'GET' and path.startswith(('/subscribe/', '/s/', '/sub/')):
            with db() as conn:
                row = conn.execute('SELECT * FROM subscriptions WHERE path=? AND enabled=1', (path,)).fetchone()
                if not row:
                    return self.error(404, '订阅不存在或已停用')
                fmt = target_format(query, self.headers.get('User-Agent', ''))
                body, mime = payload(row, fmt)
                if self.command != 'HEAD':
                    conn.execute('UPDATE subscriptions SET hits=hits+1,last_access=? WHERE id=?', (int(time.time()), row['id']))
            return self.reply(200, body, mime, {'profile-update-interval': '24', 'profile-title': 'Singapore Private', 'Vary': 'User-Agent'})
        if not path.startswith('/api/'):
            return self.error(404, 'Not found')
        match = re.fullmatch(r'/api/domain-result/([a-f0-9]{32})', path)
        if match and method == 'GET':
            progress = domain_control.status(CONFIG)
            if progress.get('id') == match[1] and progress.get('domain') == DOMAIN and progress.get('status') == 'succeeded':
                return self.reply(200, {'status': 'ready', 'domain': DOMAIN, 'newUrl': BASE},
                                  headers={'Access-Control-Allow-Origin': '*'})
            return self.error(404, '尚未完成切换')
        if method != 'GET' and (not self.valid_origin()):
            return self.error(403, '请求来源校验失败，请重新打开管理页面。')
        if path == '/api/login' and method == 'POST':
            data = self.body()
            now = time.time()
            with FAIL_LOCK:
                FAILURES[:] = [t for t in FAILURES if t > now - 600]
                if len(FAILURES) >= 8:
                    return self.error(429, '登录失败次数较多，请 10 分钟后重试。')
            supplied = str(data.get('password', ''))
            if len(supplied) > 1024:
                return self.error(400, '密码过长')
            valid = hmac.compare_digest(password_hash(supplied, meta('admin_salt')), meta('admin_hash'))
            if not valid:
                with FAIL_LOCK:
                    FAILURES.append(now)
                return self.error(401, '密码不正确')
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            with db() as conn:
                conn.execute('DELETE FROM sessions WHERE expires<=?', (now,))
                conn.execute('INSERT INTO sessions VALUES (?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), csrf, now + SESSION_SECONDS))
            return self.reply(200, {'csrf': csrf}, headers={'Set-Cookie': f'proxy_session={token}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}'})
        session = self.session()
        if not session:
            return self.error(401, '请先登录')
        if method != 'GET' and not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf']):
            return self.error(403, '登录校验已失效，请刷新页面。')
        if path == '/api/state' and method == 'GET':
            with db() as conn:
                subscriptions = [public_subscription(r) for r in conn.execute('SELECT * FROM subscriptions ORDER BY created,id')]
            return self.reply(200, {'csrf': session['csrf'], 'domain': DOMAIN, 'origin_ip': HOST, 'base': BASE,
                'subscriptions': subscriptions, 'domain_ready': True, 'version': '2.1.0',
                'domainChange': domain_control.status(CONFIG),
                'generator': generator.public_state(generator.get_snapshot(db), CONFIG)})
        if path == '/api/domain' and method == 'GET':
            return self.reply(200, domain_control.status(CONFIG))
        if path == '/api/domain' and method == 'POST':
            return self.reply(202, domain_control.enqueue(self.body().get('domain'), CONFIG))
        if path == '/api/generate' and method == 'POST':
            if domain_control.busy(CONFIG):
                return self.error(409, '域名正在更换，请完成后再保存订阅设置。')
            snapshot = generator.save(self.body(), CONFIG, db)
            return self.reply(200, generator.public_state(snapshot, CONFIG))
        if path == '/api/logout' and method == 'POST':
            self.body()
            with db() as conn:
                conn.execute('DELETE FROM sessions WHERE token_hash=?', (session['token_hash'],))
            return self.reply(200, {'ok': True}, headers={'Set-Cookie': 'proxy_session=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0'})
        if path == '/api/password' and method == 'POST':
            data = self.body()
            if not hmac.compare_digest(password_hash(str(data.get('current', '')), meta('admin_salt')), meta('admin_hash')):
                return self.error(400, '当前密码不正确')
            new = str(data.get('password', ''))
            if not 8 <= len(new) <= 128:
                return self.error(400, '新密码需要 8–128 个字符')
            salt = secrets.token_hex(16)
            with db() as conn:
                conn.execute('UPDATE metadata SET value=? WHERE key=?', (salt, 'admin_salt'))
                conn.execute('UPDATE metadata SET value=? WHERE key=?', (password_hash(new, salt), 'admin_hash'))
                conn.execute('DELETE FROM sessions')
            return self.reply(200, {'ok': True}, headers={'Set-Cookie': 'proxy_session=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0'})
        if path == '/api/subscriptions' and method == 'POST':
            data = self.body()
            name = str(data.get('name', '')).strip()
            if not 1 <= len(name) <= 60:
                raise ValueError('请填写 1–60 个字符的名称')
            slug = str(data.get('slug', '')).strip().lower()
            if slug and not re.fullmatch('[a-z0-9][a-z0-9_-]{2,47}', slug):
                raise ValueError('易记路径需要 3–48 位小写字母、数字、短横线或下划线')
            route = '/subscribe/' + slug if slug else '/s/' + secrets.token_urlsafe(24)
            with db() as conn:
                count = conn.execute('SELECT count(*) FROM subscriptions').fetchone()[0]
                if count >= 500:
                    raise ValueError('订阅数量已达 500，请先删除不需要的记录')
                cursor = conn.execute('INSERT INTO subscriptions(name,path,enabled,transport,created) VALUES (?,?,1,?,?)', (name, route, 'ws', int(time.time())))
                row = conn.execute('SELECT * FROM subscriptions WHERE id=?', (cursor.lastrowid,)).fetchone()
            return self.reply(201, public_subscription(row))
        match = re.fullmatch(r'/api/subscriptions/(\d+)(/qr)?', path)
        if match:
            with db() as conn:
                row = conn.execute('SELECT * FROM subscriptions WHERE id=?', (int(match[1]),)).fetchone()
                if not row:
                    return self.error(404, '订阅不存在')
                if match[2] and method == 'GET':
                    fmt = query.get('target', query.get('format', ['auto']))[0]
                    if fmt not in ('auto', 'raw', 'clash', 'surge', 'json', 'base64', 'browser'):
                        raise ValueError('不支持的二维码格式')
                    link = BASE + row['path'] + ('?target=' + fmt if fmt != 'auto' else '')
                    output = io.BytesIO()
                    qrcode.make(link, image_factory=qrcode.image.svg.SvgPathImage, border=4).save(output)
                    return self.reply(200, output.getvalue(), 'image/svg+xml')
                if not match[2] and method == 'DELETE':
                    self.body()
                    conn.execute('DELETE FROM subscriptions WHERE id=?', (row['id'],))
                    return self.reply(200, {'ok': True})
                if not match[2] and method == 'PATCH':
                    data = self.body()
                    name = str(data.get('name', row['name'])).strip()
                    enabled = data.get('enabled', bool(row['enabled']))
                    if not 1 <= len(name) <= 60 or not isinstance(enabled, bool):
                        raise ValueError('订阅名称或状态不正确')
                    route = row['path']
                    if 'slug' in data:
                        slug = str(data['slug']).strip().lower()
                        if not re.fullmatch('[a-z0-9][a-z0-9_-]{2,47}', slug):
                            raise ValueError('易记路径需要 3–48 位小写字母、数字、短横线或下划线')
                        route = '/subscribe/' + slug
                    conn.execute('UPDATE subscriptions SET name=?,path=?,enabled=? WHERE id=?', (name, route, int(enabled), row['id']))
                    result = conn.execute('SELECT * FROM subscriptions WHERE id=?', (row['id'],)).fetchone()
                    return self.reply(200, public_subscription(result))
        if path == '/api/connection' and method == 'GET':
            try:
                request = urllib.request.Request(BASE + '/', method='HEAD')
                with HTTP.open(request, timeout=12) as response:
                    proxied = bool(response.headers.get('CF-Ray'))
                return self.reply(200, {'proxied': proxied, 'checked': int(time.time())})
            except OSError:
                return self.reply(200, {'proxied': None, 'checked': int(time.time())})
        return self.error(404, 'Not found')


if __name__ == '__main__':
    os.umask(0o077)
    generator.initialize(CONFIG, db)
    public = Server(('0.0.0.0', int(os.environ.get('PROXY_PUBLIC_PORT', '80'))), Handler)
    private = Server(('127.0.0.1', int(os.environ.get('PROXY_PRIVATE_PORT', '8080'))), Handler)
    threading.Thread(target=public.serve_forever, daemon=True).start()
    private.serve_forever()
