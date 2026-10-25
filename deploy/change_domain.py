"""Root-only systemd worker. Accepts one validated domain, never shell commands."""
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import socket
import ssl
import stat
import subprocess
import sys
import time
import urllib.request

import provision

sys.path.insert(0, str(provision.APP/'app'))
from domain_control import normalize_domain

ADMIN = Path('/var/lib/proxy-manager-admin')
ACTIVE = ADMIN/'active-request.json'
TRANSACTION = ADMIN/'transaction.json'
CERTBOT = '/opt/certbot/bin/certbot'
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def run(*args, timeout=90):
    return subprocess.run(list(args), check=True, timeout=timeout)


def set_status(job, state, message):
    provision.write(ADMIN/'status.json', {key: job[key] for key in ('id', 'domain', 'previousDomain')} |
                    {'status': state, 'message': message, 'newUrl': 'https://'+job['domain'], 'updated': int(time.time())},
                    0o640, 'proxy-sub')


def cache(config_path, *flags):
    run('runuser', '-u', 'proxy-sub', '--', 'env', 'PROXY_CONFIG='+str(config_path),
        '/opt/proxy-web/bin/python', str(provision.APP/'app/domain_cache.py'), *flags)


def prove_domain(domain, job):
    addresses = socket.getaddrinfo(domain, 80, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('新域名必须解析到公网地址，请检查 DNS 记录。')
    # Proves the domain actually reaches this server, with either DNS-only or Cloudflare.
    challenge = Path('/var/lib/proxy-acme/.well-known/acme-challenge')/('proxysub-'+job['id'])
    token = secrets.token_urlsafe(32)
    provision.write(challenge, token, 0o644)
    try:
        url = 'http://'+domain+'/.well-known/acme-challenge/'+challenge.name
        with HTTP.open(urllib.request.Request(url, headers={'User-Agent': 'ProxySub-domain-check'}), timeout=20) as response:
            if response.read(256).decode() != token:
                raise ValueError('新域名未指向本服务器，请检查 DNS 和 HTTP 80 端口。')
    finally:
        challenge.unlink(missing_ok=True)


def new_config(old, domain):
    retired = set(old.get('retired_domains', [])) | {old['domain']}
    retired.discard(domain)
    return old | {'domain': domain, 'node_password': secrets.token_urlsafe(32),
                  'ws_path': '/connect-'+secrets.token_hex(12), 'retired_domains': sorted(retired)}


def health(domain):
    deadline = time.monotonic()+20
    while True:
        try:
            with socket.create_connection(('127.0.0.1', 443), timeout=5) as plain:
                with ssl.create_default_context().wrap_socket(plain, server_hostname=domain) as secure:
                    secure.sendall(('HEAD / HTTP/1.1\r\nHost: '+domain+'\r\nConnection: close\r\n\r\n').encode())
                    if b' 200 ' not in secure.recv(1024).split(b'\r\n')[0]:
                        raise OSError('New domain health check failed')
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.5)


def rollback(old):
    run('systemctl', 'stop', 'proxy-subscription.service')
    provision.write(provision.ETC/'config.json', old, 0o640, 'proxy-sub')
    cache(provision.ETC/'config.json')
    run('systemctl', 'start', 'proxy-subscription.service')
    provision.activate()


def finish(job, old):
    warning = False
    # Old lineages are no longer referenced; stop renewing the retired domain.
    browser = old.get('browser_proxy', {})
    retained = browser.get('certificate_name', browser.get('host', old['origin_ip'])) if browser.get('enabled') else None
    for name in (value for value in (old['domain'], old['origin_ip']) if value != retained):
        if (provision.LETSENCRYPT/'renewal'/f'{name}.conf').exists():
            try:
                run(CERTBOT, 'delete', '--non-interactive', '--cert-name', name)
            except (subprocess.SubprocessError, OSError):
                warning = True
    for name in ('fullchain.pem', 'privkey.pem'):
        (provision.CERTS/name).unlink(missing_ok=True)
    set_status(job, 'succeeded', '域名已更换，旧域名和旧节点已停用。请在新域名使用原密码登录并重新导入订阅。'+
               ('旧证书清理未全部完成，请查看服务器日志；旧域名仍已停用。' if warning else ''))
    TRANSACTION.unlink(missing_ok=True)


