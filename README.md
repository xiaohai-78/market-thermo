# market-thermo 市场温度看板

A 股大盘与自选标的的恐惧/冰点看板，附美股 VIX 与 CNN 恐惧贪婪指数。只依赖 Python 3.7+ 标准库，前端使用 ECharts（CDN 加载）。

接手开发先读 [AGENTS.md](AGENTS.md)（运行环境、规则、待办）和 [docs/DESIGN.md](docs/DESIGN.md)（设计取舍与踩坑记录）。

## 结构
- `thermo/core.py`：数据获取与温度计算（命令行和看板共用）
- `thermo/cli.py`：命令行版，`python3 -m thermo.cli 159530 国证机器人`
- `server.py`：HTTP 服务与 API，后台每 10 分钟采集一次市场宽度
- `thermo/signals.py`：买入机会 / 过热卖出评分与看板设置（档位、权重、提醒开关，存 `data/settings.json`），看板与邮件提醒共用；方法说明见知识库 `invest/方法/机会评分.md`、`过热评分.md`
- `thermo/alerts.py`：买卖点提醒引擎（状态机、冷却、合并发送），经 notify-service 发邮件；规则见知识库 `invest/方法/提醒规则.md`
- `static/`：前端页面
- `deploy/`：systemd 单元与部署脚本

## 本地运行
```
python3 server.py            # http://localhost:8600
```

## 部署
```
./deploy/deploy.sh root@192.168.28.66
```
- 服务器是 iStoreOS（OpenWrt，aarch64），没有 Python 和 systemd，所以用 Docker 运行：容器名 `market-thermo`，镜像 `python:3.12-alpine`（经 DaoCloud 镜像站拉取），`--restart unless-stopped`
- 代码在 `/mnt/data/market-thermo/app`（以只读方式挂载），数据在 `/mnt/data/market-thermo/data`（自选列表、宽度历史、VIX 历史）
- 容器 DNS 使用路由器的 dnsmasq，阿里 DNS 兜底；以 nobody 身份运行
- 排查：`/api/health` 查看各缓存的数据年龄和刷新状态；`docker logs market-thermo` 查看刷新失败和慢刷新

## 运行机制
- 后台每 30 秒（盘中）或 5 分钟（休市）预热一次总览和自选，有失败项时 30 秒后重试
- 缓存过期时先返回旧值，后台再刷新；刷新超过 5 分钟视为卡住，允许重新发起
- VIX 历史（CBOE）从服务器下载常超时：落盘到数据目录，6 小时内直接读文件，下载失败时也用文件；远端没有文件时，部署脚本从本机下载后上传

## API
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/overview` | 大盘温度、宽度、VIX、CNN |
| GET | `/api/watchlist` | 自选列表与摘要 |
| POST | `/api/watchlist` | `{"code": "159530"}` 添加自选 |
| DELETE | `/api/watchlist?code=sz159530` | 移除自选 |
| GET | `/api/symbol?code=159530&days=500` | 单个标的的完整序列 |
| GET | `/api/health` | 缓存年龄与刷新状态 |
| GET / PUT | `/api/settings` | 档位、权重、提醒设置（PUT 为部分合并） |
| GET | `/api/alerts` | 提醒记录、状态与下次检查时间 |
| POST | `/api/alerts/preview?phase=close&fresh=1` | 预演：不发送、不改状态 |
| POST | `/api/alerts/test` | 发送测试邮件 |

## 邮件提醒依赖 notify-service
提醒经 personal-super-assistant 的 notify-service（`http://192.168.28.66:8081/v1/notify`，`source_id=market-thermo`）发送。为了只发主邮箱，2026-10-16 在服务器的 `/opt/personal-super-assistant/deploy/config/notify_channels.yaml` 做了两处改动（原文件备份为同目录 `.bak-20261016-market-thermo`）：

```yaml
channels:
  email_stock:                 # 新增：与 email 相同，只是收件人只有 SMTP_TO
    enabled: ${EMAIL_ENABLED:-false}
    type: smtp
    smtp: {host: "${SMTP_HOST}", port: 465, username: "${SMTP_USERNAME}", password: "${SMTP_PASSWORD}",
           from: "${SMTP_FROM}", to: ["${SMTP_TO}"], use_html: true}
routing:
  rules:
    - source_id: market-thermo # 新增，必须放在按 level 匹配的规则之前
      channels: [email_stock, log]
```

**该文件由 personal-super-assistant 仓库管理，需要把同样的改动提交进仓库**，否则下次部署那个项目时会被覆盖，股票提醒会改发两个邮箱。

任一 GET 请求加 `refresh=1` 可绕过缓存。缓存时长：盘中 60 秒，其他时间 15 分钟；数据源失败时返回上一次的结果。

## 数据源注意
- 东方财富 `push2` 和 `push2his` 有频率限制，连续请求会被临时封禁，所以 K 线以腾讯为主
- 腾讯接口没有国证系列指数（如 980022）的历史数据，这部分走新浪
- 市场宽度没有历史接口，只能靠服务定时采集来积累
- 新浪的部分指数数据早已停更（如中证全指停在 2016 年）：最后一根 K 线超过 20 天的数据源一律丢弃
