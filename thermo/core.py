"""数据获取与温度计算。仅依赖标准库，兼容 Python 3.7+。

温度 = 5 个指标在自身近 3 年历史中的分位数均值 (0-100):
  RSI14、MA60 乖离、20 日涨跌、距一年高点回撤、一年区间位置。
K 线: 腾讯 → 东方财富 → 新浪；宽度: 东方财富涨跌分布；
海外: VIX（CBOE 历史 + 新浪实时）、CNN 恐惧贪婪指数。
A 股无可用的 VIX 对应物（中国波指 iVIX 2018 年起停发）。
"""
import bisect
import csv
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
CN_TZ = timezone(timedelta(hours=8))
HIST_WINDOW = 750   # 分位数回看约 3 年
YEAR = 250

MARKET_INDICES = [("sh000300", "沪深300"), ("sh000852", "中证1000"), ("sh000985", "中证全指")]
ALIASES = {
    "上证": "sh000001", "上证指数": "sh000001", "深证成指": "sz399001",
    "沪深300": "sh000300", "中证500": "sh000905", "中证1000": "sh000852",
    "中证全指": "sh000985", "创业板": "sz399006", "创业板指": "sz399006",
    "科创50": "sh000688", "国证机器人": "sz980022",
}
INDICATORS = ["RSI14", "MA60乖离", "20日涨跌", "一年回撤", "区间位置"]


def now_cn():
    return datetime.now(CN_TZ)


def is_trading_time(t=None):
    t = t or now_cn()
    if t.weekday() >= 5:
        return False
    hm = t.hour * 100 + t.minute
    return 915 <= hm <= 1505


def get(url, timeout=10, referer="https://quote.eastmoney.com/", retries=2, ua=UA):
    headers = {"Referer": referer, "Accept": "*/*"}
    if ua:  # ua=None 时不设置，用 urllib 默认 UA
        headers["User-Agent"] = ua
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError):  # DNS 偶发失败、连接重置、超时
            if attempt == retries:
                raise
            time.sleep(1 + attempt)


def kind(sym):
    """个股 / 基金 / 指数，用于机会评分的风险提示。"""
    c = sym[2:]
    if sym.startswith("sh") and c.startswith("000") or c.startswith(("39", "98")):
        return "指数"
    if c.startswith(("5", "15", "16", "18")):
        return "基金"
    return "个股"


SEARCH_TYPES = {"GP-A": "股票", "GP-A-KCB": "科创板", "GP-A-CYB": "创业板", "ETF": "ETF", "LOF": "LOF",
                "QDII-ETF": "跨境ETF", "QDII-LOF": "跨境LOF", "ZS": "指数"}


def search(q, limit=10):
    """模糊搜索 A 股、场内基金、指数：中文名、拼音首字母、代码片段都可以。别名优先。

    数据源为腾讯 smartbox，每次最多 10 条，混有场外基金(jj)/港美股，这里过滤掉。
    """
    q = q.strip()
    out, seen = [], set()
    for alias, sym in ALIASES.items():
        if q and (q in alias or alias in q):
            out.append({"sym": sym, "code": sym[2:], "name": alias, "type": "指数"})
            seen.add(sym)
    raw = get("https://smartbox.gtimg.cn/s3/?v=2&t=all&q=" + urllib.parse.quote(q), referer="https://gu.qq.com/")
    body = raw.split('"', 1)[1].rsplit('"', 1)[0] if raw.count('"') >= 2 else ""
    for item in body.split("^"):
        f = item.split("~")
        if len(f) < 5 or f[0] not in ("sh", "sz", "bj") or f[4] not in SEARCH_TYPES:
            continue
        sym = f[0] + f[1]
        if sym in seen:
            continue
        seen.add(sym)
        try:
            name = json.loads('"%s"' % f[2])  # 名称是 \uXXXX 转义
        except ValueError:
            name = f[2]
        out.append({"sym": sym, "code": f[1], "name": name, "type": SEARCH_TYPES[f[4]]})
    return out[:limit]


