# 设计说明

记录设计取舍和踩过的坑。代码能直接看出来的内容不在这里重复。

## 1. 架构

```
数据源（腾讯/东方财富/新浪/CBOE/CNN）
   └─ thermo/core.py      取数、指标、温度          ← thermo/cli.py（命令行）
        └─ server.py Cache（内存，过期先返回旧值）
             ├─ HTTP API ── static/（ECharts 看板）
             ├─ warmer 线程：预热总览与自选，宽度落盘
             └─ alert_loop 线程 ── thermo/alerts.py ── notify-service ── 邮件
   thermo/signals.py：评分与设置，API 和提醒共用
```

## 2. 数据源（坑最多的部分）

| 用途 | 源 | 要点 |
|---|---|---|
| 日 K（主） | 腾讯 `web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,800,qfq` | 前复权，约 3 年。个股返回 `qfqday`，指数返回 `day`。行数据可能带第 7 列（除权信息），只取前 6 列。`qt` 字段：[1] 名称 [3] 现价 [4] 昨收 [5] 开 [6] 量 [30] 时间 [32] 涨跌% [33] 高 [34] 低。**取不到国证指数（如 980022）的历史** |
| 日 K（备） | 东方财富 `push2his` | 有频率限制，连续请求后会被临时封（空回复，curl exit 52），只作备用 |
| 日 K（备） | 新浪 `quotes.sina.cn ... getKLineData` | 980022 只有 2024-12 以后的数据。**部分指数早已停更**（中证全指停在 2016 年），所以有 `STALE_DAYS=20` 过滤：最后一根 K 线超过 20 天的源一律丢弃；全部失败时抛异常，让缓存保留旧值 |
| 当日补齐 | 腾讯 `qt` | 历史里缺当天时用实时行情补一根；非腾讯历史与腾讯行情的成交量单位不同，此时量比置空（`mixed`） |
| 市场宽度 | 东方财富 `push2ex.../getTopicZDFenBu` | 一次返回涨跌分布。键 k：k>0 表示涨幅在 (k-1, k]%，k<0 同理，0 为平盘，±11 为涨跌停。**没有历史接口**，只能靠服务定时落盘积累 |
| VIX 历史 | CBOE `VIX_History.csv` | 约 0.5MB，从服务器下载极慢、常超时：落盘 `vix_history.json`，6 小时内直接读文件，下载失败也用文件；服务器上没有该文件时，部署脚本从 Mac 下载后上传 |
| VIX 实时 | 新浪 `hq.sinajs.cn/list=znb_VIX` | 必须带新浪 Referer；GBK 编码，只取数值字段 |
| CNN 恐惧贪婪 | `production.dataviz.cnn.io/index/fearandgreed/graphdata` | 必须带 `Accept` 请求头，否则返回 418。含 7 个分项和约 1 年历史 |
| 美国国债总额 | 财政部 FiscalData `v2/accounting/od/debt_to_penny`，取最近 400 个工作日 | 按日更新，滞后约 1 个工作日；一年前的值取不晚于“最新日期 − 365 天”的最后一条 |
| 美国财政收支 | 财政部 FiscalData `v1/accounting/mts/mts_table_9`（月度财政报告），取最近 40 行再筛出最新日期 | 每个日期 33 行，**收入合计和支出合计都叫 `Total`**，靠 `parent_id` 对应的分组名区分；金额是财年累计（美国财年 10 月起，9 月底即全年），含上年同期。表结构变了会抛 `ValueError`，不静默出错 |
| 10 年期美债收益率 | FRED `fredgraph.csv?id=DGS10`（近 3 年） | 滞后约 1 个交易日，假日为空值。**必须用 urllib 默认 UA**：浏览器 UA 和自定义 UA 都会被挂起到超时；财政部自家的 `home.treasury.gov` 收益率 CSV 从国内要 17 秒，所以不用 |
| 搜索 | 腾讯 `smartbox.gtimg.cn/s3/?v=2&t=all&q=` | 支持中文名、拼音首字母、代码片段。返回 `v_hint="市场~代码~名称(\uXXXX 转义)~拼音~类型^..."`，最多 10 条，混有场外基金（`jj`）、港股和美股；`core.search` 只保留 sh/sz/bj 市场里的 `GP-A*`、`ETF`、`LOF`、`QDII-*`、`ZS`，别名优先。服务端按查询词缓存 1 小时 |

