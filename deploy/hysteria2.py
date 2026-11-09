"""Opt-in Hysteria 2 listener alongside the existing TCP services."""
import argparse
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import platform
import re
import secrets
import socket
import subprocess
import tempfile
import time

import provision

VERSION = '2.13.0'
ASSETS = {
    'x86_64': ('amd64', '907ba8c9693edb104b20582681fb7dc15639d5b64a9cbb616a7b539190a86691'),
    'aarch64': ('arm64', 'a68a61a84452ca250ce0368202521965ca9cc9d801a404f1dc9008ac6cf677a7'),
}
BINARY = Path('/opt/proxy-hysteria/hysteria')
SERVER = provision.XRAY_ETC / 'proxysub-hysteria2.json'
ECH_DIR = provision.XRAY_ETC / 'hysteria2-ech'
DEFAULT_ECH_NAME = 'www.apple.com.cn'
PUBLISHED = provision.ETC / 'hysteria2.json'
SERVICE = 'proxy-hysteria2.service'
PRIVATE_NETWORKS = ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
                    '169.254.0.0/16', '172.16.0.0/12', '192.168.0.0/16', '224.0.0.0/3',
                    '::/128', '::1/128', 'fc00::/7', 'fe80::/10', 'ff00::/8')


def install_binary():
    asset = ASSETS.get(platform.machine())
    if not asset:
        raise ValueError('Hysteria 2 仅支持 Linux x86_64 和 ARM64。')
    arch, digest = asset
    if BINARY.is_file() and hashlib.sha256(BINARY.read_bytes()).hexdigest() == digest:
        return
    BINARY.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    BINARY.parent.chmod(0o755)
    with tempfile.TemporaryDirectory(dir=BINARY.parent) as temporary:
        candidate = Path(temporary) / 'hysteria'
        url = f'https://github.com/apernet/hysteria/releases/download/app/v{VERSION}/hysteria-linux-{arch}'
        provision.run('curl', '-fLsS', '--retry', '2', '--connect-timeout', '15', '--max-time', '120',
                      url, '-o', str(candidate))
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != digest:
            raise ValueError('Hysteria 2 SHA256 验证失败。')
        candidate.chmod(0o755)
        candidate.replace(BINARY)


def server_config(port, password):
    certs = provision.CERTS / 'domain'
    return {'listen': f'0.0.0.0:{port}',
            'tls': {'cert': str(certs / 'fullchain.pem'), 'key': str(certs / 'privkey.pem'), 'sniGuard': 'strict'},
            'auth': {'type': 'password', 'password': password},
            'acl': {'inline': [f'reject({network})' for network in PRIVATE_NETWORKS]}}


def is_active():
    return subprocess.run(['systemctl', 'is-active', '--quiet', SERVICE]).returncode == 0


def bound(port):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.bind(('0.0.0.0', port))
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
            return True
        return False


def prepare_ech(previous, public_name):
    if public_name is None and previous.get('ech', {}).get('keyPath'):
        key = Path(previous['ech']['keyPath'])
    else:
        name = (public_name or DEFAULT_ECH_NAME).lower()
        label = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
        if len(name) > 253 or not re.fullmatch(rf'{label}(?:\.{label})+', name):
            raise ValueError('ECH 公开域名必须是完整 DNS 域名。')
        key = ECH_DIR / f'{name}.pem'
        ECH_DIR.mkdir(exist_ok=True, mode=0o750)
        os.chown(ECH_DIR, 0, provision.grp.getgrnam('xray').gr_gid)
        ECH_DIR.chmod(0o750)
        if not key.exists():
            with tempfile.TemporaryDirectory(dir=ECH_DIR) as temporary:
                candidate = Path(temporary) / 'ech.pem'
                provision.run(str(BINARY), 'ech', '--public-name', name, '--output', str(candidate),
                              stdout=subprocess.PIPE)
                candidate.chmod(0o640)
                os.chown(candidate, 0, provision.grp.getgrnam('xray').gr_gid)
                candidate.replace(key)
    match = re.search(r'-----BEGIN ECH CONFIGS-----\s+([A-Za-z0-9+/=\s]+)-----END ECH CONFIGS-----',
                      key.read_text(encoding='ascii'))
    if not match:
        raise ValueError('ECH 密钥文件缺少公开配置。')
    return key, ''.join(match[1].split())


def enable(port, publish, public_name=None):
    if not all((provision.CERTS / 'domain' / name).is_file() for name in ('fullchain.pem', 'privkey.pem')):
        raise ValueError('请先完成 ProxySub 的域名证书安装。')
    previous = provision.read(SERVER, {})
    if bound(port) and not (is_active() and previous.get('listen') == f'0.0.0.0:{port}'):
        raise ValueError(f'UDP {port} 已被占用。')
    install_binary()
    password = previous.get('auth', {}).get('password') or secrets.token_urlsafe(32)
    current = server_config(port, password)
    key, ech = prepare_ech(previous, public_name)
    provision.run('runuser', '-u', 'xray', '--', 'test', '-r', str(key))
    current['ech'] = {'keyPath': str(key)}
    provision.unit(SERVICE, f'''[Unit]
Description=ProxySub Hysteria 2 direct listener
After=network-online.target
Wants=network-online.target

[Service]
User=xray
Group=xray
ExecStart={BINARY} server --config {SERVER}
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
Restart=on-failure
RestartSec=2s

[Install]
WantedBy=multi-user.target
''')
    provision.run('systemctl', 'daemon-reload')
    was_active = is_active()
    provision.write(SERVER, current, 0o640, 'xray')
    try:
        provision.run('systemctl', 'restart', SERVICE)
        for _ in range(30):
            if is_active() and bound(port):
                break
            time.sleep(0.2)
        else:
            raise RuntimeError(f'Hysteria 2 未能监听 UDP {port}，请检查服务日志。')
        provision.run('systemctl', 'enable', SERVICE)
        if publish:
            provision.write(PUBLISHED, {'port': port, 'password': password, 'ech': ech}, 0o640, 'proxy-sub')
        else:
            PUBLISHED.unlink(missing_ok=True)
    except Exception:
        provision.run('systemctl', 'stop', SERVICE)
        if previous:
            provision.write(SERVER, previous, 0o640, 'xray')
        if was_active:
            provision.run('systemctl', 'start', SERVICE)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('enable', 'disable', 'refresh'))
    parser.add_argument('--port', type=int, default=443)
    parser.add_argument('--ech-public-name', help=f'指定外层 SNI，默认使用现有设置或 {DEFAULT_ECH_NAME}。')
    parser.add_argument('--no-publish', action='store_true', help='启动测试入口，暂不加入订阅。')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('请用 root 或 sudo 运行。')
    if not 1 <= args.port <= 65535:
        parser.error('UDP 端口必须在 1–65535 之间。')
    os.umask(0o077)
    with (provision.ETC / 'hysteria2.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == 'enable':
            enable(args.port, not args.no_publish, args.ech_public_name)
        elif args.action == 'refresh':
            if is_active() or subprocess.run(['systemctl', 'is-enabled', '--quiet', SERVICE]).returncode == 0:
                previous = provision.read(SERVER)
                port = int(previous['listen'].rsplit(':', 1)[1])
                enable(port, PUBLISHED.exists(), args.ech_public_name)
        elif PUBLISHED.exists() or is_active():
            provision.run('systemctl', 'disable', '--now', SERVICE)
            PUBLISHED.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
