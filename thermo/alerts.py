"""买卖点提醒：按状态变化触发，经 notify-service 发邮件。

事件（规则详见知识库 invest/方法/提醒规则.md）:
  B1 档位触达  B2 买入信号成立  B3 进入冰点
  S1 止盈信号成立  S2 进入过热  S3 过热后回落 10%
  M  市场层面：大盘冰点/过热、宽度极度恐慌/狂热、CNN 极度恐惧/贪婪
阶段:
  intraday  盘中每 10 分钟：只查价格类事件 B1 B3 S2 S3（每轮只报一次，不会刷屏）
  preview   14:45：全部标的事件
  close     15:10：全部标的事件 + 市场事件 + 14:45 预警的收盘复核
首次见到某个标的或市场时只记录基线、不提醒，避免上线当天把已经满足的条件全报一遍。
预演（dry）可选 fresh：把所有标的当作从零开始，用来查看“当前满足哪些条件”。
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
PRIORITY = {"B2": 0, "S1": 0, "B1": 1, "S2": 1, "S3": 2, "B3": 3, "M": 4}
EVENT_NAMES = {"B1": "档位触达", "B2": "买入信号", "B3": "进入冰点", "S1": "止盈信号",
               "S2": "进入过热", "S3": "过热后回落", "M": "市场"}


def _price(p):
    return "%.3f" % p if p < 10 else "%.2f" % p


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

    def _symbol_events(self, state, d, sig, phase, settings, fresh=False):
        """更新单个标的的状态，返回触发的事件（未过滤开关与静音）。"""
        sym, tiers = d["sym"], settings["tiers"]
        dd = -d["raw"]["一年回撤"] * 100
        high, price, temp = d["series"]["high250"][-1], d["price"], d["temp"]
        st = state.setdefault("sym", {}).get(sym)
        zone = "cold" if temp < 10 else "hot" if temp >= 90 else "normal"
        if st is None and fresh:
            st = state["sym"][sym] = {"peak": high, "hit": [], "buy_lv": 0, "sell_lv": 0, "zone": "normal",
                                      "hot_peak": None, "s3_done": False}
        elif st is None:  # 基线
            state["sym"][sym] = {"peak": high, "hit": [t for t in tiers if dd >= t], "buy_lv": sig["buy"]["level"],
                                 "sell_lv": sig["sell"]["level"], "zone": zone, "hot_peak": price if zone == "hot" else None,
                                 "s3_done": False}
            return []
        events = []
        base = "%s(%s)" % (d["name"], sym)
        line2 = "温度 %.0f %s｜距一年高点 %.1f%%｜买入评分 %.0f %s｜过热评分 %.0f %s" % (
            temp, d["label"], -dd, sig["buy"]["score"], sig["buy"]["label"], sig["sell"]["score"], sig["sell"]["label"])

        # B1 档位：创出新高后重置
        if high > st["peak"] * 1.0001:
            st["peak"], st["hit"] = high, [t for t in tiers if dd >= t]
        new = [t for t in tiers if dd >= t and t not in st["hit"]]
        if new:
            st["hit"] = sorted(set(st["hit"]) | set(new))
            idx = tiers.index(max(new)) + 1
            events.append({"type": "B1", "side": "buy", "sym": sym, "name": d["name"],
                           "headline": "%s 触及第 %d 档 -%s%%" % (d["name"], idx, max(new)),
                           "lines": ["%s 回撤触及第 %d 档（-%s%%）：距一年高点 -%.1f%%，一年高点 %s，现价 %s" % (
                               base, idx, max(new), dd, _price(high), _price(price)), line2,
                               "⚠ 先核对赛道证伪条件，确认不是基本面恶化"]})

        # B2 / S1：评分升级，只在 preview / close 判断（依赖 MA5、RSI，盘中易跳动）
        if phase != "intraday":
            blv, slv = sig["buy"]["level"], sig["sell"]["level"]
            if blv >= 2 and st["buy_lv"] < 2:
                p = sig["buy"]["parts"]
                events.append({"type": "B2", "side": "buy", "sym": sym, "name": d["name"],
                               "headline": "%s 买入%s %.0f" % (d["name"], sig["buy"]["label"], sig["buy"]["score"]),
                               "lines": ["%s 买入评分升至 %.0f（%s）：便宜 %.0f / 档位 %.0f / 企稳 %.0f / 环境 %.0f" % (
                                   base, sig["buy"]["score"], sig["buy"]["label"], p["cheap"], p["tier"], p["stab"], p["env"]),
                                   line2]})
            if slv >= 2 and st["sell_lv"] < 2:
                p = sig["sell"]["parts"]
                events.append({"type": "S1", "side": "sell", "sym": sym, "name": d["name"],
                               "headline": "%s %s %.0f" % (d["name"], sig["sell"]["label"], sig["sell"]["score"]),
                               "lines": ["%s 过热评分升至 %.0f（%s）：过热 %.0f / 涨幅 %.0f / 滞涨 %.0f / 环境 %.0f" % (
                                   base, sig["sell"]["score"], sig["sell"]["label"], p["hot"], p["rise"], p["stall"], p["env"]),
                                   line2]})
            st["buy_lv"], st["sell_lv"] = blv, slv

        # S3 过热后回落：在离开过热区之前判断
        if st["zone"] == "hot" and st.get("hot_peak"):
            st["hot_peak"] = max(st["hot_peak"], price)
            if not st.get("s3_done") and price <= st["hot_peak"] * 0.9:
                st["s3_done"] = True
                events.append({"type": "S3", "side": "sell", "sym": sym, "name": d["name"],
                               "headline": "%s 过热后回落 %.1f%%" % (d["name"], (1 - price / st["hot_peak"]) * 100),
                               "lines": ["%s 进入过热后从高点 %s 回落至 %s（-%.1f%%）" % (
                                   base, _price(st["hot_peak"]), _price(price), (1 - price / st["hot_peak"]) * 100), line2]})
        # 温度分区（滞回：冰点 <10 进、>25 出；过热 ≥90 进、<75 出）
        old = st["zone"]
        if old == "cold" and temp > 25:
            st["zone"] = "normal"
        elif old == "hot" and temp < 75:
            st["zone"], st["hot_peak"], st["s3_done"] = "normal", None, False
        if st["zone"] != "cold" and temp < 10:
            st["zone"] = "cold"
            events.append({"type": "B3", "side": "buy", "sym": sym, "name": d["name"],
                           "headline": "%s 进入冰点 %.0f" % (d["name"], temp), "lines": ["%s 温度降至 %.0f，进入冰点" % (base, temp), line2]})
        if st["zone"] != "hot" and temp >= 90:
            st["zone"], st["hot_peak"], st["s3_done"] = "hot", price, False
            events.append({"type": "S2", "side": "sell", "sym": sym, "name": d["name"],
                           "headline": "%s 进入过热 %.0f" % (d["name"], temp), "lines": ["%s 温度升至 %.0f，进入过热" % (base, temp), line2]})
        return events

    def _market_events(self, state, ov, fresh=False):
        ms = state.get("market")
        if ms is None and fresh:
            ms = state["market"] = {"zone": "normal", "breadth": "normal", "cnn": "normal"}
        mt = ov["market"]["temp"]
        b = ov["breadth"]["data"] if ov["breadth"]["ok"] else None
        c = ov["cnn"]["data"] if ov["cnn"]["ok"] else None
        bz = None if b is None else ("fear" if b["ratio"] < 0.10 or b["dt"] >= 100 else "frenzy" if b["ratio"] >= 0.90 else "normal")
        if ms is None:  # 基线
            state["market"] = {
                "zone": "cold" if mt is not None and mt < 10 else "hot" if mt is not None and mt >= 90 else "normal",
                "breadth": bz or "normal",
                "cnn": "fear" if c and c["score"] < 25 else "greed" if c and c["score"] >= 75 else "normal",
            }
            return []
        lines = []
        if mt is not None:
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
        return [{"type": "M", "side": "market", "sym": "", "name": "市场", "headline": ln, "lines": [ln]} for ln in lines]

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
            events, review = [], []
            for d, sig in items:
                if d.get("error") or sig is None or (d.get("date") != today and not fresh):
                    continue  # 数据异常或不是当天数据：宁可漏报
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
                # 收盘复核 14:45 发出的 B2 / S1
                if phase == "close":
                    for pe in state.get("preview_sent", {}).get(today, []):
                        if pe["sym"] != d["sym"]:
                            continue
                        side = "buy" if pe["type"] == "B2" else "sell"
                        ok = sig[side]["level"] >= 2
                        review.append("%s %s：收盘%s（收盘评分 %.0f %s）" % (
                            d["name"], EVENT_NAMES[pe["type"]], "确认" if ok else "未确认",
                            sig[side]["score"], sig[side]["label"]))
            if phase == "close" and enabled.get("M"):
                events += self._market_events(state, ov, fresh)
            elif phase == "close":
                self._market_events(state, ov, fresh)  # 关闭时也更新状态
            if phase == "preview":
                state.setdefault("preview_sent", {})[today] = [
                    {"sym": e["sym"], "type": e["type"]} for e in events if e["type"] in SIGNAL_EVENTS]
                state["preview_sent"] = {k: v for k, v in state["preview_sent"].items() if k == today}
            events.sort(key=lambda e: PRIORITY[e["type"]])
            result = {"time": core.now_cn().strftime("%Y-%m-%d %H:%M:%S"), "phase": phase, "events": events,
                      "review": review, "dry": dry}
            if dry:
                result["message"] = self.compose(phase, events, review) if (events or review) else None
                return result
            if events or review:
                msg = self.compose(phase, events, review)
                result.update(title=msg["title"], delivered=False)
                if a["enabled"]:
                    try:
                        self.send(msg)
                        result["delivered"] = True
                    except Exception as e:
                        result["error"] = str(e)
                        self.log.warning("alert send failed: %s", e)
                else:
                    result["error"] = "提醒总开关已关闭，仅记录"
                self.history.append(result)
                self.history = self.history[-300:]
                self._dump(self.history_path, self.history)
            self._dump(self.state_path, state)
            return result

    def compose(self, phase, events, review):
        n = {s: sum(1 for e in events if e["side"] == s) for s in ("buy", "sell", "market")}
        tag = {"buy": "买点", "sell": "卖点", "market": "市场"}
        when = {"intraday": "盘中", "preview": "14:45 预警", "close": "收盘"}[phase]
        if events:
            top = events[0]
            title = "【%s·%s】%s" % (tag[top["side"]], when, top["headline"])
            if len(events) > 1:
                title += "（共 %d 条）" % len(events)
        else:
            title = "【复核·收盘】盘中预警的收盘结果"
        summary = "买点 %d 条 · 卖点 %d 条 · 市场 %d 条（%s）" % (n["buy"], n["sell"], n["market"], {
            "intraday": "盘中价格触发，收盘前可能回落",
            "preview": "盘中预警，15:10 用收盘数据复核",
            "close": "收盘数据"}[phase])
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
            parts += ["· " + r for r in review]
            parts.append("")
        parts.append("看板：%s" % self.public_url)
        parts.append("按规则自动提醒，不构成投资建议；证伪条件与基本面需人工核对。")
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
    return out
