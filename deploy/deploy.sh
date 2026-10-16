#!/usr/bin/env bash
# 用 Docker 部署到服务器：[环境变量...] ./deploy/deploy.sh [user@host]
# 需要 SSH 密钥登录，远端需有 docker、tar；建议 root（要 chown 数据目录）。
# 代码以只读方式挂载进容器；数据目录不会被覆盖。
#
# 可用环境变量（不设置时用默认值，默认值是作者的 iStoreOS 服务器；设为空字符串表示关闭该项）：
#   THERMO_BASE        远端目录，代码在 $BASE/app，数据在 $BASE/data      默认 /mnt/data/market-thermo
#   THERMO_IMAGE       Python 镜像                                       默认 DaoCloud 镜像站的 python:3.12-alpine（国内拉不到 Docker Hub）
#   THERMO_PORT        宿主机端口                                         默认 8600
#   THERMO_DNS         容器 DNS，空格分隔；空字符串 = 用 Docker 默认        默认 "<服务器IP> 223.5.5.5"（作者的服务器本身是路由器，自带 DNS）
#   THERMO_NOTIFY_URL  notify-service 地址；空字符串 = 不发邮件，只记录    默认 http://<服务器IP>:8081
#   THERMO_PUBLIC_URL  邮件里的看板链接                                   默认 http://<服务器IP>:<端口>
set -euo pipefail
HOST="${1:-root@192.168.28.66}"
IP="${HOST#*@}"
BASE="${THERMO_BASE:-/mnt/data/market-thermo}"
IMAGE="${THERMO_IMAGE:-docker.m.daocloud.io/library/python:3.12-alpine}"
PORT="${THERMO_PORT:-8600}"
DNS="${THERMO_DNS-$IP 223.5.5.5}"
NOTIFY_URL="${THERMO_NOTIFY_URL-http://$IP:8081}"
PUBLIC_URL="${THERMO_PUBLIC_URL:-http://$IP:$PORT}"
cd "$(dirname "$0")/.."

DNS_ARGS=""
for d in $DNS; do DNS_ARGS="$DNS_ARGS --dns $d"; done

echo "部署到 $HOST:$BASE（端口 $PORT，镜像 $IMAGE，DNS ${DNS:-Docker 默认}，邮件 ${NOTIFY_URL:-关闭}）"

COPYFILE_DISABLE=1 tar czf - --no-xattrs --exclude __pycache__ --exclude .DS_Store thermo static server.py \
  | ssh "$HOST" "rm -rf $BASE/app.new && mkdir -p $BASE/app.new $BASE/data && tar xzf - -C $BASE/app.new \
      && rm -rf $BASE/app && mv $BASE/app.new $BASE/app && chown -R 65534:65534 $BASE/data"

# CBOE 从国内服务器下载常超时：远端没有 VIX 历史时，从本机下载后传过去；失败不影响部署，服务会在后台重试
if ! ssh "$HOST" "test -s $BASE/data/vix_history.json"; then
  if python3 -c "import json; from thermo import core; print(json.dumps(core.vix_history()))" \
      | ssh "$HOST" "cat > $BASE/data/vix_history.tmp && test -s $BASE/data/vix_history.tmp \
          && mv $BASE/data/vix_history.tmp $BASE/data/vix_history.json && chown 65534:65534 $BASE/data/vix_history.json"; then
    echo "已上传 VIX 历史"
  else
    echo "VIX 历史预下载失败，服务启动后会在后台重试"
  fi
fi

ssh "$HOST" "docker image inspect $IMAGE >/dev/null 2>&1 || docker pull $IMAGE >/dev/null || docker pull $IMAGE >/dev/null
docker rm -f market-thermo >/dev/null 2>&1 || true
docker run -d --name market-thermo --restart unless-stopped \
  -p $PORT:8600 --user 65534:65534 $DNS_ARGS -e TZ=CST-8 \
  -e THERMO_DATA=/data -e PYTHONUNBUFFERED=1 -e PYTHONDONTWRITEBYTECODE=1 \
  -e THERMO_NOTIFY_URL=$NOTIFY_URL -e THERMO_PUBLIC_URL=$PUBLIC_URL \
  -v $BASE/app:/app:ro -v $BASE/data:/data -w /app \
  --health-cmd 'wget -qO- http://127.0.0.1:8600/ >/dev/null || exit 1' --health-interval 60s \
  --log-opt max-size=5m --log-opt max-file=2 \
  $IMAGE python server.py >/dev/null
sleep 3
docker ps --filter name=market-thermo --format '容器状态: {{.Status}}'
if command -v curl >/dev/null; then curl -s -o /dev/null -w '本机访问: HTTP %{http_code}\n' http://127.0.0.1:$PORT/;
else wget -q -O /dev/null http://127.0.0.1:$PORT/ && echo '本机访问: OK'; fi"
echo "看板地址: $PUBLIC_URL"
