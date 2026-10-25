"""Certbot deploy hook shared by fresh installs and migrated IP certificates."""
import grp
import json
import os
from pathlib import Path
import subprocess

config = json.loads(Path('/etc/proxy-manager/config.json').read_text())
lineage = Path(os.environ.get('RENEWED_LINEAGE', '/nonexistent')).resolve()
domain_lineage = (Path('/etc/letsencrypt/live')/config['domain']).resolve()
ip_lineage = (Path('/etc/letsencrypt/live')/config['origin_ip']).resolve()
browser = config.get('browser_proxy', {})
browser_lineage = (Path('/etc/letsencrypt/live')/browser.get('certificate_name', browser.get('host', config['origin_ip']))).resolve()
service = 'xray.service'
if lineage == browser_lineage and browser.get('enabled'):
    destination = Path('/etc/xray/tls/browser') if browser.get('fronted_by_xray') else Path(browser['certificate']).parent
    group_name = 'xray' if browser.get('fronted_by_xray') else 'proxy-sub'
    service = 'xray.service' if browser.get('fronted_by_xray') else 'proxy-browser.service'
elif lineage == domain_lineage:
    destination = Path('/etc/xray/tls/domain')
    group_name = 'xray'
elif lineage == ip_lineage and Path('/etc/xray/tls/fullchain.pem').exists():
    destination = Path('/etc/xray/tls')
    group_name = 'xray'
else:
    raise SystemExit(0)
group = grp.getgrnam(group_name).gr_gid
for name in ('fullchain.pem','privkey.pem'):
    candidate = destination/(name+'.new')
    candidate.write_bytes((lineage/name).read_bytes())
    candidate.chmod(0o640)
    os.chown(candidate, 0, group)
    candidate.replace(destination/name)
if service == 'xray.service':
    subprocess.run(['/opt/xray/xray','run','-test','-config','/etc/xray/config.json'],check=True)
subprocess.run(['systemctl','try-restart',service],check=True)