def resolve(code):
    """代码/别名 → 带市场前缀的代码。6 位 000xxx 默认按深市个股处理，指数需加前缀或用别名。"""
    c = code.strip()
    if c in ALIASES:
        return ALIASES[c]
    low = c.lower()
    if low[:2] in ("sh", "sz", "bj") and low[2:].isdigit() and len(low) == 8:
        return low
    if not (c.isdigit() and len(c) == 6):
        raise ValueError("无法识别代码: " + code)
    if c.startswith(("39", "98")):
        return "sz" + c
    if c.startswith(("60", "68", "90", "50", "51", "52", "56", "58")):
        return "sh" + c
    if c.startswith(("00", "30", "12", "15", "16", "18")):
        return "sz" + c
    if c.startswith(("4", "8", "92")):
        return "bj" + c
    raise ValueError("无法识别代码: " + code)


# ---------- K 线 ----------
# bar = (date, open, close, high, low, volume)

def _from_tencent(sym):
    raw = json.loads(get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,800,qfq" % sym))
    d = raw["data"][sym]
    rows = d.get("qfqday") or d.get("day") or []
    bars = [(r[0],) + tuple(float(x) for x in r[1:6]) for r in rows]
    qt = d.get("qt", {}).get(sym, [])
    quote = None
    if len(qt) > 34 and qt[3]:
        ts = "".join(ch for ch in qt[30] if ch.isdigit())
        quote = {
            "name": qt[1], "price": float(qt[3]), "pct": float(qt[32] or 0),
            "date": "%s-%s-%s" % (ts[:4], ts[4:6], ts[6:8]) if len(ts) >= 8 else None,
            "time": ts[8:12] if len(ts) >= 12 else "",
            "bar": (float(qt[5] or qt[3]), float(qt[3]), float(qt[33] or qt[3]),
                    float(qt[34] or qt[3]), float(qt[6] or 0)),
        }
    return bars, quote


def _from_eastmoney(sym):
    secid = ("1." if sym.startswith("sh") else "0.") + sym[2:]
    raw = json.loads(get("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=" + secid +
                         "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56&klt=101&fqt=1&beg=20220101&end=20500101"))
    data = raw.get("data") or {}
    bars = []
    for line in data.get("klines") or []:
        p = line.split(",")
        bars.append((p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])))
    return bars, data.get("name")


def _from_sina(sym):
    rows = json.loads(get("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol="
                          + sym + "&scale=240&ma=no&datalen=800") or "[]") or []
    bars = [(r["day"], float(r["open"]), float(r["close"]), float(r["high"]), float(r["low"]), float(r["volume"]))
            for r in rows]
    return bars, None


STALE_DAYS = 20   # 最后一根 K 线早于此天数的数据源视为停更（如新浪的中证全指停在 2016 年）


def load_bars(sym):
    """返回 (name, bars, quote, source, mixed)。

    腾讯优先；腾讯失败或历史不足一年时尝试其他源，只接受未停更的数据。
    所有源都不可用时抛异常，让上层缓存保留上一次的正确结果。
    """
    bars, quote, name, source = [], None, None, ""
    cutoff = (now_cn() - timedelta(days=STALE_DAYS)).strftime("%Y-%m-%d")
    fresh = lambda b: bool(b) and b[-1][0] >= cutoff  # noqa: E731
    try:
        bars, quote = _from_tencent(sym)
        name, source = (quote or {}).get("name"), "腾讯"
    except Exception:
        pass
    if not fresh(bars):
        bars = []
    if len(bars) < YEAR:
        for label, fn in (("东方财富", _from_eastmoney), ("新浪", _from_sina)):
            try:
                alt, alt_name = fn(sym)
            except Exception:
                continue
            if fresh(alt) and len(alt) > len(bars):
                bars, source = alt, label
                name = name or alt_name
            if len(bars) >= YEAR:
                break
    if not bars:
        raise RuntimeError("所有数据源均不可用或已停更: " + sym)
    # 历史里缺当天时用实时行情补一根；非腾讯历史与腾讯行情的成交量单位不一致
    mixed = False
    if quote and quote["date"] and (not bars or quote["date"] > bars[-1][0]):
        bars.append((quote["date"],) + quote["bar"])
        mixed = source != "腾讯"
    return name or sym, bars, quote, source, mixed


# ---------- 指标 ----------