- 中国波指 iVIX 2018 年起停发，A 股没有 VIX 对应物，所以海外情绪单独展示，不计入 A 股温度。
- 网络：`core.get()` 遇到连接错误或 DNS 失败会重试 2 次，HTTP 错误不重试。容器 DNS 用路由器 dnsmasq，阿里 DNS 兜底（Docker 默认的 8.8.8.8 在国内慢）。路由器上游含 8.8.8.8，会偶发 `EAI_AGAIN`。
- 代码识别：6 位代码按前缀分沪、深、北；`000xxx` 默认当作深市个股，指数需加 `sh`/`sz` 前缀或用别名（`core.ALIASES`）。

## 3. 温度

5 个指标各自在近 3 年（750 个有效值）里的滚动分位取平均：RSI14、MA60 乖离、20 日涨跌、距 250 日高点回撤、250 日区间位置。有效值少于 20 个时该项为 None，可用项少于 3 个时温度为 None。分位用 `bisect` 维护有序滑窗计算，单个标的约 0.3 秒。

标签：<10 冰点、<25 恐惧、<75 中性、<90 贪婪、≥90 过热。大盘温度 = 沪深300、中证1000、中证全指三者的平均。

## 4. 信号评分（thermo/signals.py）

| 买入机会 | 计算 | 过热卖出 | 计算 |
|---|---|---|---|
| 便宜度 cheap | 100 − 温度 | 过热度 hot | 温度 |
| 回撤档位 tier | 回撤 ÷ 最深档位，封顶 100 | 涨幅偏离 rise | 距一年低点涨幅 ÷ `rise_ref` |
| 企稳 stab | 站上 MA5、RSI 较 5 日低点回升 ×10、100−30×连跌天数，三项平均 | 滞涨 stall | 跌破 MA5、RSI 较 5 日高点回落 ×10、100−30×连涨天数，三项平均 |
| 环境 env | 100 − 大盘温度 | 环境 env | 大盘温度 |

- 默认权重 40/25/20/15，归一化后使用。标签：≥75 / ≥60 / ≥45。
- **门槛 `GATE=34`**：总分 ≥60 但企稳（或滞涨）低于 34，即三项条件一项都不满足时，买入侧标“待企稳”，卖出侧标“过热未见顶”。原因：大跌时便宜、档位、环境三项会同时接近满分，没有门槛的话连跌 7 天的标的也会被标成“信号强”。
- 环境项对所有标的相同，只影响分数高低，不影响排名。
- 设置存在服务器的 `data/settings.json`（结构见 `DEFAULT_SETTINGS`，通过 `validate()` 规整），`/api/watchlist` 每次请求都按当前设置重算 `signals`，所以改权重会立即生效。前端只做展示。

## 5. 缓存与后台线程（server.py）

- TTL：盘中 60 秒，其他时间 15 分钟。过期时先返回旧值，同时在后台刷新；后台刷新超过 300 秒视为卡住，允许重新发起。`force=True`（`?refresh=1`）时同步拉取，失败则返回旧值。
- 美国财政三项（`fiscal_debt`/`fiscal_budget`/`fiscal_yield`）更新慢，TTL 分别 6 小时、12 小时、2 小时，且 `block=False`：没有缓存时总览直接返回 None，后台加载，不拖慢首页；刷新按钮不强制刷新它们。取不到时前端只让对应那一列显示“加载中/获取失败”。“大白话”那句由 `core.fiscal_plain` 按已有的数据拼出，缺哪项略过哪项。
- VIX 历史使用 `block=False`：没有缓存时直接返回 None 并在后台加载，此时 VIX 先只给实时值。
- warmer：盘中每 30 秒、其他时间每 5 分钟预热一次；有失败项时 30 秒后重试。市场宽度在获取时顺带写入 `breadth_history.csv`（同一天覆盖写）。
- `/api/health`：各缓存的数据年龄、后台刷新状态、线程列表、`alert_loop_age`。

## 6. 邮件提醒（thermo/alerts.py）

