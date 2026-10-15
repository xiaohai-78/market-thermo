"""命令行版温度计。

  python3 -m thermo.cli                 # 只看大盘
  python3 -m thermo.cli 159530 300024   # 大盘 + 标的
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from . import core

BREADTH_CSV = os.environ.get("THERMO_BREADTH_CSV") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "breadth_history.csv")


def fmt_price(p):
    return "%.3f" % p if p < 10 else "%.2f" % p


def print_item(r):
    if "error" in r:
        print("  %s(%s)  %s" % (r["name"], r["sym"], r["error"]))
        return
    t = "--" if r["temp"] is None else "%.0f" % r["temp"]
    tag = "盘中" if r["intraday"] else r["date"]
    print("  %s(%s)  %s  %+.2f%%  [%s]" % (r["name"], r["sym"], fmt_price(r["price"]), r["pct"], tag))
    print("    温度 %s → %s" % (t, r["label"]))
    raw, p = r["raw"], r["pcts"]

    def pp(k):
        return "--" if p[k] is None else "%.0f" % p[k]
    print("    距一年高点 %.1f%%(分位%s)  RSI14 %.0f(分位%s)  MA60乖离 %+.1f%%(分位%s)" % (
        raw["一年回撤"] * 100, pp("一年回撤"), raw["RSI14"], pp("RSI14"), raw["MA60乖离"] * 100, pp("MA60乖离")))
    vr = "--" if r["vol_ratio"] is None else "%.2f" % r["vol_ratio"]
    print("    20日涨跌 %+.1f%%(分位%s)  一年区间位置 %.0f%%  量比 %s  连跌 %d 天" % (
        raw["20日涨跌"] * 100, pp("20日涨跌"), raw["区间位置"] * 100, vr, r["down_days"]))
    note = "    样本 %d 根，来源 %s" % (r["bars"], r["source"])
    if r["short_history"]:
        note += "（不足 3 年，分位数参考性下降）"
    print(note)


def safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        return e


def main(argv):
    targets = argv[1:]
    print("=== 市场温度计  %s ===\n" % core.now_cn().strftime("%Y-%m-%d %H:%M"))
    syms = []
    for code in targets:
        try:
            syms.append(core.resolve(code))
        except ValueError as e:
            syms.append(e)

    with ThreadPoolExecutor(8) as ex:
        idx_f = [ex.submit(safe, core.analyze, s) for s, _ in core.MARKET_INDICES]
        tgt_f = [ex.submit(safe, core.analyze, s) if isinstance(s, str) else None for s in syms]
        b_f, v_f, c_f = ex.submit(safe, core.breadth), ex.submit(safe, core.vix), ex.submit(safe, core.cnn)

    print("【大盘】")
    temps = []
    for f in idx_f:
        r = f.result()
        if isinstance(r, Exception):
            print("  获取失败：%s" % r)
            continue
        print_item(r)
        if r.get("temp") is not None:
            temps.append(r["temp"])
    b = b_f.result()
    if isinstance(b, Exception):
        print("  宽度数据获取失败：%s" % b)
        breadth_txt = "未知"
    else:
        hist = core.save_breadth(BREADTH_CSV, b)
        print("  宽度[%s]  涨 %d / 跌 %d / 平 %d  上涨占比 %.0f%%" % (b["date"], b["up"], b["down"], b["flat"], b["ratio"] * 100))
        print("    涨停 %d  跌停 %d  涨超5%% %d  跌超5%% %d  → 今日情绪：%s" % (b["zt"], b["dt"], b["up5"], b["down5"], b["label"]))
        if len(hist) >= 5:
            r5 = [int(x["up"]) / max(1, int(x["up"]) + int(x["down"]) + int(x["flat"])) for x in hist[-5:]]
            print("    近5个记录日上涨占比均值 %.0f%%" % (sum(r5) / 5 * 100))
        else:
            print("    （宽度历史已记录 %d 天，满 5 天后显示均值）" % len(hist))
        breadth_txt = b["label"]
    if temps:
        avg = sum(temps) / len(temps)
        print("\n  ▶ 大盘温度 %.0f（%s）｜今日情绪 %s" % (avg, core.label(avg), breadth_txt))

    print("\n【海外（美股）】")
    v = v_f.result()
    if isinstance(v, Exception):
        print("  VIX 获取失败：%s" % v)
    else:
        print("  VIX %.2f  %+.2f%%  [%s]  近3年分位 %.0f → %s" % (v["value"], v["pct"], v["when"], v["pctile"], v["label"]))
    c = c_f.result()
    if isinstance(c, Exception):
        print("  CNN 恐惧贪婪获取失败：%s" % c)
    else:
        print("  CNN 恐惧贪婪 %.0f → %s  （前收 %.0f / 1周前 %.0f / 1月前 %.0f）" % (
            c["score"], c["rating"], c["previous_close"], c["previous_1_week"], c["previous_1_month"]))
        print("    分项：" + "  ".join("%s %.0f" % (p["name"], p["score"]) for p in c["parts"]))

    if targets:
        print("\n【标的】")
        for code, s, f in zip(targets, syms, tgt_f):
            r = s if f is None else f.result()
            if isinstance(r, Exception):
                print("  %s  获取失败：%s" % (code, r))
            else:
                print_item(r)

    print("\n温度 0-100：<10 冰点 / <25 恐惧 / <75 中性 / <90 贪婪 / ≥90 过热（相对自身近 3 年）")
    print("VIX：<15 平静 / <20 正常 / <30 紧张 / <40 恐慌 / ≥40 极度恐慌；CNN 指数 0-100 越低越恐惧")


if __name__ == "__main__":
    main(sys.argv)
