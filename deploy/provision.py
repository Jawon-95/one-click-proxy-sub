"""Provision/migrate a generic Linux deployment. No installation credentials in source."""
import argparse
import getpass
import grp
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import pwd
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ETC = Path('/etc/proxy-manager')
DATA = Path('/var/lib/proxy-manager')
APP = Path('/opt/proxy-manager')
RUNTIME = Path('/opt/proxy-web')
XRAY_ETC = Path('/etc/xray')
LETSENCRYPT = Path('/etc/letsencrypt')
CERTS = XRAY_ETC/'tls'
ROOT = Path(__file__).resolve().parents[1]
LEGACY_DATA = Path('/var/lib/proxy-sub')
LEGACY_ADMIN = Path('/root/proxy-deployment')
DOMAIN_ADMIN = Path('/var/lib/proxy-manager-admin')

# XHTTP rides Cloudflare's HTTP/2 connections, so one TLS session carries many proxied
# streams. HTTP/2 has no per-connection path to fall back on; a small local router sends
# each request to the XHTTP inbound, the WebSocket inbound or the panel.
ROUTER_VERSION = '2.11.9'
ROUTER_ASSETS = {
    'x86_64': ('amd64', '5b8cb7faf8726a2028ad96a66e54e6e81e8f361bc6175e5d22bbd997cc5177fb'),
    'aarch64': ('arm64', '5e377cff994fc9362a6fcf695c53f226cfca3a3ef85bd3a3687cf252d6310165'),
}
ROUTER_BINARY = Path('/opt/proxy-router/caddy')
ROUTER_SERVICE = 'proxy-router.service'
PANEL_PORT, WS_PORT, XHTTP_PORT, ROUTER_PORT = 8080, 10001, 10002, 10003


def run(*args, **kwargs):
    return subprocess.run(list(args), check=True, **kwargs)


