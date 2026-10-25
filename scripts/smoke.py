"""Exercise a deployment through its public HTTPS API without exposing credentials."""
import argparse
import base64
import getpass
import http.cookiejar
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--password-file')
    args=parser.parse_args()
    base=args.url.rstrip('/')
    if not base.startswith('https://'):raise SystemExit('Use a trusted HTTPS deployment URL')
    password=Path(args.password_file).read_text(encoding='utf-8').rstrip('\r\n') if args.password_file else getpass.getpass('Panel password: ')
    jar=http.cookiejar.CookieJar()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(jar))
    csrf=''
    def request(path,method='GET',body=None,status=200,ua='deployment-verifier'):
        headers={'User-Agent':ua,'Origin':base}
        if method!='GET':headers.update({'Content-Type':'application/json','X-CSRF-Token':csrf})
        req=urllib.request.Request(base+path,method=method,headers=headers,data=json.dumps(body).encode() if body is not None else None)
        try:response=opener.open(req,timeout=30)
        except urllib.error.HTTPError as error:response=error
        data=response.read()
        assert response.code==status, f'Unexpected HTTP {response.code}, expected {status}'
        return json.loads(data) if 'application/json' in response.headers.get('Content-Type','') else data
    request('/api/state',status=401)
    csrf=request('/api/login','POST',{'password':password})['csrf']
    cookie=next(c for c in jar if c.name=='proxy_session')
    assert cookie.secure and cookie.expires>time.time()+29*86400
    state=request('/api/state')
    assert state['version']=='2.1.0' and 'nodeLinks' not in state['generator']
    first=next(r for r in state['subscriptions'] if r['enabled'])
    raw=request(first['path']+'?target=raw')
    nodes=base64.b64decode(raw).decode().splitlines()
    assert len(nodes)==state['generator']['counts']['outputNodes']
    print('Password-only login, 30-day session and',len(nodes),'nodes verified',flush=True)
    assert b'proxies:' in request(first['path']+'?target=clash')
    assert b'[Proxy]' in request(first['path']+'?target=surge')
    assert request(first['path'],ua='Clash-Verge')==request(first['path']+'?target=clash')
    for target in ('auto','raw','clash','surge'):
        qr=request(f'/api/subscriptions/{first["id"]}/qr?target={target}')
        assert ET.fromstring(qr).tag.endswith('svg')
    request('/api/generate','POST',{'preferredIps':'not a valid address'},status=400)
    assert raw==request(first['path']+'?target=raw')
    request('/api/generate','POST',{'preferredIps':state['generator']['preferredIps']})
    assert raw==request(first['path']+'?target=raw')
    print('All client formats, QR generation, atomic save and invalid-input preservation verified',flush=True)
    row=request('/api/subscriptions','POST',{'name':'__smoke_disposable__'},status=201)
    try:
        assert request(row['path']+'?target=raw')==raw
        request(f'/api/subscriptions/{row["id"]}','PATCH',{'enabled':False})
        request(row['path'],status=404)
        request(f'/api/subscriptions/{row["id"]}','PATCH',{'enabled':True})
        assert request(row['path']+'?target=raw')==raw
    finally:
        request(f'/api/subscriptions/{row["id"]}','DELETE',{})
    print('Disposable link creation, disabling, enabling and deletion verified',flush=True)
    request('/api/logout','POST',{})
    request('/api/state',status=401)
    print('DEPLOYMENT SMOKE TEST PASSED')


if __name__=='__main__':main()