事件：B1 档位触达、B2 买入信号、B3 进入冰点、S1 止盈信号、S2 进入过热、S3 过热后回落 10%、M 市场层面。默认关闭 B3 和 S3。

| 阶段 | 时间（工作日） | 检查内容 |
|---|---|---|
| intraday | 9:40–11:30、13:00–14:55，每 10 分钟 | 只查价格类事件 B1、B3、S2、S3 |
| preview | 14:45 | 全部标的事件 |
| close | 15:10 | 全部标的事件 + 市场事件 + 14:45 预警的收盘复核 |

设计要点：
- **只在状态变化时触发**，不按状态水平反复提醒。B1 每档每轮只报一次，创一年新高后重置；B3 和 S2 有滞回（冰点 <10 进入、>25 退出；过热 ≥90 进入、<75 退出）；B2 和 S1 是标签等级从 <2 升到 ≥2 时触发。
- **B2 和 S1 不在盘中判断**：它们依赖 MA5 和 RSI，盘中会来回跳；而且一次误报会触发冷却期，把之后真正成立的信号压下去。14:45 已过 94% 交易时间，又能在 14:57 收盘集合竞价前留出操作时间；15:10 留出数据源更新收盘价的余量。
- 冷却：B2、B3、S1、S2 按“标的 + 事件”冷却 `cooldown_days`（默认 7 天）。
- **基线**：第一次见到某个标的或市场时只记录当前状态、不提醒，避免上线当天把已经满足的条件一次全报出来。预演的 `fresh=1` 会把所有标的当作从零开始，用来查看当前满足哪些条件。
- **宁可漏报**：数据失败或不是当天数据时跳过；以沪深300 最新 K 线日期等于今天作为交易日判断（节假日自然跳过）。
- 一次检查最多发一封邮件，合并所有事件；标题取优先级最高的事件（B2/S1 > B1/S2 > S3 > B3 > M）。
- 关闭的事件、静音的标的照样更新状态，这样重新开启时不会把积压的事件一起报出来。
- 状态文件 `alerts_state.json`：`sym.{peak, hit, buy_lv, sell_lv, zone, hot_peak, s3_done}`、`market.{zone, breadth, cnn}`、`last_sent`、`preview_sent`、`runs`（防止重启后同一阶段重复运行）。发送记录写在 `alerts_history.json`，保留最近 300 条。

**发信通道**：`POST {THERMO_NOTIFY_URL}/v1/notify`，请求体 `{source_id: "market-thermo", level: "info", template: "alert", data: {summary, detail, source}}`。notify 的 alert 模板只渲染标题、摘要、详情、来源四个字段（详情里的换行会转成 `<br>`），所以邮件正文是排好版的纯文本。只发主邮箱，依赖服务器上 notify 配置里新增的 `email_stock` 通道和 `source_id` 路由规则（片段见 README）。`send()` 检查结果里至少有一个非 log 通道显示 delivered，否则记为失败。待办：level 改为自定义的 `stock` 作为防错发兜底。

## 7. 前端（static/）

