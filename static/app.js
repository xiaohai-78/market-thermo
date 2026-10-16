"use strict";

const $ = (s, el = document) => el.querySelector(s);
const state = {
  ov: null, wl: [], sort: { key: "temp", dir: 1 }, sel: null, detail: null,
  days: 250, tab: "temp", timer: null, loadedAt: 0,
};
const charts = {};

// ---------- 工具 ----------
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const ZONES = [[0, 10, "--t0", "冰点"], [10, 25, "--t1", "恐惧"], [25, 75, "--t2", "中性"], [75, 90, "--t3", "贪婪"], [90, 100, "--t4", "过热"]];
const zoneIdx = (t) => (t == null ? 2 : t < 10 ? 0 : t < 25 ? 1 : t < 75 ? 2 : t < 90 ? 3 : 4);
const tcolor = (t) => css(ZONES[zoneIdx(t)][2]);
const tag = (text, z) => `<span class="tag t${z}">${text}</span>`;
const n0 = (v) => (v == null ? "--" : Math.round(v));
const sgn = (v, d = 2) => (v == null ? "--" : (v > 0 ? "+" : "") + v.toFixed(d) + "%");
const pctf = (v, d = 1) => (v == null ? "--" : (v * 100).toFixed(d) + "%");
const ud = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const price = (p) => (p == null ? "--" : p < 10 ? p.toFixed(3) : p.toFixed(2));
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
// VIX 低 = 自满（偏贪婪），高 = 恐慌
const vixZone = (v) => (v < 15 ? 3 : v < 20 ? 2 : v < 30 ? 1 : 0);
// CNN 官方分段：25 / 45 / 55 / 75
const cnnZone = (s) => (s < 25 ? 0 : s < 45 ? 1 : s < 55 ? 2 : s < 75 ? 3 : 4);

function store(k, v) {
  try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; }
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

function chart(id, el) {
  if (charts[id] && charts[id].getDom() !== el) { charts[id].dispose(); delete charts[id]; }
  if (!charts[id]) charts[id] = echarts.init(el);
  return charts[id];
}
function disposeWhere(pred) {
  for (const k of Object.keys(charts)) if (pred(k)) { charts[k].dispose(); delete charts[k]; }
}
const base = () => ({
  animationDuration: 400,
  textStyle: { color: css("--muted"), fontFamily: "inherit" },
  tooltip: {
    trigger: "axis", backgroundColor: css("--card"), borderColor: css("--border"),
    textStyle: { color: css("--text"), fontSize: 12 }, confine: true,
  },
});
const axisStyle = () => ({
  axisLine: { lineStyle: { color: css("--border") } },
  axisLabel: { color: css("--muted"), fontSize: 11 },
  splitLine: { lineStyle: { color: css("--border"), type: "dashed" } },
  axisTick: { show: false },
});
const zoneBands = (opacity = 0.08) => ({
  silent: true,
  data: ZONES.map(([a, b, c]) => [{ yAxis: a, itemStyle: { color: css(c), opacity } }, { yAxis: b }]),
});

function spark(id, el, data, color, opts = {}) {
  const c = chart(id, el);
  c.setOption({
    animation: false, grid: { left: 0, right: 0, top: 2, bottom: 2 },
    xAxis: { type: "category", show: false, data: data.map((_, i) => i) },
    yAxis: { type: "value", show: false, min: opts.min, max: opts.max, scale: opts.min == null },
    series: [{
      type: "line", data, showSymbol: false, smooth: true, lineStyle: { width: 1.5, color },
      areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: color + "55" }, { offset: 1, color: color + "00" }]) },
    }],
  }, true);
}

// ---------- 总览 ----------
function renderOverview() {
  const ov = state.ov;
  $("#updated").textContent = "更新于 " + ov.time;
  const st = $("#status");
  st.textContent = ov.trading ? "盘中 · 60 秒刷新" : "休市 · 10 分钟刷新";
  st.classList.toggle("live", ov.trading);

  // 大盘仪表
  const t = ov.market.temp;
  chart("gauge", $("#gauge-market")).setOption({
    series: [{
      type: "gauge", min: 0, max: 100, startAngle: 205, endAngle: -25, radius: "100%", center: ["50%", "60%"],
      axisLine: { lineStyle: { width: 14, color: ZONES.map(([, b, c]) => [b / 100, css(c)]) } },
      pointer: { length: "50%", width: 5, offsetCenter: [0, 0], itemStyle: { color: css("--text") } },
      anchor: { show: true, size: 12, itemStyle: { color: css("--text") } },
      axisTick: { show: false }, splitLine: { show: false },
      axisLabel: { distance: -34, color: css("--faint"), fontSize: 10, formatter: (v) => ([0, 10, 25, 75, 90, 100].includes(v) ? v : "") },
      splitNumber: 20,
      title: { offsetCenter: [0, "64%"], color: css("--muted"), fontSize: 12 },
      detail: { offsetCenter: [0, "38%"], fontSize: 30, fontWeight: 700, color: tcolor(t), formatter: (v) => (t == null ? "--" : Math.round(v)) },
      data: [{ value: t == null ? 0 : t, name: "大盘温度" }],
    }],
  });
  const b = ov.breadth.ok ? ov.breadth.data : null;
  $("#market-foot").innerHTML = tag(ov.market.label, zoneIdx(t)) +
    (b ? ` <span class="tag soft">今日情绪 · ${b.label}</span>` : "");

  renderBreadth();
  renderGlobal();
  renderIndices();
  renderTrend();
}

