# market-thermo 市场温度看板

A 股大盘与自选标的的恐惧/冰点看板：
- **温度**：5 项技术指标在自身近 3 年历史中的分位均值（0–100），判断大盘和标的处在冰点、恐惧、中性、贪婪还是过热
- **市场宽度**：全市场涨跌家数、涨跌停、涨跌幅分布
- **海外情绪**：美股 VIX、CNN 恐惧贪婪指数（单独展示，不计入 A 股温度）
- **美国财政**：国债总额、净利息占财政收入（对比国防支出）、10 年期美债收益率，卡片最下面附一句由当前数字拼出的大白话；数据来自美国财政部 FiscalData 和 FRED，免密钥，不计入 A 股温度
- **信号评分**：按“逢跌分档”规则给自选打买入机会分和过热卖出分
- **邮件提醒**：买卖点状态变化时发邮件（可选）
- **其他**：自选模糊搜索、详情图（价格、档位线、温度）、页面截图

后端只依赖 Python 3.7+ 标准库，前端是原生 JS + ECharts（CDN 加载），不需要安装任何依赖，也没有构建步骤。

> 仅供参考，不构成投资建议。

接手开发先读 [AGENTS.md](AGENTS.md)（运行环境、规则、待办）和 [docs/DESIGN.md](docs/DESIGN.md)（设计取舍与踩坑记录）。

## 快速开始（本地体验）

需要 Python 3.7+，以及能访问国内行情接口（腾讯、东方财富、新浪）的网络。

```bash
git clone https://github.com/xiaohai-78/market-thermo.git
cd market-thermo
python3 server.py
```

打开 http://localhost:8600 。第一次加载要拉取约 3 年的历史数据，需要几秒。数据（自选、设置、宽度历史等）写在 `./data/`，这个目录不进 git。

也可以只用命令行：

```bash
python3 -m thermo.cli                  # 只看大盘
python3 -m thermo.cli 159530 600519    # 大盘 + 标的（6 位代码；指数加前缀，如 sh000300）
```

## 部署到服务器（Docker）

部署脚本在本机执行：把代码打包传到服务器，再用官方 Python 镜像起一个容器，服务器上不需要装 Python。

### 1. 准备
- **服务器**：Linux（x86_64 或 arm64）并装好 Docker，远端需要有 `tar`，以及 `curl` 或 `wget` 中的一个。要能访问国内行情接口；访问不了 CBOE、CNN 只影响海外情绪模块
- **本机**：bash、ssh、tar、Python 3.7+（用来预先下载 VIX 历史）
- **SSH 免密登录**，建议用 root，因为脚本要 `chown` 数据目录：

```bash
ssh-keygen -t ed25519
```

```bash
ssh-copy-id root@your-server
```

### 2. 部署
脚本参数的默认值是作者自己的服务器（局域网内一台 iStoreOS 路由器），**新环境请用环境变量覆盖**。一个比较通用的例子：

```bash
THERMO_BASE=/opt/market-thermo THERMO_IMAGE=python:3.12-alpine THERMO_DNS= THERMO_NOTIFY_URL= ./deploy/deploy.sh root@your-server
```

| 环境变量 | 作用 | 默认值（作者的环境） | 新环境建议 |
|---|---|---|---|
| `THERMO_BASE` | 远端目录：代码在 `$BASE/app`，数据在 `$BASE/data` | `/mnt/data/market-thermo` | 任意有空间的目录，如 `/opt/market-thermo` |
| `THERMO_IMAGE` | Python 镜像 | `docker.m.daocloud.io/library/python:3.12-alpine` | 能访问 Docker Hub 的话用 `python:3.12-alpine`；在国内可保留 DaoCloud 镜像站 |
| `THERMO_PORT` | 宿主机端口 | `8600` | 按需修改 |
| `THERMO_DNS` | 容器 DNS，多个用空格分隔；**空字符串表示用 Docker 默认** | `<服务器IP> 223.5.5.5` | **必须改**：除非服务器本身提供 DNS，否则设为空，或设成 `223.5.5.5 119.29.29.29` |
| `THERMO_NOTIFY_URL` | 邮件通知服务地址；**空字符串表示不发邮件**（提醒只记录在看板上） | `http://<服务器IP>:8081` | 没有通知服务就设为空，见下文“邮件提醒” |
| `THERMO_PUBLIC_URL` | 邮件里的看板链接 | `http://<服务器IP>:<端口>` | 一般不用改 |

脚本会：
1. 上传代码，替换 `$BASE/app`，数据目录保持不动
2. 远端没有 VIX 历史时，从本机下载后上传。失败也不影响部署，服务启动后会在后台重试
3. 拉取镜像，以 nobody 身份启动容器 `market-thermo`，设置 `--restart unless-stopped`，日志轮转为 5MB×2

### 3. 检查
- 打开 `http://your-server:8600`
- 查看健康状态。正常情况下，`threads` 里有 `alert_loop` 和 `warmer`，`alert_loop_age` 小于 60 秒：

```bash
curl -s http://your-server:8600/api/health
```

- 查看日志，数据源失败和慢刷新会记录在这里：

```bash
ssh root@your-server docker logs --tail 50 market-thermo
```

### 4. 更新与回滚
更新就是拉取最新代码，再用同样的环境变量重新执行部署脚本。回滚则是先 `git checkout <提交号>` 再部署。两种情况都不会动数据目录。

```bash
git pull && THERMO_BASE=/opt/market-thermo THERMO_DNS= THERMO_NOTIFY_URL= ./deploy/deploy.sh root@your-server
```

