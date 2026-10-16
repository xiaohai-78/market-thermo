#!/usr/bin/env python3
"""市场温度看板服务。仅依赖标准库。

环境变量: THERMO_HOST(默认 0.0.0.0)  THERMO_PORT(默认 8600)  THERMO_DATA(默认 ./data)
          THERMO_NOTIFY_URL(notify-service 地址，空则不发邮件)  THERMO_PUBLIC_URL(邮件里的看板链接)
"""
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from thermo import alerts, core, signals

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
DATA = os.environ.get("THERMO_DATA") or os.path.join(ROOT, "data")
BREADTH_CSV = os.path.join(DATA, "breadth_history.csv")
WATCHLIST = os.path.join(DATA, "watchlist.json")
VIX_FILE = os.path.join(DATA, "vix_history.json")
SETTINGS = os.path.join(DATA, "settings.json")
DEFAULT_WATCHLIST = ["sz159530", "sz980022"]
NOTIFY_URL = os.environ.get("THERMO_NOTIFY_URL", "")
PUBLIC_URL = os.environ.get("THERMO_PUBLIC_URL", "http://localhost:8600")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("thermo")
pool = ThreadPoolExecutor(8)
os.makedirs(DATA, exist_ok=True)
engine = alerts.AlertEngine(DATA, NOTIFY_URL, PUBLIC_URL, log)

settings_lock = threading.Lock()
_settings = signals.load_settings(SETTINGS)


def get_settings():
    return _settings


def update_settings(patch):
    global _settings
    with settings_lock:
        _settings = signals.save_settings(SETTINGS, signals._merge(_settings, patch))
        return _settings


# ---------- 缓存：盘中 60 秒，其他时间 15 分钟；过期先返回旧值并后台刷新 ----------

STUCK_AFTER = 300   # 后台刷新超过这么久视为卡住，允许重新发起


class Cache:
    def __init__(self):
        self._data, self._locks, self._refreshing, self._lock = {}, {}, {}, threading.Lock()

    def _lock_for(self, key):
        with self._lock:
            return self._locks.setdefault(key, threading.Lock())

    def _spawn(self, key, fn):
        with self._lock:
            started = self._refreshing.get(key)
            if started and time.time() - started < STUCK_AFTER:
                return
            if started:
                log.warning("refresh %s stuck for %.0fs, starting another", key, time.time() - started)
            started = self._refreshing[key] = time.time()
        threading.Thread(target=self._bg_refresh, args=(key, fn, started), daemon=True).start()

    def _bg_refresh(self, key, fn, started):
        try:
            self._data[key] = (time.time(), fn())
        except Exception as e:
            log.warning("refresh %s failed, keeping stale: %s", key, e)
        finally:
            if time.time() - started > 20:
                log.info("refresh %s took %.0fs", key, time.time() - started)
            with self._lock:
                if self._refreshing.get(key) == started:  # 卡住后被新刷新取代的线程不清理别人的标记
                    self._refreshing.pop(key, None)

    def status(self):
        now = time.time()
        return {k: {"age": round(now - v[0]),
                    "refreshing": round(now - self._refreshing[k]) if k in self._refreshing else None}
                for k, v in sorted(self._data.items())}

    def get(self, key, fn, force=False, ttl=None, block=True):
        """block=False 时，无缓存直接返回 None 并在后台加载。"""
        ttl = ttl or (60 if core.is_trading_time() else 900)
        hit = self._data.get(key)
        if hit and not force:
            if time.time() - hit[0] >= ttl:
                self._spawn(key, fn)
            return hit[1]
        if not hit and not block:
            self._spawn(key, fn)
            return None
        lock = self._lock_for(key)
        t0 = time.time()
        with lock:  # 同一个 key 并发请求只取一次数据
            hit = self._data.get(key)
            if hit and hit[0] >= t0:
                return hit[1]
            try:
                value = fn()
            except Exception as e:
                if hit:  # 数据源失败时返回旧值
                    log.warning("refresh %s failed, serving stale: %s", key, e)
                    return hit[1]
                raise
            self._data[key] = (time.time(), value)
            return value


cache = Cache()


def _safe(fn, *a):
    try:
        return {"ok": True, "data": fn(*a)}
    except Exception as e:
        log.warning("%s%s failed: %s", fn.__name__, a, e)
        return {"ok": False, "error": str(e)}


def strip_series(r, days):
    if "series" not in r:
        return r
    out = dict(r)
    out["series"] = {k: v[-days:] for k, v in r["series"].items()}
    return out


def symbol(sym, force=False):
    return cache.get("sym:" + sym, lambda: core.analyze(sym), force)


def get_breadth(force=False):
    def fetch():
        b = core.breadth()
        core.save_breadth(BREADTH_CSV, b)
        return b
    return cache.get("breadth", fetch, force)