function renderBreadth() {
  const box = $("#breadth-body");
  box.classList.remove("skeleton");
  const r = state.ov.breadth;
  if (!r.ok) { box.innerHTML = `<div class="err">宽度数据获取失败：${esc(r.error)}</div>`; return; }
  const b = r.data, total = b.up + b.down + b.flat || 1;
  $("#breadth-date").textContent = b.date;
  const bz = b.label === "极度恐慌" ? 0 : b.label === "恐慌" ? 1 : b.label === "分化" ? 2 : b.label === "普涨" ? 3 : 4;
  box.innerHTML = `
    <div class="row"><span class="big">${(b.ratio * 100).toFixed(0)}%</span><span class="hint">上涨占比</span>${tag(b.label, bz)}</div>
    <div class="split"><i class="u" style="width:${b.up / total * 100}%"></i><i class="f" style="width:${b.flat / total * 100}%"></i><i class="d" style="width:${b.down / total * 100}%"></i></div>
    <div class="split-legend"><span class="up">涨 ${b.up}</span><span>平 ${b.flat}</span><span class="down">跌 ${b.down}</span></div>
    <div class="kv">
      <div><span>涨停</span><b class="up">${b.zt}</b></div><div><span>跌停</span><b class="down">${b.dt}</b></div>
      <div><span>涨超5%</span><b class="up">${b.up5}</b></div><div><span>跌超5%</span><b class="down">${b.down5}</b></div>
    </div>
    <div class="mini" id="buckets"></div>`;
  const ks = b.buckets.map(([k]) => k);
  const name = (k) => (k === 11 ? "涨停" : k === -11 ? "跌停" : k === 0 ? "平" : k > 0 ? `${k - 1}~${k}%` : `${k + 1}~${k}%`);
  chart("buckets", $("#buckets")).setOption({
    ...base(), grid: { left: 2, right: 2, top: 6, bottom: 18 },
    tooltip: { ...base().tooltip, formatter: (p) => `${name(ks[p[0].dataIndex])}：${p[0].value} 家` },
    xAxis: { type: "category", data: ks.map(name), ...axisStyle(), axisLabel: { color: css("--muted"), fontSize: 10, interval: (i) => [0, ks.indexOf(0), ks.length - 1].includes(i) } },
    yAxis: { type: "value", show: false },
    series: [{
      type: "bar", barWidth: "70%", data: b.buckets.map(([k, v]) => ({ value: v, itemStyle: { color: k > 0 ? css("--up") : k < 0 ? css("--down") : css("--faint"), borderRadius: [2, 2, 0, 0] } })),
    }],
  }, true);
}

function renderGlobal() {
  const box = $("#global-body");
  box.classList.remove("skeleton");
  const v = state.ov.vix, c = state.ov.cnn;
  const vixHtml = v.ok ? `
      <div class="g-title">VIX 波动率</div>
      <div class="row"><span class="big">${v.data.value.toFixed(2)}</span><span class="${ud(v.data.pct)}">${sgn(v.data.pct)}</span>${tag(v.data.label, vixZone(v.data.value))}</div>
      <div class="hint">近 3 年分位 ${n0(v.data.pctile)} · ${esc(v.data.when)}</div>
      <div class="mini" id="vix-spark"></div>`
    : `<div class="err">VIX 获取失败：${esc(v.error)}</div>`;
  let cnnHtml;
  if (c.ok) {
    const d = c.data;
    cnnHtml = `
      <div class="g-title">CNN 恐惧贪婪</div>
      <div class="row"><span class="big">${n0(d.score)}</span>${tag(d.rating, cnnZone(d.score))}</div>
      <div class="fg-bar"><i style="left:${d.score}%"></i></div>
      <div class="hint">前收 ${n0(d.previous_close)} · 1周前 ${n0(d.previous_1_week)} · 1月前 ${n0(d.previous_1_month)}</div>
      <div class="parts">${d.parts.map((p) => `
        <div class="p"><span>${p.name}</span><em><i style="width:${p.score}%;background:${css(ZONES[cnnZone(p.score)][2])}"></i></em><b>${n0(p.score)}</b></div>`).join("")}
      </div>`;
  } else cnnHtml = `<div class="err">CNN 获取失败：${esc(c.error)}</div>`;
  box.innerHTML = `<div class="global"><div>${vixHtml}</div><div>${cnnHtml}</div></div>`;
  if (v.ok) spark("vix-spark", $("#vix-spark"), v.data.series.close.slice(-250), css(ZONES[vixZone(v.data.value)][2]));
}

function renderIndices() {
  const box = $("#indices");
  disposeWhere((k) => k.startsWith("idx-"));
  box.innerHTML = state.ov.indices.map((r, i) => {
    if (!r.ok) return `<article class="card idx"><div class="err">获取失败：${esc(r.error)}</div></article>`;
    const d = r.data;
    if (d.error) return `<article class="card idx"><div class="name">${esc(d.name)}</div><div class="err">${esc(d.error)}</div></article>`;
    return `<article class="card idx" data-sym="${d.sym}" style="cursor:pointer" title="查看详情">
      <div class="top">
        <div><span class="name">${esc(d.name)}</span><span class="code">${d.sym}</span>
          <div class="row"><b class="num">${price(d.price)}</b><span class="num ${ud(d.pct)}">${sgn(d.pct)}</span></div></div>
        <div class="temp"><b style="color:${tcolor(d.temp)}">${n0(d.temp)}</b>${tag(d.label, zoneIdx(d.temp))}</div>
      </div>
      <div class="metrics num">
        <div><span>距一年高点</span>${pctf(d.raw["一年回撤"])}</div>
        <div><span>RSI14</span>${n0(d.raw.RSI14)}</div>
        <div><span>MA60乖离</span>${pctf(d.raw["MA60乖离"])}</div>
        <div><span>量比</span>${d.vol_ratio == null ? "--" : d.vol_ratio.toFixed(2)}</div>
      </div>
      <div class="spark" id="idx-${i}"></div>
    </article>`;
  }).join("");
  state.ov.indices.forEach((r, i) => {
    if (r.ok && !r.data.error) spark("idx-" + i, $("#idx-" + i), r.data.series.temp, tcolor(r.data.temp), { min: 0, max: 100 });
  });
  box.querySelectorAll("[data-sym]").forEach((el) => el.addEventListener("click", () => openDetail(el.dataset.sym)));
}

