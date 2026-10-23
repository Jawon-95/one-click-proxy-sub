"""Compile on save, serve cached formats from SQLite without Node per request."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from urllib.parse import quote, urlencode

LOCK = threading.Lock()
ROOT = Path(__file__).resolve().parent


def defaults(config):
    domain = config['domain']
    query = urlencode({'security': 'tls', 'type': 'ws', 'host': domain, 'sni': domain,
                       'path': config.get('ws_path', '/connect'), 'allowInsecure': '0'})
    node = f'trojan://{quote(config["node_password"], safe="")}@{domain}:443?{query}#My-Proxy'
    preset = (ROOT.parent / 'config/preferred-endpoints.txt').read_text(encoding='utf-8').strip()
    return {'nodeLinks': node, 'preferredIps': preset, 'namePrefix': '',
            'keepOriginalHost': True, 'includeOriginal': True}


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
        options['preferredIps'] = str(settings.get('preferredIps', options['preferredIps']))
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
    return {'preferredIps': snapshot['settings']['preferredIps'], 'updatedAt': snapshot['updatedAt'], 'counts': result['counts'],
            'preview': result['preview'], 'warnings': result['warnings'],
            'availableFormats': list(result['formats']), 'defaultPreferredIps': defaults(config)['preferredIps']}


def render(snapshot, target, subscription_url):
    output = snapshot['result']['formats'].get(target)
    if not output:
        raise ValueError('该基础节点暂不支持此客户端格式。')
    return output['body'].replace('__SUBSCRIPTION_URL__', subscription_url+'?target=surge').encode('utf-8'), output['contentType']
