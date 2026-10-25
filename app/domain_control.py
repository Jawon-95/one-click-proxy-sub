"""Unprivileged request/status side of the narrowly scoped domain worker."""
import json
import os
from pathlib import Path
import re
import secrets
import time

BUSY = {'queued', 'validating', 'certificate', 'switching'}


def normalize_domain(value):
    if not isinstance(value, str):
        raise ValueError('请输入完整域名，例如 proxy.example.com。')
    domain = value.strip().lower().rstrip('.')
    if len(domain) > 253 or '.' not in domain or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in domain.split('.')):
        raise ValueError('请输入有效的完整域名，不要包含协议、路径或端口。')
    if domain.endswith(('.local', '.localhost', '.internal', '.test', '.invalid')) or re.fullmatch(r'[\d.]+', domain):
        raise ValueError('请使用可公开解析的域名。')
    return domain


def status(config):
    path = Path(config.get('domain_admin_dir', '/var/lib/proxy-manager-admin'))/'status.json'
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return {key: value[key] for key in ('id', 'domain', 'previousDomain', 'status', 'message', 'updated', 'newUrl') if key in value}
    except FileNotFoundError:
        return {'status': 'idle'}


def busy(config):
    return status(config).get('status') in BUSY or (Path(config['data_dir'])/'domain-request.json').exists()


def enqueue(value, config):
    domain = normalize_domain(value)
    if domain == config['domain']:
        raise ValueError('新域名与当前域名相同。')
    if config.get('browser_proxy', {}).get('enabled') and domain == config['browser_proxy'].get('host'):
        raise ValueError('该域名正用于浏览器代理，请选择独立的管理域名。')
    if busy(config):
        raise ValueError('已有域名更换任务正在执行，请稍候。')
    request = {'id': secrets.token_hex(16), 'domain': domain, 'previousDomain': config['domain'], 'created': int(time.time())}
    path = Path(config['data_dir'])/'domain-request.json'
    # Exclusive creation prevents simultaneous admin submissions from overwriting a job.
    temporary = path.with_name('domain-request.'+request['id']+'.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as target:
            json.dump(request, target)
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ValueError('已有域名更换任务正在执行，请稍候。') from exc
    finally:
        temporary.unlink(missing_ok=True)
    return {**request, 'status': 'queued', 'newUrl': 'https://'+domain, 'message': '已提交，正在验证域名和申请证书。'}
