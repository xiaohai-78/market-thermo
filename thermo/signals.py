"""信号评分（买入机会 / 过热卖出）与看板设置。看板展示和邮件提醒共用这一份逻辑。

方法说明见知识库 invest/方法/机会评分.md、过热评分.md。
"""
import copy
import json
import os

GATE = 34   # 企稳/滞涨 < 34（三项条件一项都不满足）时，高分只给“待企稳 / 过热未见顶”

DEFAULT_SETTINGS = {
    "tiers": [20, 30, 40, 50],
    "weights": {
        "buy": {"cheap": 40, "tier": 25, "stab": 20, "env": 15},
        "sell": {"hot": 40, "rise": 25, "stall": 20, "env": 15},
    },
    "rise_ref": 80,
    "alerts": {
        "enabled": True,
        # B1 档位触达 B2 买入信号 B3 进入冰点；S1 止盈信号 S2 进入过热 S3 过热后回落；M 市场层面
        "events": {"B1": True, "B2": True, "B3": False, "S1": True, "S2": True, "S3": False, "M": True},
        "cooldown_days": 7,
        "muted": [],
    },
}


def _merge(base, patch):
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def validate(s):
    """规整设置，丢弃非法值。"""
    s = _merge(DEFAULT_SETTINGS, s)
    tiers = sorted({float(t) for t in s["tiers"] if 0 < float(t) < 100})
    s["tiers"] = [int(t) if t == int(t) else t for t in tiers] or list(DEFAULT_SETTINGS["tiers"])
    for mode, keys in DEFAULT_SETTINGS["weights"].items():
        s["weights"][mode] = {k: max(0.0, float(s["weights"][mode].get(k, keys[k]) or 0)) for k in keys}
    s["rise_ref"] = min(500.0, max(10.0, float(s["rise_ref"])))
    a = s["alerts"]
    a["enabled"] = bool(a["enabled"])
    a["events"] = {k: bool(a["events"].get(k, v)) for k, v in DEFAULT_SETTINGS["alerts"]["events"].items()}
    a["cooldown_days"] = int(min(60, max(0, int(a["cooldown_days"]))))
    a["muted"] = [str(x) for x in a.get("muted", [])]
    return s


def load_settings(path):
    try:
        with open(path, encoding="utf-8") as f:
            return validate(json.load(f))
    except (OSError, ValueError, TypeError):
        return validate({})


def save_settings(path, s):
    s = validate(s)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return s


def _norm(w):
    total = sum(w.values()) or 1.0
    return {k: v / total for k, v in w.items()}


def _clamp(v):
    return max(0.0, min(100.0, v))


def buy_label(score, stab):
    if score >= 60 and stab < GATE:
        return "待企稳", 1
    if score >= 75:
        return "信号强", 3
    if score >= 60:
        return "有信号", 2
    if score >= 45:
        return "观察", 1
    return "无信号", 0


def sell_label(score, stall):
    if score >= 60 and stall < GATE:
        return "过热未见顶", 1
    if score >= 75:
        return "止盈信号强", 3
    if score >= 60:
        return "止盈信号", 2
    if score >= 45:
        return "偏热", 1
    return "无信号", 0


def score(d, market_temp, s):
    """d 为 core.analyze 的结果；返回 {"buy": {...}, "sell": {...}}，温度缺失时返回 None。"""
    if d.get("error") or d.get("temp") is None:
        return None
    env = 50.0 if market_temp is None else market_temp
    dd = -d["raw"]["一年回撤"] * 100
    max_tier = s["tiers"][-1]

    buy_parts = {
        "cheap": 100 - d["temp"],
        "tier": _clamp(dd / max_tier * 100),
        "stab": d["stab"]["score"],
        "env": 100 - env,
    }
    wb = _norm(s["weights"]["buy"])
    buy_score = sum(buy_parts[k] * wb[k] for k in wb)
    buy_flags = []
    if d.get("kind") == "个股":
        buy_flags.append(["个股：核对基本面", True])
    if dd > max_tier:
        buy_flags.append(["跌穿最深档", True])
    if d["down_days"] >= 3:
        buy_flags.append(["仍在连跌 %d 天" % d["down_days"], True])
    if d.get("short_history"):
        buy_flags.append(["历史不足 3 年", False])

    sell_parts = {
        "hot": d["temp"],
        "rise": _clamp(d["rise_from_low"] * 100 / s["rise_ref"] * 100),
        "stall": d["stall"]["score"],
        "env": env,
    }
    ws = _norm(s["weights"]["sell"])
    sell_score = sum(sell_parts[k] * ws[k] for k in ws)
    sell_flags = []
    if d["stall"]["up_days"] >= 3:
        sell_flags.append(["连涨 %d 天" % d["stall"]["up_days"], True])
    if d["raw"]["区间位置"] >= 0.98:
        sell_flags.append(["接近一年新高", True])
    if d.get("vol_ratio") is not None and d["vol_ratio"] >= 2:
        sell_flags.append(["放量", True])
    if d.get("short_history"):
        sell_flags.append(["历史不足 3 年", False])

    bl, blv = buy_label(buy_score, buy_parts["stab"])
    sl, slv = sell_label(sell_score, sell_parts["stall"])
    return {
        "buy": {"score": buy_score, "parts": buy_parts, "label": bl, "level": blv, "flags": buy_flags},
        "sell": {"score": sell_score, "parts": sell_parts, "label": sl, "level": slv, "flags": sell_flags},
    }