function renderTrend() {
  const ov = state.ov, el = $("#trend-chart"), note = $("#trend-note");
  const c = chart("trend", el);
  const ax = axisStyle();
  note.textContent = "";
  let opt;
  if (state.tab === "temp") {
    const ok = ov.indices.filter((r) => r.ok && !r.data.error);
    const palette = [css("--accent"), css("--t3"), css("--t4")];
    opt = {
      legend: { top: 0, textStyle: { color: css("--muted") } },
      grid: { left: 36, right: 16, top: 34, bottom: 58 },
      xAxis: { type: "category", data: ok.length ? ok[0].data.series.dates : [], ...ax, splitLine: { show: false } },
      yAxis: { type: "value", min: 0, max: 100, ...ax },
      dataZoom: [{ type: "inside" }, { type: "slider", height: 16, bottom: 8, showDetail: false, borderColor: "transparent" }],
      series: ok.map((r, i) => ({
        name: r.data.name, type: "line", showSymbol: false, smooth: 0.2, data: r.data.series.temp.map((v) => (v == null ? null : +v.toFixed(1))),
        lineStyle: { width: 1.8, color: palette[i % 3] }, itemStyle: { color: palette[i % 3] },
        markArea: i === 0 ? zoneBands() : undefined,
      })),
    };
    note.textContent = "近一年温度。背景色带：冰点 <10 / 恐惧 <25 / 中性 / 贪婪 ≥75 / 过热 ≥90。";
  } else if (state.tab === "breadth") {
    const h = ov.breadth_history;
    opt = {
      legend: { top: 0, textStyle: { color: css("--muted") } },
      grid: { left: 40, right: 40, top: 34, bottom: 30 },
      xAxis: { type: "category", data: h.map((r) => r.date), ...ax, splitLine: { show: false } },
      yAxis: [{ type: "value", min: 0, max: 100, name: "上涨占比%", nameTextStyle: { color: css("--muted") }, ...ax },
              { type: "value", name: "跌停", nameTextStyle: { color: css("--muted") }, ...ax, splitLine: { show: false } }],
      series: [
        { name: "上涨占比", type: "bar", barMaxWidth: 22, data: h.map((r) => { const t = +r.up + +r.down + +r.flat; const v = t ? +r.up / t * 100 : 0; return { value: +v.toFixed(1), itemStyle: { color: v >= 50 ? css("--up") : css("--down"), borderRadius: [3, 3, 0, 0] } }; }) },
        { name: "跌停", type: "line", yAxisIndex: 1, data: h.map((r) => +r.dt), lineStyle: { color: css("--t0") }, itemStyle: { color: css("--t0") } },
      ],
    };
    note.textContent = h.length < 20
      ? `宽度没有历史接口，从 ${h.length ? h[0].date : "今天"} 起由服务每 10 分钟采集一次，目前已记录 ${h.length} 天。`
      : "每个交易日收盘后的全市场上涨占比与跌停家数。";
  } else if (state.tab === "cnn") {
    if (!ov.cnn.ok) { c.clear(); note.textContent = "CNN 数据获取失败"; return; }
    const s = ov.cnn.data.series;
    opt = {
      grid: { left: 36, right: 16, top: 16, bottom: 58 },
      xAxis: { type: "category", data: s.dates, ...ax, splitLine: { show: false } },
      yAxis: { type: "value", min: 0, max: 100, ...ax },
      dataZoom: [{ type: "inside" }, { type: "slider", height: 16, bottom: 8, showDetail: false, borderColor: "transparent" }],
      series: [{
        name: "恐惧贪婪", type: "line", showSymbol: false, data: s.score.map((v) => +v.toFixed(1)),
        lineStyle: { color: css("--text"), width: 1.6 }, itemStyle: { color: css("--text") },
        markArea: { silent: true, data: [[0, 25, "--t0"], [25, 45, "--t1"], [45, 55, "--t2"], [55, 75, "--t3"], [75, 100, "--t4"]].map(([a, b2, col]) => [{ yAxis: a, itemStyle: { color: css(col), opacity: 0.1 } }, { yAxis: b2 }]) },
      }],
    };
    note.textContent = "CNN 分段：极度恐惧 <25 / 恐惧 <45 / 中性 <55 / 贪婪 <75 / 极度贪婪。";
  } else {
    if (!ov.vix.ok) { c.clear(); note.textContent = "VIX 数据获取失败"; return; }
    const s = ov.vix.data.series;
    opt = {
      grid: { left: 36, right: 16, top: 16, bottom: 58 },
      xAxis: { type: "category", data: s.dates, ...ax, splitLine: { show: false } },
      yAxis: { type: "value", scale: true, ...ax },
      dataZoom: [{ type: "inside", start: 50 }, { type: "slider", height: 16, bottom: 8, showDetail: false, borderColor: "transparent", start: 50 }],
      series: [{
        name: "VIX", type: "line", showSymbol: false, data: s.close, lineStyle: { color: css("--t4"), width: 1.4 }, itemStyle: { color: css("--t4") },
        markLine: { silent: true, symbol: "none", lineStyle: { type: "dashed", color: css("--faint") }, label: { color: css("--muted"), formatter: "{c}" }, data: [{ yAxis: 20 }, { yAxis: 30 }] },
      }],
    };
    note.textContent = "VIX 收盘价（CBOE）。20 以上偏紧张，30 以上为恐慌。";
  }
  c.setOption({ ...base(), ...opt }, true);
}

// ---------- 自选 ----------
const sortVal = {
  name: (d) => d.name, price: (d) => d.price, pct: (d) => d.pct, temp: (d) => d.temp,
  dd: (d) => d.raw["一年回撤"], rsi: (d) => d.raw.RSI14, ret20: (d) => d.raw["20日涨跌"], vr: (d) => d.vol_ratio,
};