def load_vix_history():
    """CBOE 历史从服务器下载很慢且常超时：落盘，6 小时内直接用文件，下载失败也退回文件。"""
    try:
        if time.time() - os.path.getmtime(VIX_FILE) < 6 * 3600:
            with open(VIX_FILE, encoding="utf-8") as f:
                return tuple(json.load(f))
    except (OSError, ValueError):
        pass
    try:
        h = core.vix_history()
    except Exception as e:
        log.warning("vix history download failed, using file: %s", e)
        with open(VIX_FILE, encoding="utf-8") as f:
            return tuple(json.load(f))
    tmp = VIX_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h, f)
    os.replace(tmp, VIX_FILE)
    return h


def get_vix(force=False):
    # 历史只在后台加载，未就绪时先给实时值；实时值随普通缓存刷新
    hist = lambda: cache.get("vix_hist", load_vix_history, ttl=6 * 3600, block=False)  # noqa: E731
    v = cache.get("vix", lambda: core.vix(history=hist(), fetch_history=False), force)
    if v["pctile"] is None and hist():  # 历史刚加载好，立即补上分位和走势
        v = cache.get("vix", lambda: core.vix(history=hist(), fetch_history=False), force=True)
    return v


def overview(force=False):
    futs = {
        "indices": [pool.submit(_safe, symbol, s, force) for s, _ in core.MARKET_INDICES],
        "breadth": pool.submit(_safe, get_breadth, force),
        "vix": pool.submit(_safe, get_vix, force),
        "cnn": pool.submit(_safe, lambda: cache.get("cnn", core.cnn, force)),
    }
    indices = []
    for f in futs["indices"]:
        r = f.result()
        indices.append(dict(r, data=strip_series(r["data"], 250)) if r["ok"] else r)
    temps = [r["data"]["temp"] for r in indices if r["ok"] and r["data"].get("temp") is not None]
    avg = sum(temps) / len(temps) if temps else None
    return {
        "time": core.now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "trading": core.is_trading_time(),
        "market": {"temp": avg, "label": core.label(avg)},
        "indices": indices,
        "breadth": futs["breadth"].result(),
        "breadth_history": core.read_breadth(BREADTH_CSV)[-120:],
        "vix": futs["vix"].result(),
        "cnn": futs["cnn"].result(),
    }


# ---------- 自选 ----------

wl_lock = threading.Lock()


def read_watchlist():
    try:
        with open(WATCHLIST, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return list(DEFAULT_WATCHLIST)


def write_watchlist(items):
    os.makedirs(DATA, exist_ok=True)
    tmp = WATCHLIST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)
    os.replace(tmp, WATCHLIST)


def market_temp():
    temps = []
    for s, _ in core.MARKET_INDICES:
        r = _safe(symbol, s)
        if r["ok"] and r["data"].get("temp") is not None:
            temps.append(r["data"]["temp"])
    return sum(temps) / len(temps) if temps else None


def watchlist(force=False):
    syms = read_watchlist()
    results = list(pool.map(lambda s: _safe(symbol, s, force), syms))
    mkt, s_ = market_temp(), get_settings()
    out = []
    for s, r in zip(syms, results):
        if r["ok"]:
            d = strip_series(r["data"], 60)
            d["signals"] = signals.score(r["data"], mkt, s_)  # 每次请求按当前设置重算，改权重即时生效
            out.append(dict(r, sym=s, data=d))
        else:
            out.append(dict(r, sym=s))
    return out


# ---------- 提醒 ----------

def run_alerts(phase, dry=False, fresh=False):
    """收盘/预警阶段强制刷新数据；盘中用缓存（盘中 TTL 60 秒）。"""
    force = phase != "intraday"
    ov = overview(force=force)
    idx = ov["indices"][0]
    today = core.now_cn().strftime("%Y-%m-%d")
    if not fresh and not (idx["ok"] and idx["data"].get("date") == today):
        return {"skipped": "非交易日或行情未更新", "phase": phase}
    syms = read_watchlist()
    results = list(pool.map(lambda s: _safe(symbol, s, force), syms))
    s_, mkt = get_settings(), ov["market"]["temp"]
    items = [(r["data"], signals.score(r["data"], mkt, s_)) for r in results if r["ok"]]
    return engine.evaluate(phase, ov, items, s_, dry=dry, fresh=fresh)


alert_heartbeat = {"time": None}


def alert_loop():
    last_intraday = 0.0
    while True:
        alert_heartbeat["time"] = time.time()
        try:
            now = core.now_cn()
            today, hm = now.strftime("%Y-%m-%d"), now.hour * 100 + now.minute
            if now.weekday() < 5:
                if 1510 <= hm < 1600 and not engine.ran("close", today):
                    r = run_alerts("close")
                    log.info("alerts close: %s", r.get("skipped") or "%d events" % len(r["events"]))
                    engine.mark_run("close", today)
                elif 1445 <= hm < 1500 and not engine.ran("preview", today):
                    r = run_alerts("preview")
                    log.info("alerts preview: %s", r.get("skipped") or "%d events" % len(r["events"]))
                    engine.mark_run("preview", today)
                elif (940 <= hm <= 1130 or 1300 <= hm <= 1455) and time.time() - last_intraday >= 600:
                    last_intraday = time.time()
                    r = run_alerts("intraday")
                    if r.get("events"):
                        log.info("alerts intraday: %d events", len(r["events"]))
        except Exception:
            log.exception("alert loop")
        time.sleep(30)


