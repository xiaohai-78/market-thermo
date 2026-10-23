"""买卖点提醒：按状态变化触发，经 notify-service 发邮件。

事件（规则详见知识库 invest/方法/提醒规则.md）:
  B1 档位触达  B2 买入信号成立  B3 进入冰点
  S1 止盈信号成立  S2 进入过热  S3 过热后回落 10%
  M  市场层面：大盘冰点/过热、宽度极度恐慌/狂热、CNN 极度恐惧/贪婪
  W  每周快照：不是状态变化，而是每周五收盘后把自选的当前状态整理成一封邮件
B2 要求买入评分 ≥60 且现价站上 MA5（看板标签不受影响）；每个事件都带风险标记。
阶段:
  intraday  盘中每 10 分钟：只查价格类事件 B1 B3 S2 S3（每轮只报一次，不会刷屏）
  preview   14:45：全部标的事件
  close     15:10：全部标的事件 + 市场事件 + 收盘复核（14:45 的 B2/S1、盘中触发的 B1/B3/S2 收盘时是否仍成立）。
            只有“全部确认”且没有新事件时不单独发邮件，仅记录
  weekly    周五 15:10 后：自选状态快照
首次见到某个标的或市场时只记录基线、不提醒，避免上线当天把已经满足的条件全报一遍。
预演（dry）可选 fresh：把所有标的当作从零开始，用来查看“当前满足哪些条件”。
数据源降级保护：状态里记着每个标的上次评估用的数据源（腾讯 / 东方财富 / 新浪），这次的不一样（如腾讯被拦截、退到新浪）
就跳过该标的的所有事件且不更新它的状态；前复权口径不同，回撤和高点对不上。大盘温度同理，对照三个指数各自的数据源。
"""
import copy
import json
import os
import threading
import urllib.request
from datetime import datetime, timedelta

from . import core

PRICE_EVENTS = ("B1", "B3", "S2", "S3")
SIGNAL_EVENTS = ("B2", "S1")
COOLDOWN_EVENTS = ("B2", "B3", "S1", "S2")
PRIORITY = {"B2": 0, "S1": 0, "B1": 1, "S2": 1, "S3": 2, "B3": 3, "M": 4, "W": 5}
EVENT_NAMES = {"B1": "档位触达", "B2": "买入信号", "B3": "进入冰点", "S1": "止盈信号",
               "S2": "进入过热", "S3": "过热后回落", "M": "市场", "W": "每周快照"}
VETO = "⚠ 先核对赛道证伪条件，确认不是基本面恶化"
RISK_HINT = {"跌穿最深档": "回撤已超过最深档位，需按策略重新评估"}
BUY_LABEL_ORDER = ("信号强", "有信号", "待企稳", "观察", "无信号")
DEGRADED_HEAD = "■ 数据源异常（本轮已跳过：不判断提醒，也不更新状态；数据源恢复后自动继续）"
REBASE_DAYS = 20   # 同一个替代数据源连续出现这么多个交易日，视为数据源永久变了，按新数据源重建基线


def _price(p):
    return "%.3f" % p if p < 10 else "%.2f" % p


def _flag_line(flags):
    """风险标记（signals.score 的 flags：[文字, 是否警示]）整理成一行，没有标记返回 None。"""
    if not flags:
        return None
    parts = []
    for text, _warn in flags:
        hint = next((v for k, v in RISK_HINT.items() if text.startswith(k)), None)
        parts.append("%s（%s）" % (text, hint) if hint else text)
    return "标记：" + "｜".join(parts)