function renderWatchlist() {
  const body = $("#wl-body");
  disposeWhere((k) => k.startsWith("sp-"));
  document.querySelectorAll(".wl th[data-sort]").forEach((th) => {
    th.classList.toggle("asc", th.dataset.sort === state.sort.key && state.sort.dir === 1);
    th.classList.toggle("desc", th.dataset.sort === state.sort.key && state.sort.dir === -1);
  });
  if (!state.wl.length) { body.innerHTML = `<tr><td colspan="10" class="empty">还没有自选，在右上角输入代码添加。</td></tr>`; return; }
  const rows = state.wl.slice();
  const { key, dir } = state.sort;
  rows.sort((a, b) => {
    const da = a.ok && !a.data.error ? sortVal[key](a.data) : null;
    const db = b.ok && !b.data.error ? sortVal[key](b.data) : null;
    if (da == null) return 1; if (db == null) return -1;
    return (typeof da === "string" ? da.localeCompare(db, "zh") : da - db) * dir;
  });
  body.innerHTML = rows.map((r) => {
    const rm = `<td><button class="rm" data-rm="${r.sym}" title="移除" aria-label="移除">✕</button></td>`;
    if (!r.ok || r.data.error) return `<tr data-sym="${r.sym}"><td class="nm"><b>${esc(r.data ? r.data.name : r.sym)}</b><span>${r.sym}</span></td><td colspan="8" class="err">${esc(r.error || r.data.error)}</td>${rm}</tr>`;
    const d = r.data;
    return `<tr data-sym="${d.sym}" class="${state.sel === d.sym ? "sel" : ""}">
      <td class="nm"><b>${esc(d.name)}</b><span>${d.sym}</span></td>
      <td class="num">${price(d.price)}</td>
      <td class="num ${ud(d.pct)}">${sgn(d.pct)}</td>
      <td class="num"><span class="tbar"><em><i style="width:${d.temp || 0}%;background:${tcolor(d.temp)}"></i></em><b>${n0(d.temp)}</b>${tag(d.label, zoneIdx(d.temp))}</span></td>
      <td class="num">${pctf(d.raw["一年回撤"])}</td>
      <td class="num hide-sm">${n0(d.raw.RSI14)}</td>
      <td class="num hide-sm ${ud(d.raw["20日涨跌"])}">${pctf(d.raw["20日涨跌"])}</td>
      <td class="num hide-sm">${d.vol_ratio == null ? "--" : d.vol_ratio.toFixed(2)}</td>
      <td class="hide-sm"><div class="sp" id="sp-${d.sym}"></div></td>${rm}</tr>`;
  }).join("");
  rows.forEach((r) => {
    if (r.ok && !r.data.error) {
      const el = $("#sp-" + r.data.sym);
      if (el && el.offsetParent) spark("sp-" + r.data.sym, el, r.data.series.temp, tcolor(r.data.temp), { min: 0, max: 100 });
    }
  });
}

async function addSymbol(code) {
  const msg = $("#add-msg");
  msg.className = "msg"; msg.textContent = "添加中…";
  try {
    const r = await api("/api/watchlist", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }) });
    msg.textContent = `已添加 ${r.name}（${r.sym}）`;
    $("#add-input").value = "";
    await loadWatchlist();
    openDetail(r.sym);
  } catch (e) { msg.className = "msg bad"; msg.textContent = "添加失败：" + e.message; }
}

// ---------- 自选搜索（后端 /api/search 转腾讯 smartbox） ----------
const sug = { items: [], active: -1, timer: null, seq: 0, q: "" };

function closeSuggest() {
  $("#add-suggest").hidden = true;
  $("#add-input").setAttribute("aria-expanded", "false");
  $("#add-input").removeAttribute("aria-activedescendant");
  sug.active = -1;
}

function highlight(name, q) {
  const i = name.toLowerCase().indexOf(q.toLowerCase());
  if (!q || i < 0) return esc(name);  // 拼音首字母匹配时无法定位，直接显示
  return esc(name.slice(0, i)) + "<mark>" + esc(name.slice(i, i + q.length)) + "</mark>" + esc(name.slice(i + q.length));
}

function renderSuggest() {
  const box = $("#add-suggest"), input = $("#add-input");
  const have = new Set(state.wl.map((r) => r.sym));
  box.innerHTML = sug.items.length ? sug.items.map((it, i) => `
    <li role="option" id="sug-${i}" data-i="${i}" aria-selected="${i === sug.active}" class="${i === sug.active ? "on" : ""} ${have.has(it.sym) ? "have" : ""}">
      <b>${highlight(it.name, sug.q)}</b><span class="c">${it.code}</span><span class="tag soft">${it.type}</span>${have.has(it.sym) ? `<span class="in">已在自选</span>` : ""}
    </li>`).join("")
    : `<li class="none">没有找到“${esc(sug.q)}”。也可以直接输入 6 位代码后点添加</li>`;
  box.hidden = false;
  input.setAttribute("aria-expanded", "true");
  if (sug.active >= 0) input.setAttribute("aria-activedescendant", "sug-" + sug.active);
  else input.removeAttribute("aria-activedescendant");
  const on = box.querySelector("li.on");
  if (on) on.scrollIntoView({ block: "nearest" });
}

async function searchSuggest(q) {
  const my = ++sug.seq;
  try {
    const items = await api("/api/search?q=" + encodeURIComponent(q));
    if (my !== sug.seq) return;  // 只渲染最后一次输入的结果
    sug.items = items;
    sug.q = q;
    sug.active = items.length ? 0 : -1;
    renderSuggest();
  } catch (e) {
    if (my === sug.seq) closeSuggest();
  }
}

function pickSuggest(i) {
  const it = sug.items[i];
  if (!it) return;
  closeSuggest();
  $("#add-input").value = "";
  if (state.wl.some((r) => r.sym === it.sym)) {
    $("#add-msg").className = "msg";
    $("#add-msg").textContent = `${it.name} 已在自选中`;
    openDetail(it.sym);
  } else {
    addSymbol(it.sym);
  }
}

async function removeSymbol(sym) {
  await api("/api/watchlist?code=" + encodeURIComponent(sym), { method: "DELETE" });
  const msg = $("#add-msg");
  msg.className = "msg"; msg.textContent = `已移除 ${sym}`;
  if (state.sel === sym) closeDetail();
  await loadWatchlist();
}

// ---------- 设置（存服务器，看板与邮件提醒共用） ----------
let saveTimer = null;
function deepMerge(a, b) {
  for (const k of Object.keys(b)) {
    if (b[k] && typeof b[k] === "object" && !Array.isArray(b[k]) && a[k] && typeof a[k] === "object") deepMerge(a[k], b[k]);
    else a[k] = b[k];
  }
  return a;
}
async function loadSettings() {
  state.settings = await api("/api/settings");
  $("#tiers-input").value = state.settings.tiers.join(",");
}
function saveSettings(patch, after) {
  deepMerge(state.settings, patch);
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      state.settings = await api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(state.settings) });
    } catch (e) { $("#opp-note").textContent = "设置保存失败：" + e.message; return; }
    if (after) after();
  }, 400);
}

