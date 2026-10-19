# 页面排版优化：设计与交接

写于 2026-10-19。代码已改完、本地验证通过、部署到线上并提交（同日）。本文件保留设计取舍、审查数据和验证记录；动前端前先读 AGENTS.md（规则）和 docs/DESIGN.md（设计取舍）。

## 1. 背景与用户用法

用户是个人投资者，执行“看好赛道 + 逢大跌分档建仓”（策略见知识库 `~/Documents/第二大脑/invest/策略/机器人-逢跌分档.md`）。每次打开看板要回答的问题，按决策顺序：

1. 市场冷不冷？（大盘温度、宽度、VIX / CNN。命令行工具 `工具/温度计.py` 也同时输出这几项，说明海外情绪是日常在看的）
2. 该动哪个标的？（信号评分排名，邮件提醒也是围绕它）
3. 逐个标的细看、管理自选（自选表、详情页）
4. 背景参考（走势图、美国财政）
5. 配置和说明（邮件提醒设置、指标说明），设一次后很少再看

## 2. 2026-10-19 对线上页面的审查结论

在 http://192.168.28.66:8600 上看了桌面 1440、平板 768、手机 375：

| # | 问题 | 数据 | 处理 |
|---|---|---|---|
| 1 | **手机上整页被撑到 578px 宽**（不是 375），整页缩小或横向滑动。原因：`.al-history` 是 grid 但没限定列宽，列宽被邮件标题撑开 | 在线上页面注入 `.al-history{grid-template-columns:minmax(0,1fr)}` 后 innerWidth 578 → 375 | 已改 CSS |
| 2 | 美国财政卡（318px 高）插在大盘和三个指数卡之间，把 A 股指数卡推到首屏以下（900px 高屏只露一半；手机上推到 2069px 处） | 已有数据 | 挪到走势之后 |
| 3 | 指标说明是两列 grid，同行等高造成大块空白（左列 277px 对右列 409px），新加的第 7 组落单；整块桌面 1902px、手机 3793px，始终展开 | 已有数据 | 改两列瀑布流 + 默认折叠 |
| 4 | 信号评分 14 行共 1267px，是最长的卡，下面自选表里又是同样 14 个标的 | 已有数据 | 默认只显示前 5 名 |

页面总高：桌面 6197px、手机约 10996px。

## 3. 排版原则与最终顺序

原则：**按使用频率和决策顺序排；低频内容默认折叠；首屏能看到信号评分前几名。**

| 顺序 | 区块 | 理由 |
|---|---|---|
| 1 | 大盘 / 宽度 / 海外情绪（hero 三卡） | 先判断市场冷不冷。海外情绪保留在这里：CNN / VIX 参与市场层面提醒（M 事件），用户日常看 |
| 2 | 三个指数卡 | 大盘温度的组成，紧跟仪表 |
| 3 | 信号评分（默认前 5） | 最该先看的行动清单 |
| 4 | 自选 + 详情页 | 逐个标的细看与管理；`#detail` 必须紧跟自选，点行时在表下方展开 |
| 5 | 走势 | 原来在信号评分前面，占 422px。指数卡自带温度小图（整段序列，约 500 个交易日），已能一眼看趋势，所以下移 |
| 6 | 美国财政 | 日更 / 月更的背景数据，不该占首屏 |
| 7 | 邮件提醒（折叠） | 配置，设一次后少看。卡头的状态和总开关常显，设置与记录折叠 |
| 8 | 指标说明（折叠） | 查阅用 |

## 4. 已写入工作区的改动（未提交）

注意：工作区同时包含已部署但未提交的“美国财政”卡片（`core.py` 的 `fiscal_*`、`server.py` 的 `get_fiscal`、`renderFiscal`、文档），以及本次排版改动。可以合并成一次提交。