class AlertEngine:
    def __init__(self, data_dir, notify_url, public_url, log):
        self.state_path = os.path.join(data_dir, "alerts_state.json")
        self.history_path = os.path.join(data_dir, "alerts_history.json")
        self.notify_url = notify_url
        self.public_url = public_url.rstrip("/")
        self.log = log
        self.lock = threading.Lock()
        self.state = self._load(self.state_path, {})
        self.history = self._load(self.history_path, [])
        self._warned = set()   # 已写过日志的降级提示（当天内同一条只写一次，盘中每 10 分钟一轮不刷屏）

    # ---------- 持久化 ----------

    @staticmethod
    def _load(path, default):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    @staticmethod
    def _dump(path, obj):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)

    # ---------- 规则 ----------

    def _cooled(self, state, key, today, days):
        last = state.setdefault("last_sent", {}).get(key)
        if not last or days <= 0:
            return True
        return (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(last, "%Y-%m-%d")).days >= days

    # ---------- 数据源降级保护 ----------

    @staticmethod
    def _stamp(st, d):
        """记下这次评估用的数据源。名称也记下：新浪等备用源给不出名称，降级时提示里要用。"""
        st["source"] = d.get("source")
        if d["name"] != d["sym"]:
            st["name"] = d["name"]

    @staticmethod
    def _tencent_up(ov, items, today, fresh):
        """这一轮腾讯是否可用：任何一个大盘指数或标的的当天数据来自腾讯。用来区分“腾讯被拦截”和“这个标的本来就不走腾讯”。"""
        live = lambda x: not x.get("error") and (fresh or x.get("date") == today)  # noqa: E731
        return (any(r["ok"] and live(r["data"]) and r["data"].get("source") == "腾讯" for r in ov["indices"])
                or any(live(d) and d.get("source") == "腾讯" for d, _sig in items))

    def _source_diff(self, d, tencent_up):
        """数据源与引擎里记录的上次评估不一致时返回描述（dict），一致或首次见到返回 None。

        对照的永远是 self.state 的真实记录：预演的 fresh 状态是空的，不对照它就看不出降级。

        旧状态（部署本功能之前）没有 source 字段，不能把第一次看到的数据源直接当基线：那时若腾讯正被拦截，
        基线会记成备用源，腾讯恢复后这个标的反而一直“不一致”。所以旧状态按“应为腾讯”处理，例外是腾讯本轮可用
        （tencent_up）而这个标的仍走备用源，说明它本来就不走腾讯（如国证指数 980022），按当前的源记基线。
        """
        ref = self.state.get("sym", {}).get(d["sym"])
        cur = d.get("source")
        if ref is None or not cur:
            return None
        prev, legacy = ref.get("source"), False
        if not prev:
            if cur == "腾讯" or tencent_up:
                return None
            prev, legacy = "腾讯", True
        if prev == cur:
            return None
        name = ref.get("name") or d["name"]
        return {"sym": d["sym"], "name": name, "source": cur, "expected": prev,
                "text": "%s(%s)：本次数据来自%s，上次评估用的是%s%s，复权口径可能不同，回撤和高点对不上" % (
                    name, d["sym"], cur, prev, "（旧状态没记数据源，按腾讯处理）" if legacy else "")}

    def _check_source(self, state, d, today, dry, tencent_up):
        """返回 None 表示可以评估；返回描述表示数据源降级，调用方应跳过该标的，不碰它的状态。

        例外：同一个替代数据源连续出现 REBASE_DAYS 个交易日（中间没有回到原来的源），视为数据源永久变了，
        丢掉旧状态，让后面的评估按新数据源重新建立基线（不提醒）；否则这个标的会被永远静默跳过。
        """
        info = self._source_diff(d, tencent_up)
        st = state.get("sym", {}).get(d["sym"])
        if info is None:
            if st is not None:
                st.pop("alt", None)
            return None
        if st is not None:
            alt = st.get("alt")
            if not alt or alt.get("source") != info["source"]:
                alt = st["alt"] = {"source": info["source"], "days": []}
            if today not in alt["days"]:
                alt["days"].append(today)
            if len(alt["days"]) >= REBASE_DAYS:
                del state["sym"][d["sym"]]
                if not dry:
                    self.log.warning("alerts: %s 的数据源连续 %d 个交易日是%s（原为%s），按新数据源重建基线",
                                     info["name"], len(alt["days"]), info["source"], info["expected"])
                return None
        return info

    @staticmethod
    def _index_sources(ov):
        """参与大盘温度的指数各自的数据源。某个指数取不到时它不在里面，同样算与上次不一致。"""
        return {r["data"]["sym"]: r["data"].get("source") for r in ov["indices"]
                if r["ok"] and r["data"].get("temp") is not None}

    def _market_diff(self, ov):
        """大盘温度的数据源降级：指数数据源与记录不一致；首次基线时三个指数没取全也算（平均值会偏）。

        旧状态有 market 但没有 sources：这三个指数腾讯都有历史，与标的的旧状态一样按“应为腾讯”处理，
        不把腾讯被拦截时看到的备用源记成基线。
        """
        ms = self.state.get("market")
        rec = (ms or {}).get("sources")
        if ms is not None and not rec:
            rec = dict((sym, "腾讯") for sym, _name in core.MARKET_INDICES)
        srcs = self._index_sources(ov)
        if (rec and rec == srcs) or (not rec and len(srcs) == len(core.MARKET_INDICES)):
            return None
        diff = []
        for sym, name in core.MARKET_INDICES:
            cur, prev = srcs.get(sym) or "缺失", (rec or {}).get(sym)
            if prev and prev != cur:
                diff.append("%s %s→%s" % (name, prev, cur))
            elif not prev and cur == "缺失":
                diff.append("%s 缺失" % name)
        return {"sym": "", "name": "大盘温度", "source": "", "expected": "",
                "text": "大盘温度：指数数据源与上次评估不一致或没取全（%s），温度是三个指数的平均，对不上" % "、".join(diff)}

    def _log_degraded(self, phase, degraded, today):
        """降级写一行 warning；同一条当天只写一次。"""
        self._warned = {k for k in self._warned if k[0] == today}
        new = [g for g in degraded if (today, g["text"]) not in self._warned]
        if new:
            self._warned.update((today, g["text"]) for g in new)
            self.log.warning("alerts %s: 数据源与上次评估不一致，已跳过且不更新状态：%s", phase, "；".join(g["text"] for g in new))

    def _review_skipped(self, state, today, g):
        """收盘时数据源异常的标的：当天发过的预警无法复核。明说出来，别让它混进“全部确认”而被静默。"""
        pend = [pe for k in ("preview_sent", "intraday_sent") for pe in state.get(k, {}).get(today, [])
                if pe["sym"] == g["sym"]]
        if not pend:
            return []
        kinds = "、".join(sorted({EVENT_NAMES[pe["type"]] for pe in pend}))
        return [{"name": g["name"], "ok": False,
                 "text": "%s 收盘复核跳过：数据源异常（本次%s，平时%s），无法确认今天的%s是否仍成立" % (
                     g["name"], g["source"], g["expected"], kinds)}]

    def _record(self, result):
        self.history.append(result)
        self.history = self.history[-300:]
        self._dump(self.history_path, self.history)

    def _symbol_events(self, state, d, sig, phase, settings, fresh=False):
        """更新单个标的的状态，返回触发的事件（未过滤开关与静音）。"""
        sym, tiers = d["sym"], settings["tiers"]
        dd = -d["raw"]["一年回撤"] * 100
        high, price, temp = d["series"]["high250"][-1], d["price"], d["temp"]
        # B2 的提醒条件：买入评分 ≥60（有信号 / 信号强）且现价站上 MA5。只看评分的话，“当天没再跌”就够了，会在反弹日一次报一堆
        buy_ok = sig["buy"]["level"] >= 2 and bool(d["stab"]["above_ma5"])
        st = state.setdefault("sym", {}).get(sym)
        zone = "cold" if temp < 10 else "hot" if temp >= 90 else "normal"
        if st is None and fresh:
            st = state["sym"][sym] = {"peak": high, "hit": [], "buy_lv": 0, "sell_lv": 0, "buy_ok": False,
                                      "zone": "normal", "hot_peak": None, "s3_done": False}
        elif st is None:  # 基线
            state["sym"][sym] = {"peak": high, "hit": [t for t in tiers if dd >= t], "buy_lv": sig["buy"]["level"],
                                 "sell_lv": sig["sell"]["level"], "buy_ok": buy_ok, "zone": zone,
                                 "hot_peak": price if zone == "hot" else None, "s3_done": False}
            self._stamp(state["sym"][sym], d)
            return []
        self._stamp(st, d)  # 旧状态没有 source 字段：能走到这里说明 _check_source 已放行（腾讯，或腾讯可用而它本来就不走腾讯），记为基线
        if "buy_ok" not in st:  # 旧状态迁移：沿用原来的评分等级，部署当天不会把已经满足的条件当成新事件报出来
            st["buy_ok"] = st.get("buy_lv", 0) >= 2
        events = []
        base = "%s(%s)" % (d["name"], sym)
        bscore, sscore = sig["buy"]["score"], sig["sell"]["score"]
        line2 = "温度 %.0f %s｜距一年高点 %.1f%%｜买入评分 %.0f %s｜过热评分 %.0f %s" % (
            temp, d["label"], -dd, bscore, sig["buy"]["label"], sscore, sig["sell"]["label"])
        buy_tail = [ln for ln in (_flag_line(sig["buy"]["flags"]), VETO) if ln]  # 买入侧事件的风险提示
        sell_tail = [ln for ln in (_flag_line(sig["sell"]["flags"]),) if ln]

        # B1 档位：创出新高后重置
        if high > st["peak"] * 1.0001:
            st["peak"], st["hit"] = high, [t for t in tiers if dd >= t]
        new = [t for t in tiers if dd >= t and t not in st["hit"]]
        if new:
            st["hit"] = sorted(set(st["hit"]) | set(new))
            idx = tiers.index(max(new)) + 1
            events.append({"type": "B1", "side": "buy", "sym": sym, "name": d["name"], "score": bscore,
                           "tier": max(new), "idx": idx,
                           "headline": "%s 触及第 %d 档 -%s%%" % (d["name"], idx, max(new)),
                           "lines": ["%s 回撤触及第 %d 档（-%s%%）：距一年高点 -%.1f%%，一年高点 %s，现价 %s" % (
                               base, idx, max(new), dd, _price(high), _price(price)), line2] + buy_tail})

        # B2 / S1：评分升级，只在 preview / close 判断（依赖 MA5、RSI，盘中易跳动）
        if phase != "intraday":
            blv, slv = sig["buy"]["level"], sig["sell"]["level"]
            if buy_ok and not st["buy_ok"]:
                p = sig["buy"]["parts"]
                events.append({"type": "B2", "side": "buy", "sym": sym, "name": d["name"], "score": bscore,
                               "headline": "%s 买入%s %.0f" % (d["name"], sig["buy"]["label"], bscore),
                               "lines": ["%s 买入评分升至 %.0f（%s）：便宜 %.0f / 档位 %.0f / 企稳 %.0f / 环境 %.0f｜现价已站上 MA5" % (
                                   base, bscore, sig["buy"]["label"], p["cheap"], p["tier"], p["stab"], p["env"]),
                                   line2] + buy_tail})
            if slv >= 2 and st["sell_lv"] < 2:
                p = sig["sell"]["parts"]
                events.append({"type": "S1", "side": "sell", "sym": sym, "name": d["name"], "score": sscore,
                               "headline": "%s %s %.0f" % (d["name"], sig["sell"]["label"], sscore),
                               "lines": ["%s 过热评分升至 %.0f（%s）：过热 %.0f / 涨幅 %.0f / 滞涨 %.0f / 环境 %.0f" % (
                                   base, sscore, sig["sell"]["label"], p["hot"], p["rise"], p["stall"], p["env"]),
                                   line2] + sell_tail})
            st["buy_lv"], st["sell_lv"], st["buy_ok"] = blv, slv, buy_ok

        # S3 过热后回落：在离开过热区之前判断
        if st["zone"] == "hot" and st.get("hot_peak"):
            st["hot_peak"] = max(st["hot_peak"], price)
            if not st.get("s3_done") and price <= st["hot_peak"] * 0.9:
                st["s3_done"] = True
                events.append({"type": "S3", "side": "sell", "sym": sym, "name": d["name"], "score": sscore,
                               "headline": "%s 过热后回落 %.1f%%" % (d["name"], (1 - price / st["hot_peak"]) * 100),
                               "lines": ["%s 进入过热后从高点 %s 回落至 %s（-%.1f%%）" % (
                                   base, _price(st["hot_peak"]), _price(price), (1 - price / st["hot_peak"]) * 100), line2] + sell_tail})
        # 温度分区（滞回：冰点 <10 进、>25 出；过热 ≥90 进、<75 出）
        old = st["zone"]
        if old == "cold" and temp > 25:
            st["zone"] = "normal"
        elif old == "hot" and temp < 75:
            st["zone"], st["hot_peak"], st["s3_done"] = "normal", None, False
        if st["zone"] != "cold" and temp < 10:
            st["zone"] = "cold"
            events.append({"type": "B3", "side": "buy", "sym": sym, "name": d["name"], "score": bscore,
                           "headline": "%s 进入冰点 %.0f" % (d["name"], temp),
                           "lines": ["%s 温度降至 %.0f，进入冰点" % (base, temp), line2] + buy_tail})
        if st["zone"] != "hot" and temp >= 90:
            st["zone"], st["hot_peak"], st["s3_done"] = "hot", price, False
            events.append({"type": "S2", "side": "sell", "sym": sym, "name": d["name"], "score": sscore,
                           "headline": "%s 进入过热 %.0f" % (d["name"], temp),
                           "lines": ["%s 温度升至 %.0f，进入过热" % (base, temp), line2] + sell_tail})
        return events

    def _market_events(self, state, ov, today, dry, fresh=False):
        """返回 (事件, 降级描述或 None)。大盘温度是三个指数的平均，指数数据源与上次不一致时跳过温度分区，不更新它。
        宽度和 CNN 是各自独立的数据源，不受影响。"""
        ms = state.get("market")
        if ms is None and fresh:
            ms = state["market"] = {"zone": "normal", "breadth": "normal", "cnn": "normal"}
        mt = ov["market"]["temp"]
        b = ov["breadth"]["data"] if ov["breadth"]["ok"] else None
        c = ov["cnn"]["data"] if ov["cnn"]["ok"] else None
        bz = None if b is None else ("fear" if b["ratio"] < 0.10 or b["dt"] >= 100 else "frenzy" if b["ratio"] >= 0.90 else "normal")
        srcs = self._index_sources(ov)
        deg = self._market_diff(ov)
        rebased = False
        if deg and ms is not None:  # 与 _check_source 相同：替代数据源连续出现 REBASE_DAYS 个交易日，按新数据源重建基线
            alt = ms.get("alt")
            if not alt or alt.get("sources") != srcs:
                alt = ms["alt"] = {"sources": srcs, "days": []}
            if today not in alt["days"]:
                alt["days"].append(today)
            if len(alt["days"]) >= REBASE_DAYS:
                if not dry:
                    self.log.warning("alerts: 大盘指数的数据源连续 %d 个交易日与原来不同，按新数据源重建基线", len(alt["days"]))
                ms = state["market"] = None
                deg, rebased = None, True
        elif ms is not None:
            ms.pop("alt", None)
        if ms is None:  # 基线
            state["market"] = {
                "zone": "cold" if mt is not None and mt < 10 else "hot" if mt is not None and mt >= 90 else "normal",
                "breadth": bz or "normal",
                "cnn": "fear" if c and c["score"] < 25 else "greed" if c and c["score"] >= 75 else "normal",
            }
            if rebased or len(srcs) == len(core.MARKET_INDICES):  # 首次基线时指数没取全就不记，免得之后恢复正常被当成降级
                state["market"]["sources"] = srcs
            return [], None
        lines = []
        if mt is not None and not deg:
            ms["sources"] = srcs  # 旧状态没有 sources 字段：能走到这里说明三个指数都是腾讯（否则 deg 非空），记为基线
            if ms["zone"] == "cold" and mt > 25 or ms["zone"] == "hot" and mt < 75:
                ms["zone"] = "normal"
            if ms["zone"] != "cold" and mt < 10:
                ms["zone"] = "cold"
                lines.append("大盘温度降至 %.0f，进入冰点" % mt)
            if ms["zone"] != "hot" and mt >= 90:
                ms["zone"] = "hot"
                lines.append("大盘温度升至 %.0f，进入过热" % mt)
        if bz is not None:
            if bz != ms["breadth"] and bz != "normal":
                lines.append("市场宽度%s：上涨占比 %.0f%%，涨停 %d / 跌停 %d" % (
                    "极度恐慌" if bz == "fear" else "狂热", b["ratio"] * 100, b["zt"], b["dt"]))
            ms["breadth"] = bz
        if c is not None:
            s = c["score"]
            if ms["cnn"] == "fear" and s > 35 or ms["cnn"] == "greed" and s < 65:
                ms["cnn"] = "normal"
            if ms["cnn"] != "fear" and s < 25:
                ms["cnn"] = "fear"
                lines.append("CNN 恐惧贪婪降至 %.0f，极度恐惧（美股）" % s)
            if ms["cnn"] != "greed" and s >= 75:
                ms["cnn"] = "greed"
                lines.append("CNN 恐惧贪婪升至 %.0f，极度贪婪（美股）" % s)
        return [{"type": "M", "side": "market", "sym": "", "name": "市场", "score": 0, "headline": ln, "lines": [ln]}
                for ln in lines], deg

    # ---------- 运行 ----------

    def evaluate(self, phase, ov, items, settings, dry=False, fresh=False):
        """phase: intraday/preview/close。items: [(analyze 结果, signals)]。

        dry=True 时不改状态、不发送；fresh=True（仅预演）时忽略已有状态并接受非当天数据。
        """
        with self.lock:
            state = ({} if fresh else copy.deepcopy(self.state)) if dry else self.state
            today = core.now_cn().strftime("%Y-%m-%d")
            a = settings["alerts"]
            enabled = a["events"]
            events, review, degraded = [], [], []
            up = self._tencent_up(ov, items, today, fresh)
            for d, sig in items:
                if d.get("error") or sig is None or (d.get("date") != today and not fresh):
                    continue  # 数据异常或不是当天数据：宁可漏报
                g = self._check_source(state, d, today, dry, up)
                if g:  # 数据源与上次评估不一致（如腾讯被拦截退到新浪）：跳过，不动它的状态
                    if d["sym"] not in a["muted"]:
                        degraded.append(g)
                        if phase == "close":
                            review += self._review_skipped(state, today, g)
                    continue
                for e in self._symbol_events(state, d, sig, phase, settings, fresh):
                    if phase == "intraday" and e["type"] not in PRICE_EVENTS:
                        continue
                    if not enabled.get(e["type"]) or d["sym"] in a["muted"]:
                        continue
                    key = "%s|%s" % (d["sym"], e["type"])
                    if e["type"] in COOLDOWN_EVENTS and not self._cooled(state, key, today, a["cooldown_days"]):
                        continue
                    state.setdefault("last_sent", {})[key] = today
                    events.append(e)
                if phase == "close":
                    review += self._review(state, today, d, sig)
            if phase == "close":
                mev, mdeg = self._market_events(state, ov, today, dry, fresh)  # M 关闭时也更新状态
                if enabled.get("M"):
                    events += mev
                if mdeg:
                    degraded.append(mdeg)
            if phase == "preview":
                state.setdefault("preview_sent", {})[today] = [
                    {"sym": e["sym"], "type": e["type"]} for e in events if e["type"] in SIGNAL_EVENTS]
                state["preview_sent"] = {k: v for k, v in state["preview_sent"].items() if k == today}
            if phase == "intraday" and events:  # 记下盘中发出的价格类事件，收盘时复核是否仍成立
                rec = state.get("intraday_sent", {}).get(today, [])
                rec = rec + [{k: e[k] for k in ("sym", "type", "tier", "idx") if k in e} for e in events]
                state["intraday_sent"] = {today: rec}
            if phase == "close" and "intraday_sent" in state:
                state["intraday_sent"] = {k: v for k, v in state["intraday_sent"].items() if k == today}
            events.sort(key=lambda e: (PRIORITY[e["type"]], -e.get("score", 0)))  # 同级按分数从高到低
            review.sort(key=lambda r: r["ok"])  # 未确认的排前面
            # 收盘复核全部确认、也没有新事件：没有需要人处理的东西，只记录不发邮件
            silent = phase == "close" and not events and bool(review) and all(r["ok"] for r in review)
            result = {"time": core.now_cn().strftime("%Y-%m-%d %H:%M:%S"), "phase": phase, "events": events,
                      "review": [r["text"] for r in review], "dry": dry}
            if silent:
                result.update(silent=True, note="收盘复核全部确认，没有新事件，不单独发邮件，仅记录")
            if degraded:
                result["degraded"] = degraded
                note = "数据源异常，已跳过（不判断提醒、不更新状态）：" + "；".join(g["text"] for g in degraded)
                result["note"] = "%s。%s" % (result["note"], note) if result.get("note") else note
            if dry:
                result["message"] = (self.compose(phase, events, review, degraded)
                                     if (events or review) and not silent else None)
                return result
            if degraded:
                self._log_degraded(phase, degraded, today)
            if events or review:
                if silent:
                    result.update(title="【复核·收盘】盘中预警全部确认（未发邮件）", delivered=False)
                else:
                    self._deliver(result, self.compose(phase, events, review, degraded), a["enabled"])
                self._record(result)
            elif degraded and phase == "close":  # 没有邮件可发，也在看板记录里留一条，每天最多一条
                result.update(title="【降级·收盘】数据源异常，%d 项本轮跳过" % len(degraded), delivered=False, silent=True)
                self._record(result)
            self._dump(self.state_path, state)
            return result

    def _deliver(self, result, msg, enabled):
        """发送一封邮件，把结果写进 result（delivered / error）。"""
        result.update(title=msg["title"], delivered=False)
        if not enabled:
            result["error"] = "提醒总开关已关闭，仅记录"
            return
        try:
            self.send(msg)
            result["delivered"] = True
        except Exception as e:
            result["error"] = str(e)
            self.log.warning("alert send failed: %s", e)

    def _review(self, state, today, d, sig):
        """收盘复核：14:45 发出的 B2 / S1，以及盘中发出的 B1 / B3 / S2，收盘时是否仍成立。"""
        out = []
        name, sym = d["name"], d["sym"]
        dd = -d["raw"]["一年回撤"] * 100
        temp, above = d["temp"], bool(d["stab"]["above_ma5"])

        def add(ok, text):
            out.append({"name": name, "ok": ok, "text": "%s %s" % (name, text)})

        for pe in state.get("preview_sent", {}).get(today, []):
            if pe["sym"] != sym:
                continue
            if pe["type"] == "B2":
                s = sig["buy"]
                ok = s["level"] >= 2 and above
                note = "，收盘仍站上 MA5" if ok else "，收盘未站上 MA5" if s["level"] >= 2 else ""
                add(ok, "买入信号：收盘%s（收盘评分 %.0f %s%s）" % ("确认" if ok else "未确认", s["score"], s["label"], note))
            else:
                s = sig["sell"]
                ok = s["level"] >= 2
                add(ok, "止盈信号：收盘%s（收盘评分 %.0f %s）" % ("确认" if ok else "未确认", s["score"], s["label"]))
        for pe in state.get("intraday_sent", {}).get(today, []):
            if pe["sym"] != sym:
                continue
            if pe["type"] == "B1":
                ok = dd >= pe["tier"]
                add(ok, "档位触达：盘中触及第 %d 档（-%s%%），收盘%s（收盘回撤 -%.1f%%）" % (
                    pe["idx"], pe["tier"], "仍在档内" if ok else "已回到该档之上", dd))
            elif pe["type"] == "B3":
                ok = temp < 10
                add(ok, "进入冰点：收盘温度 %.0f，%s" % (temp, "仍在冰点" if ok else "已回到冰点之上"))
            elif pe["type"] == "S2":
                ok = temp >= 90
                add(ok, "进入过热：收盘温度 %.0f，%s" % (temp, "仍在过热" if ok else "已回到过热之下"))
        return out

    def compose(self, phase, events, review, degraded=()):
        n = {s: sum(1 for e in events if e["side"] == s) for s in ("buy", "sell", "market")}
        tag = {"buy": "买点", "sell": "卖点", "market": "市场"}
        when = {"intraday": "盘中", "preview": "14:45 预警", "close": "收盘"}[phase]
        if events:  # 标题列出排在最前的两条，其余写个数
            title = "【%s·%s】%s" % (tag[events[0]["side"]], when, " / ".join(e["headline"] for e in events[:2]))
            if len(events) > 2:
                title += " 等 %d 条" % len(events)
        else:
            bad = [r["name"] for r in review if not r["ok"]]
            title = "【复核·收盘】盘中预警未确认：" + "、".join(bad) if bad else "【复核·收盘】盘中预警全部确认"
        summary = "买点 %d 条 · 卖点 %d 条 · 市场 %d 条（%s）" % (n["buy"], n["sell"], n["market"], {
            "intraday": "盘中价格触发，15:10 收盘邮件会复核是否仍成立",
            "preview": "盘中预警，15:10 用收盘数据复核；全部确认时不再单独发邮件",
            "close": "收盘数据"}[phase])
        if degraded:
            summary += "；另有 %d 项数据源异常已跳过" % len(degraded)
        parts = []
        for side, head in (("buy", "■ 买点"), ("sell", "■ 卖点"), ("market", "■ 市场")):
            group = [e for e in events if e["side"] == side]
            if not group:
                continue
            parts.append(head)
            for e in group:
                parts.append("· [%s] %s" % (EVENT_NAMES[e["type"]], e["lines"][0]))
                parts += ["   " + ln for ln in e["lines"][1:]]
                if e["sym"]:
                    parts.append("   详情：%s/#sym=%s" % (self.public_url, e["sym"]))
            parts.append("")
        if review:
            parts.append("■ 盘中预警复核")
            parts += ["· " + r["text"] for r in review]
            parts.append("")
        if degraded:
            parts.append(DEGRADED_HEAD)
            parts += ["· " + g["text"] for g in degraded]
            parts.append("")
        parts.append("看板：%s" % self.public_url)
        parts.append("按规则自动提醒，不构成投资建议；证伪条件与基本面需人工核对。")
        return {"title": title, "summary": summary, "detail": "\n".join(parts)}

    def snapshot(self, ov, items, settings, dry=False, fresh=False):
        """每周五收盘后的自选状态快照，不改变提醒状态。

        提醒只在状态变化时发：上线时就已满足的条件、长期没变化的状态都不会出现在提醒里。
        快照把每个标的当前的位置、评分和“提醒条件是否满足”列一遍，补上这部分信息。
        数据源与上次评估不一致的标的不列数值，只在“数据源异常”一节点名：快照按买入评分排序、统计个数，
        对不上的数值（腾讯前复权回撤 -46%，新浪不复权 -73%）混进去会误导，写“仅供参考”也挡不住先入为主。
        """
        with self.lock:
            today = core.now_cn().strftime("%Y-%m-%d")
            a = settings["alerts"]
            rows, degraded = [], []
            up = self._tencent_up(ov, items, today, fresh)
            for d, sig in items:
                if (d.get("error") or sig is None or d["sym"] in a["muted"]
                        or not (fresh or d.get("date") == today)):  # 数据不是当天的不列，宁可少列
                    continue
                g = self._source_diff(d, up)
                if g:
                    degraded.append(g)
                else:
                    rows.append((d, sig))
            mdeg = self._market_diff(ov)
            if mdeg:
                degraded.append(mdeg)
            result = {"time": core.now_cn().strftime("%Y-%m-%d %H:%M:%S"), "phase": "weekly", "events": [],
                      "review": [], "dry": dry}
            if degraded:
                result["degraded"] = degraded
            if not a["events"].get("W"):
                result["note"] = "每周快照已关闭"
                return result
            if not rows:
                result["note"] = ("数据源异常，没有可列的标的：" + "；".join(g["text"] for g in degraded)
                                  if degraded else "没有可用的当天数据")
                if degraded and not dry:
                    self._log_degraded("weekly", degraded, today)
                    result.update(title="【降级·周报】数据源异常，自选快照没有可列的标的", delivered=False, silent=True)
                    self._record(result)
                return result
            msg = self._snapshot_message(self.state, today, rows, ov, settings, degraded, mdeg is not None)
            if dry:
                result["message"] = msg
                return result
            if degraded:
                self._log_degraded("weekly", degraded, today)
            result["note"] = msg["summary"]
            self._deliver(result, msg, a["enabled"])
            self._record(result)
            return result

    def _snapshot_message(self, state, today, rows, ov, settings, degraded=(), market_degraded=False):
        tiers = settings["tiers"]
        rows = sorted(rows, key=lambda r: -r[1]["buy"]["score"])
        counts = {}
        for _d, sig in rows:
            counts[sig["buy"]["label"]] = counts.get(sig["buy"]["label"], 0) + 1
        n_ok = sum(1 for d, sig in rows if sig["buy"]["level"] >= 2 and d["stab"]["above_ma5"])
        n_hot = sum(1 for _d, sig in rows if sig["sell"]["level"] >= 2)
        head = " · ".join("%s %d" % (k, counts[k]) for k in BUY_LABEL_ORDER if k in counts)
        title = "【周报·周五收盘】自选快照：%s（共 %d 个标的）" % (head, len(rows))
        summary = "共 %d 个标的 · 满足买入提醒条件 %d 个 · 过热侧有止盈信号 %d 个（每周五收盘后的状态快照，不是新触发的提醒）" % (
            len(rows), n_ok, n_hot)
        if degraded:
            summary += "；另有 %d 项数据源异常未列出" % len(degraded)

        m = []
        mt = None if market_degraded else ov["market"]["temp"]  # 大盘温度的指数数据源异常时不列温度，见“数据源异常”一节
        if mt is not None:
            m.append("大盘温度 %.0f %s" % (mt, ov["market"]["label"]))
        if ov["breadth"]["ok"]:
            b = ov["breadth"]["data"]
            m.append("市场宽度 %s（上涨占比 %.0f%%，涨停 %d / 跌停 %d）" % (b["label"], b["ratio"] * 100, b["zt"], b["dt"]))
        if ov["cnn"]["ok"]:
            m.append("CNN 恐惧贪婪 %.0f %s" % (ov["cnn"]["data"]["score"], ov["cnn"]["data"]["rating"]))
        if ov["vix"]["ok"]:
            m.append("VIX %.1f %s" % (ov["vix"]["data"]["value"], ov["vix"]["data"]["label"]))
        parts = []
        if m:
            parts += ["■ 市场", "· " + "｜".join(m), ""]
        parts.append("■ 自选（按买入评分从高到低，共 %d 个）" % len(rows))
        for d, sig in rows:
            b, s = sig["buy"], sig["sell"]
            dd = -d["raw"]["一年回撤"] * 100
            hit = [t for t in tiers if dd >= t]
            above = bool(d["stab"]["above_ma5"])
            last = state.get("last_sent", {}).get("%s|B2" % d["sym"])
            parts.append("· %s(%s) 买入评分 %.0f %s｜过热评分 %.0f %s" % (
                d["name"], d["sym"], b["score"], b["label"], s["score"], s["label"]))
            parts.append("   距一年高点 -%.1f%%｜%s｜%s｜温度 %.0f %s" % (
                dd, "已触第 %d 档（-%s%%）" % (len(hit), hit[-1]) if hit else "未触及档位",
                "现价站上 MA5" if above else "现价在 MA5 下方", d["temp"], d["label"]))
            if b["level"] >= 2 and above:
                why = "买入提醒条件：已满足，" + ("上次提醒 %s" % last[5:] if last else "上线以来没有发过这个标的的买入信号提醒")
            elif b["level"] >= 2:
                why = "买入提醒条件：未满足（评分达标，但现价在 MA5 下方）"
            elif b["score"] >= 60:
                why = "买入提醒条件：未满足（评分达标，但企稳不足：%s）" % b["label"]
            else:
                why = "买入提醒条件：未满足（评分 %.0f，未到 60）" % b["score"]
            if s["level"] >= 2:
                why += "｜止盈提醒条件：已满足"
            parts.append("   " + why)
            flags = list(b["flags"]) + [f for f in (s["flags"] if s["level"] >= 1 else []) if f not in b["flags"]]
            if _flag_line(flags):
                parts.append("   " + _flag_line(flags))
            parts.append("   详情：%s/#sym=%s" % (self.public_url, d["sym"]))
        if degraded:
            parts += ["", "■ 数据源异常（以下不列数值：数据源与上次评估不一致，口径对不上，列出来会误导）"]
            parts += ["· " + g["text"] for g in degraded]
        parts += ["", "看板：%s" % self.public_url,
                  "每周快照只列出当前状态；买卖点提醒仍只在状态变化时发。按规则自动整理，不构成投资建议；证伪条件与基本面需人工核对。"]
        return {"title": title, "summary": summary, "detail": "\n".join(parts)}

    def send(self, msg):
        if not self.notify_url:
            raise RuntimeError("未配置 THERMO_NOTIFY_URL")
        body = json.dumps({"source_id": "market-thermo", "level": "info", "title": msg["title"], "template": "alert",
                           "data": {"summary": msg["summary"], "detail": msg["detail"], "source": "market-thermo"}},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.notify_url.rstrip("/") + "/v1/notify", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            out = json.loads(r.read().decode("utf-8"))
        bad = [x for x in out.get("results", []) if x.get("Channel") != "log" and x.get("Status") != "delivered"]
        if bad or not any(x.get("Channel") != "log" for x in out.get("results", [])):
            raise RuntimeError("notify 未投递到邮件通道: %s" % out.get("results"))
        return out

    def ran(self, phase, day):
        return self.state.get("runs", {}).get(phase) == day

    def mark_run(self, phase, day):
        with self.lock:
            self.state.setdefault("runs", {})[phase] = day
            self._dump(self.state_path, self.state)

    def summary(self):
        with self.lock:
            return {
                "symbols": self.state.get("sym", {}),
                "market": self.state.get("market"),
                "history": list(reversed(self.history[-50:])),
            }


def next_runs(now=None):
    """供界面展示的下一次检查时间。"""
    now = now or core.now_cn()
    out = []
    for hm, name in (((14, 45), "14:45 预警"), ((15, 10), "15:10 收盘")):
        t = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        while t <= now or t.weekday() >= 5:
            t += timedelta(days=1)
        out.append("%s %s" % (t.strftime("%m-%d"), name))
    t = now.replace(hour=15, minute=10, second=0, microsecond=0)  # 周五收盘后的快照
    while t <= now or t.weekday() != 4:
        t += timedelta(days=1)
    out.append("%s 周报快照" % t.strftime("%m-%d"))
    return out
