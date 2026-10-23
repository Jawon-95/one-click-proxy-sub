#!/usr/bin/env bash
# ProxySub installer. Safe to run again; persistent configuration is retained.
set -Eeuo pipefail
umask 027
VERSION=2.0.0
XRAY_VERSION=26.3.27
NODE_VERSION=22.23.4
DOMAIN=
ORIGIN_IP=
PASSWORD_FILE=
CHECK=0
while (($#)); do
  case "$1" in
    --domain) DOMAIN=${2:?Missing domain}; shift 2 ;;
    --origin-ip) ORIGIN_IP=${2:?Missing IP}; shift 2 ;;
    --password-file) PASSWORD_FILE=${2:?Missing password file}; shift 2 ;;
    --upgrade) shift ;;
    --check) CHECK=1; shift ;;
    --help|-h)
      cat <<'HELP'
ProxySub 一键安装 / 升级
  sudo bash install.sh                         交互安装；已有配置自动保留
  sudo bash install.sh --domain proxy.example.com
  sudo bash install.sh --upgrade               更新程序，保留密码/优选地址/链接
  sudo bash install.sh --check                 查看服务与证书续期状态
  --origin-ip IPv4                            手动指定服务器公网 IPv4
  --password-file /root/password.txt           从本地文件读取管理密码
HELP
      exit 0 ;;
    *) printf '未知参数：%s\n' "$1" >&2; exit 2 ;;
  esac
done
[[ $(id -u) == 0 ]] || { echo '请使用 root 或 sudo 运行。' >&2; exit 1; }
if ((CHECK)); then
  systemctl --no-pager --full status xray proxy-subscription
  systemctl is-enabled xray proxy-subscription proxy-cert-renew.timer
  systemctl list-timers proxy-cert-renew.timer --no-pager
  exit 0
fi
[[ -d /run/systemd/system ]] || { echo '本工具需要使用 systemd 的 Linux 服务器。' >&2; exit 1; }
. /etc/os-release
case "$ID" in
  ubuntu|debian)
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends python3 python3-venv python3-pip ca-certificates curl tar xz-utils unzip
    PYTHON=python3 ;;
  opencloudos|rocky|almalinux|rhel|centos)
    dnf install -y --setopt=install_weak_deps=False python3 python3-pip ca-certificates curl tar xz unzip
    PYTHON=python3
    if ! python3 -c 'import sys; assert sys.version_info >= (3,10)' 2>/dev/null; then
      dnf install -y python3.11 python3.11-pip
      PYTHON=python3.11
    fi ;;
  *) echo '支持 Ubuntu 22.04+、Debian 12+、OpenCloudOS/Rocky/AlmaLinux 9。' >&2; exit 1 ;;
esac
"$PYTHON" -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'

WORK=$(mktemp -d /tmp/proxysub.XXXXXXXX)
cleanup() { [[ ${WORK:-} == /tmp/proxysub.* && -d $WORK ]] && rm -rf -- "$WORK"; }
trap cleanup EXIT
trap 'echo "安装未完成，请修复上方错误后重新运行；不会清空现有配置。" >&2' ERR
download() { curl -fLsS --retry 3 --connect-timeout 20 --max-time 300 "$1" -o "$2"; }
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-/dev/stdin}")" && pwd)
if [[ ! -f $SOURCE/app/server.py || ! -f $SOURCE/deploy/provision.py ]]; then
  REPO=${PROXYSUB_REPO:-Jawon-95/one-click-proxy-sub}
  REF=${PROXYSUB_REF:-main}
  [[ $REPO =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ && $REF =~ ^[A-Za-z0-9_.-]+$ ]] || { echo '仓库或版本参数不正确。' >&2; exit 2; }
  echo '下载 ProxySub 源码…'
  download "https://codeload.github.com/$REPO/tar.gz/$REF" "$WORK/source.tar.gz"
  mkdir "$WORK/source"
  tar -xzf "$WORK/source.tar.gz" -C "$WORK/source" --strip-components=1
  SOURCE=$WORK/source
fi
[[ -f $SOURCE/app/server.py && -f $SOURCE/config/preferred-endpoints.txt ]] || { echo '源码不完整。' >&2; exit 1; }

# Refuse to replace a web server owned by another application.
"$PYTHON" - <<'PY'
import socket, subprocess
for port,unit in [(80,'proxy-subscription.service'),(443,'xray.service')]:
    with socket.socket() as s:
        try: s.bind(('0.0.0.0',port))
        except OSError:
            value=subprocess.run(['systemctl','is-active','--quiet',unit]).returncode
            if value:
                raise SystemExit(f'TCP {port} 已被其他程序占用，请先处理端口冲突。')