// ---------- 信号评分（买入机会 / 过热卖出），分数由后端 thermo/signals.py 计算 ----------
const MODES = {
  buy: {
    hint: "按“逢跌分档”规则给自选打分，只看价格与情绪，不含基本面，也不考虑你的仓位",
    keys: ["cheap", "tier", "stab", "env"],
    names: { cheap: "便宜", tier: "档位", stab: "企稳", env: "环境" },
    full: { cheap: "便宜度", tier: "回撤档位", stab: "企稳", env: "大盘环境" },
    colors: { cheap: "--c-cheap", tier: "--c-tier", stab: "--c-stab", env: "--c-env" },
    defaults: { cheap: 40, tier: 25, stab: 20, env: 15 },
    tagPrefix: "s",
    note: (wp, s) => `评分 = 便宜度×${wp.cheap} + 回撤档位×${wp.tier} + 企稳×${wp.stab} + 大盘环境×${wp.env}；最深档位取 ${s.tiers[s.tiers.length - 1]}%（与详情里的“档位%”同步）。` +
      "高分但还没出现任何企稳迹象的标为“待企稳”。评分只按价格与情绪规则排序，不代表买入建议；证伪条件和基本面需要人工核对。",
  },
  sell: {
    hint: "按价格与情绪给自选的过热程度打分，对应卖出方法里的分批止盈，不含成本与仓位",
    keys: ["hot", "rise", "stall", "env"],
    names: { hot: "过热", rise: "涨幅", stall: "滞涨", env: "环境" },
    full: { hot: "过热度", rise: "涨幅偏离", stall: "滞涨", env: "市场过热" },
    colors: { hot: "--c-hot", rise: "--c-rise", stall: "--c-stall", env: "--c-env" },
    defaults: { hot: 40, rise: 25, stall: 20, env: 15 },
    tagPrefix: "r",
    note: (wp, s) => {
      const m = [], ov = state.ov;
      if (ov && ov.breadth.ok && ov.breadth.data.ratio >= 0.75) m.push(`宽度${ov.breadth.data.label}`);
      if (ov && ov.cnn.ok && ov.cnn.data.score >= 75) m.push("CNN 极度贪婪");
      if (ov && ov.vix.ok && ov.vix.data.value < 15) m.push("VIX 低于 15");
      return (m.length ? `市场层面过热提示：${m.join("、")}。` : "") +
        `评分 = 过热度×${wp.hot} + 涨幅偏离×${wp.rise} + 滞涨×${wp.stall} + 市场过热×${wp.env}；涨幅以距一年低点 ${s.rise_ref}% 记满分。` +
        "高分但还没出现滞涨迹象的标为“过热未见顶”。评分只按价格与情绪规则排序，不代表卖出建议；按成本的止盈和仓位再平衡需自行对照。";
    },
  },
};
const scoreState = { mode: MODES[store("opp_mode")] ? store("opp_mode") : "buy" };

function normWeights(mode) {
  const m = MODES[mode], raw = state.settings.weights[mode], w = {};
  m.keys.forEach((k) => { w[k] = Math.max(0, +raw[k] || 0); });
  const sum = m.keys.reduce((a, k) => a + w[k], 0) || 1;
  m.keys.forEach((k) => { w[k] /= sum; });
  return w;
}

function renderWeights() {
  if (!state.settings) return;
  const m = MODES[scoreState.mode], raw = state.settings.weights[scoreState.mode];
  $("#w-grid").innerHTML = m.keys.map((k) => `<label><i class="sw" style="background:${css(m.colors[k])}"></i>${m.full[k]}` +
      `<input type="number" min="0" max="100" step="5" data-w="${k}" value="${raw[k]}"></label>`).join("") +
    (scoreState.mode === "sell" ? `<label>满分涨幅%<input type="number" min="10" max="500" step="10" id="rise-ref" value="${state.settings.rise_ref}"></label>` : "") +
    `<button type="button" class="btn ghost" id="w-reset">恢复默认</button>` +
    `<span class="hint">权重存在服务器上，邮件提醒用同一套</span>`;
}

function renderOpportunity() {
  if (!state.settings) return;
  const mode = scoreState.mode, m = MODES[mode], body = $("#opp-body");
  $("#opp-hint").textContent = m.hint;
  const w = normWeights(mode);
  const rows = state.wl.filter((r) => r.ok && !r.data.error && r.data.signals)
    .map((r) => ({ d: r.data, sg: r.data.signals[mode] }))
    .sort((a, b) => b.sg.score - a.sg.score);
  body.dataset.mode = mode;
  if (!rows.length) { body.innerHTML = `<div class="empty">自选为空或数据不足</div>`; return; }
  body.innerHTML = rows.map(({ d, sg }, i) => {
    const segs = m.keys.map((k) => `<i style="width:${sg.parts[k] * w[k]}%;background:${css(m.colors[k])}" title="${m.full[k]} ${Math.round(sg.parts[k])} × ${Math.round(w[k] * 100)}%"></i>`).join("");
    return `<div class="opp-row" data-sym="${d.sym}" title="查看详情">
      <div class="rank">${i + 1}</div>
      <div class="opp-name"><b>${esc(d.name)}</b><span>${d.sym} · ${d.kind}</span></div>
      <div class="opp-bar">${segs}</div>
      <div class="opp-score"><b>${Math.round(sg.score)}</b><span class="tag ${m.tagPrefix}${sg.level}">${sg.label}</span></div>
      <div class="opp-parts">${m.keys.map((k) => `<span><i class="sw" style="background:${css(m.colors[k])}"></i>${m.names[k]} ${Math.round(sg.parts[k])}</span>`).join("")}` +
      `${sg.flags.map(([t, warn]) => `<span class="flag ${warn ? "warn" : ""}">${esc(t)}</span>`).join("")}</div>
    </div>`;
  }).join("");
  const wp = {};
  m.keys.forEach((k) => { wp[k] = Math.round(w[k] * 100) + "%"; });
  $("#opp-note").textContent = m.note(wp, state.settings);
}

// ---------- 提醒 ----------
const EVENTS = [
  ["B1", "档位触达", "回撤首次跌破某一档，每档每轮只报一次（盘中每 10 分钟检查）"],
  ["B2", "买入信号", "买入评分升级为“有信号 / 信号强”：便宜且开始企稳（14:45 预警、15:10 复核）"],
  ["B3", "进入冰点", "温度跌破 10（回到 25 以上后才会再次提醒）"],
  ["S1", "止盈信号", "过热评分升级为“止盈信号 / 止盈信号强”：过热且开始滞涨（14:45 预警、15:10 复核）"],
  ["S2", "进入过热", "温度升破 90（回到 75 以下后才会再次提醒，盘中检查）"],
  ["S3", "过热后回落", "进入过热后从高点回落 10%，每轮只报一次"],
  ["M", "市场层面", "大盘冰点 / 过热、宽度极度恐慌 / 狂热、CNN 极度恐惧 / 贪婪（15:10 检查）"],
];
const PHASE = { intraday: "盘中", preview: "14:45 预警", close: "15:10 收盘" };