def _rsi(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(closes)):
        if i > n:
            ag = (ag * (n - 1) + gains[i - 1]) / n
            al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def indicator_series(closes):
    n = len(closes)
    bias, ret20, dd, pos, high = [None] * n, [None] * n, [None] * n, [None] * n, [None] * n
    for i in range(n):
        w = closes[max(0, i - YEAR + 1):i + 1]
        high[i] = max(w)
        if i >= 20:
            ret20[i] = closes[i] / closes[i - 20] - 1
        if i >= 59:
            bias[i] = closes[i] / (sum(closes[i - 59:i + 1]) / 60) - 1
            lo = min(w)
            dd[i] = closes[i] / high[i] - 1
            pos[i] = (closes[i] - lo) / (high[i] - lo) if high[i] > lo else 0.5
    return {"RSI14": _rsi(closes), "MA60乖离": bias, "20日涨跌": ret20, "一年回撤": dd, "区间位置": pos}, high


def rolling_percentile(series):
    """每个位置的值在其前 HIST_WINDOW 个有效值（含自身）中的分位，样本少于 20 时为 None。"""
    out, window, sorted_w = [None] * len(series), [], []
    for i, v in enumerate(series):
        if v is None:
            continue
        window.append(v)
        bisect.insort(sorted_w, v)
        if len(window) > HIST_WINDOW:
            old = window.pop(0)
            del sorted_w[bisect.bisect_left(sorted_w, old)]
        if len(sorted_w) >= 20:
            out[i] = bisect.bisect_right(sorted_w, v) / len(sorted_w) * 100
    return out


def label(t):
    if t is None:
        return "数据不足"
    if t < 10:
        return "冰点"
    if t < 25:
        return "恐惧"
    if t < 75:
        return "中性"
    if t < 90:
        return "贪婪"
    return "过热"


def analyze(sym, series_days=500):
    """单个标的的完整分析；series_days 控制返回的历史序列长度。"""
    name, bars, quote, source, mixed = load_bars(sym)
    if len(bars) < 80:
        return {"sym": sym, "name": name, "error": "历史数据不足（%d 根）" % len(bars)}
    closes = [b[2] for b in bars]
    vols = [b[5] for b in bars]
    ser, high = indicator_series(closes)
    pct_ser = {k: rolling_percentile(v) for k, v in ser.items()}
    temps = []
    for i in range(len(closes)):
        vals = [pct_ser[k][i] for k in INDICATORS if pct_ser[k][i] is not None]
        temps.append(sum(vals) / len(vals) if len(vals) >= 3 else None)
    down_days = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            down_days += 1
        else:
            break
    prev_vol = vols[-21:-1]
    # 企稳（0-100）：站上 5 日线、RSI 较近 5 日低点回升（回升 10 点记满分）、连跌天数少
    rsi = ser["RSI14"]
    recent_rsi = [v for v in rsi[-6:] if v is not None]
    ma5 = sum(closes[-5:]) / 5
    rsi_rebound = rsi[-1] - min(recent_rsi) if recent_rsi and rsi[-1] is not None else 0.0
    s_ma5 = 100.0 if closes[-1] > ma5 else 0.0
    s_rsi = max(0.0, min(100.0, rsi_rebound * 10))
    s_down = max(0.0, 100.0 - 30 * down_days)
    # 滞涨（0-100，与企稳对称）：跌破 5 日线、RSI 较近 5 日高点回落（回落 10 点记满分）、连涨中断
    up_days = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] > closes[i - 1]:
            up_days += 1
        else:
            break
    rsi_drop = max(recent_rsi) - rsi[-1] if recent_rsi and rsi[-1] is not None else 0.0
    t_ma5 = 100.0 if closes[-1] < ma5 else 0.0
    t_rsi = max(0.0, min(100.0, rsi_drop * 10))
    t_up = max(0.0, 100.0 - 30 * up_days)
    k = -series_days
    return {
        "sym": sym, "name": name, "kind": kind(sym), "source": source, "date": bars[-1][0], "bars": len(bars),
        "price": closes[-1],
        "pct": quote["pct"] if quote else (closes[-1] / closes[-2] - 1) * 100,
        "intraday": bool(quote and quote["date"] == bars[-1][0] and quote["time"] and quote["time"] < "1500"),
        "temp": temps[-1], "label": label(temps[-1]),
        "raw": {k2: v[-1] for k2, v in ser.items()},
        "pcts": {k2: v[-1] for k2, v in pct_ser.items()},
        "vol_ratio": vols[-1] / (sum(prev_vol) / len(prev_vol)) if prev_vol and sum(prev_vol) > 0 and not mixed else None,
        "down_days": down_days,
        "stab": {"score": (s_ma5 + s_rsi + s_down) / 3, "above_ma5": closes[-1] > ma5,
                 "rsi_rebound": rsi_rebound, "down_days": down_days},
        "stall": {"score": (t_ma5 + t_rsi + t_up) / 3, "below_ma5": closes[-1] < ma5,
                  "rsi_drop": rsi_drop, "up_days": up_days},
        "rise_from_low": closes[-1] / min(closes[-YEAR:]) - 1,
        "short_history": len(bars) < HIST_WINDOW,
        "series": {
            "dates": [b[0] for b in bars[k:]],
            "close": closes[k:],
            "high250": high[k:],
            "temp": temps[k:],
            "drawdown": ser["一年回撤"][k:],
        },
    }