- 单文件 `app.js`，状态在 `state` 和 `scoreState` 里。浏览器 localStorage 只存界面偏好：`opp_mode`（买卖标签）、`tab`（走势标签）、`opp_all`（信号评分是否展开全部）、`fold_fold-alerts` 和 `fold_fold-gloss`（两个折叠区的开合，键名是 `fold_` 加折叠区的 id）。
- 区块顺序按使用频率和决策顺序排：大盘 / 宽度 / 海外情绪、三个指数卡、信号评分、自选与详情（`#detail` 必须紧跟自选表，点行时在表下方展开）、走势、美国财政、邮件提醒、指标说明。低频内容默认折叠。取舍和审查数据见 [LAYOUT-REDESIGN.md](LAYOUT-REDESIGN.md)。
- 折叠区是 `details.fold`：邮件提醒只折叠设置和记录，卡头的状态与总开关常显；指标说明整块折叠。信号评分默认只显示前 `OPP_TOP`（5）名，列表末尾的 `[data-more]` 按钮展开全部。`.al-history`、`.al-events`、`.opp` 这几个 grid 必须写 `grid-template-columns: minmax(0, 1fr)`，否则邮件标题等长内容会撑宽整页（手机上曾被撑到 578px）。
- 深度链接 `#sym=sh562500` 可以直接打开详情，邮件里的链接用的就是它。
- 添加自选的搜索框是 ARIA combobox：输入停顿 200ms 后发起搜索，`sug.seq` 保证只渲染最后一次输入的结果；默认选中第一条，回车添加；已在自选中的选项标注出来，选中后只打开详情。列表项用 `mousedown` 选中，因为 click 触发前输入框已经失焦、列表会先被关掉；失焦后 150ms 关闭，重新聚焦且内容没变时恢复列表。
- 截图使用 html-to-image：给 body 加 `shooting` 类，隐藏按钮和 `.no-shot` 元素，并把顶栏改为静态；“只截数据”模式再加 `shooting-data` 类，隐藏 `#alerts` 和 `#glossary`。图片像素控制在约 1600 万以内（Safari 画布上限），超出时自动降低分辨率。**等待布局刷新用 `setTimeout`，不用 `requestAnimationFrame`**，因为页面不在前台时它不会触发。截图前会临时展开所有 `details.fold` 并展开全部评分，`finally` 里还原（用 try 包住，出错也会还原）。**截图里不要用 CSS `columns` 分栏**：html-to-image 会把计算后的 `height` 固定下来，多列容器在截图里会重新分栏，整页 PNG 曾因此丢掉“美国财政”一组说明，页面上却看不出来。指标说明因此用两个固定的 `.g-col`（grid 两列），不用 `columns`。
- 验证时的坑：浏览器面板隐藏时视口宽度可能是 0，截图会全黑，ECharts 动画也不会播放，导致折线缺失。这时改用 `resize_window` 模拟视口，加 `javascript_tool` 读取 DOM 来验证；需要看截图效果时，先关掉图表动画。

## 8. 部署（deploy/deploy.sh）

- iStoreOS 上没有 Python 和 systemd，所以用 Docker 运行，镜像 `docker.m.daocloud.io/library/python:3.12-alpine`。Docker Hub 在国内连不上，DaoCloud 偶尔返回 502，脚本里重试一次。
- 代码以只读方式挂载（`/mnt/data/market-thermo/app` → `/app`），数据以读写方式挂载（`/mnt/data/market-thermo/data` → `/data`）。容器以 nobody（65534）运行，配置 `--restart unless-stopped`，dockerd 开机自启。
- 容器参数：`TZ=CST-8`（alpine 没有 tzdata；代码里统一用固定的 +8 时区 `core.CN_TZ`）、`--dns` 指向路由器加 223.5.5.5、日志轮转 5MB×2、`THERMO_NOTIFY_URL`、`THERMO_PUBLIC_URL`。
- SSH 公钥在 `/etc/dropbear/authorized_keys`。
- 脚本参数都可以用环境变量覆盖，默认值对应作者的服务器。用 `${VAR-默认}`（不带冒号）实现“设为空字符串即关闭”：`THERMO_DNS=` 表示用 Docker 默认 DNS，`THERMO_NOTIFY_URL=` 表示不发邮件。默认 DNS 用的是服务器自己的 IP，只适用于本身就是路由器的机器；部署到普通服务器时必须覆盖。
- VIX 历史预下载失败不会中断部署：先写到临时文件，非空时才替换。

## 9. 持久化数据

| 位置 | 内容 |
|---|---|
| 服务器 `/mnt/data/market-thermo/data/` | `watchlist.json` 自选、`settings.json` 设置（首次修改后才生成）、`breadth_history.csv` 宽度历史、`vix_history.json`、`alerts_state.json`、`alerts_history.json` |
| 服务器 notify 配置 | `/opt/personal-super-assistant/deploy/config/notify_channels.yaml`，备份为 `.bak-20261016-market-thermo` |
| notify-service 数据库 | 每次通知请求都会写入它自己的 Postgres（未查看过） |
| 浏览器 | 只存界面偏好 |
| Mac | `./data/`（本地测试用，不同步到服务器）、知识库 `invest/工具/数据/宽度历史.csv`（命令行运行时写入） |

服务器数据目前没有备份，见待办。