def process():
    if TRANSACTION.exists():
        transaction = provision.read(TRANSACTION)
        job, old = transaction['job'], transaction['previousConfig']
        if transaction.get('phase') == 'committed':
            finish(job, old)
        else:
            rollback(old)
            set_status(job, 'failed', '上次切换意外中断，已自动恢复原域名，请重新提交。')
            TRANSACTION.unlink()
        ACTIVE.unlink(missing_ok=True)
        return
    config = provision.read(provision.ETC/'config.json')
    request = provision.DATA/'domain-request.json'
    if request.exists():
        # The request is data owned by proxy-sub, not an executable or a path to run.
        fd = os.open(request, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != pwd.getpwnam('proxy-sub').pw_uid or info.st_size > 4096:
                raise ValueError('Invalid request file')
            with os.fdopen(fd, 'r', encoding='utf-8', closefd=False) as stream:
                job = json.load(stream)
            provision.write(ACTIVE, job, 0o600)
        finally:
            os.close(fd)
            request.unlink(missing_ok=True)
    elif ACTIVE.exists():
        job = provision.read(ACTIVE)
        completed = provision.read(ADMIN/'status.json', {})
        if completed.get('id') == job.get('id') and completed.get('status') in ('succeeded', 'failed'):
            ACTIVE.unlink()
            return
    else:
        return
    candidate = provision.ETC/'domain-candidate.json'
    try:
        domain = normalize_domain(job['domain'])
        if domain != job['domain'] or not isinstance(job.get('id'), str) or not re.fullmatch('[a-f0-9]{32}', job['id']):
            raise ValueError('Invalid request')
        if job['previousDomain'] != config['domain'] or domain == config['domain'] or not 0 <= time.time()-job['created'] <= 3600:
            raise ValueError('域名任务已过期或配置发生变化，请重新提交。')
        set_status(job, 'validating', '正在验证新域名是否指向本服务器。')
        prove_domain(domain, job)
        set_status(job, 'certificate', '域名验证成功，正在申请 HTTPS 证书，通常需要几十秒。')
        run(CERTBOT, 'certonly', '--webroot', '-w', '/var/lib/proxy-acme', '-d', domain, '--cert-name', domain,
            '--agree-tos', '--register-unsafely-without-email', '--non-interactive', '--keep-until-expiring', timeout=240)
        updated = new_config(config, domain)
        provision.write(candidate, updated, 0o640, 'proxy-sub')
        cache(candidate, '--check')
        transaction = {'job': job, 'previousConfig': config, 'phase': 'switching'}
        provision.write(TRANSACTION, transaction, 0o600)
        set_status(job, 'switching', '证书就绪，正在切换服务；稍后自动打开新域名。')
        time.sleep(3)  # Lets the open panel observe the transition before its old host is disabled.
        run('systemctl', 'stop', 'proxy-subscription.service')
        provision.write(provision.ETC/'config.json', updated, 0o640, 'proxy-sub')
        cache(provision.ETC/'config.json', '--clear-sessions')
        run('systemctl', 'start', 'proxy-subscription.service')
        provision.activate()
        health(domain)
        transaction['phase'] = 'committed'
        provision.write(TRANSACTION, transaction, 0o600)
        finish(job, config)
    except Exception as error:
        print('Domain change failed:', type(error).__name__, flush=True)
        if TRANSACTION.exists():
            transaction = provision.read(TRANSACTION)
            if transaction.get('phase') == 'committed':
                raise  # Retry cleanup, never resurrect the old domain after a successful cutover.
            rollback(transaction['previousConfig'])
            TRANSACTION.unlink()
        message = str(error) if isinstance(error, ValueError) else '请检查新域名 DNS、HTTP 80 端口和证书申请日志；原域名仍可使用。'
        set_status(job, 'failed', '更换失败：'+message)
    finally:
        candidate.unlink(missing_ok=True)
        ACTIVE.unlink(missing_ok=True)


if __name__ == '__main__':
    os.umask(0o077)
    with (ADMIN/'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        process()