def write(path, value, mode=0o644, group=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.new')
    temporary.write_text(value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
    temporary.chmod(mode)
    if group:
        os.chown(temporary, 0, grp.getgrnam(group).gr_gid)
    temporary.replace(path)


def read(path, fallback=None):
    path = Path(path)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else fallback


def unit(name, body):
    write('/etc/systemd/system/'+name, body)


def prompt(label):
    try:
        with open('/dev/tty', 'r+') as tty:
            tty.write(label)
            tty.flush()
            return tty.readline().strip()
    except OSError as exc:
        raise SystemExit('没有交互终端。请使用 --domain 和 --password-file 参数。') from exc


def detect_ip():
    for address in ('https://api.ipify.org', 'https://ipv4.icanhazip.com'):
        try:
            with urlopen(address, timeout=10) as response:
                value = response.read(128).decode().strip()
            if ipaddress.ip_address(value).version == 4:
                return value
        except Exception:
            continue
    return prompt('服务器公网 IPv4：')


def detect_country(ip):
    """Return the ISO 3166-1 alpha-2 country of the server's public IPv4, or None."""
    # The first two look up the recorded origin IP itself, so an IPv6 or tunnelled
    # egress route cannot change the answer; Cloudflare's trace is the last resort.
    sources = ((f'https://ipinfo.io/{ip}/country', r'\s*([A-Z]{2})\s*'),
               (f'https://api.country.is/{ip}', r'.*"country"\s*:\s*"([A-Z]{2})".*'),
               ('https://www.cloudflare.com/cdn-cgi/trace', r'.*^loc=([A-Z]{2})$.*'))
    for _ in range(2):
        for address, pattern in sources:
            try:
                request = Request(address, headers={'User-Agent': 'proxysub-installer', 'Accept': '*/*'})
                with urlopen(request, timeout=10) as response:
                    text = response.read(4096).decode('utf-8', 'replace')
                match = re.fullmatch(pattern, text, re.S | re.M)
                if match and match.group(1) != 'XX':
                    return match.group(1)
            except Exception:
                continue
    return None


def xray_dns_config():
    nameservers = []
    try:
        lines = Path('/etc/resolv.conf').read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        fields = line.split()
        if len(fields) < 2 or fields[0] != 'nameserver':
            continue
        try:
            nameservers.append(str(ipaddress.ip_address(fields[1])))
        except ValueError:
            continue

    ipv4_only = False
    if shutil.which('ip'):
        try:
            route = subprocess.run(['ip', '-6', 'route', 'show', 'default'],
                                   capture_output=True, text=True, timeout=5)
            ipv4_only = route.returncode == 0 and not route.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    servers = list(dict.fromkeys([*nameservers, '1.1.1.1', '8.8.8.8']))
    strategy = 'UseIPv4' if ipv4_only else 'UseIP'
    return {'servers': servers, 'queryStrategy': strategy, 'tag': 'system-dns'}, strategy


def tune_tcp():
    run('modprobe', 'tcp_bbr')
    run('modprobe', 'sch_fq')
    settings = {'net.core.default_qdisc': 'fq', 'net.ipv4.tcp_congestion_control': 'bbr'}
    previous = {key: Path('/proc/sys', *key.split('.')).read_text().strip() for key in settings}
    try:
        run('sysctl', '-w', *(f'{key}={value}' for key, value in settings.items()))
    except subprocess.CalledProcessError:
        run('sysctl', '-w', *(f'{key}={value}' for key, value in previous.items()))
        raise
    write('/etc/modules-load.d/90-proxysub-bbr.conf', 'tcp_bbr\nsch_fq\n')
    write('/etc/sysctl.d/90-proxysub-bbr.conf', ''.join(f'{key} = {value}\n' for key, value in settings.items()))


def prepare(args):
    for user in ('xray', 'proxy-sub'):
        try:
            pwd.getpwnam(user)
        except KeyError:
            run('useradd', '--system', '--user-group', '--no-create-home', '--shell', '/usr/sbin/nologin', user)
    config = read(ETC / 'config.json', {})
    legacy = read(LEGACY_DATA/'settings.json', {})
    previous_panel = read(LEGACY_ADMIN/'panel-admin.json', {})
    domain = (args.domain or config.get('domain') or urlsplit(previous_panel.get('url', '')).hostname or '').lower().rstrip('.')
    if not domain:
        domain = prompt('管理域名（例如 proxy.example.com）：').lower().rstrip('.')
    if len(domain) > 253 or '.' not in domain or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', x) for x in domain.split('.')):
        raise SystemExit('请输入有效的完整域名，不要包含协议、路径或端口。')
    origin = args.origin_ip or config.get('origin_ip') or urlsplit(legacy.get('uri', '').strip()).hostname or detect_ip()
    try:
        address = ipaddress.ip_address(origin)
        if address.version != 4 or not address.is_global:
            raise ValueError()
    except ValueError:
        raise SystemExit('公网 IPv4 地址不正确。')
    node_password = config.get('node_password') or urlsplit(legacy.get('uri', '').strip()).username
    if not node_password:
        old_xray = read(XRAY_ETC/'config.json', {})
        node_password = next((c.get('password') for i in old_xray.get('inbounds', []) if i.get('protocol')=='trojan'
                              for c in i.get('settings', {}).get('clients', []) if c.get('password')), None)
    node_password = node_password or secrets.token_urlsafe(32)
    config.update(domain=domain, origin_ip=origin, node_password=node_password,
                  ws_path=config.get('ws_path', '/connect'), data_dir=str(DATA),
                  acme_root='/var/lib/proxy-acme', node_binary='/opt/proxy-node/bin/node')
    # Node names carry the server's country code. It is detected once per origin IP,
    # so names stay stable across upgrades; editing "country" by hand overrides it.
    country = str(config.get('country') or '').upper() if config.get('country_ip') == origin else ''
    if not re.fullmatch(r'[A-Z]{2,3}', country):
        country = detect_country(origin)
    if country:
        config.update(country=country, country_ip=origin)
        print('服务器所在国家/地区：'+country)
    else:
        config.pop('country', None)
        config.pop('country_ip', None)
        print('未能识别服务器所在国家/地区，节点名称暂不带国家缩写，下次更新时会重试。')
    if not re.fullmatch(r'/[a-zA-Z0-9/_-]{1,60}', config['ws_path']):
        raise SystemExit('WebSocket 路径格式不正确。')
    ETC.mkdir(mode=0o750, parents=True, exist_ok=True)
    ETC.chmod(0o750)
    os.chown(ETC, 0, grp.getgrnam('proxy-sub').gr_gid)
    write(ETC / 'config.json', config, 0o640, 'proxy-sub')
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    owner = pwd.getpwnam('proxy-sub')
    old_db = LEGACY_DATA/'panel.sqlite3'
    migrating = not (DATA/'panel.sqlite3').exists() and old_db.exists()
    if migrating:
        run('systemctl', 'stop', 'proxy-subscription.service')
        with sqlite3.connect(old_db) as source, sqlite3.connect(DATA/'panel.sqlite3') as destination:
            source.backup(destination)
    fresh = not (DATA/'panel.sqlite3').exists()
    password = None
    if args.password_file:
        password = Path(args.password_file).read_text(encoding='utf-8').rstrip('\r\n')
    elif fresh:
        password = getpass.getpass('设置管理密码（至少 8 位，直接回车则随机生成）：')
        if not password:
            password = secrets.token_urlsafe(18)
            print('随机管理密码：'+password)
    if password is not None and not 8 <= len(password) <= 128:
        raise SystemExit('管理密码需要 8–128 个字符。')
    with sqlite3.connect(DATA/'panel.sqlite3') as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,csrf TEXT NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS subscriptions(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,
            path TEXT UNIQUE NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,transport TEXT NOT NULL DEFAULT 'ws',
            created INTEGER NOT NULL,hits INTEGER NOT NULL DEFAULT 0,last_access INTEGER);
        ''')
        if password is not None:
            salt = secrets.token_hex(16)
            hashed = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 310000).hex()
            for key, value in [('admin_salt', salt), ('admin_hash', hashed)]:
                conn.execute('INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
            conn.execute('DELETE FROM sessions')
        if not conn.execute('SELECT 1 FROM subscriptions LIMIT 1').fetchone():
            conn.execute('INSERT INTO subscriptions(name,path,created) VALUES (?,?,?)', ('我的订阅', '/subscribe/main', int(time.time())))
        conn.execute("UPDATE subscriptions SET transport='ws'")
        if migrating:
            conn.execute("UPDATE subscriptions SET name='迁移订阅链接' WHERE name='原 IP 订阅'")
    os.chown(DATA, owner.pw_uid, owner.pw_gid)
    DATA.chmod(0o700)
    for path in DATA.glob('panel.sqlite3*'):
        os.chown(path, owner.pw_uid, owner.pw_gid)
        path.chmod(0o600)
    # Refresh internal template on every installation, retaining the saved endpoints.
    sys.path.insert(0, str(APP/'app'))
    os.environ['PROXY_CONFIG'] = str(ETC/'config.json')
    import server
    import generator
    with server.db() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS generator_state(id INTEGER PRIMARY KEY CHECK(id=1),settings TEXT NOT NULL,result TEXT NOT NULL,updated INTEGER NOT NULL)')
    snapshot = generator.get_snapshot(server.db)
    options = snapshot['settings'] if snapshot else generator.defaults(config)
    generator.save(options, config, server.db)
    for path in DATA.glob('panel.sqlite3*'):
        os.chown(path, owner.pw_uid, owner.pw_gid)
        path.chmod(0o600)
    for name in ('/var/lib/proxy-acme', '/var/lib/proxy-acme/.well-known', '/var/lib/proxy-acme/.well-known/acme-challenge'):
        Path(name).mkdir(mode=0o755, parents=True, exist_ok=True)
        Path(name).chmod(0o755)
    unit('proxy-subscription.service', f'''[Unit]
Description=ProxySub management, subscriptions and ACME
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=proxy-sub
Group=proxy-sub
WorkingDirectory={APP}
Environment=PROXY_CONFIG={ETC}/config.json
ExecStart={RUNTIME}/bin/python {APP}/app/server.py
Restart=always
RestartSec=5
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths={DATA}
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
MemoryMax=384M
TasksMax=64
UMask=0077

[Install]
WantedBy=multi-user.target
''')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'proxy-subscription.service')
    run('systemctl', 'restart', 'proxy-subscription.service')
    print('Management prepared for '+domain)


def xhttp_identity(config):
    """XHTTP path and VLESS id. Both follow the saved settings, so links survive upgrades."""
    path = config.get('xhttp_path', '/link')
    if not re.fullmatch(r'/[a-zA-Z0-9_-]{1,60}', path) or path == config.get('ws_path'):
        raise ValueError('XHTTP 路径格式不正确。')
    return {'path': path, 'id': str(uuid.uuid5(uuid.NAMESPACE_URL, 'proxysub-xhttp:'+config['node_password']))}


def install_router():
    asset = ROUTER_ASSETS.get(platform.machine())
    if not asset:
        raise ValueError('XHTTP 路由仅支持 Linux x86_64 和 ARM64。')
    arch, digest = asset
    marker = ROUTER_BINARY.with_name('archive.sha256')
    if ROUTER_BINARY.is_file() and marker.is_file() and marker.read_text().strip() == digest:
        return
    ROUTER_BINARY.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    ROUTER_BINARY.parent.chmod(0o755)
    with tempfile.TemporaryDirectory(dir=ROUTER_BINARY.parent) as temporary:
        archive = Path(temporary)/'caddy.tar.gz'
        run('curl', '-fLsS', '--retry', '2', '--connect-timeout', '15', '--max-time', '180',
            f'https://github.com/caddyserver/caddy/releases/download/v{ROUTER_VERSION}/'
            f'caddy_{ROUTER_VERSION}_linux_{arch}.tar.gz', '-o', str(archive))
        if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
            raise ValueError('XHTTP 路由组件 SHA256 验证失败。')
        candidate = Path(temporary)/'caddy'
        with tarfile.open(archive) as bundle, bundle.extractfile('caddy') as source:
            candidate.write_bytes(source.read())
        candidate.chmod(0o755)
        candidate.replace(ROUTER_BINARY)
    write(marker, digest+'\n')


def router_config(config, tunnel):
    return f"""{{
	admin off
	auto_https off
	persist_config off
	log {{
		level WARN
	}}
	servers {{
		protocols h1 h2c
	}}
}}
http://:{ROUTER_PORT} {{
	bind 127.0.0.1
	@tunnel path {tunnel['path']} {tunnel['path']}/*
	handle @tunnel {{
		reverse_proxy h2c://127.0.0.1:{XHTTP_PORT} {{
			flush_interval -1
		}}
	}}
	@websocket path {config['ws_path']}
	handle @websocket {{
		reverse_proxy 127.0.0.1:{WS_PORT}
	}}
	handle {{
		reverse_proxy 127.0.0.1:{PANEL_PORT}
	}}
}}
"""


def start_router(config):
    """Install and start the request router; returns the XHTTP settings it serves."""
    tunnel = xhttp_identity(config)
    install_router()
    path = XRAY_ETC/'proxysub-router.caddyfile'
    write(path, router_config(config, tunnel), 0o640, 'xray')
    with tempfile.TemporaryDirectory() as home:
        run(str(ROUTER_BINARY), 'validate', '--config', str(path), '--adapter', 'caddyfile',
            env=dict(os.environ, HOME=home, XDG_DATA_HOME=home, XDG_CONFIG_HOME=home),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    unit(ROUTER_SERVICE, f"""[Unit]
Description=ProxySub HTTP/2 request router for XHTTP, WebSocket and the panel
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=xray
Group=xray
StateDirectory=proxy-router
Environment=HOME=/var/lib/proxy-router XDG_DATA_HOME=/var/lib/proxy-router XDG_CONFIG_HOME=/var/lib/proxy-router
ExecStart={ROUTER_BINARY} run --config {path} --adapter caddyfile
Restart=always
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LimitNOFILE=65536
MemoryMax=384M
TasksMax=256
UMask=0027

[Install]
WantedBy=multi-user.target
""")
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', ROUTER_SERVICE)
    run('systemctl', 'restart', ROUTER_SERVICE)
    return tunnel


def stop_router():
    try:
        run('systemctl', 'disable', '--now', ROUTER_SERVICE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        pass  # Never installed on this server.


def router_status(path, method='GET', host='localhost'):
    with socket.create_connection(('127.0.0.1', ROUTER_PORT), timeout=5) as conn:
        conn.sendall(f'{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n'.encode())
        head = conn.recv(64).split(b' ')
    return int(head[1]) if len(head) > 1 and head[1].isdigit() else 0


def router_healthy(config, tunnel):
    """The panel and the XHTTP inbound both have to answer through the router."""
    deadline = time.monotonic()+15
    while True:
        try:
            # An idle XHTTP inbound rejects this bare request itself; 502-504 mean it is unreachable.
            if (router_status('/', 'HEAD', config['domain']) == 200
                    and router_status(tunnel['path']+'/health') not in (0, 502, 503, 504)):
                return True
        except OSError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(.5)


def xray_config(config, certificates, tunnel):
    clients = [{'password':config['node_password'], 'email':'owner'}]
    dns, strategy = xray_dns_config()
    # With the router every request is routed by its own path, so Cloudflare may reuse and
    # multiplex origin connections. The WebSocket path keeps its direct route for upgrades.
    front = ROUTER_PORT if tunnel else PANEL_PORT
    inbounds = [
        {'tag':'trojan-tls','listen':'0.0.0.0','port':443,'protocol':'trojan',
         'settings':{'clients':clients,'fallbacks':[{'dest':f'127.0.0.1:{front}','xver':0},
             {'path':config['ws_path'],'dest':f'127.0.0.1:{WS_PORT}','xver':0}]},
         'streamSettings':{'network':'tcp','security':'tls','tlsSettings':{'minVersion':'1.2',
             'alpn':['h2','http/1.1'] if tunnel else ['http/1.1'],
             'rejectUnknownSni':bool(config.get('retired_domains')), 'certificates':certificates}}},
        {'tag':'trojan-ws','listen':'127.0.0.1','port':WS_PORT,'protocol':'trojan','settings':{'clients':clients},
         'streamSettings':{'network':'ws','security':'none','wsSettings':{'path':config['ws_path'],'heartbeatPeriod':30}}}]
    if tunnel:
        inbounds.append(
            {'tag':'vless-xhttp','listen':'127.0.0.1','port':XHTTP_PORT,'protocol':'vless',
             'settings':{'clients':[{'id':tunnel['id'],'email':'owner'}],'decryption':'none'},
             'streamSettings':{'network':'xhttp','security':'none',
                               'xhttpSettings':{'path':tunnel['path'],'mode':'auto'}}})
    return {'log':{'loglevel':'warning','access':'none'}, 'dns': dns, 'inbounds':inbounds,
        'outbounds':[{'tag':'direct','protocol':'freedom','streamSettings':{'sockopt':{'domainStrategy':strategy}}},
                     {'tag':'block','protocol':'blackhole'}],
        'routing':{'domainStrategy':'IPIfNonMatch','rules':[
            {'type':'field','inboundTag':['system-dns'],'ip':dns['servers'],'port':'53','outboundTag':'direct'},
            {'type':'field','ip':['geoip:private'],'outboundTag':'block'}]}}


def install_xray_config(xray):
    candidate = ETC/'xray-candidate.json'
    write(candidate, xray, 0o640, 'xray')
    # The service user must be able to read the final location; validation here is root.
    run('/opt/xray/xray', 'run', '-test', '-config', str(candidate))
    write(XRAY_ETC/'config.json', xray, 0o640, 'xray')
    candidate.unlink()


def activate():
    config = read(ETC/'config.json')
    domain = config['domain']
    xray_gid = grp.getgrnam('xray').gr_gid
    XRAY_ETC.mkdir(mode=0o750, exist_ok=True)
    XRAY_ETC.chmod(0o750)
    os.chown(XRAY_ETC, 0, xray_gid)
    domain_cert = CERTS/'domain'
    domain_cert.mkdir(mode=0o750, parents=True, exist_ok=True)
    CERTS.chmod(0o750)
    domain_cert.chmod(0o750)
    os.chown(CERTS, 0, xray_gid)
    os.chown(domain_cert, 0, xray_gid)
    for name in ('fullchain.pem', 'privkey.pem'):
        source = LETSENCRYPT/'live'/domain/name
        write(domain_cert/name, source.read_text(), 0o640, 'xray')
    certificates = [{'certificateFile':str(domain_cert/'fullchain.pem'), 'keyFile':str(domain_cert/'privkey.pem')}]
    if not config.get('retired_domains') and (CERTS/'fullchain.pem').exists() and (CERTS/'privkey.pem').exists():
        certificates.append({'certificateFile':str(CERTS/'fullchain.pem'),'keyFile':str(CERTS/'privkey.pem')})
    # XHTTP is an addition: any failure here leaves the WebSocket nodes exactly as before.
    try:
        tunnel = start_router(config)
    except Exception as error:
        print('XHTTP 长连接入口未启用，继续使用 WebSocket 节点：'+(str(error) or type(error).__name__), flush=True)
        stop_router()
        tunnel = None
    install_xray_config(xray_config(config, certificates, tunnel))
    unit('xray.service', '''[Unit]
Description=ProxySub Xray TLS and WebSocket service
After=network-online.target proxy-subscription.service
Wants=network-online.target proxy-subscription.service
StartLimitIntervalSec=0

[Service]
Type=simple
User=xray
Group=xray
WorkingDirectory=/opt/xray
Environment=XRAY_LOCATION_ASSET=/opt/xray
ExecStart=/opt/xray/xray run -config /etc/xray/config.json
Restart=always
RestartSec=5
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LimitNOFILE=65536
MemoryMax=512M
TasksMax=256
UMask=0027

[Install]
WantedBy=multi-user.target
''')
    unit('proxy-cert-renew.service', '''[Unit]
Description=Renew ProxySub TLS certificates
After=network-online.target proxy-subscription.service
Wants=network-online.target proxy-subscription.service

[Service]
Type=oneshot
ExecStart=/opt/certbot/bin/certbot renew --quiet --no-random-sleep-on-renew
TimeoutStartSec=15min
''')
    unit('proxy-cert-renew.timer', '''[Unit]
Description=Check ProxySub certificates twice daily

[Timer]
OnCalendar=*-*-* 00,12:00:00
RandomizedDelaySec=30m
Persistent=true
Unit=proxy-cert-renew.service

[Install]
WantedBy=timers.target
''')
    hook = LETSENCRYPT/'renewal-hooks/deploy/20-xray-cert'
    write(hook, '#!/usr/bin/env bash\nset -euo pipefail\nexec /opt/proxy-web/bin/python /opt/proxy-manager/deploy/renew_cert.py\n', 0o750)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'xray.service', 'proxy-cert-renew.timer')
    run('systemctl', 'restart', 'xray.service')
    if tunnel and not router_healthy(config, tunnel):
        print('XHTTP 长连接入口未通过自检，已恢复为仅 WebSocket 节点。', flush=True)
        stop_router()
        tunnel = None
        install_xray_config(xray_config(config, certificates, None))
        run('systemctl', 'restart', 'xray.service')
    # The subscription service offers XHTTP nodes only while this file exists.
    if tunnel:
        write(ETC/'xhttp.json', tunnel, 0o640, 'proxy-sub')
    else:
        (ETC/'xhttp.json').unlink(missing_ok=True)
    run('runuser', '-u', 'xray', '--', '/opt/xray/xray', 'run', '-test', '-config', str(XRAY_ETC/'config.json'))
    run('systemctl', 'is-active', 'xray.service', 'proxy-subscription.service', 'proxy-cert-renew.timer')
    setup_domain_worker()
    print('Ready: https://'+domain)


def setup_domain_worker():
    DOMAIN_ADMIN.mkdir(parents=True, exist_ok=True)
    DOMAIN_ADMIN.chmod(0o750)
    os.chown(DOMAIN_ADMIN, 0, grp.getgrnam('proxy-sub').gr_gid)
    unit('proxy-domain-change.service', '''[Unit]
Description=ProxySub validated domain change worker and recovery
After=network-online.target proxy-subscription.service
Wants=network-online.target proxy-subscription.service
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=oneshot
User=root
ExecStart=/opt/proxy-web/bin/python /opt/proxy-manager/deploy/change_domain.py
TimeoutStartSec=6min
Restart=on-failure
RestartSec=5
PrivateTmp=true
NoNewPrivileges=true
UMask=0077

[Install]
WantedBy=multi-user.target
''')
    unit('proxy-domain-change.path', '''[Unit]
Description=Watch authenticated ProxySub domain change requests

[Path]
PathExists=/var/lib/proxy-manager/domain-request.json
Unit=proxy-domain-change.service

[Install]
WantedBy=multi-user.target
''')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', 'proxy-domain-change.service')
    run('systemctl', 'enable', '--now', 'proxy-domain-change.path')


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare','activate','domain','tune-tcp'])
    parser.add_argument('--domain')
    parser.add_argument('--origin-ip')
    parser.add_argument('--password-file')
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args)
    elif args.action == 'activate':
        activate()
    elif args.action == 'tune-tcp':
        tune_tcp()
    else:
        print(read(ETC/'config.json')['domain'])