### 不用 Docker
任何装有 Python 3.7+ 的机器都可以直接运行。用 `THERMO_DATA` 指定数据目录，再交给 systemd、supervisor 或 `nohup` 托管：

```bash
THERMO_DATA=/var/lib/market-thermo THERMO_PORT=8600 python3 server.py
```

服务端可用的环境变量有：`THERMO_HOST`（默认 0.0.0.0）、`THERMO_PORT`（默认 8600）、`THERMO_DATA`（默认 `./data`）、`THERMO_NOTIFY_URL`、`THERMO_PUBLIC_URL`。日志里的时间按服务器时区显示，而交易时段的判断在代码里固定使用北京时间。

## 邮件提醒（可选）

提醒引擎（`thermo/alerts.py`）在工作日的盘中每 10 分钟、14:45 和 15:10 检查自选，只在状态变化时提醒，规则见 [docs/DESIGN.md](docs/DESIGN.md) 第 6 节。发信不在本项目里实现，而是调用一个兼容的通知服务：

```
POST {THERMO_NOTIFY_URL}/v1/notify
{"source_id": "market-thermo", "level": "info", "title": "...", "template": "alert",
 "data": {"summary": "...", "detail": "多行纯文本", "source": "market-thermo"}}
```

响应需包含 `{"results": [{"Channel": "...", "Status": "delivered"}, ...]}`，并且至少有一个非 `log` 的通道显示 `delivered`，才算发送成功。不配置 `THERMO_NOTIFY_URL` 时，看板的“邮件提醒”区会显示“未配置 notify 服务，只记录不发送”，其他功能不受影响。

**自己实现通知服务时**，只要提供上面这个接口，把 `title` 当作邮件标题，把 `summary` 和 `detail` 当作正文发出去即可。可以用 SMTP、企业微信、Server 酱，任何渠道都行。

### 作者的环境：复用 personal-super-assistant 的 notify-service
提醒经 `http://192.168.28.66:8081/v1/notify` 发送。为了只发主邮箱，2026-10-16 在服务器的 `/opt/personal-super-assistant/deploy/config/notify_channels.yaml` 做了两处改动（原文件备份为同目录 `.bak-20261016-market-thermo`）：

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

## 数据与备份

| 文件（在数据目录下） | 内容 |
|---|---|
| `watchlist.json` | 自选代码 |
| `settings.json` | 档位、评分权重、提醒设置（第一次在页面上修改后才生成） |
| `breadth_history.csv` | 每日市场宽度。**没有历史接口，无法补抓，建议备份** |
| `vix_history.json` | VIX 历史缓存 |
| `alerts_state.json`、`alerts_history.json` | 提醒的基线、冷却、发送记录 |

备份示例：

```bash
scp -r root@your-server:/opt/market-thermo/data ./backup-$(date +%F)
```

## 项目结构
- `thermo/core.py`：数据获取、温度计算、模糊搜索（命令行和看板共用）
- `thermo/signals.py`：买入机会 / 过热卖出评分与看板设置，看板与邮件提醒共用
- `thermo/alerts.py`：买卖点提醒引擎（状态机、冷却、合并发送）
- `thermo/cli.py`：命令行版
- `server.py`：HTTP 服务与 API、缓存、后台预热与提醒调度
- `static/`：前端页面
- `deploy/deploy.sh`：Docker 部署脚本

## 运行机制
- 后台每 30 秒（盘中）或 5 分钟（休市）预热一次总览和自选，有失败项时 30 秒后重试；顺带每次写入当天的市场宽度
- 缓存时长：盘中 60 秒，其他时间 15 分钟。过期时先返回旧值，后台再刷新；刷新超过 5 分钟视为卡住，允许重新发起；数据源失败时返回上一次的结果。任一 GET 请求加 `refresh=1` 可绕过缓存

## API
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/overview` | 大盘温度、宽度、VIX、CNN |
| GET | `/api/watchlist` | 自选列表、摘要与信号评分 |
| POST | `/api/watchlist` | `{"code": "159530"}` 添加自选 |
| DELETE | `/api/watchlist?code=sz159530` | 移除自选 |
| GET | `/api/symbol?code=159530&days=500` | 单个标的的完整序列 |
| GET | `/api/search?q=芯片` | 模糊搜索（名称 / 拼音首字母 / 代码片段），最多 10 条 |
| GET | `/api/health` | 缓存年龄、线程与提醒调度心跳 |
| GET / PUT | `/api/settings` | 档位、权重、提醒设置（PUT 为部分合并） |
| GET | `/api/alerts` | 提醒记录、状态与下次检查时间 |
| POST | `/api/alerts/preview?phase=close&fresh=1` | 预演：不发送、不改状态 |
| POST | `/api/alerts/test` | 发送测试邮件 |

## 数据源注意
- 东方财富 `push2` 和 `push2his` 有频率限制，连续请求会被临时封禁，所以 K 线以腾讯为主
- 腾讯接口没有国证系列指数（如 980022）的历史数据，这部分走新浪
- 新浪的部分指数数据早已停更（如中证全指停在 2016 年）：最后一根 K 线超过 20 天的数据源一律丢弃
- 市场宽度没有历史接口，只能靠服务定时采集来积累
- FRED（`fredgraph.csv`）对浏览器 UA 和自定义 UA 会挂起不返回，只有 urllib 默认 UA 正常，所以 `core.get(ua=None)` 不设 UA；财政部 FiscalData 没有这个问题

更多细节见 [docs/DESIGN.md](docs/DESIGN.md)。
