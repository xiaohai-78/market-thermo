#!/usr/bin/env bash
# 部署到局域网服务器（iStoreOS，无 Python/systemd，用 Docker 运行）：./deploy/deploy.sh [user@host]
# 需要已配置 SSH 密钥登录。代码以只读方式挂载进容器，数据目录不会被覆盖。
# 容器默认 DNS 为 8.8.8.8，国内解析慢；改用路由器 dnsmasq，阿里 DNS 兜底（其偶发解析失败）。
set -euo pipefail
HOST="${1:-root@192.168.28.66}"
BASE=/mnt/data/market-thermo
IMAGE=docker.m.daocloud.io/library/python:3.12-alpine   # Docker Hub 在国内不可达，走 DaoCloud 镜像
cd "$(dirname "$0")/.."

COPYFILE_DISABLE=1 tar czf - --no-xattrs --exclude __pycache__ --exclude .DS_Store thermo static server.py \
  | ssh "$HOST" "rm -rf $BASE/app.new && mkdir -p $BASE/app.new $BASE/data && tar xzf - -C $BASE/app.new \
      && rm -rf $BASE/app && mv $BASE/app.new $BASE/app && chown -R 65534:65534 $BASE/data"

# CBOE 从服务器下载常超时：远端没有 VIX 历史时，从本机下载后传过去
if ! ssh "$HOST" "test -s $BASE/data/vix_history.json"; then
  python3 -c "import json; from thermo import core; print(json.dumps(core.vix_history()))" \
    | ssh "$HOST" "cat > $BASE/data/vix_history.json && chown 65534:65534 $BASE/data/vix_history.json"
fi

ssh "$HOST" "docker image inspect $IMAGE >/dev/null 2>&1 || docker pull $IMAGE >/dev/null || docker pull $IMAGE >/dev/null
docker rm -f market-thermo >/dev/null 2>&1 || true
docker run -d --name market-thermo --restart unless-stopped \
  -p 8600:8600 --user 65534:65534 \
  --dns ${HOST#*@} --dns 223.5.5.5 -e TZ=CST-8 \
  -e THERMO_DATA=/data -e PYTHONUNBUFFERED=1 -e PYTHONDONTWRITEBYTECODE=1 \
  -e THERMO_NOTIFY_URL=http://${HOST#*@}:8081 -e THERMO_PUBLIC_URL=http://${HOST#*@}:8600 \
  -v $BASE/app:/app:ro -v $BASE/data:/data -w /app \
  --health-cmd 'wget -qO- http://127.0.0.1:8600/ >/dev/null || exit 1' --health-interval 60s \
  --log-opt max-size=5m --log-opt max-file=2 \
  $IMAGE python server.py >/dev/null
sleep 3
docker ps --filter name=market-thermo --format '容器状态: {{.Status}}'
curl -s -o /dev/null -w '本机访问: HTTP %{http_code}\n' http://127.0.0.1:8600/"
echo "看板地址: http://${HOST#*@}:8600"
