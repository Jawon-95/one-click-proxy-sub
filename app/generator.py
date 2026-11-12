"""Compile on save, serve cached formats from SQLite without Node per request."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from urllib.parse import quote, urlencode, urlsplit

LOCK = threading.Lock()
ROOT = Path(__file__).resolve().parent
# Client side of the XHTTP nodes: a small pool of long-lived HTTP/2 connections to the
# Cloudflare entry. Opening a site reuses one of them instead of paying a TCP + TLS +
# WebSocket handshake each time; several connections keep one lost packet from stalling
# every stream. packet-up needs no Cloudflare setting, and its many small uploads are why
# the per-connection request budget is raised.
XHTTP_EXTRA = {'xmux': {'maxConnections': 8, 'hMaxRequestTimes': '4000-6000', 'hMaxReusableSecs': '1800-3000'}}


def defaults(config):
    domain = config['domain']
    query = urlencode({'security': 'tls', 'type': 'ws', 'host': domain, 'sni': domain,
                       'path': config.get('ws_path', '/connect'), 'allowInsecure': '0'})
    node = f'trojan://{quote(config["node_password"], safe="")}@{domain}:443?{query}#My-Proxy'
    preset = (ROOT.parent / 'config/preferred-endpoints.txt').read_text(encoding='utf-8').strip()
    return {'nodeLinks': node, 'preferredIps': preset, 'namePrefix': '', 'directIp': config.get('origin_ip', ''),
            'country': config.get('country', ''),
            'keepOriginalHost': True, 'includeOriginal': True, 'nodeLimit': 0}


def compile_settings(settings, config):
    node = config.get('node_binary') or os.environ.get('PROXY_NODE') or shutil.which('node')
    if not node:
        raise ValueError('Node.js 不可用，请重新执行安装脚本修复依赖。')
    try:
        result = subprocess.run([node, str(ROOT / 'convert.mjs')], input=json.dumps(settings),
                                text=True, encoding='utf-8', capture_output=True, timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError('订阅转换失败，请检查运行环境或减少输入。') from exc
    if result.returncode:
        raise ValueError(result.stderr.strip()[:2000] or '订阅转换失败')
    return json.loads(result.stdout)


def get_snapshot(db):
    with db() as conn:
        row = conn.execute('SELECT settings,result,updated FROM generator_state WHERE id=1').fetchone()
    if not row:
        return None
    return {'settings': json.loads(row[0]), 'result': json.loads(row[1]), 'updatedAt': row[2]}


def save(settings, config, db):
    with LOCK:
        # The server owns credentials/Host/SNI/path; the panel edits endpoints only.
        options = defaults(config)
        previous = get_snapshot(db)
        retained = previous['settings'] if previous else options
        options['preferredIps'] = str(settings.get('preferredIps', retained['preferredIps']))
        options['nodeLimit'] = settings.get('nodeLimit', retained.get('nodeLimit', 0))
        result = compile_settings(options, config)
        stamp = int(time.time())
        with db() as conn:
            conn.execute('INSERT INTO generator_state(id,settings,result,updated) VALUES (1,?,?,?) '
                         'ON CONFLICT(id) DO UPDATE SET settings=excluded.settings,result=excluded.result,updated=excluded.updated',
                         (json.dumps(result['settings'], ensure_ascii=False),
                          json.dumps(result, ensure_ascii=False), stamp))
        return {'settings': result['settings'], 'result': result, 'updatedAt': stamp}


def initialize(config, db):
    with db() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS generator_state('
                     'id INTEGER PRIMARY KEY CHECK(id=1),settings TEXT NOT NULL,result TEXT NOT NULL,updated INTEGER NOT NULL)')
    return get_snapshot(db) or save(defaults(config), config, db)


def public_state(snapshot, config):
    result = snapshot['result']
    return {'preferredIps': snapshot['settings']['preferredIps'], 'nodeLimit': snapshot['settings'].get('nodeLimit', 0), 'updatedAt': snapshot['updatedAt'], 'counts': result['counts'],
            'preview': result['preview'], 'warnings': result['warnings'],
            'availableFormats': list(result['formats']), 'defaultPreferredIps': defaults(config)['preferredIps']}


def xhttp_nodes(nodes, config, config_path):
    """Rewrite the Cloudflare entries as VLESS + XHTTP; other nodes keep their transport."""
    try:
        tunnel = json.loads(config_path.with_name('xhttp.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return nodes  # The server has no XHTTP inbound; WebSocket remains valid.
    domain = config['domain']
    query = urlencode({'encryption': 'none', 'security': 'tls', 'sni': domain, 'alpn': 'h2', 'fp': 'chrome',
                       'type': 'xhttp', 'host': domain, 'path': tunnel['path'], 'mode': 'packet-up',
                       'extra': json.dumps(XHTTP_EXTRA, separators=(',', ':'))})
    lines = []
    for line in nodes.decode('utf-8').splitlines(keepends=True):
        node = urlsplit(line.strip())
        if node.scheme == 'trojan' and node.hostname != config['origin_ip']:
            ending = line[len(line.rstrip()):]
            line = f'vless://{tunnel["id"]}@{node.netloc.rpartition("@")[2]}?{query}#{node.fragment}{ending}'
        lines.append(line)
    return ''.join(lines).encode('utf-8')


def render(snapshot, target, subscription_url, config, config_path, *, cdn_only=False, xhttp=False):
    output = snapshot['result']['formats'].get(target)
    if not output:
        raise ValueError('该基础节点暂不支持此客户端格式。')
    body = output['body'].replace('__SUBSCRIPTION_URL__', subscription_url+'?target=surge').encode('utf-8')
    if target not in ('raw', 'plain'):
        if cdn_only:
            raise ValueError('仅 CDN 订阅支持 v2rayN 和纯文本格式。')
        return body, output['contentType']
    if xhttp:
        nodes = xhttp_nodes(base64.b64decode(body) if target == 'raw' else body, config, config_path)
        body = base64.b64encode(nodes) if target == 'raw' else nodes
    if cdn_only:
        nodes = base64.b64decode(body) if target == 'raw' else body
        body = b''.join(line for line in nodes.splitlines(keepends=True)
                        if urlsplit(line.decode('utf-8')).hostname != config['origin_ip'])
    else:
        try:
            direct = json.loads(config_path.with_name('hysteria2.json').read_text(encoding='utf-8'))
        except FileNotFoundError:
            return body, output['contentType']
        auth = quote(direct['password'], safe='')
        params = {'sni': config['domain']}
        if direct.get('ech'):
            params['ech'] = direct['ech']
        query = urlencode(params)
        country = config.get('country', '')
        name = quote(f'000 | {country} Hysteria 2 直连' if country else '000 | Hysteria 2 直连', safe='')
        node = f'hysteria2://{auth}@{config["origin_ip"]}:{direct["port"]}/?{query}#{name}\n'.encode('utf-8')
        body = node + (base64.b64decode(body) if target == 'raw' else body)
    return (base64.b64encode(body) if target == 'raw' else body), output['contentType']