# ---------- 后台：预热缓存，并顺带定时记录市场宽度 ----------

def warmer():
    while True:
        ok = False
        try:
            ov = overview()
            wl = watchlist()
            parts = ov["indices"] + [ov["breadth"], ov["vix"], ov["cnn"]] + wl
            ok = all(p["ok"] for p in parts)
        except Exception as e:
            log.warning("warmer: %s", e)
        # 有失败项（多为 DNS 偶发失败）时 30 秒后重试
        time.sleep(30 if core.is_trading_time() or not ok else 300)


# ---------- HTTP ----------

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/"):
            return
        log.info("%s %s", self.address_string(), fmt % args)

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def route(self, method):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        force = q.get("refresh") == "1"
        try:
            if method == "GET" and u.path == "/api/overview":
                return self.send_json(overview(force))
            if method == "GET" and u.path == "/api/search":
                q = q.get("q", "").strip()[:20]
                if not q:
                    return self.send_json([])
                return self.send_json(cache.get("search:" + q, lambda: core.search(q), ttl=3600))
            if method == "GET" and u.path == "/api/settings":
                return self.send_json(get_settings())
            if method == "PUT" and u.path == "/api/settings":
                return self.send_json(update_settings(self.read_body()))
            if method == "GET" and u.path == "/api/alerts":
                return self.send_json(dict(engine.summary(), notify=bool(NOTIFY_URL),
                                           next_runs=alerts.next_runs(), settings=get_settings()["alerts"]))
            if method == "POST" and u.path == "/api/alerts/test":
                engine.send({"title": "【测试】市场温度看板提醒", "summary": "从看板发出的测试邮件，收到说明提醒通道正常。",
                             "detail": "看板：%s\n按规则自动提醒，不构成投资建议。" % PUBLIC_URL})
                return self.send_json({"ok": True})
            if method == "POST" and u.path == "/api/alerts/preview":  # 预演：不发送、不改状态
                phase = q.get("phase", "close")
                if phase not in ("intraday", "preview", "close"):
                    raise ValueError("phase 只能是 intraday / preview / close")
                return self.send_json(run_alerts(phase, dry=True, fresh=q.get("fresh") == "1"))
            if method == "GET" and u.path == "/api/health":
                hb = alert_heartbeat["time"]
                return self.send_json({
                    "time": core.now_cn().strftime("%Y-%m-%d %H:%M:%S"), "cache": cache.status(),
                    "threads": sorted(t.name for t in threading.enumerate()),
                    "alert_loop_age": None if hb is None else round(time.time() - hb),  # 正常应小于 60 秒
                })
            if method == "GET" and u.path == "/api/watchlist":
                return self.send_json(watchlist(force))
            if method == "GET" and u.path == "/api/symbol":
                sym = core.resolve(q.get("code", ""))
                days = max(60, min(800, int(q.get("days", 500))))
                return self.send_json(strip_series(symbol(sym, force), days))
            if method == "POST" and u.path == "/api/watchlist":
                sym = core.resolve(str(self.read_body().get("code", "")))
                r = symbol(sym)
                if "error" in r:
                    return self.send_json({"error": r["error"]}, 400)
                with wl_lock:
                    items = read_watchlist()
                    if sym not in items:
                        items.append(sym)
                        write_watchlist(items)
                return self.send_json({"sym": sym, "name": r["name"]})
            if method == "DELETE" and u.path == "/api/watchlist":
                sym = core.resolve(q.get("code", ""))
                with wl_lock:
                    write_watchlist([s for s in read_watchlist() if s != sym])
                return self.send_json({"sym": sym})
            if method == "PUT" and u.path == "/api/watchlist":  # 整体排序
                items = [core.resolve(str(s)) for s in self.read_body().get("items", [])]
                with wl_lock:
                    current = set(read_watchlist())
                    write_watchlist([s for s in items if s in current])
                return self.send_json({"ok": True})
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        except Exception as e:
            log.exception("api error")
            return self.send_json({"error": str(e)}, 500)
        if method == "GET":
            return super().do_GET()
        self.send_json({"error": "not found"}, 404)

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")

    def do_PUT(self):
        self.route("PUT")


def main():
    host = os.environ.get("THERMO_HOST", "0.0.0.0")
    port = int(os.environ.get("THERMO_PORT", "8600"))
    os.makedirs(DATA, exist_ok=True)
    threading.Thread(target=warmer, name="warmer", daemon=True).start()
    threading.Thread(target=alert_loop, name="alert_loop", daemon=True).start()
    server = ThreadingHTTPServer((host, port), Handler)
    log.info("market-thermo listening on http://%s:%d", host, port)
    server.serve_forever()


if __name__ == "__main__":
    main()