async function loadAlerts() {
  state.alerts = await api("/api/alerts");
  renderAlerts();
}

function renderAlerts() {
  const a = state.alerts;
  if (!a || !state.settings) return;
  const cfg = state.settings.alerts;
  $("#al-enabled").checked = cfg.enabled;
  $("#al-status").textContent = !a.notify ? "未配置 notify 服务，只记录不发送" : cfg.enabled ? "已开启 · 经 notify-service 发邮件" : "已暂停（只记录）";
  $("#al-status").classList.toggle("live", a.notify && cfg.enabled);
  $("#al-events").innerHTML = EVENTS.map(([k, name, desc]) => `<label class="al-ev ${k[0] === "S" ? "sell" : k === "M" ? "mkt" : "buy"}">
      <input type="checkbox" data-ev="${k}" ${cfg.events[k] ? "checked" : ""}><span><b>${k} ${name}</b><em>${desc}</em></span></label>`).join("");
  $("#al-cooldown").value = cfg.cooldown_days;
  const syms = state.wl.filter((r) => r.ok && !r.data.error);
  $("#al-syms").innerHTML = syms.map((r) => `<label class="al-sym"><input type="checkbox" data-sym="${r.sym}" ${cfg.muted.includes(r.sym) ? "" : "checked"}>${esc(r.data.name)}</label>`).join("") || `<span class="hint">自选为空</span>`;
  $("#al-next").textContent = "检查时间：盘中每 10 分钟（9:40–11:30、13:00–14:55，仅价格类事件）· " + a.next_runs.join(" · ");
  $("#al-history").innerHTML = a.history.length ? a.history.map((h, i) => `<details class="al-h">
      <summary><span class="t">${h.time.slice(5, 16)}</span><span class="tag soft">${PHASE[h.phase] || h.phase}</span>
        <span class="ti">${esc(h.title || "")}</span>${h.delivered ? `<span class="ok">已发送</span>` : `<span class="bad" title="${esc(h.error || "")}">未发送</span>`}</summary>
      <div class="al-detail">${h.events.map((e) => `<div>· [${e.type}] ${esc(e.lines[0])}</div>`).join("")}${(h.review || []).map((r) => `<div>· [复核] ${esc(r)}</div>`).join("")}${h.error ? `<div class="bad">${esc(h.error)}</div>` : ""}</div>
    </details>`).join("") : `<div class="empty">还没有发出过提醒。上线后第一次检查只记录各标的的当前状态作为基线，之后发生变化才会提醒。</div>`;
}

function saveAlertSettings(patch) {
  saveSettings({ alerts: patch }, renderAlerts);
}

async function previewAlerts() {
  const box = $("#al-preview");
  box.hidden = false;
  box.textContent = "预演中…（按收盘阶段、从零评估当前数据，不发送、不改变提醒状态）";
  try {
    const r = await api("/api/alerts/preview?phase=close&fresh=1", { method: "POST" });
    box.textContent = r.message ? `${r.message.title}\n${r.message.summary}\n\n${r.message.detail}` : (r.skipped || "当前没有满足条件的事件。");
  } catch (e) { box.textContent = "预演失败：" + e.message; }
}

async function testEmail() {
  const btn = $("#al-test");
  btn.disabled = true;
  try { await api("/api/alerts/test", { method: "POST" }); btn.textContent = "已发送，请查收"; }
  catch (e) { btn.textContent = "发送失败"; $("#al-preview").hidden = false; $("#al-preview").textContent = e.message; }
  setTimeout(() => { btn.disabled = false; btn.textContent = "发送测试邮件"; }, 4000);
}

// ---------- 详情 ----------
function tiers() {
  return $("#tiers-input").value.split(/[,，\s]+/).map(Number).filter((v) => v > 0 && v < 100).sort((a, b) => a - b);
}

async function openDetail(sym) {
  state.sel = sym;
  document.querySelectorAll("#wl-body tr").forEach((tr) => tr.classList.toggle("sel", tr.dataset.sym === sym));
  const box = $("#detail");
  box.hidden = false;
  $("#detail-title").textContent = "加载中…";
  $("#detail-sub").textContent = "";
  $("#detail-stats").innerHTML = "";
  box.scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    state.detail = await api("/api/symbol?days=800&code=" + encodeURIComponent(sym));
    if (state.sel === sym) renderDetail();
  } catch (e) { $("#detail-title").textContent = "加载失败：" + e.message; }
}

function closeDetail() {
  state.sel = null; state.detail = null;
  $("#detail").hidden = true;
  document.querySelectorAll("#wl-body tr.sel").forEach((tr) => tr.classList.remove("sel"));
}

