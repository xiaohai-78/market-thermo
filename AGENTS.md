# market-thermo · 给 AI agent 的说明

A 股市场温度看板：大盘与自选标的的恐惧/冰点判断、买入机会/过热卖出评分、买卖点邮件提醒。用户是个人投资者，执行“看好赛道 + 逢大跌分档建仓”的策略。设计细节与取舍见 `docs/DESIGN.md`，改动前先读。

## 运行环境
- 线上：局域网服务器 `root@192.168.28.66`（iStoreOS/OpenWrt，aarch64），Docker 容器 `market-thermo`，看板 http://192.168.28.66:8600 。SSH 用 Mac 上的 `~/.ssh/id_ed25519` 免密登录。
- 本地：Mac 系统自带 Python 3.9.6；线上容器为 Python 3.12。**只用标准库，保持 3.7+ 兼容**（不用 `match`、`X | Y` 类型标注、`zoneinfo`）。
- 前端：原生 JS + ECharts 5.5.1 + html-to-image 1.11.11（都走 npmmirror CDN，jsdelivr 兜底），无构建步骤。

## 常用命令
- 本地运行：`python3 server.py`（http://localhost:8600 ，数据写 `./data/`；未设 `THERMO_NOTIFY_URL`，不会发邮件）
- 命令行：`python3 -m thermo.cli 159530 国证机器人`
- 部署：`./deploy/deploy.sh`（打包代码 → 替换服务器 `/mnt/data/market-thermo/app` → 重建容器，数据目录不动）。默认值就是作者的服务器；部署到别的环境用 `THERMO_BASE`、`THERMO_IMAGE`、`THERMO_PORT`、`THERMO_DNS`、`THERMO_NOTIFY_URL`、`THERMO_PUBLIC_URL` 覆盖，见 README
- 部署后验证：`curl http://192.168.28.66:8600/api/health`，`alert_loop_age` 应 <60 秒，`threads` 含 `alert_loop`、`warmer`
- 提醒预演（不发送、不改状态）：`curl -X POST 'http://192.168.28.66:8600/api/alerts/preview?phase=close&fresh=1'`

## 规则
1. **不要在测试中发真实邮件。** 验证提醒用预演接口或本地服务；`/api/alerts/test` 和直接调 notify 会真的发信，需用户同意。
2. **不存用户的持仓、成本、盈亏或券商截图**，自选只存代码。`data/` 含个人自选与提醒记录，已在 `.gitignore`，不要提交。
3. **不给投资建议。** 评分和提醒都表述为“按规则触发”，不写“建议买入/卖出”。
4. **A 股配色：红涨绿跌。** 温度配色冷蓝热红；买入侧蓝色系，卖出侧暖色系。
5. 界面文案用中文。评分逻辑只在后端 `thermo/signals.py`，前端只展示，不要在 JS 里再实现一遍公式。
6. 服务器上的 `notify_channels.yaml` 属于用户另一个项目 personal-super-assistant，改动前先问用户；它以单文件方式挂载进容器，必须原地改写（保持 inode）。
7. 浏览器面板隐藏时 `requestAnimationFrame` 不触发、ECharts 动画不播放：截图或图表的验证结果可能是假象，参见 DESIGN.md。
8. 服务器是 BusyBox 环境：没有 `diff`、`python`，`grep` 不支持 `--include`。

## 相关位置
- 用户知识库（方法论与策略文档）：`~/Documents/第二大脑/invest/`
  - `方法/机会评分.md`、`方法/过热评分.md`、`方法/提醒规则.md`、`方法/入场-回撤分档.md`、`方法/卖出-止盈证伪再平衡.md`
  - `策略/机器人-逢跌分档.md`（参数待回测、多项待用户确认）
  - `工具/温度计.py` 是本项目 `thermo.cli` 的薄入口，依赖本项目路径
- 修改评分或提醒规则时，同步更新知识库对应的方法文档。

## 当前进度与待办（2026-10-21）
已完成：看板、温度与宽度、VIX/CNN、美国财政卡片（国债、利息负担、10 年期收益率）、信号评分（买/卖）、邮件提醒（10-16 起运行，当天 9:40 已记录基线）、截图、自选模糊搜索、页面排版优化（10-19 已部署，见 docs/LAYOUT-REDESIGN.md）、提醒优化（10-21 已部署：B2 要求站上 MA5、事件带风险标记、盘中触档收盘复核、复核全部确认时不单独发邮件、每周五收盘快照）、部署与 git 仓库。

待办（按优先级）：
- [ ] 第一个周五（10-23）15:10 之后看一眼周报邮件的内容与排版；平时可用 `POST /api/alerts/preview?phase=weekly&fresh=1` 预演
- [ ] 数据源降级保护：腾讯 K 线接口不可用时（10-21 凌晨返回 501）会退到新浪，新浪的 K 线与腾讯的前复权数据对不上，且没有名称（实测半导体ETF鹏华回撤 -73.2%，腾讯前复权为 -46.4%；大洋电机 -52.0% 对 -51.2%；名称显示成代码）。盘中若遇到同样情况，提醒可能按错误的回撤触发；可考虑降级时跳过价格类提醒，或在提醒里标注数据源
- [ ] notify 防错发：`alerts.send()` 的 level 从 `info` 改为自定义的 `stock`（notify 不校验 level）。路由规则若被覆盖，消息只进日志，不会错发到两个邮箱
- [ ] 服务器 `data/` 每日备份回 Mac（宽度历史无法补抓）
- [ ] `/api/alerts/test` 限频；启动时清理浏览器里早期遗留的 localStorage 键 `tiers`、`weights`、`weights_sell`、`rise_ref`
- [ ] 回测：档位、评分权重、企稳/滞涨门槛、冷却天数目前都是经验值（知识库文档标了“待确认”）
- [ ] 节假日：页面“盘中”状态与缓存 TTL 只按工作日判断（提醒有当日行情校验，不会误发）
- [ ] 服务健康感知：数据源连续失败告警（每周快照只列自选状态，不含服务健康）
- [ ] 截图在 Safari 和手机上未验证；提醒时间（14:45、15:10）可配置化
- [ ] 用户侧：把 notify 配置改动同步进 personal-super-assistant 仓库（见 README）
