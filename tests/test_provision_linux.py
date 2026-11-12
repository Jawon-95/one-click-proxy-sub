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
            module.DOMAIN_ADMIN=root/'domain-admin'
            module.write(module.ETC/'config.json',{'domain':'proxy.example.com','node_password':'fixture-node','ws_path':'/connect','retired_domains':['old.example.com']})
            cert=module.LETSENCRYPT/'live/proxy.example.com'
            module.write(cert/'fullchain.pem','fixture-certificate')
            module.write(cert/'privkey.pem','fixture-private-key',0o600)
            with patch.object(module,'unit'),patch.object(module,'run'),\
                 patch.object(module,'start_router',side_effect=OSError('router unavailable')):
                previous=os.umask(0o077)
                try:module.activate()
                finally:os.umask(previous)
            # Exercise traversal and file access with the actual non-root service identity.
            subprocess.run(['runuser','-u','xray','--','python3','-c',
                            'from pathlib import Path;import sys;assert Path(sys.argv[1]).read_text()=="fixture-private-key"',
                            str(module.CERTS/'domain/privkey.pem')],check=True)
            self.assertEqual((module.CERTS/'domain/privkey.pem').stat().st_mode&0o777,0o640)
            config=json.loads((module.XRAY_ETC/'config.json').read_text())
            self.assertTrue(config['inbounds'][0]['streamSettings']['tlsSettings']['rejectUnknownSni'])
            # Without the router the server stays WebSocket-only and offers no XHTTP nodes.
            self.assertEqual(config['inbounds'][0]['streamSettings']['tlsSettings']['alpn'],['http/1.1'])
            self.assertEqual([i['tag'] for i in config['inbounds']],['trojan-tls','trojan-ws'])
            self.assertEqual(config['inbounds'][0]['settings']['fallbacks'][0]['dest'],'127.0.0.1:8080')
            self.assertFalse((module.ETC/'xhttp.json').exists())
            saved=module.read(module.ETC/'config.json')
            tunnel=module.xhttp_identity(saved)
            caddyfile=module.router_config(saved,tunnel)
            for expected in ('bind 127.0.0.1','path /link /link/*','h2c://127.0.0.1:10002','@websocket path /connect','127.0.0.1:8080'):
                self.assertIn(expected,caddyfile)
            # A healthy router publishes XHTTP; an unhealthy one is rolled back to WebSocket only.
            for healthy in (True,False):
                with patch.object(module,'unit'),patch.object(module,'run'),\
                     patch.object(module,'start_router',return_value=tunnel),\
                     patch.object(module,'router_healthy',return_value=healthy):
                    previous=os.umask(0o077)
                    try:module.activate()
                    finally:os.umask(previous)
                config=json.loads((module.XRAY_ETC/'config.json').read_text())
                self.assertEqual((module.ETC/'xhttp.json').exists(),healthy)
                self.assertEqual([i['tag'] for i in config['inbounds']],['trojan-tls','trojan-ws']+['vless-xhttp']*healthy)
                self.assertEqual(config['inbounds'][0]['streamSettings']['tlsSettings']['alpn'],['h2','http/1.1'] if healthy else ['http/1.1'])
                self.assertEqual(config['inbounds'][0]['settings']['fallbacks'],
                                 [{'dest':'127.0.0.1:10003' if healthy else '127.0.0.1:8080','xver':0},
                                  {'path':'/connect','dest':'127.0.0.1:10001','xver':0}])
                if healthy:
                    self.assertEqual(json.loads((module.ETC/'xhttp.json').read_text()),tunnel)
                    self.assertEqual(config['inbounds'][2]['settings']['clients'][0]['id'],tunnel['id'])
                    self.assertEqual(config['inbounds'][2]['streamSettings']['xhttpSettings']['path'],'/link')
                    self.assertEqual((module.ETC/'xhttp.json').stat().st_mode&0o777,0o640)

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
                with patch.object(module,'run'),patch.object(module,'write',side_effect=isolated_write),patch.dict(os.environ,{},clear=False),\
                     patch.object(module,'detect_country',return_value='SG') as detect:
                    module.prepare(args)
                    config=json.loads((root/'etc/config.json').read_text())
                    self.assertEqual((config['country'],config['country_ip']),('SG','1.1.1.1'))
                    with sqlite3.connect(root/'data/panel.sqlite3') as conn:
                        original_hash=conn.execute("select value from metadata where key='admin_hash'").fetchone()[0]
                        self.assertEqual(conn.execute('select path from subscriptions').fetchone()[0],'/subscribe/main')
                        conn.execute("insert into sessions values ('fixture-hash','fixture-csrf',9999999999)")
                    server=sys.modules['server'];generator=sys.modules['generator']
                    generator.save({'preferredIps':'104.16.1.2#Saved','nodeLimit':1},config,server.db)
                    args.password_file=None
                    module.prepare(args)
                    detect.assert_called_once_with('1.1.1.1')
                    with sqlite3.connect(root/'data/panel.sqlite3') as conn:
                        self.assertEqual(conn.execute("select value from metadata where key='admin_hash'").fetchone()[0],original_hash)
                        names=[n['name'] for n in json.loads(json.loads(conn.execute('select result from generator_state').fetchone()[0])['formats']['json']['body'])]
                        self.assertEqual(names,['001 | SG 直连','002 | SG Saved'])
                        self.assertEqual(conn.execute('select count(*) from sessions').fetchone()[0],1)
                        result=json.loads(conn.execute('select result from generator_state').fetchone()[0])
                        self.assertEqual(result['counts']['outputNodes'],2)
                        self.assertEqual(result['settings']['nodeLimit'],1)
                    unit=(root/'units/proxy-subscription.service').read_text()
                    self.assertIn('Restart=always',unit)
                    self.assertIn(str(root/'data'),unit)
            finally:
                for key,value in previous.items():
                    sys.modules.pop(key,None)
                    if value is not None:sys.modules[key]=value


if __name__=='__main__':unittest.main()