- `static/index.html`
  - 区块顺序按上表调整
  - `#alerts`：卡头保留，其余包进 `<details class="fold" id="fold-alerts">`，summary 里有 `#al-sum`（最近一封邮件的时间和标题）
  - `#glossary`：`<details class="fold" id="fold-gloss">`，summary 即标题；7 个 `.g-group` 分在两个固定的 `.g-col` 里（左 4 组：温度、单项指标、市场宽度、买入评分；右 3 组：卖出评分、海外情绪、美国财政）
- `static/style.css`
  - `.glossary` 是两列 grid（≤860px 单列），列内各组自然堆叠。**不要改回 CSS `columns`**，原因见 §6
  - 新增 `.fold` 折叠样式（“展开 ▾ / 收起 ▴”）
  - `.al-history`、`.al-events`、`.opp` 增加 `grid-template-columns: minmax(0, 1fr)`（修手机溢出）；新增 `.opp-more`
- `static/app.js`
  - `OPP_TOP = 5`，`scoreState.all`（localStorage 键 `opp_all`）；`renderOpportunity` 只渲染前 N 条，超过时在列表末尾加 `[data-more]` 按钮；`#opp-body` 点击处理先判断按钮
  - `renderAlerts` 写 `#al-sum`
  - `bind()` 里给所有 `details.fold` 恢复和保存展开状态（localStorage 键 `fold_<id>`）
  - `screenshot()`：截图前临时展开所有折叠区、展开全部评分，`finally` 里还原（否则整页截图会缺内容，只截数据会只有前 5 名）

## 5. 待办（新会话执行）

### 5.1 验证（已完成，2026-10-19）
本地用 14 个标的的临时数据目录（端口 8601，没配 `THERMO_NOTIFY_URL`，没发过邮件）逐项验证，均通过：
- 评分默认 5 条，按钮“展开全部 14 个 ▾”↔“收起，只看前 5 名”；买入 / 过热切换后排序正确；刷新后记住展开状态
- 点评分行打开详情；点展开 / 收起按钮不会打开详情
- 两个折叠区展开、收起、刷新后记住；折叠状态下总开关可用（已确认写入服务端设置并还原）
- 截图：用桩替换下载动作，在 `toPng` 调用时检查页面状态。“整页”时折叠区全部展开、评分 14 条，截完还原；“只截数据”时 `#alerts` 和 `#glossary` 被隐藏、评分 14 条，截完还原；`toPng` 抛错、`renderOpportunity` 抛错两条失败路径下，`shooting` 类和 `busy` 都被清掉。另外逐像素核对整页 PNG：7 个指标说明分组都画出来了，位置与页面一致（见 §6 的分栏坑）
- 手机 375：`innerWidth` 为 375，无溢出，折叠后全页约 5158px（原约 10996px）；平板 768：无溢出，指标说明单列
- 指标说明两列：列内无空白，右列比左列短约 310px（7 组高矮不一的固有结果）；亮色、暗色下新增文字的对比度均 ≥ 4.7
- 1440×900 首屏：信号评分前 2 名完整可见，第 3 名露出大半

### 5.2 部署（已完成，2026-10-19）
已用 `./deploy/deploy.sh` 部署，`/api/health` 正常：`alert_loop_age` 个位数秒，`threads` 含 `alert_loop`、`warmer`，美国财政三项都有缓存。线上 `index.html`、`style.css`、`app.js` 已是新版本。以后部署的确认方式同上：
```bash
./deploy/deploy.sh
curl -s http://192.168.28.66:8600/api/health
```
`alert_loop_age` 应 <60 秒，`threads` 含 `alert_loop`、`warmer`。美国财政三项首次取数时可能撞上服务器 DNS 偶发失败（`Try again`），约 30 秒内会自动重试成功，不用处理。部署会重启容器，状态文件在数据目录里，不受影响。

### 5.3 文档同步与提交（已完成）
- `docs/DESIGN.md` 第 7 节已补：localStorage 键（`opp_all`、`fold_fold-alerts`、`fold_fold-gloss`）、区块顺序与理由、折叠区、截图时临时展开、不要用 CSS `columns`。AGENTS.md 里没有 localStorage 那句，其中提到的旧键（`tiers` 等）是另一件待办，没动
- 提交时 `.claude/` 不在 `.gitignore` 里（`data/` 在），所以没有用 `git add -A`，而是逐个文件暂存。以后要提交时同样注意

