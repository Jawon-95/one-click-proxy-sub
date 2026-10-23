"""Fresh + repeated provisioning against isolated paths, without touching units."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
CAN_RUN=os.name=='posix' and hasattr(os,'geteuid') and os.geteuid()==0 and Path('/opt/proxy-node/bin/node').exists()


@unittest.skipUnless(CAN_RUN,'Linux root integration test; needs installed Node runtime')
class ProvisionTests(unittest.TestCase):
    def test_fresh_install_and_repeat_keep_state(self):
        spec=importlib.util.spec_from_file_location('isolated_provision',ROOT/'deploy/provision.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='proxysub-provision-test-') as folder:
            root=Path(folder)
            module.ETC=root/'etc';module.DATA=root/'data';module.APP=ROOT
            module.LEGACY_DATA=root/'no-legacy-data';module.LEGACY_ADMIN=root/'no-legacy-admin'
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
