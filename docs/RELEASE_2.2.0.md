纯浏览器插件与服务器 HTTPS 代理入口，保留原 Trojan、WebSocket、Cloudflare CDN、多平台订阅及管理功能。

下载与使用：

1. 下载 **browser-extension.zip**，解压到固定位置。
2. 用原来的分身快捷方式打开 Edge，进入 `edge://extensions`，开启开发人员模式，选择“加载解压缩的扩展”，选择解压后的 `browser-extension` 文件夹。
3. 点击“新加坡专线”，展开“订阅设置”，粘贴自己的订阅链接，保存并获取节点，然后连接。
4. 连接成功后网页才联网；断开或节点故障时锁网。首次安装后重启分身。不要禁用或卸载插件来代替断开。

无需安装本地核心、Windows 服务或证书。插件包含订阅更新、手动节点选择、HTTPS 延迟测试、出口 IP 检测。每台服务器当前发布一个真实 HTTPS 入口，原 Trojan/CDN 候选节点继续供 v2rayN 等客户端使用。

服务器首次部署：先将两个不同域名的 A 记录指向服务器 IPv4，并放行 TCP 80、443。首次签发证书时均设为仅 DNS；浏览器专用域名须一直保持仅 DNS。

```bash
curl -fsSL https://raw.githubusercontent.com/Jawon-95/one-click-proxy-sub/refs/heads/main/install.sh | sudo bash -s -- --domain proxy.example.com --browser-host browser.example.com
```

替换示例域名，按提示设置管理密码，打开管理页获取订阅。已有安装重复执行可升级并保留数据。**one-click-proxy-sub.tar.gz** 是完整服务端和插件源码包；**SHA256SUMS** 可验证两个下载包。

保护范围：插件正常启用时，此浏览器个人资料的网页请求。只在分身安装；禁用、卸载、其他个人资料、无痕窗口、终端 Claude Code 和桌面软件不受保护。全局 TUN 仍可能影响底层连接。首次导入订阅允许插件自身直接获取订阅，网页始终锁定。

实现采用固定 HTTPS 代理，无国内直连和 DIRECT 回退，限制非代理 WebRTC UDP 并阻止 WebTransport。服务端保护私网目标和 DNS 重绑定，代理凭据按订阅派生；HTTPS 网站保持端到端 TLS。

验证：真实隔离 Edge 完成新装锁网、订阅导入、认证、服务器出口验证、国内网页访问、WebRTC 设置、测速恢复、主动断开，以及实际停止代理服务后的失败和无直连测试。Python 回归覆盖转换、HTTP/数据库和 Linux 部署流程。

完整步骤见仓库 [README](https://github.com/Jawon-95/one-click-proxy-sub#readme)、[插件说明](https://github.com/Jawon-95/one-click-proxy-sub/blob/main/browser-extension/README.md) 与 [部署说明](https://github.com/Jawon-95/one-click-proxy-sub/blob/main/docs/BROWSER_PROXY.md)。
