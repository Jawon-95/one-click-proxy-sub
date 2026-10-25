/* Shared by the service worker and offline validation tests. */
globalThis.ProxyModel = {
  types: ['main_frame', 'sub_frame', 'stylesheet', 'script', 'image', 'font', 'object', 'xmlhttprequest', 'ping', 'csp_report', 'media', 'websocket', 'webtransport', 'other'],
  dead: { mode: 'fixed_servers', rules: { singleProxy: { scheme: 'https', host: '127.0.0.1', port: 9 }, bypassList: ['<-loopback>'] } },
  config(node) { return { mode: 'fixed_servers', rules: { singleProxy: { scheme: 'https', host: node.host, port: node.port }, bypassList: ['<-loopback>'] } }; },
  subscriptionUrl(value) {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username || url.password || url.hash) throw new Error('请输入没有内嵌账号密码的 HTTPS 订阅链接。');
    url.searchParams.set('target', 'browser'); return url.href;
  },
  parse(data) {
    if (data?.version !== 1 || !Array.isArray(data.nodes) || !data.nodes.length || data.nodes.length > 50) throw new Error('不是兼容的浏览器订阅，请先更新服务器。');
    const ids = new Set();
    return data.nodes.map(n => {
      if (!/^[a-zA-Z0-9_-]{1,80}$/.test(n.id) || ids.has(n.id) || n.scheme !== 'https' ||
          typeof n.host !== 'string' || !/^[a-zA-Z0-9.-]{1,253}$/.test(n.host) ||
          !Number.isInteger(n.port) || n.port < 1 || n.port > 65535 ||
          !['username', 'password'].every(k => typeof n[k] === 'string' && n[k].length > 0 && n[k].length <= 256)) throw new Error('订阅节点格式不合法，原节点列表保持不变。');
      if (/^(127\.|10\.|0\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.)/.test(n.host) || /(^|\.)(localhost|local|internal)$/.test(n.host)) throw new Error('不接受本机或内网代理节点。');
      ids.add(n.id);
      return { id: n.id, name: String(n.name || n.id).replace(/[\x00-\x1f\x7f]/g, '').slice(0, 80), scheme: 'https', host: n.host.toLowerCase(), port: n.port, username: n.username, password: n.password };
    });
  }
};