# ---------- 市场宽度 ----------

def breadth():
    raw = json.loads(get("https://push2ex.eastmoney.com/getTopicZDFenBu?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"))
    data = raw["data"]
    buckets = {int(k): v for d in data["fenbu"] for k, v in d.items()}
    q = str(data["qdate"])
    b = {
        "date": "%s-%s-%s" % (q[:4], q[4:6], q[6:]),
        "up": sum(v for k, v in buckets.items() if k > 0),
        "down": sum(v for k, v in buckets.items() if k < 0),
        "flat": buckets.get(0, 0),
        "zt": buckets.get(11, 0), "dt": buckets.get(-11, 0),
        "up5": sum(v for k, v in buckets.items() if k >= 6),     # 键 6 = (5%, 6%]
        "down5": sum(v for k, v in buckets.items() if k <= -6),
        "buckets": [[k, buckets[k]] for k in sorted(buckets)],
    }
    total = b["up"] + b["down"] + b["flat"]
    b["ratio"] = b["up"] / total if total else 0
    b["label"] = breadth_label(b["ratio"], b["dt"])
    return b


def breadth_label(ratio, dt):
    if ratio < 0.10 or dt >= 100:
        return "极度恐慌"
    if ratio < 0.25:
        return "恐慌"
    if ratio < 0.75:
        return "分化"
    if ratio < 0.90:
        return "普涨"
    return "狂热"


BREADTH_FIELDS = ["date", "up", "down", "flat", "zt", "dt", "up5", "down5"]


def read_breadth(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return sorted(csv.DictReader(f), key=lambda r: r["date"])


def save_breadth(path, b):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    rows = {r["date"]: r for r in read_breadth(path)}
    rows[b["date"]] = {k: b[k] for k in BREADTH_FIELDS}
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=BREADTH_FIELDS)
        w.writeheader()
        for d in sorted(rows):
            w.writerow(rows[d])
    os.replace(tmp, path)
    return [rows[d] for d in sorted(rows)]


# ---------- 海外情绪（美股） ----------

CNN_RATING = {"extreme fear": "极度恐惧", "fear": "恐惧", "neutral": "中性", "greed": "贪婪", "extreme greed": "极度贪婪"}
CNN_PARTS = [("market_momentum_sp500", "标普动量"), ("stock_price_strength", "股价强度"),
             ("stock_price_breadth", "股价宽度"), ("put_call_options", "看跌看涨比"),
             ("market_volatility_vix", "波动率"), ("junk_bond_demand", "垃圾债需求"),
             ("safe_haven_demand", "避险需求")]


def vix_label(v):
    if v < 15:
        return "平静"
    if v < 20:
        return "正常"
    if v < 30:
        return "紧张"
    if v < 40:
        return "恐慌"
    return "极度恐慌"


def vix_history():
    """CBOE 官方日收盘（约 0.5MB，国内下载慢，调用方应长时间缓存）。"""
    rows = list(csv.reader(get("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
                               timeout=60).splitlines()))[1:]
    rows = [r for r in rows if len(r) >= 5]
    return ["%s-%s-%s" % (r[0][6:10], r[0][0:2], r[0][3:5]) for r in rows], [float(r[4]) for r in rows]