function renderDetail() {
  const d = state.detail;
  if (!d) return;
  if (d.error) { $("#detail-title").textContent = d.name + "：" + d.error; return; }
  $("#detail-title").innerHTML = `${esc(d.name)} <span class="hint">${d.sym}</span>`;
  $("#detail-sub").textContent = `${d.intraday ? "盘中" : d.date} · 样本 ${d.bars} 根 · 来源 ${d.source}` + (d.short_history ? " · 不足 3 年，分位数参考性下降" : "");
  const s = d.series, n = Math.min(state.days, s.dates.length), from = s.dates.length - n;
  const ts = tiers(), dd = -d.raw["一年回撤"] * 100, hi = s.high250[s.high250.length - 1];
  const hit = ts.filter((t) => dd >= t);
  const next = ts.find((t) => dd < t);
  $("#detail-stats").innerHTML = `
    <div><span>现价 / 涨跌</span><b class="num">${price(d.price)}</b> <span class="num ${ud(d.pct)}" style="display:inline">${sgn(d.pct)}</span></div>
    <div><span>温度</span><b style="color:${tcolor(d.temp)}">${n0(d.temp)}</b> ${tag(d.label, zoneIdx(d.temp))}</div>
    <div><span>一年最高收盘</span><b class="num">${price(hi)}</b></div>
    <div><span>距一年高点</span><b class="num">${pctf(d.raw["一年回撤"])}</b></div>
    <div><span>已触及档位</span><b>${hit.length ? hit.map((t) => "-" + t + "%").join(" ") : "无"}</b></div>
    <div><span>下一档</span><b class="num">${next ? `-${next}% @ ${price(hi * (1 - next / 100))}` : "已过最深档"}</b></div>`;

  const dates = s.dates.slice(from), ax = axisStyle();
  const tierSeries = ts.map((t, i) => ({
    name: `-${t}%`, type: "line", xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, silent: true,
    data: s.high250.slice(from).map((h) => +(h * (1 - t / 100)).toFixed(4)),
    lineStyle: { type: "dashed", width: 1, color: css(ZONES[Math.min(i, 1)][2]), opacity: 0.4 + 0.6 * (1 - i / Math.max(1, ts.length)) },
    endLabel: { show: true, formatter: `-${t}%`, color: css("--muted"), fontSize: 10 },
  }));
  chart("detail", $("#detail-chart")).setOption({
    ...base(),
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    legend: { top: 0, data: ["收盘", "一年高点", "温度"], textStyle: { color: css("--muted") } },
    grid: [{ left: 52, right: 48, top: 30, height: "54%" }, { left: 52, right: 48, top: "72%", bottom: 58 }],
    xAxis: [
      { type: "category", data: dates, ...ax, splitLine: { show: false }, axisLabel: { show: false } },
      { type: "category", data: dates, gridIndex: 1, ...ax, splitLine: { show: false } },
    ],
    yAxis: [
      { type: "value", scale: true, ...ax },
      { type: "value", gridIndex: 1, min: 0, max: 100, interval: 50, ...ax },
    ],
    dataZoom: [{ type: "inside", xAxisIndex: [0, 1] }, { type: "slider", xAxisIndex: [0, 1], height: 16, bottom: 8, showDetail: false, borderColor: "transparent" }],
    series: [
      { name: "收盘", type: "line", showSymbol: false, data: s.close.slice(from), lineStyle: { color: css("--text"), width: 1.6 }, itemStyle: { color: css("--text") } },
      { name: "一年高点", type: "line", showSymbol: false, step: "end", data: s.high250.slice(from), lineStyle: { color: css("--faint"), width: 1 }, itemStyle: { color: css("--faint") } },
      ...tierSeries,
      {
        name: "温度", type: "line", xAxisIndex: 1, yAxisIndex: 1, showSymbol: false, data: s.temp.slice(from).map((v) => (v == null ? null : +v.toFixed(1))),
        lineStyle: { color: css("--accent"), width: 1.4 }, itemStyle: { color: css("--accent") }, markArea: zoneBands(0.12),
      },
    ],
  }, true);
}

// ---------- 截图 ----------
function toast(msg, ms = 2500) {
  let t = $(".toast");
  if (!t) {
    t = document.createElement("div");
    t.className = "toast no-shot";
    t.setAttribute("role", "status");
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, ms);
}

async function screenshot(mode) {
  const menu = $("#shot-menu");
  menu.open = false;
  if (!window.htmlToImage) { toast("截图组件还没加载好，请稍后再试"); return; }
  menu.classList.add("busy");
  toast("正在生成图片…", 60000);
  document.body.classList.add("shooting");
  if (mode === "data") document.body.classList.add("shooting-data");
  // 等隐藏生效、布局刷新（不用 requestAnimationFrame：页面不在前台时它不触发）
  await new Promise((r) => setTimeout(r, 80));
  try {
    const node = document.body, w = node.scrollWidth, h = node.scrollHeight;
    // Safari 画布上限约 1678 万像素：长页面自动降低分辨率
    const ratio = Math.max(0.5, Math.min(2, window.devicePixelRatio || 1, Math.sqrt(16e6 / (w * h))));
    const url = await htmlToImage.toPng(node, {
      backgroundColor: css("--bg"), pixelRatio: ratio, width: w, height: h, skipFonts: true,
      filter: (el) => !(el.classList && el.classList.contains("toast")),
    });
    const stamp = (state.ov ? state.ov.time : new Date().toISOString()).replace(/[-:T ]/g, "").slice(0, 12);
    const a = document.createElement("a");
    a.href = url;
    a.download = `市场温度-${mode === "data" ? "数据" : "整页"}-${stamp}.png`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast("图片已保存");
  } catch (e) {
    toast("截图失败：" + ((e && e.message) || e), 5000);
  } finally {
    document.body.classList.remove("shooting", "shooting-data");
    menu.classList.remove("busy");
  }
}

// ---------- 加载与刷新 ----------
async function loadOverview(force) {
  state.ov = await api("/api/overview" + (force ? "?refresh=1" : ""));
  renderOverview();
  renderOpportunity();
}
async function loadWatchlist(force) {
  state.wl = await api("/api/watchlist" + (force ? "?refresh=1" : ""));
  renderWatchlist();
  renderOpportunity();
  renderAlerts();
}
async function loadAll(force) {
  const btn = $("#refresh");
  btn.classList.add("spin");
  try {
    await Promise.all([
      loadOverview(force).catch((e) => { $("#updated").textContent = "总览加载失败：" + e.message; }),
      loadWatchlist(force).catch((e) => { $("#wl-body").innerHTML = `<tr><td colspan="10" class="err">${esc(e.message)}</td></tr>`; }),
    ]);
    await loadAlerts().catch((e) => { $("#al-history").innerHTML = `<div class="err">提醒信息加载失败：${esc(e.message)}</div>`; });
    if (state.sel && force) await openDetail(state.sel);
    state.loadedAt = Date.now();
  } finally {
    btn.classList.remove("spin");
    schedule();
  }
}
function interval() { return state.ov && state.ov.trading ? 60e3 : 600e3; }
function schedule() {
  clearTimeout(state.timer);
  state.timer = setTimeout(() => { if (!document.hidden) loadAll(false); else schedule(); }, interval());
}