## 6. 验证方法与踩坑（本次踩到的）

- **`preview_start` 的 launch.json 传不进环境变量**：用 `env THERMO_DATA=… python3 …` 和 `bash -c "THERMO_DATA=… exec python3 …"` 两种写法都试过，服务始终读写项目的 `./data/`。后果：本次本地验证改动了 `data/alerts_state.json`（提醒基线）和 `data/breadth_history.csv`（追加了今天的宽度）。这两个文件本来就是本地测试数据，已 gitignore，本地没配 `THERMO_NOTIFY_URL`，没有发过邮件，也没动 `watchlist.json` 和 `settings.json`
- 要用 14 个标的测本地：先 `mkdir -p /tmp/thermo-data && cp data/watchlist.json /tmp/thermo-data/`，再 `THERMO_DATA=/tmp/thermo-data python3 server.py`，用浏览器打开 http://localhost:8600，在页面的搜索框里把自选加到 14 个。标的不必与线上一致，任意 A 股或 ETF 都行，只为凑够条数
  **不要把真实自选清单写进本文档或任何提交的文件**：仓库是公开的，自选属于个人数据（`data/` 被忽略就是这个原因）
  也可以直接只读检查线上页面；或者临时在本地 `data/watchlist.json` 里加标的，测完恢复成 `["sz159530", "sz980022"]`
- **截图不要用 CSS `columns` 做分栏**：现象（已用像素扫描确认）：用 `columns: 2` 时，整页 PNG 里分栏与页面不同，“买入评分”一组被挪到右列，“美国财政”一组整个没画出来，左列底部留下约 550px 空白，而页面上完全看不出问题。推测原因：html-to-image 把计算后的 `height` 写成内联固定值，截图渲染的文字高度只要比页面多几个像素，固定高度的多列容器就会重新分栏，放不下的组溢出到不可见的第三列。改成两个固定的 `.g-col` 后，PNG 里 7 组齐全，位置与页面一致
- **浏览器面板隐藏时 `computer` 的 screenshot 可能返回旧画面**（改了 DOM 也不变），不能当验证依据。验证截图内容的办法：把下载桩掉后拿到 PNG 的 data URL，画到 canvas 上用 `getImageData` 逐行扫描，例如按强调色找各分组标题的 y 位置，与页面里 `getBoundingClientRect` 量到的位置对比。`img.decode()` 在隐藏面板里可能永远不返回，改用 `onload`
- 本次起本地服务的办法：先 `mkdir -p` 一个临时数据目录并放好 `watchlist.json`，在终端面板里运行 `env -u THERMO_NOTIFY_URL THERMO_DATA=<目录> THERMO_PORT=8601 python3 server.py`，再用 `preview_start` 的 `url` 参数打开 http://localhost:8601 。不碰项目的 `./data/`，也不会误发邮件。测完关掉终端标签
- 判断手机溢出看 `innerWidth`：手机模拟下内容过宽时，布局视口会被撑大（本次是 578），`scrollWidth > innerWidth` 反而是 false
- 找溢出元素：筛选 `getBoundingClientRect().right > 视口宽度` 且不在 `.table-wrap` / canvas / svg 里的元素
- 视口模拟记得测完用 `resize_window` 的 `desktop` 预设还原
- 浏览器面板隐藏时图表动画不播放，见 DESIGN.md 第 7 节

## 7. 没做、可选
- 页面内锚点导航（粘性）：折叠后页面已明显变短，暂不需要；若觉得手机上仍长再加
- 合并“信号评分”和“自选”：两处标的重复，但职责不同（排名 vs 管理 / 全指标），改动大，暂不做
- `OPP_TOP = 5` 是经验值。更贴策略的做法是按分数阈值显示（只有 ≥60 才算有信号，且至少显示 3 条），需要用户确认再改
