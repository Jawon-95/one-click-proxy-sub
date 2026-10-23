"""Run the real HTTP handler against a disposable SQLite database."""
import base64
import hashlib
from http.cookies import SimpleCookie
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        root=Path(cls.tmp.name)
        cfg={'domain':'proxy.example.com','origin_ip':'203.0.113.10','node_password':'fixture-proxy-secret',
             'ws_path':'/connect','data_dir':str(root),'acme_root':str(root/'acme')}
        path=root/'config.json';path.write_text(json.dumps(cfg),encoding='utf-8')
        os.environ['PROXY_CONFIG']=str(path)
        import server
        cls.server=server
        with server.db() as conn:
            conn.executescript('''CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE sessions(token_hash TEXT PRIMARY KEY,csrf TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE subscriptions(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,path TEXT UNIQUE NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,transport TEXT NOT NULL DEFAULT 'ws',created INTEGER NOT NULL,hits INTEGER NOT NULL DEFAULT 0,last_access INTEGER);''')
            salt='11'*16
            conn.executemany('INSERT INTO metadata VALUES (?,?)',[('admin_salt',salt),('admin_hash',server.password_hash('fixture-admin-password',salt))])
            conn.execute("INSERT INTO subscriptions(name,path,created) VALUES ('Fixture','/subscribe/main',0)")
        server.generator.initialize(cfg,server.db)
        cls.http=server.Server(('127.0.0.1',0),server.Handler)
        os.environ['PROXY_PRIVATE_PORT']=str(cls.http.server_port)
        cls.base='http://127.0.0.1:'+str(cls.http.server_port)
        cls.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True);cls.thread.start()
        cls.cookie='';cls.csrf=''

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown();cls.http.server_close();cls.thread.join(5);cls.tmp.cleanup()

    def request(self,path,method='GET',body=None,status=200,headers=None):
        h={'Content-Type':'application/json','Origin':'https://proxy.example.com','Cookie':self.cookie,'X-CSRF-Token':self.csrf}
        h.update(headers or {})
        request=urllib.request.Request(self.base+path,method=method,headers=h,data=json.dumps(body).encode() if body is not None else None)
        try:response=self.opener.open(request,timeout=15)
        except urllib.error.HTTPError as error:response=error
        data=response.read()
        self.assertEqual(response.code,status,data[:200])
        return response,json.loads(data) if 'application/json' in response.headers.get('Content-Type','') else data

    def login(self):
        response,value=self.request('/api/login','POST',{'password':'fixture-admin-password'})
        cookie=SimpleCookie(response.headers['Set-Cookie'])['proxy_session']
        self.assertEqual(cookie['max-age'],str(30*86400))
        self.assertTrue(cookie['secure']);self.assertTrue(cookie['httponly'])
        self.cookie='proxy_session='+cookie.value;self.csrf=value['csrf']

    def test_complete_workflow(self):
        self.request('/api/state',status=401)
        self.login()
        _,state=self.request('/api/state')
        self.assertEqual(state['generator']['counts']['outputNodes'],220)
        self.assertNotIn('nodeLinks',state['generator'])
        self.assertNotIn('node_password',state)
        _,data=self.request('/subscribe/main?target=raw')
        self.assertEqual(len(base64.b64decode(data).decode().splitlines()),220)
        self.request('/api/generate','POST',{'preferredIps':'104.16.1.2'},status=403,headers={'X-CSRF-Token':'wrong'})
        self.request('/api/generate','POST',{'preferredIps':'104.16.1.2'},status=403,headers={'Origin':'https://attacker.example'})
        self.request('/api/generate','POST',{'preferredIps':'invalid host'},status=400)
        _,still_valid=self.request('/subscribe/main?target=raw')
        self.assertEqual(data,still_valid)
        _,result=self.request('/api/generate','POST',{'preferredIps':'104.16.1.2#Fixture','nodeLinks':'trojan://attacker@wrong.example'})
        self.assertEqual(result['counts']['outputNodes'],2)
        _,data=self.request('/subscribe/main?target=raw')
        decoded=base64.b64decode(data).decode()
        self.assertIn('fixture-proxy-secret',decoded);self.assertNotIn('attacker',decoded)
        _,row=self.request('/api/subscriptions','POST',{'name':'Disposable'},status=201)
        _,qr=self.request('/api/subscriptions/'+str(row['id'])+'/qr?target=surge')
        self.assertIn(b'<svg',qr)
        self.request('/api/subscriptions/'+str(row['id']),'PATCH',{'enabled':False})
        self.request(row['path'],status=404)
        self.request('/api/subscriptions/'+str(row['id']),'DELETE',{})
        self.request('/api/logout','POST',{})
        self.request('/api/state',status=401)

    def test_static_traversal_and_no_base_node_form(self):
        _,html=self.request('/')
        self.assertNotIn(b'id="nodeLinks"',html)
        self.assertNotIn(b'sourceSubscriptionUrl',html)
        self.request('/assets/../server.py',status=404)


if __name__=='__main__':
    unittest.main()
