"""Fresh + repeated provisioning against isolated paths, without touching units."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
CAN_RUN=os.name=='posix' and hasattr(os,'geteuid') and os.geteuid()==0 and Path('/opt/proxy-node/bin/node').exists()


@unittest.skipUnless(CAN_RUN,'Linux root integration test; needs installed Node runtime')
class ProvisionTests(unittest.TestCase):
    def test_fresh_certificate_tree_readable_by_service_user(self):
        spec=importlib.util.spec_from_file_location('isolated_activate',ROOT/'deploy/provision.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='proxysub-certificate-test-') as folder:
            root=Path(folder);root.chmod(0o755)
            module.ETC=root/'manager';module.XRAY_ETC=root/'xray'
            module.CERTS=module.XRAY_ETC/'tls';module.LETSENCRYPT=root/'acme'
            module.write(module.ETC/'config.json',{'domain':'proxy.example.com','node_password':'fixture-node','ws_path':'/connect'})
            cert=module.LETSENCRYPT/'live/proxy.example.com'
            module.write(cert/'fullchain.pem','fixture-certificate')
            module.write(cert/'privkey.pem','fixture-private-key',0o600)
            with patch.object(module,'unit'),patch.object(module,'run'):
                previous=os.umask(0o077)
                try:module.activate()
                finally:os.umask(previous)
            # Exercise traversal and file access with the actual non-root service identity.
            subprocess.run(['runuser','-u','xray','--','python3','-c',
                            'from pathlib import Path;import sys;assert Path(sys.argv[1]).read_text()=="fixture-private-key"',
                            str(module.CERTS/'domain/privkey.pem')],check=True)
            self.assertEqual((module.CERTS/'domain/privkey.pem').stat().st_mode&0o777,0o640)

    def test_fresh_install_and_repeat_keep_state(self):
        spec=importlib.util.spec_from_file_location('isolated_provision',ROOT/'deploy/provision.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='proxysub-provision-test-') as folder:
            root=Path(folder)
            module.ETC=root/'etc';module.DATA=root/'data';module.APP=ROOT
            module.LEGACY_DATA=root/'no-legacy-data';module.LEGACY_ADMIN=root/'no-legacy-admin'
            module.XRAY_ETC=root/'no-legacy-xray'
            password=root/'password';password.write_text('fixture-admin-password',encoding='utf-8')
            args=argparse.Namespace(domain='proxy.example.com',origin_ip='1.1.1.1',password_file=str(password))
            original_write=module.write
            def isolated_write(path,*a,**kw):
                if str(path).startswith('/etc/systemd/system/'):
                    path=root/'units'/Path(path).name
                return original_write(path,*a,**kw)
            previous={key:sys.modules.pop(key,None) for key in ('server','generator')}
            try:
                with patch.object(module,'run'),patch.object(module,'write',side_effect=isolated_write),patch.dict(os.environ,{},clear=False):
                    module.prepare(args)
                    config=json.loads((root/'etc/config.json').read_text())
                    with sqlite3.connect(root/'data/panel.sqlite3') as conn:
                        original_hash=conn.execute("select value from metadata where key='admin_hash'").fetchone()[0]
                        self.assertEqual(conn.execute('select path from subscriptions').fetchone()[0],'/subscribe/main')
                        conn.execute("insert into sessions values ('fixture-hash','fixture-csrf',9999999999)")
                    server=sys.modules['server'];generator=sys.modules['generator']
                    generator.save({'preferredIps':'104.16.1.2#Saved'},config,server.db)
                    args.password_file=None
                    module.prepare(args)
                    with sqlite3.connect(root/'data/panel.sqlite3') as conn:
                        self.assertEqual(conn.execute("select value from metadata where key='admin_hash'").fetchone()[0],original_hash)
                        self.assertEqual(conn.execute('select count(*) from sessions').fetchone()[0],1)
                        result=json.loads(conn.execute('select result from generator_state').fetchone()[0])
                        self.assertEqual(result['counts']['outputNodes'],2)
                    unit=(root/'units/proxy-subscription.service').read_text()
                    self.assertIn('Restart=always',unit)
                    self.assertIn(str(root/'data'),unit)
            finally:
                for key,value in previous.items():
                    sys.modules.pop(key,None)
                    if value is not None:sys.modules[key]=value


if __name__=='__main__':unittest.main()
