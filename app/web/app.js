'use strict';
const $=id=>document.getElementById(id);
const PLATFORMS=[
  {key:'auto',name:'通用订阅',description:'按客户端自动识别',icon:'auto'},
  {key:'raw',name:'V2rayN / v2rayNG',description:'Windows · Android',icon:'v2rayn'},
  {key:'shadowrocket',name:'Shadowrocket',description:'iPhone · iPad',icon:'shadowrocket',target:'raw'},
  {key:'clash',name:'Clash / Mihomo',description:'Clash Verge 等客户端',icon:'clash'},
  {key:'surge',name:'Surge',description:'macOS · iOS',icon:'surge'},
];
let state,csrf='',selectedId,editing,dirty=false,toastTimer,domainJob,domainTimer,pendingDomain='';
const text=(tag,cls,value)=>{const el=document.createElement(tag);if(cls)el.className=cls;if(value!==undefined)el.textContent=value;return el;};
function notice(message){clearTimeout(toastTimer);$('toast').textContent=message;$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,3500);}
function errorAt(id,error){$(id).textContent=error?.message||'';$(id).hidden=!error;}
function showLogin(){state=undefined;csrf='';dirty=false;$('appShell').hidden=true;$('loginScreen').hidden=false;document.querySelectorAll('dialog[open]').forEach(d=>d.close());}
async function api(path,method='GET',body){
  const response=await fetch(path,{method,credentials:'same-origin',cache:'no-store',headers:method==='GET'?{}:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:body===undefined?undefined:JSON.stringify(body)});
  const data=await response.json();
  if(!response.ok){if(response.status===401&&path!=='/api/login')showLogin();throw new Error(data.error||'请求失败，请重试。');}
  return data;
}
async function busy(button,fn){button.disabled=true;try{await fn();}catch(error){notice(error.message);}finally{button.disabled=false;}}
async function copy(value){if(!value)return;await navigator.clipboard.writeText(value);notice('订阅链接已复制');}
function selected(){return state?.subscriptions.find(r=>String(r.id)===String(selectedId)&&r.enabled);}
function urlFor(row,target='auto'){return row.url+(target!=='auto'?'?target='+target:'');}
function qrFor(row,target='auto'){return '/api/subscriptions/'+row.id+'/qr?target='+target;}
function date(value){return value?new Date(value*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}):'尚未更新';}
function markDirty(){dirty=$('preferredIps').value.trim()!==state?.generator.preferredIps.trim()||$('nodeLimit').valueAsNumber!==(state?.generator.nodeLimit||0);$('editStatus').textContent=dirty?'有未保存修改':'已保存';const list=$('preferredIps').value.split(/[\n,;]+/).map(s=>s.trim()).filter(Boolean);$('lineCount').textContent=list.length+' 行地址';}
function renderOutput(){
  const row=selected();
  if(row){$('mainQr').src=qrFor(row);$('mainQr').hidden=false;$('mainUrl').value=row.url;}
  else{$('mainQr').removeAttribute('src');$('mainQr').hidden=true;$('mainUrl').value='请先创建或启用一个订阅链接';}
  $('copyMain').disabled=!row;
  const fragment=document.createDocumentFragment();
  for(const platform of PLATFORMS){
    const target=platform.target||platform.key;
    const available=!!row&&(target==='auto'||state.generator.availableFormats.includes(target));
    const card=text('article','platform-card');
    const head=text('div','client-head');const icon=text('img');icon.src='/assets/icons/'+platform.icon+'.svg';icon.alt='';const labels=text('div');labels.append(text('h3','',platform.name),text('p','',platform.description));head.append(icon,labels);
    const qr=text('img','card-qr');qr.alt=platform.name+' 订阅二维码';qr.width=132;qr.height=132;
    const input=text('input');input.readOnly=true;input.setAttribute('aria-label',platform.name+' 订阅链接');
    const button=text('button','button secondary','复制订阅链接');button.type='button';button.disabled=!available;
    if(available){input.value=urlFor(row,target);qr.src=qrFor(row,target);button.addEventListener('click',()=>busy(button,()=>copy(input.value)));}
    else{input.value='此格式暂不可用';qr.hidden=true;}
    card.append(head,qr,input,button);fragment.append(card);
  }
  $('platformCards').replaceChildren(fragment);
}
function renderPreview(){
  const generator=state.generator;
  $('nodeCount').textContent=generator.counts.outputNodes;
  $('endpointCount').textContent=generator.counts.preferredEndpoints;
  $('resultCount').textContent=generator.counts.outputNodes+' 个节点';
  $('previewCount').textContent='预览前 '+Math.min(generator.preview.length,generator.counts.outputNodes)+' / '+generator.counts.outputNodes+' 个';
  const fragment=document.createDocumentFragment();
  for(const node of generator.preview){const tr=text('tr');const name=text('td','node-name',node.name);const host=text('td');host.append(text('code','',node.server));tr.append(name,host,text('td','',node.port),text('td','',node.type.toUpperCase()+' · '+node.network.toUpperCase()));fragment.append(tr);}
  $('previewRows').replaceChildren(fragment);
  $('formatWarning').hidden=!generator.warnings.length;$('formatWarning').textContent=generator.warnings.join('\n');
}
function action(label,fn,cls=''){const button=text('button',cls,label);button.type='button';button.addEventListener('click',()=>busy(button,fn));return button;}
function renderLinks(){
  const enabled=state.subscriptions.filter(r=>r.enabled);
  if(!enabled.some(r=>String(r.id)===String(selectedId)))selectedId=(enabled.find(r=>r.path.startsWith('/subscribe/'))||enabled[0])?.id;
  const options=document.createDocumentFragment();
  for(const row of enabled){const option=text('option','',row.name);option.value=row.id;options.append(option);}
  $('subscriptionSelect').replaceChildren(options);$('subscriptionSelect').value=selectedId||'';
  $('subscriptionSelect').disabled=!enabled.length;
  const fragment=document.createDocumentFragment();
  for(const row of state.subscriptions){
    const tr=text('tr');const name=text('td');name.append(text('div','link-name',row.name),text('div','link-path',row.path));
    const operations=text('td');const buttons=text('div','row-actions');
    buttons.append(action('复制',()=>copy(row.url)),action('编辑',()=>openLink(row)),action(row.enabled?'停用':'启用',async()=>{await api('/api/subscriptions/'+row.id,'PATCH',{enabled:!row.enabled});await reload();notice(row.enabled?'链接已停用':'链接已启用');}),action('删除',()=>openDelete(row),'delete'));
    operations.append(buttons);tr.append(name,text('td','',date(row.last_access)),text('td',row.enabled?'enabled':'disabled',row.enabled?'使用中':'已停用'),operations);fragment.append(tr);
  }
  $('linkRows').replaceChildren(fragment);$('linkCount').textContent=state.subscriptions.length+' 个链接';renderOutput();
}
async function reload(){state=await api('/api/state');csrf=state.csrf;$('loginScreen').hidden=true;$('appShell').hidden=false;$('domainBadge').textContent=state.domain;$('versionText').textContent='v'+state.version;$('preferredIps').value=state.generator.preferredIps;$('nodeLimit').value=state.generator.nodeLimit||0;markDirty();renderDomain(state.domainChange);renderPreview();renderLinks();$('saveStatus').textContent='上次保存：'+date(state.generator.updatedAt)+'。在客户端更新订阅，即可获取最新节点。';}
function openLink(row){editing=row||null;$('linkTitle').textContent=row?'编辑订阅链接':'自定义订阅链接';$('linkName').value=row?.name||'';$('linkSlug').value=row?.path.startsWith('/subscribe/')?row.path.slice(11):'';errorAt('linkError',null);$('linkDialog').showModal();}
function openDelete(row){$('deleteMessage').textContent='删除「'+row.name+'」后，这个地址将无法再更新订阅。此操作无法恢复。';$('deleteDialog').showModal();$('confirmDelete').onclick=()=>busy($('confirmDelete'),async()=>{await api('/api/subscriptions/'+row.id,'DELETE',{});$('deleteDialog').close();await reload();notice('链接已删除');});}
$('loginForm').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{errorAt('loginError',null);try{const value=await api('/api/login','POST',{password:$('loginPassword').value});csrf=value.csrf;$('loginPassword').value='';await reload();}catch(error){errorAt('loginError',error);}});});
$('preferredIps').addEventListener('input',markDirty);
$('nodeLimit').addEventListener('input',markDirty);
$('preferredForm').addEventListener('submit',event=>{event.preventDefault();busy($('saveButton'),async()=>{errorAt('saveError',null);try{await api('/api/generate','POST',{preferredIps:$('preferredIps').value,nodeLimit:$('nodeLimit').valueAsNumber});await reload();notice('已保存，所有订阅链接立即更新');}catch(error){errorAt('saveError',error);}});});
$('restoreButton').addEventListener('click',()=>{$('preferredIps').value=state.generator.defaultPreferredIps;markDirty();notice('已填入预置地址，点击保存后生效');});
$('subscriptionSelect').addEventListener('change',()=>{selectedId=$('subscriptionSelect').value;renderOutput();});
$('copyMain').addEventListener('click',()=>busy($('copyMain'),()=>copy(selected()?.url)));
$('createRandom').addEventListener('click',()=>busy($('createRandom'),async()=>{const row=await api('/api/subscriptions','POST',{name:'新订阅 '+new Date().toLocaleDateString('zh-CN')});selectedId=row.id;await reload();notice('已生成新的订阅链接和二维码');}));
$('createCustom').addEventListener('click',()=>openLink());
$('linkForm').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{errorAt('linkError',null);const value={name:$('linkName').value.trim()};const slug=$('linkSlug').value.trim();if(slug||!editing)value.slug=slug;try{const row=await api(editing?'/api/subscriptions/'+editing.id:'/api/subscriptions',editing?'PATCH':'POST',value);selectedId=row.id;$('linkDialog').close();await reload();notice('链接已保存');}catch(error){errorAt('linkError',error);}});});
$('securityButton').addEventListener('click',()=>{$('passwordForm').reset();errorAt('passwordError',null);$('passwordDialog').showModal();});
$('passwordForm').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{try{await api('/api/password','POST',{current:$('currentPassword').value,password:$('newPassword').value});$('passwordForm').reset();showLogin();notice('密码已修改，请重新登录');}catch(error){errorAt('passwordError',error);}});});
$('logoutButton').addEventListener('click',()=>busy($('logoutButton'),async()=>{await api('/api/logout','POST',{});showLogin();}));
$('cancelDelete').addEventListener('click',()=>$('deleteDialog').close());
document.querySelectorAll('.close-dialog').forEach(button=>button.addEventListener('click',()=>button.closest('dialog').close()));
$('checkConnection').addEventListener('click',()=>busy($('checkConnection'),async()=>{const value=await api('/api/connection');$('connectionStatus').textContent=value.proxied===true?'已检测到 Cloudflare 橙云':value.proxied===false?'当前为仅 DNS 直连':'暂时无法确认，请稍后重试';}));
function renderDomain(progress){
  if(!progress||progress.status==='idle')return;
  const running=['queued','validating','certificate','switching'].includes(progress.status);
  $('domainStatus').textContent=progress.message||'正在等待域名更换任务…';
  $('domainButton').disabled=running;$('newDomain').disabled=running;$('saveButton').disabled=running;
  if(progress.status==='failed'){errorAt('domainError',new Error(progress.message));clearTimeout(domainTimer);domainJob=undefined;return;}
  if(running){domainJob=progress;const link=$('newDomainLink');link.href=progress.newUrl;link.hidden=false;clearTimeout(domainTimer);domainTimer=setTimeout(pollDomain,1500);}
}
async function pollDomain(){
  if(!domainJob)return;
  const job=domainJob;
  try{
    const response=await fetch('/api/domain',{credentials:'same-origin',cache:'no-store'});
    if(response.status===401){clearTimeout(domainTimer);domainJob=undefined;showLogin();notice('请使用原密码重新登录，查看域名更换结果');return;}
    if(response.ok){const progress=await response.json();if(progress.id===job.id){renderDomain(progress);if(progress.status==='failed')return;}}
  }catch{}
  try{
    const response=await fetch(job.newUrl+'/api/domain-result/'+job.id,{credentials:'omit',cache:'no-store',mode:'cors'});
    if(response.ok&&(await response.json()).status==='ready'){dirty=false;window.location.assign(job.newUrl);return;}
  }catch{}
  clearTimeout(domainTimer);domainTimer=setTimeout(pollDomain,2000);
}
$('domainForm').addEventListener('submit',event=>{event.preventDefault();errorAt('domainError',null);if(dirty){errorAt('domainError',new Error('请先保存优选地址和数量设置，再更换域名。'));return;}pendingDomain=$('newDomain').value.trim();$('domainConfirmMessage').textContent='将 '+state.domain+' 更换为 '+pendingDomain+'。系统会先验证解析并申请证书，失败时保留原域名。';$('domainConfirmDialog').showModal();});
$('cancelDomain').addEventListener('click',()=>$('domainConfirmDialog').close());
$('confirmDomain').addEventListener('click',()=>busy($('confirmDomain'),async()=>{try{const job=await api('/api/domain','POST',{domain:pendingDomain});$('domainConfirmDialog').close();renderDomain(job);}catch(error){$('domainConfirmDialog').close();errorAt('domainError',error);}}));
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
reload().catch(error=>{showLogin();if(error.message!=='请先登录')errorAt('loginError',error);});