def vix(series_days=500, history=None, fetch_history=True):
    """CBOE 历史 + 新浪实时。VIX 越高越恐慌。没有历史时只给实时值，分位为 None。"""
    if history is None and fetch_history:
        history = vix_history()
    dates, closes = history or ([], [])
    value = pct = when = None
    realtime = False
    if len(closes) >= 2:
        value, pct, when = closes[-1], (closes[-1] / closes[-2] - 1) * 100, dates[-1] + " 收盘"
    try:
        f = get("https://hq.sinajs.cn/list=znb_VIX", referer="https://finance.sina.com.cn").split('"')[1].split(",")
        if (not dates or f[6] >= dates[-1]) and float(f[1]) > 0:
            value, pct, when, realtime = float(f[1]), float(f[3]), "%s %s 北京时间" % (f[6], f[7][:5]), True
    except Exception:
        if value is None:
            raise
    window = closes[-HIST_WINDOW:]
    return {"value": value, "pct": pct, "when": when, "realtime": realtime, "label": vix_label(value),
            "pctile": sum(v <= value for v in window) / len(window) * 100 if window else None,
            "series": {"dates": dates[-series_days:], "close": closes[-series_days:]}}


def cnn():
    raw = json.loads(get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
                         referer="https://edition.cnn.com/"))
    fg = raw["fear_and_greed"]
    hist = raw.get("fear_and_greed_historical", {}).get("data", [])
    return {
        "score": fg["score"], "rating": CNN_RATING.get(fg["rating"], fg["rating"]),
        "previous_close": fg.get("previous_close"), "previous_1_week": fg.get("previous_1_week"),
        "previous_1_month": fg.get("previous_1_month"), "previous_1_year": fg.get("previous_1_year"),
        "updated": fg.get("timestamp"),
        "parts": [{"key": k, "name": zh, "score": raw[k]["score"],
                   "rating": CNN_RATING.get(raw[k].get("rating"), raw[k].get("rating"))}
                  for k, zh in CNN_PARTS if isinstance(raw.get(k), dict)],
        "series": {
            "dates": [datetime.fromtimestamp(p["x"] / 1000, timezone.utc).strftime("%Y-%m-%d") for p in hist],
            "score": [p["y"] for p in hist],
        },
    }


# ---------- 美国财政（财政部 FiscalData + FRED，均为官方数据，免密钥） ----------

FISCAL_API = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"
FISCAL_REFERER = "https://fiscaldata.treasury.gov/"


def _fiscal_get(path, params):
    return json.loads(get("%s/%s?%s" % (FISCAL_API, path, urllib.parse.urlencode(params)),
                          timeout=15, referer=FISCAL_REFERER, retries=1))["data"]


def fiscal_debt(series_days=250):
    """国债总额（Debt to the Penny，工作日更新），单位万亿美元；对比一年前。"""
    rows = _fiscal_get("v2/accounting/od/debt_to_penny", {
        "sort": "-record_date", "page[size]": 400, "fields": "record_date,tot_pub_debt_out_amt"})[::-1]
    dates = [r["record_date"] for r in rows]
    vals = [float(r["tot_pub_debt_out_amt"]) / 1e12 for r in rows]
    target = (datetime.strptime(dates[-1], "%Y-%m-%d") - timedelta(days=365)).strftime("%Y-%m-%d")
    i = bisect.bisect_right(dates, target) - 1
    ago = vals[i] if i >= 0 else None
    return {"value": vals[-1], "date": dates[-1], "year_ago": ago,
            "change": vals[-1] - ago if ago else None,
            "change_pct": (vals[-1] / ago - 1) * 100 if ago else None,
            "series": {"dates": dates[-series_days:], "close": vals[-series_days:]}}


