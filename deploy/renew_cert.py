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
if lineage == domain_lineage:
    destination = Path('/etc/xray/tls/domain')
elif lineage == ip_lineage and Path('/etc/xray/tls/fullchain.pem').exists():
    destination = Path('/etc/xray/tls')
else:
    raise SystemExit(0)
group = grp.getgrnam('xray').gr_gid
for name in ('fullchain.pem','privkey.pem'):
    candidate = destination/(name+'.new')
    candidate.write_bytes((lineage/name).read_bytes())
    candidate.chmod(0o640)
    os.chown(candidate, 0, group)
    candidate.replace(destination/name)
subprocess.run(['/opt/xray/xray','run','-test','-config','/etc/xray/config.json'],check=True)
subprocess.run(['systemctl','try-restart','xray.service'],check=True)
