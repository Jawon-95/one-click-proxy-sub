# 部署

## 首次安装

1. 准备一台带 systemd 的 Linux 服务器，使用 root 或 sudo。安装器占用 80、443；若已有其他网站服务，请先解决端口冲突。
2. Cloudflare 添加 A 记录：例如名称 `proxy`，地址为服务器公网 IPv4，先用「仅 DNS」。不要为这个子域名添加指向其他服务器的同名 A/AAAA 记录。
3. 在云厂商防火墙和本机防火墙放行 TCP 80、443；保留管理服务器所需的 SSH 端口。
4. 执行：

```bash
curl -fsSL https://raw.githubusercontent.com/Jawon-95/one-click-proxy-sub/refs/heads/main/install.sh | sudo bash
```

按提示输入完整域名和管理密码。域名不要包含 `https://`、端口或路径。密码至少 8 个字符；留空将随机生成并在安装终端显示一次。

安装后会输出管理页面地址。确认可以访问后，在 Cloudflare 开启橙云，SSL/TLS 使用「完全（严格）」，Network 中保持 WebSockets 开启。优选 IP/域名需要域名已经通过 Cloudflare 代理，灰云模式下通常只有默认域名入口能用。

安装器不修改 Cloudflare 账号设置、不申请 API Token、不修改腾讯云等云厂商的防火墙，也不会关闭系统已有的防火墙。它会在发现 80/443 被非本工具服务占用时停止。

## 查看脚本后安装

```bash
curl -fsSL https://raw.githubusercontent.com/Jawon-95/one-click-proxy-sub/refs/heads/main/install.sh -o /tmp/proxysub-install.sh
less /tmp/proxysub-install.sh
sudo bash /tmp/proxysub-install.sh
```

也可以克隆仓库后执行根目录 `sudo bash install.sh`，或上传 `python scripts/package.py` 生成的源码包，解压后执行同一命令。

## 参数

```bash
sudo bash install.sh --domain proxy.example.com
sudo bash install.sh --domain proxy.example.com --origin-ip 203.0.113.10
sudo bash install.sh --upgrade
sudo bash install.sh --check
```

`--origin-ip` 使用实际的公网 IPv4，上方地址仅为文档示例。未指定时，已有配置优先；新服务器会尝试检测出口 IPv4。

自动化安装可以使用 root 可读的本地密码文件：

```bash
sudo bash install.sh --domain proxy.example.com --origin-ip YOUR_PUBLIC_IPV4 --password-file /root/proxysub-password.txt
```

文件内容为一行密码。不要把该文件放进仓库，使用后按自己的凭证管理方式保管或删除。升级时传入该参数会重设管理密码并使旧会话失效；不传则保留密码和会话。

`PROXYSUB_REF` 可指定 GitHub 的提交 SHA 或标签，默认为 `main`。`PROXYSUB_REPO` 可指定自己的 fork，例如 `owner/repo`。固定源码版本不会改变安装器所固定的 Xray/Node.js 版本。

## 安装内容

| 路径 | 内容 |
| --- | --- |
| `/opt/proxy-manager` | 当前程序源码与静态资源 |
| `/etc/proxy-manager/config.json` | 域名、源站 IP、内部节点凭证，root/proxy-sub 可读 |
| `/var/lib/proxy-manager/panel.sqlite3` | 优选地址、订阅格式缓存、链接、密码哈希、会话 |
| `/var/lib/proxy-manager-admin` | 域名任务进度及仅 root 可读的切换恢复记录 |
| `/opt/xray` | 官方 Xray 与路由数据 |
| `/etc/xray/config.json` | TLS、WebSocket 节点与回落 |
| `/etc/xray/tls/domain` | Xray 使用的域名证书副本 |
| `/opt/proxy-web` | 管理页 Python venv |
| `/opt/proxy-node` | 用于本地订阅转换的 Node.js |
| `/opt/certbot` | 证书工具 Python venv |
| `/etc/letsencrypt` | ACME 账户、证书与续期配置 |

程序使用非 root 的 `xray` 和 `proxy-sub` 账户运行。仅为监听低端口保留 `CAP_NET_BIND_SERVICE`，不把数据库或证书目录暴露成静态资源。

域名更换由按需运行的 root systemd 工作进程执行，只接受经过校验的域名请求；网页进程继续使用非 root 账户。`proxy-domain-change.path` 监听请求，`proxy-domain-change.service` 在需要时签证书、更新服务，并在重启后恢复未完成事务。

## 在面板更换域名

1. 在 Cloudflare 为新域名添加指向本服务器的 A 记录，等待解析生效。首次申请该域名证书建议使用灰云，关闭拦截 HTTP 验证的强制 HTTPS/WAF 规则，保持 TCP 80 可达。
2. 在面板底部「更换管理和订阅域名」填写完整新域名，点击「更换域名」，在确认框提交。
3. 系统先验证该域名确实到达本服务器，再签发证书。准备阶段原域名仍可用；证书申请失败不会切换。
4. 就绪后统一切换 Host/SNI、WebSocket 路径、内部代理密码、订阅地址及二维码，自动打开新域名。切换时连接会短暂中断，旧节点和旧域名随即失效。
5. 在新域名使用原管理密码登录，重新导入新订阅。跨域名不会迁移浏览器登录 Cookie；新的登录仍保留 30 天。订阅路径、优选列表和数量限制保留。
6. 新域名需要使用优选入口时，在 Cloudflare 开启橙云并使用「完全（严格）」。域名相关的 DNS 记录仍由用户手动维护；旧记录即使尚未删除，服务也会拒绝旧域名。

不要通过 `--domain` 安装参数代替面板的退役流程。安装参数用于首次配置；面板更换还会停用旧域名和旧节点、清理旧证书，并提供失败恢复。

## 限制订阅节点数量

在优选地址输入框下方设置「订阅节点数量」后保存。0 表示全部（含自动入口）；正整数表示按保存列表顺序、去重后取前 N 个优选地址，不额外加入自动入口。比如 100 返回恰好 100 个优选节点；不足 N 个时返回现有的全部优选节点。完整列表不会截短，恢复为 0 即可再次返回全部。所有平台订阅共享此限制，客户端需刷新订阅。

## 常见问题

**证书申请失败：** 检查 DNS 是否已生效、域名是否指向本服务器、TCP 80 是否可达、是否存在多余 AAAA 记录。首次安装暂时关闭橙云和强制 HTTPS 规则，修复后重跑安装器。不要短时间反复强制申请证书。

**橙云后打不开页面/节点：** 使用「完全（严格）」而不是「灵活」；源站域名证书需要有效。检查 WebSockets 是否开启，是否有 WAF、挑战页或缓存规则拦截订阅和 `/connect`。普通浏览器能打开页面不等于所有优选地址都适合当前网络。

**节点列表没变：** 点击面板「保存并更新订阅」后，还需要在客户端执行更新订阅。链接保持不变；缓存由本机 SQLite 原子更新，不经过 Cloudflare KV。页面和订阅带 `Cache-Control: no-store`，不要用自定义 Cloudflare 规则强制缓存这些路径。

**开机后无法使用：** `sudo bash /opt/proxy-manager/install.sh --check`。自启解决进程启动，不能修复操作系统、磁盘、网络或云防火墙故障。
