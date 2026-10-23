// Subscription conversion adapted from cf-ips-to-sub-link (MIT).
import {isIP} from 'node:net';
import {readFileSync} from 'node:fs';
import {parseNodeLinks, parsePreferredEndpoints, expandNodes, renderSubscription,
        renderNodeUri, summarizeNodes} from './vendor/cf-sub/core.mjs';

export function convert(input) {
  const source = String(input.nodeLinks || '').trim();
  const preferred = String(input.preferredIps || '').trim();
  if (source.length > 65536 || preferred.length > 131072) throw new Error('输入超过大小限制。');
  const parsed = parseNodeLinks(source);
  const endpoints = parsePreferredEndpoints(preferred);
  // Never silently drop an invalid line when saving a persistent subscription.
  if (parsed.warnings.length || endpoints.warnings.length) throw new Error([...parsed.warnings, ...endpoints.warnings].join('\n'));
  if (parsed.nodes.length > 20 || endpoints.endpoints.length > 1000) throw new Error('最多支持 20 个基础节点和 1000 个优选地址。');
  for (const ep of endpoints.endpoints) {
    const domain = ep.host.includes('.') && ep.host.length <= 253 && ep.host.split('.').every(s => /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/.test(s));
    if (!isIP(ep.host) && !domain) throw new Error(`优选地址格式不正确：${ep.host}`);
    if (/[\r\n]/.test(ep.label) || ep.label.length > 100) throw new Error('地址备注过长或包含换行。');
    if (ep.port && ![443,2053,2083,2087,2096,8443].includes(ep.port)) throw new Error(`Cloudflare HTTPS 不支持入口端口 ${ep.port}。`);
  }
  for (const node of parsed.nodes) {
    for (const field of ['name','server','hostHeader','sni','path','password','uuid','serviceName','authority']) {
      if (/[\r\n\0]/.test(String(node[field]||''))) throw new Error('基础节点包含不支持的控制字符。');
    }
    if (node.network !== 'ws' || !node.tls || node.security === 'reality') {
      throw new Error(`「${node.name}」需要使用 WebSocket + TLS，普通 TCP / REALITY 不能这样替换 Cloudflare 入口。`);
    }
  }
  const prefix = String(input.namePrefix || '').trim();
  if (prefix.length > 40 || /[\r\n]/.test(prefix)) throw new Error('备注前缀需为 40 字以内的单行文本。');
  const options = {keepOriginalHost: input.keepOriginalHost !== false, namePrefix: prefix};
  const expanded = expandNodes(parsed.nodes, endpoints.endpoints, options);
  const nodes = input.includeOriginal !== false ? [
    ...parsed.nodes.map(n=>({...n, name: n.name+' | 自动入口'})), ...expanded.nodes,
  ] : expanded.nodes;
  if (nodes.length > 5000) throw new Error('生成节点数量超过 5000 个。');
  // Keep user remarks visible and ensure policy names remain unique.
  const names = new Map();
  for (const node of nodes) {
    const count = (names.get(node.name)||0)+1; names.set(node.name,count);
    if (count > 1) node.name += ` (${count})`;
  }
  const formats = {};
  const warnings = expanded.warnings;
  for (const target of ['raw','clash','surge','json']) {
    try { formats[target] = renderSubscription(target, nodes, '__SUBSCRIPTION_URL__'); }
    catch(error) {
      if (target === 'surge') warnings.push('Surge 格式不可用：'+error.message);
      else throw error;
    }
  }
  formats.plain = {body:nodes.map(renderNodeUri).join('\n')+'\n',contentType:'text/plain; charset=utf-8'};
  return {settings:{nodeLinks:source,preferredIps:preferred,namePrefix:prefix,
                   keepOriginalHost:options.keepOriginalHost,includeOriginal:input.includeOriginal!==false},
    counts:{baseNodes:parsed.nodes.length,preferredEndpoints:endpoints.endpoints.length,outputNodes:nodes.length},
    preview:summarizeNodes(nodes,30), warnings,formats};
}

if (process.argv[1] && new URL(import.meta.url).pathname.replace(/^\//,'').endsWith(process.argv[1].replaceAll('\\','/').replace(/^\//,''))) {
  try { process.stdout.write(JSON.stringify(convert(JSON.parse(readFileSync(0,'utf8'))))); }
  catch(error) { process.stderr.write(error.message+'\n'); process.exitCode=1; }
}