def fiscal_budget():
    """月度财政报告（MTS Table 9）：财年累计的收入、净利息、国防，单位万亿美元。"""
    rows = _fiscal_get("v1/accounting/mts/mts_table_9", {"sort": "-record_date", "page[size]": 40})
    last = max(r["record_date"] for r in rows)
    rows = [r for r in rows if r["record_date"] == last]
    desc = {r["classification_id"]: r["classification_desc"] for r in rows}
    pick = {}
    for r in rows:
        name = r["classification_desc"]
        if name == "Total":  # 收入合计和支出合计同名，按所属分组区分
            name = "Total receipts" if desc.get(r["parent_id"]) == "Receipts" else "Total outlays"
        pick[name] = r

    def amt(name, field):
        v = pick[name]["%s_fytd_rcpt_outly_amt" % field]
        return float(v) / 1e12 if v not in (None, "null") else None
    try:
        rcpt, outl = amt("Total receipts", "current"), amt("Total outlays", "current")
        intr, intr0 = amt("Net Interest", "current"), amt("Net Interest", "prior")
        defn, defn0 = amt("National Defense", "current"), amt("National Defense", "prior")
    except KeyError as e:
        raise ValueError("MTS 表结构变化，缺少 %s" % e)
    if not (rcpt and outl and intr and defn):
        raise ValueError("MTS 数据不完整")
    fy = int(pick["Net Interest"]["record_fiscal_year"])
    full = last[5:7] == "09"  # 美国财年 10 月 1 日起，9 月底为全年
    return {"date": last, "fy": fy, "period": "FY%d %s" % (fy, "全年" if full else "累计至 " + last[5:]), "full_year": full,
            "receipts": rcpt, "outlays": outl, "deficit": outl - rcpt,
            "interest": intr, "interest_yoy": (intr / intr0 - 1) * 100 if intr0 else None,
            "defense": defn, "defense_yoy": (defn / defn0 - 1) * 100 if defn0 else None,
            "interest_vs_receipts": intr / rcpt * 100, "interest_vs_outlays": intr / outl * 100,
            "interest_vs_defense": (intr / defn - 1) * 100}


def fiscal_yield(series_days=250):
    """10 年期美债收益率（FRED DGS10，滞后约 1 个交易日）：当前值、日变动（bp）、近 3 年分位。"""
    since = (datetime.now(timezone.utc) - timedelta(days=3 * 365)).strftime("%Y-%m-%d")
    rows = [ln.split(",") for ln in get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10&cosd=" + since,
                                        timeout=15, referer="https://fred.stlouisfed.org/", retries=1,
                                        ua=None).splitlines()[1:]]  # FRED 对浏览器 UA 和自定义 UA 会挂起不返回，只有 urllib 默认 UA 正常
    rows = [(r[0], float(r[1])) for r in rows if len(r) == 2 and r[1] not in ("", ".")]  # 假日为空值
    if len(rows) < 2:
        raise ValueError("FRED 返回数据不足")
    dates, vals = [r[0] for r in rows], [r[1] for r in rows]
    return {"value": vals[-1], "date": dates[-1], "change_bp": (vals[-1] - vals[-2]) * 100,
            "pctile": sum(v <= vals[-1] for v in vals) / len(vals) * 100,
            "series": {"dates": dates[-series_days:], "close": vals[-series_days:]}}


def fiscal_plain(debt=None, budget=None, yld=None):
    """把已有的数据拼成一句大白话；哪项没取到就略过哪项。"""
    out = []
    if debt:
        s = "美国一共欠了 %.1f 万亿美元" % debt["value"]
        if debt["change"] is not None:
            s += "，比一年前%s了 %.1f 万亿" % ("多" if debt["change"] >= 0 else "少", abs(debt["change"]))
        out.append(s)
    if budget:
        s = ("一年光利息就要付 %.2f 万亿" if budget["full_year"] else "这个财年到现在光利息就付了 %.2f 万亿") % budget["interest"]
        d = budget["interest_vs_defense"]
        s += "，%s，等于每收进 100 美元就有 %.0f 美元先拿去还利息" % (
            "比军费还多" if d > 0 else "相当于军费的 %.0f%%" % (100 + d), budget["interest_vs_receipts"])
        out.append(s)
    if yld:
        s = "现在想借它十年的钱，市场要 %.1f%% 的年息" % yld["value"]
        if yld["pctile"] >= 80:
            s += "，是近 3 年的高位"
        elif yld["pctile"] <= 20:
            s += "，是近 3 年的低位"
        out.append(s)
    return "；".join(out) + "。" if out else None
