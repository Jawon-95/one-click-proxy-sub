"""Provision/migrate a generic Linux deployment. No installation credentials in source."""
import argparse
import getpass
import grp
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

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
    browser = config.get('browser_proxy', {})
    browser_fallback = []
    if browser.get('enabled') and browser.get('fronted_by_xray'):
        browser_cert = CERTS/'browser'
        browser_cert.mkdir(mode=0o750, parents=True, exist_ok=True)
        browser_cert.chmod(0o750)
        os.chown(browser_cert, 0, xray_gid)
        for name in ('fullchain.pem', 'privkey.pem'):
            write(browser_cert/name, (LETSENCRYPT/'live'/browser['certificate_name']/name).read_text(), 0o640, 'xray')
        certificates.append({'certificateFile': str(browser_cert/'fullchain.pem'), 'keyFile': str(browser_cert/'privkey.pem')})
        browser_fallback.append({'name': browser['host'], 'dest': '127.0.0.1:'+str(browser.get('internal_port', 18080)), 'xver': 0})
    if not config.get('retired_domains') and (CERTS/'fullchain.pem').exists() and (CERTS/'privkey.pem').exists():
        certificates.append({'certificateFile':str(CERTS/'fullchain.pem'),'keyFile':str(CERTS/'privkey.pem')})
    clients = [{'password':config['node_password'], 'email':'owner'}]
    xray = {'log':{'loglevel':'warning','access':'none'}, 'inbounds':[
        {'tag':'trojan-tls','listen':'0.0.0.0','port':443,'protocol':'trojan',
         'settings':{'clients':clients,'fallbacks':browser_fallback+[{'dest':'127.0.0.1:8080','xver':0},
             {'path':config['ws_path'],'dest':'127.0.0.1:10001','xver':0}]},
         'streamSettings':{'network':'tcp','security':'tls','tlsSettings':{'minVersion':'1.2','alpn':['http/1.1'],
             'rejectUnknownSni':bool(config.get('retired_domains')), 'certificates':certificates}}},
        {'tag':'trojan-ws','listen':'127.0.0.1','port':10001,'protocol':'trojan','settings':{'clients':clients},
         'streamSettings':{'network':'ws','security':'none','wsSettings':{'path':config['ws_path'],'heartbeatPeriod':30}}}],
        'outbounds':[{'tag':'direct','protocol':'freedom'},{'tag':'block','protocol':'blackhole'}],
        'routing':{'domainStrategy':'IPIfNonMatch','rules':[{'type':'field','ip':['geoip:private'],'outboundTag':'block'}]}}
    candidate = ETC/'xray-candidate.json'
    write(candidate, xray, 0o640, 'xray')
    # The service user must be able to read the final location; validation here is root.
    run('/opt/xray/xray', 'run', '-test', '-config', str(candidate))
    write(XRAY_ETC/'config.json', xray, 0o640, 'xray')
    candidate.unlink()
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


def setup_browser_proxy(browser_host=None, browser_port=None):
    """Opt-in HTTPS proxy. Port 443 uses an SNI fallback to an independent backend."""
    config = read(ETC/'config.json')
    origin = config['origin_ip']
    options = config.get('browser_proxy', {})
    host = (browser_host or options.get('host') or origin).lower().rstrip('.')
    if host == config['domain']:
        raise ValueError('浏览器专用入口必须使用独立域名，不能与管理域名相同。')
    if host != origin:
        if len(host) > 253 or '.' not in host or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p) for p in host.split('.')):
            raise ValueError('浏览器代理域名格式不正确。')
        addresses = {answer[4][0] for answer in socket.getaddrinfo(host, 80, type=socket.SOCK_STREAM)}
        if addresses != {origin}:
            raise ValueError('浏览器代理域名必须为仅 DNS，且只解析到本机公网 IPv4。不能使用 Cloudflare 橙云。')
    port = int(browser_port or options.get('port') or (443 if host != origin else 8443))
    if (port != 443 and not 1024 <= port <= 65535) or port in (8080, 10001, 18080):
        raise ValueError('浏览器代理端口不能与现有服务冲突。')
    if port == 443 and host == origin:
        raise ValueError('共用 443 端口时需要独立的仅 DNS 域名。')
    identity = ['--ip-address', origin, '--required-profile', 'shortlived'] if host == origin else ['-d', host]
    run('/opt/certbot/bin/certbot', 'certonly', '--webroot', '-w', config['acme_root'],
        *identity, '--cert-name', host, '--agree-tos', '--register-unsafely-without-email', '--non-interactive', '--keep-until-expiring')
    destination = ETC/'browser-tls'
    destination.mkdir(mode=0o750, parents=True, exist_ok=True)
    destination.chmod(0o750)
    os.chown(destination, 0, grp.getgrnam('proxy-sub').gr_gid)
    for name in ('fullchain.pem', 'privkey.pem'):
        write(destination/name, (LETSENCRYPT/'live'/host/name).read_text(), 0o640, 'proxy-sub')
    options.update(enabled=True, host=host, certificate_name=host, port=port, fronted_by_xray=port == 443,
                   internal_port=18080, secret=options.get('secret') or secrets.token_hex(32),
                   certificate=str(destination/'fullchain.pem'), private_key=str(destination/'privkey.pem'))
    config['browser_proxy'] = options
    write(ETC/'config.json', config, 0o640, 'proxy-sub')
    unit('proxy-browser.service', f'''[Unit]
Description=ProxySub authenticated browser HTTPS proxy
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=proxy-sub
Group=proxy-sub
WorkingDirectory={APP}
Environment=PROXY_CONFIG={ETC}/config.json
ExecStart={RUNTIME}/bin/python {APP}/app/browser_proxy.py
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
# SQLite WAL readers still need shared-memory lock coordination.
# SQL access itself remains read-only in browser_proxy.py.
ReadWritePaths={config['data_dir']}
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LimitNOFILE=4096
MemoryMax=384M
TasksMax=160
UMask=0077

[Install]
WantedBy=multi-user.target
''')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'proxy-browser.service')
    run('systemctl', 'restart', 'proxy-browser.service', 'proxy-subscription.service')
    if options['fronted_by_xray']:
        # Validate candidate and service-account certificate access before restart.
        activate()
    # Only open this explicit port if a supported host firewall is active.
    if shutil.which('firewall-cmd') and subprocess.run(['firewall-cmd', '--state'], capture_output=True).returncode == 0:
        run('firewall-cmd', '--permanent', f'--add-port={port}/tcp')
        run('firewall-cmd', f'--add-port={port}/tcp')
    elif shutil.which('ufw') and 'Status: active' in subprocess.run(['ufw', 'status'], capture_output=True, text=True).stdout:
        run('ufw', 'allow', f'{port}/tcp')
    print(f'Browser HTTPS proxy ready on TCP {port}. Cloud security groups must also permit this port.')


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare','activate','domain','browser'])
    parser.add_argument('--domain')
    parser.add_argument('--origin-ip')
    parser.add_argument('--password-file')
    parser.add_argument('--browser-host')
    parser.add_argument('--browser-port', type=int)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args)
    elif args.action == 'activate':
        activate()
    elif args.action == 'browser':
        setup_browser_proxy(args.browser_host, args.browser_port)
    else:
        print(read(ETC/'config.json')['domain'])