PY

case $(uname -m) in
  x86_64) NODE_ARCH=x64; XRAY_ASSET=linux-64; NODE_SHA=1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce ;;
  aarch64|arm64) NODE_ARCH=arm64; XRAY_ASSET=linux-arm64-v8a; NODE_SHA=6287aa46b6c375d816e63ae66d024fbbfd70404825aabdfdf898a970871d2f4b ;;
  *) echo '仅支持 Linux x86_64 和 ARM64。' >&2; exit 1 ;;
esac
if [[ ! -x /opt/proxy-node/bin/node ]] || [[ $(/opt/proxy-node/bin/node --version) != v$NODE_VERSION ]]; then
  echo '安装官方 Node.js…'
  download "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-$NODE_ARCH.tar.xz" "$WORK/node.tar.xz"
  printf '%s  %s\n' "$NODE_SHA" "$WORK/node.tar.xz" | sha256sum -c -
  install -d -m 0755 /opt/proxy-node
  tar -xJf "$WORK/node.tar.xz" -C /opt/proxy-node --strip-components=1
fi
if [[ ! -x /opt/xray/xray ]] || ! /opt/xray/xray version | head -1 | grep -Fq "Xray $XRAY_VERSION "; then
  echo '安装官方 Xray…'
  download "https://github.com/XTLS/Xray-core/releases/download/v$XRAY_VERSION/Xray-$XRAY_ASSET.zip" "$WORK/xray.zip"
  download "https://github.com/XTLS/Xray-core/releases/download/v$XRAY_VERSION/Xray-$XRAY_ASSET.zip.dgst" "$WORK/xray.dgst"
  "$PYTHON" - "$WORK" <<'PY'
import hashlib,pathlib,sys,zipfile
root=pathlib.Path(sys.argv[1]); archive=root/'xray.zip'
digest=hashlib.sha256(archive.read_bytes()).hexdigest()
if digest not in (root/'xray.dgst').read_text().lower(): raise SystemExit('Xray SHA256 验证失败')
destination=pathlib.Path('/opt/xray'); destination.mkdir(parents=True,exist_ok=True)
with zipfile.ZipFile(archive) as z:
    for name in ('xray','geoip.dat','geosite.dat','LICENSE'):
        (destination/name).write_bytes(z.read(name))
PY
fi
chmod 0755 /opt/xray /opt/xray/xray
chmod 0644 /opt/xray/geoip.dat /opt/xray/geosite.dat
"$PYTHON" -m venv /opt/proxy-web
/opt/proxy-web/bin/pip install --disable-pip-version-check -q -r "$SOURCE/requirements.txt"
"$PYTHON" -m venv /opt/certbot
/opt/certbot/bin/pip install --disable-pip-version-check -q 'certbot==5.8.0'

echo '部署程序并初始化本地数据库…'
install -d -m 0755 /opt/proxy-manager
if [[ $(realpath "$SOURCE") != /opt/proxy-manager ]]; then
  for folder in app config deploy docs; do
    cp -a "$SOURCE/$folder" /opt/proxy-manager/
  done
  for file in install.sh requirements.txt VERSION README.md LICENSE THIRD_PARTY_NOTICES.md; do
    cp "$SOURCE/$file" /opt/proxy-manager/
  done
fi
chown -R root:root /opt/proxy-manager
find /opt/proxy-manager -type d -exec chmod 0755 {} +
find /opt/proxy-manager -type f -exec chmod 0644 {} +
ARGS=(prepare)
[[ -n $DOMAIN ]] && ARGS+=(--domain "$DOMAIN")
[[ -n $ORIGIN_IP ]] && ARGS+=(--origin-ip "$ORIGIN_IP")
[[ -n $PASSWORD_FILE ]] && ARGS+=(--password-file "$PASSWORD_FILE")
/opt/proxy-web/bin/python /opt/proxy-manager/deploy/provision.py "${ARGS[@]}"
DOMAIN=$(/opt/proxy-web/bin/python /opt/proxy-manager/deploy/provision.py domain)
echo '签发或检查域名 TLS 证书…'
/opt/certbot/bin/certbot certonly --webroot -w /var/lib/proxy-acme -d "$DOMAIN" --cert-name "$DOMAIN" \
  --agree-tos --register-unsafely-without-email --non-interactive --keep-until-expiring
/opt/proxy-web/bin/python /opt/proxy-manager/deploy/provision.py activate
echo "安装完成： https://$DOMAIN"
echo '打开域名，输入管理密码即可查看预置入口、订阅链接和二维码。'
echo 'Cloudflare DNS 橙云开启后可使用优选入口；SSL/TLS 请选择完全（严格）。'
