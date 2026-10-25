let latest;
const el = id => document.getElementById(id);
function render(state) {
  latest = state; const selected = el('nodes').value || state.selected;
  el('nodes').replaceChildren(...state.nodes.map(node => {
    const option = document.createElement('option'); option.value = node.id;
    const result = state.results[node.id]; option.textContent = node.name + (result ? result.ok ? ` · ${result.ms} ms` : ' · 超时或失败' : '');
    option.selected = node.id === selected; return option;
  }));
  if (!el('nodes').value && state.nodes[0]) el('nodes').value = state.nodes[0].id;
  el('ip').textContent = state.exitIp || '尚未连接'; el('message').textContent = state.message;
  el('badge').textContent = state.phase === 'connected' ? '全站代理中' : '网页已锁定';
  el('badge').classList.toggle('on', state.phase === 'connected');
}
async function send(command, args = {}) {
  document.querySelectorAll('button').forEach(b => { b.disabled = b.id !== 'disconnect'; });
  try { const result = await chrome.runtime.sendMessage({ command, ...args }); if (result.state) render(result.state); if (!result.ok) throw new Error(result.error); }
  catch (error) { el('message').textContent = error.message; }
  finally { document.querySelectorAll('button').forEach(b => { b.disabled = false; }); }
}
el('connect').onclick = () => send('connect', { id: el('nodes').value });
el('disconnect').onclick = () => send('disconnect');
el('test').onclick = () => { el('message').textContent = '正在测速；网页会暂时锁定，完成后恢复…'; return send('test', { ids: [el('nodes').value] }); };
el('testAll').onclick = () => { el('message').textContent = '正在依次测速；网页会暂时锁定…'; return send('test', { ids: latest.nodes.map(n => n.id) }); };
el('update').onclick = () => send('updateSubscription');
el('save').onclick = async () => { const url = el('subscription').value.trim(); if (url) { await send('saveSubscription', { url }); el('subscription').value = ''; } };
void send('status');
