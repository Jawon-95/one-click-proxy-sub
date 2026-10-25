# 浏览器原生 HTTPS 代理

v2.2 增加独立浏览器入口与 `browser-extension/`，保留原 Trojan、CDN、多平台订阅、管理与域名切换功能。源代码一起提交，服务器运行配置和个人凭据不进入仓库。

## 部署

建议准备一个**独立的灰云域名**，例如 `browser.example.com`，A 记录只指向本服务器公网 IPv4，不要添加无关 AAAA。管理域名仍可使用 Cloudflare 橙云。默认复用已开放的 TCP 443，以 TLS SNI 将浏览器域名分发到独立的回环代理后端。原 Trojan、WebSocket 和面板仍保留原分发规则。

```bash
sudo bash install.sh --upgrade --browser-host browser.example.com
```

已有部署、源码已经更新时可以单独启用。**第一次接入共用的 443 前端需要短暂重启 Xray**，操作前切换到其他服务器的备用节点：

```bash
sudo /opt/proxy-web/bin/python /opt/proxy-manager/deploy/provision.py browser --browser-host browser.example.com --browser-port 443
```

可指定 `--browser-port 8443` 使用独立 TLS 监听，不需修改 Xray 前端，但需要云安全组放行该端口。也支持 `--browser-proxy` 使用受信任的 IP 地址证书和独立端口，但部分网络会重置不带域名 SNI 的 TLS，因此推荐灰云域名加标准 443。

安装器验证域名仅解析到本机公网 IP，经现有端口 80 的 ACME webroot 签发 Let's Encrypt 证书，再启动独立服务。443 模式下后端只监听 `127.0.0.1:18080`；只有 Xray TLS 前端可从网络转发给它。配置保存在 `/etc/proxy-manager/config.json` 的 `browser_proxy` 中；认证密钥仅在服务器生成，升级保留。证书私钥仅服务账户可读，不发送到浏览器。

## 订阅和兼容性

原来的订阅后增加 `?target=browser` 返回：

```json
{"version":1,"name":"Singapore Private","nodes":[{"id":"singapore-origin","name":"新加坡 · HTTPS 专线","scheme":"https","host":"browser.example.com","port":443,"username":"browser_1","password":"server-derived-secret"}]}
```

这是供插件读取的浏览器兼容入口。原始通用订阅、Clash、Surge、Shadowrocket 和二维码逻辑保留；管理页额外显示浏览器格式。一个服务器当前发布一个真实浏览器入口。插件支持多个兼容节点，但不把 Trojan/CDN 候选 IP 冒充 HTTPS 节点。

HTTPS 代理使用标准 HTTP CONNECT 和 Basic 代理认证，认证信息只在 TLS 内发送。隧道中的目标网站 HTTPS 不做中间人解密。普通 HTTP 转发移除 Proxy-Authorization 和逐跳头。服务器阻止内网、回环、链路本地、云元数据及代理自环；DNS 返回的所有地址均检查，并按已校验的数值地址连接。

## 运维

```bash
systemctl status proxy-browser proxy-subscription xray
journalctl -u proxy-browser --no-pager -n 30
systemctl list-timers proxy-cert-renew.timer
```

复用原证书续期定时器和 deploy hook。443 模式下浏览器证书由 Xray 读取，续期后校验配置并重启 Xray；独立端口模式仅重启 `proxy-browser.service`。IP 证书有效期约六天，必须保留自动续期；域名证书按 CA 当前政策续期。更换管理域名时保留仍被浏览器入口使用的证书，两个角色不能使用同一个域名。

回滚浏览器功能：先在分身中断开并保留锁网，然后停止 `proxy-browser.service`。如需完全恢复旧服务端，从部署前备份还原源码和配置，用恢复版本的 `deploy/provision.py activate` 重新生成并校验原 Xray 配置，再重启相关服务；若另外备份了原 Xray 配置，也可校验后恢复该配置。数据库不需要回退，避免丢失部署后的管理修改。443 模式的 Xray 改动仅为新增浏览器证书和按域名分发；不删除原节点、订阅数据库、原域名证书或原节点密码。

浏览器扩展的保护边界和安装步骤见 [插件使用说明](../browser-extension/README.md)。不要将“关闭插件”当成“断开并锁网”。

## 验证

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
node tests/browser_extension_unit.mjs
node --check browser-extension/background.js
bash -n install.sh
```

Windows 上用独立临时 Edge 资料运行真实联网测试：

```powershell
$env:PROXYSUB_BROWSER_TEST_URL = 'https://panel.example.com/subscribe/your-private-link'
node tests/browser_extension_smoke.mjs
```

`--fault` 测试在连通后等待操作人员停止浏览器代理服务，继续后确认代理不可用时没有本机直连。只应在维护窗口对自有入口测试，完成后重新启动该服务。临时资料、截图和报告位于 `.verification/`，不会打包或提交。

参考：[Chromium 代理行为](https://chromium.googlesource.com/chromium/src/+/HEAD/net/docs/proxy.md)、[Chrome proxy API](https://developer.chrome.com/docs/extensions/reference/api/proxy)、[声明式网络规则](https://developer.chrome.com/docs/extensions/reference/api/declarativeNetRequest)、[WebRTC 隐私设置](https://developer.chrome.com/docs/extensions/reference/api/privacy)、[Let's Encrypt IP 证书](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability)。
