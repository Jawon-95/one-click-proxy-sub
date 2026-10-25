// Real Edge + real deployed HTTPS proxy. Uses an isolated profile only.
// Subscription stays in an ignored .local file or environment variable.
import net from 'node:net';
import http from 'node:http';
import {spawn,spawnSync} from 'node:child_process';
import {once} from 'node:events';
import {mkdir,mkdtemp,readFile,writeFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const id='lmanbccbafopgkaciihpghmdndijibda';
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const settings=process.env.PROXYSUB_BROWSER_TEST_URL?{subscription:process.env.PROXYSUB_BROWSER_TEST_URL}:JSON.parse(await readFile(path.join(root,'.local/browser-test-settings.json'),'utf8'));
const results=path.join(root,'.verification');await mkdir(results,{recursive:true});
const profile=await mkdtemp(path.join(results,'edge-extension-'));
const reserve=net.createServer();reserve.listen(0,'127.0.0.1');await once(reserve,'listening');const debugPort=reserve.address().port;await new Promise(r=>reserve.close(r));
let directRequests=0;const local=http.createServer((req,res)=>{directRequests++;res.end('UNEXPECTED DIRECT');});local.listen(0,'127.0.0.1');await once(local,'listening');const localPort=local.address().port;
const edge=spawn(process.env.EDGE_BINARY||'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',[
 '--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-quic',
 ...(process.env.PROXYSUB_TEST_RESOLVE ? ['--host-resolver-rules='+process.env.PROXYSUB_TEST_RESOLVE] : []),
 '--user-data-dir='+profile,'--remote-debugging-address=127.0.0.1','--remote-debugging-port='+debugPort,
 '--load-extension='+path.join(root,'browser-extension'),'--disable-extensions-except='+path.join(root,'browser-extension'),'about:blank'
],{windowsHide:true,stdio:['ignore','ignore','pipe']});
edge.stderr.on('data',()=>{});const clients=[];
async function cdp(target){
 const ws=new WebSocket(target.webSocketDebuggerUrl);await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true});});let next=0;const pending=new Map();
 const events = [];
 ws.addEventListener('message',event=>{const data=JSON.parse(event.data),item=pending.get(data.id);if(item){clearTimeout(item.timer);pending.delete(data.id);data.error?item.reject(Error(JSON.stringify(data.error))):item.resolve(data.result);}else if(data.method==='Network.loadingFailed')events.push({error:data.params.errorText,blocked:data.params.blockedReason});});
 const api={events,call(method,params={}){return new Promise((resolve,reject)=>{const id=++next;const timer=setTimeout(()=>{pending.delete(id);reject(Error('CDP timeout: '+method));},45000);pending.set(id,{resolve,reject,timer});ws.send(JSON.stringify({id,method,params}));});},async evaluate(expression){const r=await this.call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw Error(JSON.stringify(r.exceptionDetails));return r.result.value;},close(){ws.close();}};clients.push(api);return api;
}
async function targets(){return(await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();}
async function request(popup,command,args={}){const r=await popup.evaluate(`chrome.runtime.sendMessage(${JSON.stringify({command,...args})})`);assert.ok(r.ok,r.error);return r.state;}
async function bodyReady(page,check){for(let i=0;i<100;i++){const text=await page.evaluate('document.body?.innerText || ""');if(check(text))return text;await pause(100);}throw Error('Page content timeout');}
const passed=[];function pass(message){passed.push(message);console.log('PASS '+message);}
try{
 let all;for(let i=0;i<100;i++){try{all=await targets();if(all.some(t=>t.type==='page'))break;}catch{}await pause(100);}
 assert.ok(all?.some(t=>t.type==='page'),'Headless Edge started');await pause(1000);
 const opened=await(await fetch(`http://127.0.0.1:${debugPort}/json/new?chrome-extension://${id}/popup.html`,{method:'PUT'})).json();
 const popup=await cdp(opened);await pause(700);assert.equal(await popup.evaluate('document.title'),'新加坡专线');
 let state=await request(popup,'status');assert.equal(state.phase,'locked');
 const page=await cdp((await targets()).find(t=>t.type==='page'&&t.id!==opened.id));
 let nav=await page.call('Page.navigate',{url:`http://127.0.0.1:${localPort}/off`});assert.match(nav.errorText,/BLOCKED/);assert.equal(directRequests,0);pass('Fresh install blocks webpages before subscription/connection');
 state=await request(popup,'saveSubscription',{url:settings.subscription});assert.ok(state.nodes.length>0);assert.equal(state.phase,'locked');pass('Pure extension imports compatible HTTPS nodes with no native program');
 const worker=await cdp((await targets()).find(t=>t.type==='service_worker'&&t.url.startsWith('chrome-extension://'+id)));
 await worker.call('Network.enable');
 await worker.evaluate('globalThis.__authLog=[];chrome.webRequest.onAuthRequired.addListener(d=>{globalThis.__authLog.push({proxy:d.isProxy,host:d.challenger.host,port:d.challenger.port})},{urls:["<all_urls>"]})');
 try {state=await request(popup,'connect',{id:state.nodes[0].id});} catch(error){console.log(JSON.stringify({network:worker.events,auth:await worker.evaluate('globalThis.__authLog')}));throw error;}
 assert.equal(state.phase,'connected');pass('Authenticated TLS proxy connects and verifies remote exit');
 nav=await page.call('Page.navigate',{url:'https://api.ipify.org?format=json'});assert.ok(!nav.errorText,nav.errorText);
 const body=await bodyReady(page,text=>text.includes('"ip"'));assert.equal(JSON.parse(body).ip,state.exitIp);pass('Real webpage exit matches selected Singapore server');
 nav=await page.call('Page.navigate',{url:'https://www.gov.cn/'});assert.ok(!nav.errorText,nav.errorText);await bodyReady(page,text=>text.includes('中国政府网'));pass('Chinese website loads through the same HTTPS proxy');
 const rtc=await popup.evaluate('chrome.privacy.network.webRTCIPHandlingPolicy.get({incognito:false})');assert.equal(rtc.value,'disable_non_proxied_udp');pass('Real WebRTC restriction is active');
 state=await request(popup,'test',{ids:[state.selected]});assert.equal(state.phase,'connected');assert.equal(state.results[state.selected].ok,true);pass('Node latency test restores the selected connection');
 await popup.call('Emulation.setDeviceMetricsOverride',{width:410,height:820,deviceScaleFactor:1,mobile:false});
 await popup.evaluate('send("status")');
 const shot=await popup.call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});await writeFile(path.join(results,'popup.png'),Buffer.from(shot.data,'base64'));
 if(process.argv.includes('--fault')){
   console.log('READY_FOR_SERVER_STOP');process.stdin.resume();await once(process.stdin,'data');process.stdin.pause();
   nav=await page.call('Page.navigate',{url:`http://127.0.0.1:${localPort}/server-off`});
   assert.ok(nav.errorText,'Stopped proxy must fail');assert.equal(directRequests,0);pass('Stopped real proxy server causes failure with no direct loopback request');
   await pause(500);state=await request(popup,'status');assert.equal(state.phase,'locked');pass('Browser proxy error automatically locks webpages');
 }
 await request(popup,'disconnect');nav=await page.call('Page.navigate',{url:`http://127.0.0.1:${localPort}/off-again`});assert.match(nav.errorText,/BLOCKED/);assert.equal(directRequests,0);pass('Explicit disconnect blocks webpages without direct fallback');
 await writeFile(path.join(results,'browser-report.json'),JSON.stringify({passed,exitIp:state.exitIp||'verified before disconnect',isolatedProfile:true,noNativePrograms:true,testDnsOverride:!!process.env.PROXYSUB_TEST_RESOLVE},null,2));
}finally{for(const c of clients)c.close();local.close();if(edge.exitCode===null)spawnSync('taskkill.exe',['/PID',String(edge.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'});}
