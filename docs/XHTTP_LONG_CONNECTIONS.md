# CDN 节点的 XHTTP 长连接：技术说明

本文记录 2026-11-12（版本 2.3.0）对 Cloudflare 优选 IP / 优选域名节点所做的改造：让这些节点像 [Hysteria 2 直连](HYSTERIA2.md)一样复用长连接，打开新网页时不再每次重新握手。日常使用说明见[部署文档](DEPLOYMENT.md#cdn-节点的-xhttp-长连接)。

## 要解决的问题

改造前，经 Cloudflare 的节点下载速度尚可，但每打开一个新网站（Google、YouTube 等）要等 3–4 秒，页面打开后站内浏览反而正常。

排查结论：

1. **出国链路丢包严重。** 本机 WiFi 和国内链路基本 0 丢包，到香港 Cloudflare 入口约 20%，到菲律宾源站约 27%。
2. **旧节点每个新连接都要握手。** Trojan + WebSocket 每开一个新连接要依次完成 TCP、TLS、WebSocket 三次握手，任何一个握手包丢了都要等 1–3 秒重传。站内浏览快，是因为浏览器已经和代理建好了连接。
3. **多数优选 IP 被运营商路由到美国。** 本机移动网络下，大部分优选 IP 实际进入美国 Cloudflare 机房（约 190 ms），只有少数进入香港（40–60 ms），路径本身就长。

HY2 不受第 2 点影响，因为它只在第一次建立 QUIC 连接，之后新请求只开新流。Cloudflare 普通橙云不转发 HY2 的 UDP，所以 CDN 节点只能在“经 Cloudflare 的 TCP + HTTP”框架内实现同样的连接复用。

## 方案：VLESS + XHTTP + 连接池

XHTTP 是 Xray 的传输方式，把代理数据装进普通的 HTTP 请求和响应里。经过 Cloudflare 时，客户端和 Cloudflare 入口之间是标准的 HTTP/2 连接；HTTP/2 本身支持在一条连接上同时跑很多个请求。

```text
浏览器打开新网站
   │
v2rayN（Xray 核心）
   │  从 8 条已建好的 HTTP/2 长连接里挑一条，发一个新的 HTTP 请求（不握手）
   ▼
Cloudflare 入口（优选 IP / 优选域名）
   │  Cloudflare 到源站同样复用连接
   ▼
服务器 Xray :443（TLS，ALPN h2）
   │  非 Trojan 流量回落到本机路由
   ▼
proxy-router（Caddy，127.0.0.1:10003，h1 + h2c）
   ├─ XHTTP 路径   → Xray VLESS + XHTTP 入站 127.0.0.1:10002
   ├─ WebSocket 路径 → Xray Trojan + WS 入站 127.0.0.1:10001（兼容旧节点）
   └─ 其他          → 管理面板 127.0.0.1:8080
```

### 客户端参数（订阅下发）

实现见 [app/generator.py](../app/generator.py) 的 `xhttp_nodes()` 和 `XHTTP_EXTRA`。

| 参数 | 值 | 作用 |
|---|---|---|
| 协议 | `vless://`，`encryption=none` | XHTTP 搭配 VLESS；TLS 由外层提供 |
| `type` / `mode` | `xhttp` / `packet-up` | `packet-up` 上行拆成普通 POST 请求，不需要修改任何 Cloudflare 设置 |
| `alpn` / `fp` | `h2` / `chrome` | 强制 HTTP/2，TLS 指纹与 Chrome 一致 |
| `xmux.maxConnections` | `8` | 连接池：最多 8 条长连接，请求分散到不同连接，单条连接丢包不会卡住全部网页 |
| `xmux.hMaxRequestTimes` | `4000-6000` | 每条连接可复用的请求数；`packet-up` 上行请求多，所以调高 |
| `xmux.hMaxReusableSecs` | `1800-3000` | 每条连接最长复用 30–50 分钟后换新，随机区间避免同时重建 |
| Host / SNI / 地址 | 管理域名 / 管理域名 / 优选 IP 或域名 | 与原 WebSocket 节点一致，只换传输方式 |

VLESS 用户 ID 由节点密码通过 UUIDv5 派生，XHTTP 路径保存在配置中，升级后订阅中的节点参数不变；面板更换域名时会随机生成新路径。

### 服务器端改动

实现见 [deploy/provision.py](../deploy/provision.py) 的 `start_router()`、`router_config()`、`router_healthy()` 和 `xray_config()`。

- **为什么需要本机路由。** 改造前，Xray 用 TLS 回落按路径区分 WebSocket 和面板，只支持 HTTP/1.1。HTTP/2 一条连接里会混着发 XHTTP、WebSocket、面板等不同路径的请求，回落无法逐个请求分流，所以增加一个只监听本机的 Caddy，按每个请求的路径转发。
- **固定版本 Caddy `2.11.9`。** 从官方 GitHub Release 下载，校验 SHA256，以 `xray` 用户运行，关闭管理接口和自动证书，只监听 `127.0.0.1:10003`；不新开公网端口。
- **Xray 443 同时声明 `h2` 和 `http/1.1`。** 只有路由启用成功时才开启 `h2`，否则保持原来的 `http/1.1`。
- **自检失败自动退回。** 路由下载、校验、启动或自检（面板和 XHTTP 入站都要能通过路由访问）任一步失败，安装器停掉路由、恢复仅 WebSocket 的 Xray 配置，并删除 `/etc/proxy-manager/xhttp.json`。订阅服务只在这个文件存在时下发 XHTTP 节点，所以服务器端失败不会让客户端拿到不能用的节点。

### 订阅如何选择传输方式

实现见 [app/server.py](../app/server.py) 的 `wants_xhttp()`。

| 情况 | 下发 |
|---|---|
| `target=v2rayn` / `v2rayng`，或客户端 User-Agent 含 `v2rayN` | XHTTP |
| 订阅地址带 `transport=ws`，或面板选择「v2rayN（兼容模式 · WebSocket）」 | WebSocket |
| 订阅地址带 `transport=xhttp` | XHTTP（适合其他基于 Xray 的客户端） |
| Clash / Mihomo、Surge、Shadowrocket 等格式 | 不变，仍为 WebSocket |

`001 | 直连` 和 `000 | Hysteria 2 直连` 不经过 Cloudflare，不做改写。

## 为什么之前的 WebSocket 长连接尝试没有效果

2026-11-07 曾尝试过 `v2rayn-mux` / `xray-mux` 订阅：在 Trojan + WebSocket 上开启 Mux 多路复用，并每约 8 秒发一次保活请求，当天即停用（实测打开新网页仍需 3–4 秒）。根据这次的测量，原因推测如下：

- Mux 把所有网页塞进**同一条** TCP 连接。在 20% 丢包下，这条连接里一个包丢失，所有网页都要等它重传（队头阻塞），省下的握手时间被重传等待抵消。XHTTP 用 8 条连接分摊，单条卡住只影响一部分请求。
- 这些订阅是“自定义配置”，在虚拟网卡（TUN）模式下需要额外设置前置端口；设置不对时实际流量可能根本没走 Mux。
- 保活只能维持一条连接，空闲重建后仍要完整握手。

## 测试方法

1. **不动本机代理。** 用 v2rayN 自带的 Xray 内核，按菲律宾服务器实际下发的订阅链接生成等效配置，监听其他本地端口，出站绑定物理网卡，绕开正在使用的虚拟网卡。
2. **三项指标：**
   - 新连接首字节：每次新建连接请求一个小页面，对应“打开新网页要等多久”；
   - 同时开 6 个站：并发请求 6 个不同网站，取全部返回的时间；
   - 12 个域名整页加载：模拟一个页面引用多个域名的资源。
3. **同一轮比较，取中位数。** 线路丢包波动大，只看同一轮内各节点的相对关系。
4. **自动化测试。** 本地 27 项测试全部通过；Linux 集成测试（`tests/test_provision_linux.py` 等）在菲律宾服务器上运行通过，并实际部署后验证订阅下发与节点连通。

## 实测结果

2026-11-12，菲律宾服务器，本机移动网络，中位数，单位毫秒：

| 节点 | 新连接首字节 | 同时开 6 个站 | 12 个域名整页加载 |
|---|---:|---:|---:|
| HY2 直连 | 669 | 1086 | 2260 |
| 旧 WS（香港入口） | 1056 | 3694 | 2603 |
| **新 XHTTP（香港入口）** | **670** | **1256** | **1743** |
| 旧 WS（美国入口） | 5086 | 8472 | 未测 |
| **新 XHTTP（美国入口）** | **3168** | **3975** | 未测 |

旧 WS 在早先几轮里新连接首字节为 1.6–2.3 秒，以上数字只能看相对关系。用户随后在 v2rayN 中实际使用确认没有问题。

另外测试过但没有采用的：

- **连接池大小：** 6、12、24 条与 8 条的差异在噪声范围内，保留 8。
- **HTTP/3（QUIC）经 Cloudflare：** 这条线路上内容加载很慢，不采用。

## 局限

- **下载吞吐没有提升。** 丢包 20% 时，经 Cloudflare 的任何 TCP 连接稳态只有约 0.5 Mbps（HY2 约 20 Mbps），瓶颈在 Cloudflare 的发送端拥塞控制，服务器无法改变。看视频、下大文件仍建议用 HY2。
- **美国入口仍然慢。** 改善明显，但达不到 HY2 的水平。建议选延迟低的香港入口节点（测试时为 `saas.sin.fan`、`icook.hk`、`104.21.81.203`，结果随网络和时间变化）。
- **空闲约 5 分钟后连接关闭。** 之后第一次访问需要重新握手。
- **只对基于 Xray 的客户端生效。** Clash、Surge、Shadowrocket 仍为 WebSocket。

## 排查与回退

```bash
sudo bash /opt/proxy-manager/install.sh --check     # 显示「XHTTP 长连接入口：已启用 / 未启用」
sudo systemctl status proxy-router                  # 路由服务状态
sudo journalctl -u proxy-router -n 50 --no-pager    # 路由日志
```

客户端临时回到旧方式：在面板复制「v2rayN（兼容模式 · WebSocket）」订阅，或在订阅地址后加 `transport=ws`。
