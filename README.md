# One-click ProxySub

自建 Xray 代理 + Cloudflare 优选入口 + 多平台订阅管理，一条命令部署到自己的 Linux 服务器。

打开自己的域名，输入管理密码，即可编辑优选 IP/域名、复制各平台订阅链接、扫描二维码。基础节点、Host、SNI 和 WebSocket 路径全部自动配置，无需粘贴节点，也不需要 Cloudflare Worker、KV 或 API Token。

## 一键安装

先准备一台 **systemd Linux 服务器**和一个托管在 Cloudflare 的域名，将子域名的 **A 记录指向服务器公网 IPv4**，首次签发证书前暂时选择灰云「仅 DNS」，并放行 TCP **22、80、443**。

在服务器执行：

```bash
curl -fsSL https://raw.githubusercontent.com/Jawon-95/one-click-proxy-sub/refs/heads/main/install.sh | sudo bash
```

按提示输入域名（例如 `proxy.example.com`）和管理密码。脚本自动安装依赖、生成节点凭证、签发 Let's Encrypt 证书、配置开机自启及证书自动续期。输入密码时不会回显；已有安装再次执行会保留管理密码、优选地址和订阅链接。

完成后：

1. 在 Cloudflare 开启该 A 记录的**橙云代理**，SSL/TLS 选择**完全（严格）**，WebSockets 保持开启。
2. 打开 `https://你的域名`，只输入管理密码登录；登录状态保留 **30 天**。
3. 直接复制通用或客户端专用订阅，或扫描页面上已显示的二维码。
4. 在客户端更新订阅并测速，选择适合当前网络的入口。

详细安装、参数和故障处理：[部署文档](docs/DEPLOYMENT.md)。

## 功能

- **自动生成节点**：内置 Trojan + WebSocket + TLS，固定保留自己的 Host/SNI，支持 Cloudflare 橙云。
- **直连节点置顶**：订阅第一个节点直接连接服务器公网 IP（SNI/Host 仍为自己的域名），不经过 Cloudflare，CDN 整体变慢时可作为备选。
- **219 个预置入口**：17 个域名、202 个 IP，加上直连节点和默认自动入口，首次订阅可获取 **221 个节点**。
- **自定义节点数量**：填 100 返回直连节点加前 100 个优选地址的节点；完整地址列表仍保存。0 表示全部，并保留自动入口。
- **面板更换域名**：验证 DNS、申请证书后，统一切换管理页、订阅与二维码；旧域名、旧订阅地址和旧节点失效。失败或切换中断时自动恢复原配置。
- **保存立即更新**：编辑地址后点击保存，现有订阅 URL 保持不变，客户端刷新即可获取新列表。无 KV 同步等待。
- **多平台格式**：通用、v2rayN/v2rayNG、Shadowrocket、Clash/Mihomo、Surge，二维码直接显示。本地完成转换和二维码生成。
- **链接管理**：随机订阅地址、易记路径、重命名、启用/停用和删除。
- **Clash 自动选择**：提供手动选择和 URL 测试策略组；不附带大陆分流规则库，分流由客户端自行配置。
- **本地 SQLite**：持久化配置、订阅链接和登录状态；保存失败时保留上一份有效订阅。
- **自动运维**：systemd 开机启动、异常自动拉起、证书续期。程序更新不清空持久数据。

这是**同一台服务器的多个接入入口**，不是多台不同地区的出口服务器。Cloudflare Anycast 可能让多个地址接入同一机房；实际效果随客户端网络、运营商和时间变化。流量最终仍从你的服务器出站，并消耗服务器带宽与套餐流量。

预置地址是候选入口，不保证每个地址在所有地区始终可用。可使用 [CloudflareSpeedTest](https://github.com/XIU2/CloudflareSpeedTest) 在自己的网络中优选，然后将结果填回面板。

## 更新与状态

重新执行同一个安装命令即可更新；也可以使用已安装的脚本：

```bash
sudo bash /opt/proxy-manager/install.sh --upgrade
sudo bash /opt/proxy-manager/install.sh --check
```

前者从当前已安装源码重建运行配置；从 GitHub 获取最新程序版本时请使用上方 `curl` 命令。

[运维、备份及迁移](docs/OPERATIONS.md)

## 环境

- Ubuntu 22.04+、Debian 12+、OpenCloudOS/Rocky/AlmaLinux 9，需使用 systemd。
- x86_64 或 ARM64；建议 1 GB 以上内存，2 GB 更宽裕。
- 安装器下载固定版本的官方 Xray、Node.js，并验证 SHA256；Python 环境使用独立 venv。
- 当前实际服务部署验证环境为 OpenCloudOS 9 x86_64。其他发行版安装分支及 ARM64 应按各自环境验证。
- 首次申请证书会自动接受 Let's Encrypt 服务条款；请确保你有权使用所填写的域名。

## 目录

```text
app/          HTTP 后端、订阅转换、静态管理页与许可保留的转换核心
config/       默认优选地址，不包含部署凭证
deploy/       初始化、迁移、证书部署与服务配置
docs/         部署与运维文档
scripts/      打包与开发工具
tests/        转换和 HTTP/SQLite 集成测试
install.sh    一键安装入口
```

运行数据、凭证、证书、Cookie、历史部署包和开发机器的测试客户端均不属于源码，已在 `.gitignore` 中排除。

## 开发验证

开发环境需要 Python 3.10+ 和 Node.js 22+：

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -p 'test_*.py' -v
node --check app/web/app.js
bash -n install.sh
python scripts/package.py
```

订阅链接是访问凭证，请勿公开传播。管理密码以 PBKDF2 哈希存储，浏览器使用 Secure/HttpOnly Cookie；停用链接只会阻止更新，不能撤回客户端已经取得的节点凭证。

## License

[MIT](LICENSE)。订阅转换核心及部分视觉资源复用自 `cf-ips-to-sub-link`，原始许可证与修改说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
