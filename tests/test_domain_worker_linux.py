"""Isolated worker transactions: real SQLite/Node/runuser, fake DNS/ACME/systemd."""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
CAN_RUN=os.name=='posix' and hasattr(os,'geteuid') and os.geteuid()==0 and Path('/opt/proxy-node/bin/node').exists()


@unittest.skipUnless(CAN_RUN,'Linux root integration test with installed runtime')
class WorkerTests(unittest.TestCase):
    @contextmanager
    def fixture(self, fail=None):
        import pwd
        sys.path.insert(0,str(ROOT/'deploy'))
        spec=importlib.util.spec_from_file_location('isolated_domain_worker',ROOT/'deploy/change_domain.py')
        worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
        with tempfile.TemporaryDirectory(prefix='proxysub-worker-test-') as folder:
            root=Path(folder);root.chmod(0o755)
            app=root/'app-source'
            shutil.copytree(ROOT/'app',app/'app',ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copytree(ROOT/'config',app/'config')
            for path in (app,*app.rglob('*')):path.chmod(0o755 if path.is_dir() else 0o644)
            data=root/'data';data.mkdir();owner=pwd.getpwnam('proxy-sub')
            etc=root/'etc';admin=root/'admin';admin.mkdir()
            with patch.multiple(worker.provision,ETC=etc,DATA=data,APP=app,LETSENCRYPT=root/'acme',CERTS=root/'tls'), \
                 patch.multiple(worker,ADMIN=admin,ACTIVE=admin/'active-request.json',TRANSACTION=admin/'transaction.json'):
                config={'domain':'old.example.com','origin_ip':'1.1.1.1','node_password':'fixture-old-node',
                        'ws_path':'/fixture-old','data_dir':str(data),'node_binary':'/opt/proxy-node/bin/node'}
                worker.provision.write(etc/'config.json',config,0o640,'proxy-sub')
                etc.chmod(0o750);os.chown(etc,0,owner.pw_gid)
                with sqlite3.connect(data/'panel.sqlite3') as db:
                    db.executescript('CREATE TABLE sessions(token_hash TEXT,csrf TEXT,expires REAL);'
                        'INSERT INTO sessions VALUES ("fixture-session","fixture-csrf",9999999999);'
                        'CREATE TABLE generator_state(id INTEGER PRIMARY KEY,settings TEXT,result TEXT,updated INTEGER);')
                sys.path.insert(0,str(ROOT/'app'))
                import generator
                @contextmanager
                def connect():
                    connection=sqlite3.connect(data/'panel.sqlite3')
                    try:
                        with connection:yield connection
                    finally:connection.close()
                generator.save({'nodeLimit':100},config,connect)
                for path in (data,*data.iterdir()):
                    os.chown(path,owner.pw_uid,owner.pw_gid);path.chmod(0o700 if path.is_dir() else 0o600)
                job={'id':'a'*32,'domain':'new.example.com','previousDomain':config['domain'],'created':int(time.time())}
                request=data/'domain-request.json';request.write_text(json.dumps(job));os.chown(request,owner.pw_uid,owner.pw_gid)
                calls=[];activations=[]
                def fake_run(*args,timeout=90):
                    calls.append(args)
                    if args[0]=='runuser':return subprocess.run(list(args),check=True,timeout=timeout)
                    if args[0]==worker.CERTBOT and args[1]=='certonly' and fail=='certificate':
                        raise subprocess.CalledProcessError(1,list(args))
                def activate():
                    activations.append(json.loads((etc/'config.json').read_text()))
                    if fail=='activation' and len(activations)==1:raise RuntimeError('fixture interrupted activation')
                with patch.object(worker,'run',side_effect=fake_run),patch.object(worker,'prove_domain'), \
                     patch.object(worker,'health'),patch.object(worker.provision,'activate',side_effect=activate),patch.object(worker.time,'sleep'):
                    yield worker,config,job,connect,calls,activations

    def test_switch_changes_identity_preserves_list_limit_and_invalidates_sessions(self):
        with self.fixture() as (worker,old,job,db,calls,activated):
            worker.process()
            current=json.loads((worker.provision.ETC/'config.json').read_text())
            self.assertEqual(current['domain'],'new.example.com')
            self.assertNotEqual(current['node_password'],old['node_password'])
            self.assertNotEqual(current['ws_path'],old['ws_path'])
            self.assertEqual(current['retired_domains'],['old.example.com'])
            with db() as conn:
                self.assertEqual(conn.execute('select count(*) from sessions').fetchone()[0],0)
                settings,result=conn.execute('select settings,result from generator_state').fetchone()
            settings=json.loads(settings);result=json.loads(result)
            self.assertEqual(settings['nodeLimit'],100)
            self.assertEqual(len(settings['preferredIps'].splitlines()),219)
            nodes=json.loads(result['formats']['json']['body'])
            self.assertEqual(len(nodes),101)
            self.assertEqual(nodes[0]['server'],'1.1.1.1')
            self.assertTrue(all(n['sni']=='new.example.com' and n['password']==current['node_password'] for n in nodes))
            self.assertEqual(json.loads((worker.ADMIN/'status.json').read_text())['status'],'succeeded')
            self.assertFalse(worker.TRANSACTION.exists())

    def test_certificate_failure_keeps_old_identity_and_sessions(self):
        with self.fixture('certificate') as (worker,old,job,db,calls,activated):
            worker.process()
            self.assertEqual(json.loads((worker.provision.ETC/'config.json').read_text()),old)
            self.assertFalse(activated)
            with db() as conn:self.assertEqual(conn.execute('select count(*) from sessions').fetchone()[0],1)
            self.assertEqual(json.loads((worker.ADMIN/'status.json').read_text())['status'],'failed')

    def test_failed_activation_restores_previous_domain_and_cache(self):
        with self.fixture('activation') as (worker,old,job,db,calls,activated):
            worker.process()
            self.assertEqual(json.loads((worker.provision.ETC/'config.json').read_text()),old)
            self.assertEqual(len(activated),2)
            with db() as conn:result=json.loads(conn.execute('select result from generator_state').fetchone()[0])
            self.assertTrue(all(n['sni']==old['domain'] and n['password']==old['node_password'] for n in json.loads(result['formats']['json']['body'])))

    def test_recovery_after_interruption_rolls_back_uncommitted_change(self):
        with self.fixture() as (worker,old,job,db,calls,activated):
            (worker.provision.DATA/'domain-request.json').unlink()
            worker.provision.write(worker.TRANSACTION,{'job':job,'previousConfig':old,'phase':'switching'})
            worker.provision.write(worker.provision.ETC/'config.json',worker.new_config(old,job['domain']),0o640,'proxy-sub')
            worker.process()
            self.assertEqual(json.loads((worker.provision.ETC/'config.json').read_text()),old)
            self.assertEqual(json.loads((worker.ADMIN/'status.json').read_text())['status'],'failed')

    def test_committed_recovery_never_reactivates_retired_domain(self):
        with self.fixture() as (worker,old,job,db,calls,activated):
            (worker.provision.DATA/'domain-request.json').unlink()
            worker.provision.write(worker.TRANSACTION,{'job':job,'previousConfig':old,'phase':'committed'})
            updated=worker.new_config(old,job['domain'])
            worker.provision.write(worker.provision.ETC/'config.json',updated,0o640,'proxy-sub')
            worker.process()
            self.assertEqual(json.loads((worker.provision.ETC/'config.json').read_text()),updated)
            self.assertFalse(activated)
            self.assertEqual(json.loads((worker.ADMIN/'status.json').read_text())['status'],'succeeded')