function bind() {
  $("#refresh").addEventListener("click", () => loadAll(true));
  $("#trend-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    state.tab = b.dataset.tab;
    $("#trend-tabs").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    store("tab", state.tab);
    if (state.ov) renderTrend();
  });
  $("#range-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    state.days = +b.dataset.days;
    $("#range-tabs").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    renderDetail();
  });
  $("#tiers-input").addEventListener("change", () => {
    const ts = tiers();
    if (!ts.length) { $("#tiers-input").value = state.settings.tiers.join(","); return; }
    renderDetail();
    saveSettings({ tiers: ts }, () => { $("#tiers-input").value = state.settings.tiers.join(","); loadWatchlist(); });
  });
  const syncModeTabs = () => $("#opp-tabs").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x.dataset.mode === scoreState.mode));
  syncModeTabs();
  $("#opp-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-mode]"); if (!b) return;
    scoreState.mode = b.dataset.mode;
    store("opp_mode", scoreState.mode);
    syncModeTabs(); renderWeights(); renderOpportunity();
  });
  $("#w-grid").addEventListener("input", (e) => {
    const el = e.target;
    if (el.id === "rise-ref") saveSettings({ rise_ref: Math.max(10, +el.value || 80) }, () => loadWatchlist());
    else if (el.dataset.w) saveSettings({ weights: { [scoreState.mode]: { [el.dataset.w]: Math.max(0, +el.value || 0) } } }, () => loadWatchlist());
  });
  $("#w-grid").addEventListener("click", (e) => {
    if (e.target.id !== "w-reset") return;
    const patch = { weights: { [scoreState.mode]: { ...MODES[scoreState.mode].defaults } } };
    if (scoreState.mode === "sell") patch.rise_ref = 80;
    saveSettings(patch, () => { renderWeights(); loadWatchlist(); });
  });
  $("#al-enabled").addEventListener("change", (e) => saveAlertSettings({ enabled: e.target.checked }));
  $("#al-events").addEventListener("change", (e) => {
    const k = e.target.dataset.ev; if (k) saveAlertSettings({ events: { [k]: e.target.checked } });
  });
  $("#al-cooldown").addEventListener("change", (e) => saveAlertSettings({ cooldown_days: Math.max(0, +e.target.value || 0) }));
  $("#al-syms").addEventListener("change", (e) => {
    const sym = e.target.dataset.sym; if (!sym) return;
    const muted = new Set(state.settings.alerts.muted);
    if (e.target.checked) muted.delete(sym); else muted.add(sym);
    state.settings.alerts.muted = [...muted];
    saveAlertSettings({ muted: [...muted] });
  });
  $("#al-preview-btn").addEventListener("click", previewAlerts);
  $("#al-test").addEventListener("click", testEmail);
  window.addEventListener("hashchange", openFromHash);
  $("#shot-menu").addEventListener("click", (e) => {
    const b = e.target.closest("[data-shot]");
    if (b) screenshot(b.dataset.shot);
  });
  document.addEventListener("click", (e) => {
    const m = $("#shot-menu");
    if (m.open && !m.contains(e.target)) m.open = false;
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#shot-menu").open = false; });
  $("#opp-body").addEventListener("click", (e) => {
    const row = e.target.closest(".opp-row[data-sym]");
    if (row) openDetail(row.dataset.sym);
  });
  $("#detail-close").addEventListener("click", closeDetail);
  $("#add-form").addEventListener("submit", (e) => {
    e.preventDefault();
    if (!$("#add-suggest").hidden && sug.active >= 0) return pickSuggest(sug.active);
    const v = $("#add-input").value.trim();
    closeSuggest();
    if (v) addSymbol(v);
  });
  $("#add-input").addEventListener("input", (e) => {
    const q = e.target.value.trim();
    clearTimeout(sug.timer);
    if (!q) { sug.seq++; closeSuggest(); return; }
    sug.timer = setTimeout(() => searchSuggest(q), 200);
  });
  $("#add-input").addEventListener("keydown", (e) => {
    const open = !$("#add-suggest").hidden && sug.items.length;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (!open) return;
      e.preventDefault();
      const n = sug.items.length;
      sug.active = (sug.active + (e.key === "ArrowDown" ? 1 : -1) + n) % n;
      renderSuggest();
    } else if (e.key === "Escape") {
      closeSuggest();
    }
  });
  $("#add-input").addEventListener("blur", () => setTimeout(closeSuggest, 150));
  $("#add-input").addEventListener("focus", (e) => {  // 切走再切回来时恢复候选列表
    if (e.target.value.trim() && e.target.value.trim() === sug.q) setTimeout(renderSuggest, 160);
  });
  // 用 mousedown：click 触发前输入框已经失焦，列表会先被关掉
  $("#add-suggest").addEventListener("mousedown", (e) => {
    const li = e.target.closest("li[data-i]");
    if (li) { e.preventDefault(); pickSuggest(+li.dataset.i); }
  });
  $("#wl-body").addEventListener("click", (e) => {
    const rm = e.target.closest("[data-rm]");
    if (rm) { e.stopPropagation(); removeSymbol(rm.dataset.rm); return; }
    const tr = e.target.closest("tr[data-sym]");
    if (tr) openDetail(tr.dataset.sym);
  });
  document.querySelectorAll(".wl th[data-sort]").forEach((th) => th.addEventListener("click", () => {
    const k = th.dataset.sort;
    state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : (k === "name" ? 1 : 1) };
    renderWatchlist();
  }));
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && Date.now() - state.loadedAt > interval()) loadAll(false);
  });
  window.addEventListener("resize", () => Object.values(charts).forEach((c) => c.resize()));
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    disposeWhere(() => true);
    if (state.ov) renderOverview();
    renderWatchlist();
    renderDetail();
    renderWeights();
    renderOpportunity();
  });

  const savedTab = store("tab");
  if (savedTab && document.querySelector(`#trend-tabs [data-tab="${savedTab}"]`)) {
    state.tab = savedTab;
    $("#trend-tabs").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x.dataset.tab === savedTab));
  }
}

// 邮件里的“详情”链接形如 #sym=sh562500
function openFromHash() {
  const m = location.hash.match(/^#sym=((?:sh|sz|bj)\d{6})$/);
  if (m) openDetail(m[1]);
}

async function boot() {
  if (!window.echarts) return setTimeout(boot, 100);  // 等待 CDN（含备用源）加载
  bind();
  try { await loadSettings(); } catch (e) { $("#opp-note").textContent = "设置加载失败：" + e.message; }
  renderWeights();
  await loadAll(false);
  openFromHash();
}
boot();
