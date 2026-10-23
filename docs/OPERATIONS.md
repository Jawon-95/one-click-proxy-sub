# 运维和迁移

## 服务

```bash
sudo systemctl status xray proxy-subscription --no-pager
sudo systemctl is-enabled xray proxy-subscription proxy-cert-renew.timer
sudo systemctl list-timers proxy-cert-renew.timer --no-pager
sudo journalctl -u xray -n 50 --no-pager
sudo journalctl -u proxy-subscription -n 50 --no-pager
sudo systemctl restart xray proxy-subscription
```

两个进程均开机自启，退出后约 5 秒由 systemd 自动重启。SQLite 配置与会话不随进程退出而清空。Node.js 只在保存订阅时作为短生命周期转换进程运行，客户端刷新直接读取已缓存的格式。

## 证书

定时器每天检查两次。签发/续期成功后，部署钩子更新 Xray 的证书副本并重启 Xray；连接可能短暂中断，客户端可重连。

```bash
sudo /opt/certbot/bin/certbot renew --dry-run --run-deploy-hooks --no-random-sleep-on-renew
```

保持 TCP 80 可达。不要移除 `/etc/letsencrypt`、证书部署钩子或续期定时器。

## 更新

重新执行 README 的 GitHub `curl` 安装命令会获取当前源码。运行 `/opt/proxy-manager/install.sh --upgrade` 则使用已安装的源码重建配置。两种方式都保留已保存的优选地址、管理密码和订阅 URL。

安装器固定代理内核及运行时版本，不会自动跟随 Xray 最新版变更协议。升级这些固定版本前，请验证客户端兼容性。

## 备份

最重要的是：

- `/etc/proxy-manager/config.json`：包含内部节点凭证。
- `/var/lib/proxy-manager/panel.sqlite3`：管理密码哈希、优选地址、订阅路径与会话。
- `/etc/letsencrypt`：ACME 账户、证书及续期设置（同域名重建时也可重新申请证书）。

数据库启用了 WAL。不要在运行中只复制 `.sqlite3` 文件而忽略尚未合并的 WAL。可使用 Python 的 SQLite 在线备份：

```bash
sudo /opt/proxy-web/bin/python - <<'PY'
import sqlite3
from pathlib import Path
path=Path('/root/proxysub-backup.sqlite3')
with sqlite3.connect('/var/lib/proxy-manager/panel.sqlite3') as source, sqlite3.connect(path) as target:
    source.backup(target)
path.chmod(0o600)
PY
```

配置和数据库备份都包含敏感信息，应放在受保护的位置，不要提交到 Git 或通过公开下载链接分享。

## 更换服务器

全新安装只需要 README 的安装命令。若要同时保留旧链接和优选地址：

1. 在旧服务器在线备份数据库，并安全复制 `config.json`。
2. 在新服务器首次安装，先验证新服务器可用。
3. 停止新服务器的管理服务，恢复数据库到 `/var/lib/proxy-manager/panel.sqlite3`，移除新库对应的空闲 WAL/SHM 文件；恢复配置并修正新服务器的公网 IP、域名。
4. 设置数据库 owner 为 `proxy-sub:proxy-sub`、权限 `600`，配置 owner 为 `root:proxy-sub`、权限 `640`。
5. 更新域名 DNS 后，执行安装器 `--upgrade`，内部节点模板和缓存会重新生成，继续使用保存的优选地址。
6. 验证新实例，最后停用旧实例。同域名、相同订阅路径可以继续由客户端刷新；如果域名改变，需重新导入地址。

恢复时应确保应用完全停止后再替换数据库，避免把旧库与新 WAL 混用。

## 数据边界

面板中的所有链接共享同一份生成结果。停用或删除链接只阻止读取订阅，不会撤回已被客户端保存的节点凭证。修改管理密码只影响后台登录，不会更换代理密码。

Cloudflare 灰云/橙云由用户在自己的控制台管理。所有优选入口仍连接同一台自建服务器，最终出口、带宽和套餐流量均属于这台服务器。
