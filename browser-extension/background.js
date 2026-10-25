importScripts('model.js');
const M = ProxyModel, ALLOW = 100, CONTROL = 101;
let nodes = [], active = null, subscription = '', reconnect = false, initializing, serial = Promise.resolve(), changing = false, epoch = 0;
let state = { phase: 'locked', message: '网页已锁定。', selected: '', exitIp: '', results: {} };
const authAttempts = new Map();
const publicState = () => ({ ...state, nodes: nodes.map(({ id, name }) => ({ id, name })), subscriptionConfigured: !!subscription });
const enqueue = task => { const result = serial.then(task, task); serial = result.catch(() => {}); return result; };
async function badge(connected) {
  await chrome.action.setBadgeText({ text: connected ? 'ON' : '锁' });
  await chrome.action.setBadgeBackgroundColor({ color: connected ? '#176b61' : '#a74438' });
}
async function lock(message = '已断开：全部网页联网已阻止。') {
  await chrome.declarativeNetRequest.updateDynamicRules({ removeRuleIds: [ALLOW] });
  state.phase = 'locked'; state.message = message; state.exitIp = '';
  await badge(false);
}
async function setProxy(value) {
  changing = true;
  try {
    await chrome.proxy.settings.set({ value, scope: 'regular' });
    const actual = await chrome.proxy.settings.get({ incognito: false });
    if (actual.levelOfControl !== 'controlled_by_this_extension' || actual.value.mode !== value.mode) throw new Error('代理被其他插件或组织策略控制，网页保持锁定。');
    if (value.mode === 'fixed_servers') {
      const a = actual.value.rules?.singleProxy, b = value.rules.singleProxy;
      if (a?.host !== b.host || a?.port !== b.port || a?.scheme !== b.scheme || !actual.value.rules.bypassList?.includes('<-loopback>')) throw new Error('实际代理参数与预期不一致，网页保持锁定。');
    }
  } finally { changing = false; }
}
async function privacy() {
  await chrome.privacy.network.webRTCIPHandlingPolicy.set({ value: 'disable_non_proxied_udp', scope: 'regular' });
  await chrome.privacy.network.networkPredictionEnabled.set({ value: false, scope: 'regular' });
  const rtc = await chrome.privacy.network.webRTCIPHandlingPolicy.get({ incognito: false });
  if (rtc.value !== 'disable_non_proxied_udp' || rtc.levelOfControl !== 'controlled_by_this_extension') throw new Error('无法应用 WebRTC 保护，网页保持锁定。');
}
async function probe(timeout = 15000) {
  const start = performance.now();
  const response = await fetch('https://api.ipify.org?format=json&nonce=' + crypto.randomUUID(), { cache: 'no-store', credentials: 'omit', signal: AbortSignal.timeout(timeout) });
  if (!response.ok) throw new Error('无法完成代理出口检测。');
  const { ip } = await response.json();
  if (typeof ip !== 'string' || !/^[0-9a-f:.]{3,45}$/i.test(ip)) throw new Error('出口检测响应不合法。');
  return { ip, ms: Math.round(performance.now() - start) };
}
async function connect(id) {
  const operation = epoch;
  await lock('正在连接并验证节点，网页暂时锁定…'); state.phase = 'checking';
  try {
    const node = nodes.find(n => n.id === id); if (!node) throw new Error('请选择一个有效节点。');
    await privacy(); active = node; authAttempts.clear(); await setProxy(M.config(node));
    const result = await probe();
    if (operation !== epoch) throw new Error('用户已请求断开。');
    // main_frame must be explicit: DNR's omitted resourceTypes excludes it.
    await chrome.declarativeNetRequest.updateDynamicRules({ removeRuleIds: [ALLOW], addRules: [{ id: ALLOW, priority: 2, action: { type: 'allow' }, condition: { regexFilter: '^(https?|wss?)://', resourceTypes: M.types } }] });
    state.phase = 'connected'; state.selected = id; state.exitIp = result.ip; state.message = '所有国内、国外网页均经过所选新加坡代理。';
    state.results[id] = { ok: true, ms: result.ms }; reconnect = true;
    await chrome.storage.local.set({ selected: id, reconnect }); await badge(true);
  } catch (error) {
    active = null; await lock('连接失败，网页保持锁定：' + error.message); await setProxy(M.dead).catch(() => {}); throw error;
  }
}
async function disconnect() {
  await lock(); active = null; reconnect = false; authAttempts.clear();
  await chrome.storage.local.set({ reconnect: false }); await setProxy(M.dead).catch(() => {});
}
async function update(value) {
  const operation = epoch;
  const url = M.subscriptionUrl(value || subscription);
  const previous = state.phase === 'connected' ? state.selected : '';
  await lock('正在更新订阅，网页暂时锁定…');
  try {
    // Bootstrap control-plane request only: static DNR continues blocking every
    // webpage. If connected, updates use the existing HTTPS proxy instead.
    if (!previous) { active = null; await setProxy({ mode: 'direct' }); }
    const response = await fetch(url, { cache: 'no-store', credentials: 'omit', redirect: 'error', signal: AbortSignal.timeout(20000) });
    if (!response.ok) throw new Error('订阅下载失败（HTTP ' + response.status + '）。');
    const data = await response.text(); if (data.length > 1048576) throw new Error('订阅响应过大。');
    const imported = M.parse(JSON.parse(data));
    await chrome.storage.local.set({ nodes: imported, subscription: value || subscription });
    nodes = imported; subscription = value || subscription; state.selected = nodes.some(n => n.id === state.selected) ? state.selected : nodes[0].id;
    state.message = '订阅已更新：' + nodes.length + ' 个浏览器兼容节点。';
  } finally {
    active = null; await setProxy(M.dead).catch(() => {});
    if (previous && operation === epoch && nodes.some(n => n.id === previous)) await connect(previous);
  }
}
async function test(ids) {
  const operation = epoch;
  const previous = state.phase === 'connected' ? state.selected : '';
  await lock('正在测速，网页暂时锁定，完成后恢复原连接…');
  try {
    await privacy();
    for (const node of nodes.filter(n => ids.includes(n.id))) {
      if (operation !== epoch) break;
      try { active = node; authAttempts.clear(); await setProxy(M.config(node)); const r = await probe(10000); state.results[node.id] = { ok: true, ms: r.ms }; }
      catch { state.results[node.id] = { ok: false, ms: 0 }; }
    }
    state.message = '测速完成。这里显示 HTTPS 请求耗时，不是下载带宽。';
  } finally {
    active = null; await setProxy(M.dead).catch(() => {});
    if (previous && operation === epoch) await connect(previous);
  }
}
chrome.webRequest.onAuthRequired.addListener((details, callback) => {
  const matches = active && details.isProxy && details.challenger?.host?.toLowerCase() === active.host && details.challenger.port === active.port;
  if (!matches) { callback({}); return; }
  const attempts = (authAttempts.get(details.requestId) || 0) + 1; authAttempts.set(details.requestId, attempts);
  callback(attempts <= 2 ? { authCredentials: { username: active.username, password: active.password } } : { cancel: true });
}, { urls: ['<all_urls>'] }, ['asyncBlocking']);
const clearAuth = details => authAttempts.delete(details.requestId);
chrome.webRequest.onCompleted.addListener(clearAuth, { urls: ['<all_urls>'] });
chrome.webRequest.onErrorOccurred.addListener(clearAuth, { urls: ['<all_urls>'] });
async function initialize() {
  const operation = epoch;
  await lock('正在初始化，网页保持锁定…');
  await chrome.declarativeNetRequest.updateDynamicRules({ removeRuleIds: [CONTROL], addRules: [{ id: CONTROL, priority: 10, action: { type: 'allow' }, condition: { initiatorDomains: [chrome.runtime.id], resourceTypes: ['xmlhttprequest'], regexFilter: '^https://' } }] });
  await privacy(); await setProxy(M.dead);
  const saved = await chrome.storage.local.get(['nodes', 'subscription', 'selected', 'reconnect']);
  if (saved.nodes?.length) nodes = M.parse({ version: 1, nodes: saved.nodes });
  subscription = saved.subscription || ''; state.selected = saved.selected || nodes[0]?.id || ''; reconnect = !!saved.reconnect;
  if (reconnect && state.selected && operation === epoch) await connect(state.selected);
  else state.message = nodes.length ? '选择节点并连接后，网页才允许联网。' : '请在订阅设置中粘贴现有订阅链接。';
}
const ready = () => initializing ||= initialize().catch(error => lock(error.message));
chrome.runtime.onInstalled.addListener(() => { void ready(); });
chrome.runtime.onStartup.addListener(() => { void ready(); });
chrome.proxy.settings.onChange.addListener(details => {
  if (!changing && state.phase === 'connected' && details.levelOfControl !== 'controlled_by_this_extension') void lock('代理控制权发生变化，网页已锁定。');
});
chrome.proxy.onProxyError.addListener(() => { if (state.phase === 'connected') void lock('浏览器报告代理故障，网页已锁定。请重新连接。'); });
chrome.privacy.network.webRTCIPHandlingPolicy.onChange.addListener(details => { if (state.phase === 'connected' && details.value !== 'disable_non_proxied_udp') void lock('WebRTC 保护发生变化，网页已锁定。'); });
chrome.alarms.create('health', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === 'health' && state.phase === 'connected') void enqueue(async () => {
    try { const result = await probe(10000); state.exitIp = result.ip; } catch { await lock('代理健康检查失败，网页已锁定。'); }
  });
});
chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.id !== chrome.runtime.id) return false;
  // A user lock takes effect immediately, even if a speed test is still running.
  if (message.command === 'disconnect') { epoch++; void lock('正在断开，网页已锁定…'); }
  enqueue(async () => {
    await ready();
    switch (message.command) {
      case 'status': break;
      case 'connect': await connect(message.id); break;
      case 'disconnect': await disconnect(); break;
      case 'saveSubscription': await update(message.url); break;
      case 'updateSubscription': await update(); break;
      case 'test': if (!Array.isArray(message.ids)) throw new Error('请选择节点。'); await test(message.ids); break;
      default: throw new Error('未知操作。');
    }
    return publicState();
  }).then(value => respond({ ok: true, state: value })).catch(error => respond({ ok: false, error: error.message, state: publicState() }));
  return true;
});
void ready();
