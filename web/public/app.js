// The web replica: same pages and look as the local app, per-user data in Firestore.
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js";
import { getAuth, GoogleAuthProvider, onAuthStateChanged, signInWithPopup, signOut } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js";
import { addDoc, collection, deleteDoc, doc, getDoc, getDocs as getDocsLive, getFirestore, query, setDoc, where } from "https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";

// Public by design: access is enforced by the Firestore rules, not by hiding this.
const fb = initializeApp({
  apiKey: "AIzaSyCvD_Jv5rKuBvus2e6dipnl62HOBa_S9kM",
  authDomain: "indian-stock-data-hub-b9b0e.firebaseapp.com",
  projectId: "indian-stock-data-hub-b9b0e",
  appId: "1:269799823961:web:2819e239a0fc73d505af48",
});
const auth = getAuth(fb), db = getFirestore(fb);
let user = null;

// --- small helpers ---------------------------------------------------------
const calm = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const fadeIn = el => { if (!calm()) el.animate?.([{opacity: 0}, {opacity: 1}], {duration: 160, easing: "ease-out"}); };
// A button whose action takes time shows it's working, can't run twice, and says so if it fails.
const guarded = (btn, action) => ev => {
  if (btn.classList.contains("busy")) return;
  let run;
  try { run = action(ev); } catch (err) { toast(plainError(err), true); return; }
  if (!run?.then) return;
  btn.classList.add("busy"); btn.disabled = true;
  run.catch(err => toast(plainError(err), true)).finally(() => { btn.classList.remove("busy"); btn.disabled = false; });
};
const h = (tag, props = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) k === "class" ? (e.className = v) : k === "onclick" && tag === "button" ? (e.onclick = guarded(e, v)) : (e[k] = v);
  e.append(...kids.flat().filter(k => k !== null && k !== undefined && k !== false));
  return e;
};
const icon = name => h("span", {class: "ms", ariaHidden: "true", textContent: name});
const shell = document.getElementById("page");
const freshPage = () => { const el = document.createElement("div"); shell.replaceChildren(el); return el; };
const wait = ms => new Promise(r => setTimeout(r, ms));
// Reads are safe to repeat, so a dropped connection or a busy server gets two more tries.
const fetchApi = async p => {
  for (let tries = 0; ; tries++) {
    let r;
    try { r = await fetch("/api/" + p, {signal: AbortSignal.timeout(30000)}); } catch (e) { if (tries < 2) { await wait(500 * (tries + 1)); continue; } throw e; }
    if ((r.status >= 500 || r.status === 429) && tries < 2) { await wait(500 * (tries + 1)); continue; }
    let b; try { b = await r.json(); } catch { throw new Error("The server sent back something unexpected. Try again in a minute."); }
    if (!r.ok) throw new Error(b.error || "That didn't work. Try again in a minute.");
    return b;
  }
};
// The same read within a minute shares one answer: faster pages, and fewer chances to fail.
const answers = new Map();
const api = p => {
  const hit = answers.get(p);
  if (hit && Date.now() - hit.at < 6e4) return hit.value.then(structuredClone);
  if (answers.size > 200) answers.clear();
  const value = fetchApi(p);
  answers.set(p, {at: Date.now(), value});
  value.catch(() => answers.delete(p));
  return value.then(structuredClone);
};
const plainError = e => /failed to fetch|networkerror|load failed|offline|unavailable|timed out|aborted/i.test(e?.message) ? "Couldn't reach the server. Check your connection and try again." : e?.message || "That didn't work. Try again in a minute.";
const failed = e => h("p", {class: "err", textContent: plainError(e)});
let toastTimer;
function toast(text, bad = false) {
  const t = document.getElementById("toast");
  t.textContent = text; t.classList.add("show"); t.classList.toggle("bad", bad);
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("show"), bad ? 6000 : 3000);
}
// Numbers settle into place, and glide when they've changed since the last look.
const lastShown = new Map();
function settle(el, text, key) {
  const m = /^(\D*)([\d,]+(?:\.\d+)?)(.*)$/.exec(text), to = m ? Number(m[2].replace(/,/g, "")) : NaN, from = lastShown.get(key);
  if (!Number.isFinite(to)) return;
  lastShown.set(key, to);
  if (from === to || calm()) return;
  const start = from ?? to * .92, places = (m[2].split(".")[1] || "").length, began = performance.now();
  const step = now => {
    const t = Math.min((now - began) / 500, 1), eased = 1 - (1 - t) ** 3;
    el.textContent = t < 1 ? m[1] + (start + (to - start) * eased).toLocaleString("en-IN", {minimumFractionDigits: places, maximumFractionDigits: places}) + m[3] : text;
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
function inr(x, signed = false, decimals = 0) {
  const s = Math.abs(x).toLocaleString("en-IN", {minimumFractionDigits: decimals, maximumFractionDigits: decimals});
  return (x < 0 ? "-" : signed ? "+" : "") + "₹" + s;
}
const tone = x => (x > 0 ? "up" : x < 0 ? "down" : "flat");
const delta = (text, x) => h("span", {class: "delta " + tone(x)}, x > 0 ? icon("arrow_upward") : x < 0 ? icon("arrow_downward") : null, text);
const metric = (label, value, sub, plain) => { const b = h("b", {textContent: value}); if (!plain) settle(b, value, `${location.hash}|${label}`); return h("div", {class: "metric" + (plain ? " plain" : "")}, h("small", {textContent: label}), b, sub); };

// --- per-user data ---------------------------------------------------------
const mine = name => collection(db, "users", user.uid, name);
// Without a connection Firestore answers from memory, which can look like "you have no data".
// That must never reach a page, or it shows Get started and can overwrite what's really saved.
const getDocs = async q => { const snap = await getDocsLive(q); if (snap.metadata?.fromCache) throw new Error("You're offline."); return snap; };
const holdings = async () => (await getDocs(mine("holdings"))).docs.map(d => ({account: "main", ...d.data(), _id: d.id}));
const alertRules = async t => (await getDocs(mine("alerts"))).docs.map(d => ({id: d.id, ...d.data()})).filter(r => r.ticker === t);

// Price, day change and last month's closes, from screener.in via the CDN cache.
async function quote(sid, days = 45) {
  const bars = await api(`prices?id=${encodeURIComponent(sid)}&days=${days}`);
  const c = bars.map(b => b[1]), price = c.at(-1), prev = c.at(-2) ?? price;
  return {price, change: price - prev, pct: prev ? (price / prev - 1) * 100 : 0, spark: c.slice(-30)};
}
function spark(values) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "spark"); svg.setAttribute("viewBox", "0 0 150 26"); svg.setAttribute("preserveAspectRatio", "none");
  const lo = Math.min(...values), hi = Math.max(...values);
  const line = document.createElementNS(svg.namespaceURI, "polyline");
  line.setAttribute("points", values.map((v, i) => `${(i / Math.max(values.length - 1, 1) * 150).toFixed(1)},${(24 - (v - lo) / (hi - lo || 1) * 22).toFixed(1)}`).join(" "));
  line.setAttribute("vector-effect", "non-scaling-stroke"); line.setAttribute("fill", "none"); line.setAttribute("stroke", "#1F4E79"); line.setAttribute("stroke-width", "1.5");
  const area = document.createElementNS(svg.namespaceURI, "polygon");
  area.setAttribute("points", `0,26 ${line.getAttribute("points")} 150,26`); area.setAttribute("fill", "#1F4E79"); area.setAttribute("fill-opacity", ".1");
  svg.append(area, line);
  return svg;
}

// --- company search, shared by Home and Stock --------------------------------
let LISTED = null;
const listedCompanies = async () => LISTED || (LISTED = await api("listed").catch(() => []));
function companySearch(onPick = openCompany) {
  const list = h("ul", {class: "hits", hidden: true}), sme = h("input", {type: "checkbox", checked: true, style: "width:auto"});
  const box = h("input", {type: "search", autocomplete: "off", ariaLabel: "Look up any company", placeholder: "Look up any company — type a name or symbol, e.g. tata motors or TMCV"});
  const pick = async row => {
    box.value = ""; list.hidden = true;
    let c = row;
    if (!c.screener_id) { const hit = (await api("search?q=" + encodeURIComponent(c.ticker)).catch(() => [])).find(x => x.ticker === c.ticker); c = {...c, ...(hit || {})}; }
    onPick(c);
  };
  const show = rows => { list.replaceChildren(...rows.slice(0, 12).map(c => h("li", {}, h("button", {textContent: `${c.name} · ${c.ticker}${c.sme ? " · SME" : ""}`, onclick: () => pick(c)})))); list.hidden = !rows.length; };
  let timer, latest = 0;
  const run = () => {
    clearTimeout(timer);
    const text = box.value.trim(), turn = ++latest;
    if (!text) { list.hidden = true; return; }
    timer = setTimeout(async () => {
      const low = text.toLowerCase(), rows = (await listedCompanies()).filter(r => (sme.checked || !r[2]) && (r[0].toLowerCase().includes(low) || r[1].toLowerCase().includes(low))).map(r => ({ticker: r[0], name: r[1], sme: r[2]}));
      if (turn !== latest) return;
      rows.sort((a, b) => (b.ticker.toLowerCase() === low) - (a.ticker.toLowerCase() === low));
      if (rows.length) return show(rows);
      try { const more = await api("search?q=" + encodeURIComponent(text)); if (turn === latest) show(more); } // a BSE-only company
      catch (e) { if (turn === latest) { list.replaceChildren(h("li", {}, failed(e))); list.hidden = false; } }
    }, 150);
  };
  box.addEventListener("input", run); sme.addEventListener("change", run);
  box.addEventListener("keydown", e => { if (e.key === "Enter" && /^[A-Za-z0-9&_-]{1,20}$/.test(box.value.trim())) pick({ticker: box.value.trim().toUpperCase(), name: box.value.trim().toUpperCase()}); });
  return h("div", {class: "search"}, h("div", {class: "row"}, h("div", {class: "grow"}, box), h("label", {class: "row", style: "margin:0", title: "Small and medium companies listed on NSE Emerge. Smaller, riskier and less traded."}, sme, "Include SMEs")), list);
}
function openCompany(c) {
  sessionStorage.setItem("company", JSON.stringify(c));
  location.hash = "#/stock/" + encodeURIComponent(c.ticker);
  if (location.hash === "#/stock/" + encodeURIComponent(c.ticker)) route();
}

// --- pages -------------------------------------------------------------------
async function home() {
  const page = freshPage();
  const hour = new Date().getHours();
  page.replaceChildren(h("h1", {textContent: "Good " + (hour < 12 ? "morning" : hour < 17 ? "afternoon" : "evening")}),
    h("p", {class: "muted", textContent: new Date().toLocaleDateString("en-IN", {weekday: "long", day: "2-digit", month: "long"})}),
    companySearch());
  const body = h("div", {class: "skel", textContent: "Loading your stocks…"});
  page.append(body);
  const owned = await holdings(), rows = [...new Map(owned.map(r => [r.ticker, {...r, qty: owned.filter(x => x.ticker === r.ticker).reduce((s, x) => s + (x.qty || 0), 0)}])).values()];
  rows.forEach(r => { const same = owned.filter(x => x.ticker === r.ticker && x.qty && x.avg_price); r.avg_price = same.length ? same.reduce((s, x) => s + x.qty * x.avg_price, 0) / same.reduce((s, x) => s + x.qty, 0) : 0; });
  if (!rows.length) {
    body.replaceWith(h("div", {class: "card"}, h("h3", {textContent: "Get started in three steps"}), h("ol", {},
      h("li", {}, h("b", {textContent: "Look up a company"}), " in the box above — its page shows the price and the key numbers."),
      h("li", {}, h("b", {textContent: "Add it to your portfolio"}), " from that page."),
      h("li", {}, h("b", {textContent: "Come back here"}), " to see how your stocks are doing."))));
    return;
  }
  await checkAlerts(rows);
  const quotes = await Promise.all(rows.map(r => quote(r.screener_id).catch(() => null))), latest = await latestAll();
  const notes = [...new Map((await events()).filter(e => !e.read && e.ts > Date.now() - 7 * 864e5).reverse().map(e => [e.ticker, e])).values()].reverse().slice(0, 5);  // the newest per stock
  let value = 0, invested = 0, day = 0, up = 0;
  rows.forEach((r, i) => {
    const q = quotes[i];
    if (q && q.change > 0) up++;
    if (q && r.qty) { value += r.qty * q.price; day += r.qty * q.change; if (r.avg_price) invested += r.qty * r.avg_price; }
  });
  const money = h("div", {class: "metrics"});
  if (value) money.append(metric("Worth today", inr(value), delta(`${inr(day, true)} today`, day)));
  if (invested) money.append(metric("Gain or loss so far", inr(value - invested, true), delta(`${((value / invested - 1) * 100).toFixed(1)}%`, value - invested)));
  money.append(metric("Your stocks", String(rows.length), delta(`${up} up today`, 0)));
  const order = rows.map((r, i) => [r, quotes[i]]).sort((a, b) => a[0].name.localeCompare(b[0].name));
  body.replaceWith(money, notes.length ? h("div", {class: "card attention"}, h("div", {class: "row"}, h("h4", {class: "grow"}, icon("notifications"), " Needs your attention"), h("span", {class: "badge orange", textContent: `${notes.length} new`}), h("a", {href: "#/portfolio", onclick: () => { pfSection = "Alerts"; }}, "See all ", icon("arrow_forward"))), h("div", {class: "unread-only"}, notes.map(eventRow))) : "", h("p", {}, h("b", {textContent: "Your stocks"}), h("span", {class: "muted", textContent: " · click a stock to open it"})),
    h("div", {class: "scroll stocks"}, h("table", {},
      h("thead", {}, h("tr", {}, ["Company", "Symbol", "Price", "Today", "Last month", "AI view"].map(t => h("th", {textContent: t})))),
      h("tbody", {}, order.map(([r, q]) => h("tr", {class: "click", tabIndex: 0, onclick: () => openCompany(r), onkeydown: e => { if (e.key === "Enter") openCompany(r); }},
        h("td", {textContent: r.name}), h("td", {textContent: r.ticker}),
        h("td", {class: "num", textContent: q ? inr(q.price, false, 2) : "—"}),
        h("td", {class: "num " + (q ? tone(q.pct) : ""), textContent: q ? `${q.pct >= 0 ? "+" : ""}${q.pct.toFixed(2)}%` : "—"}),
        h("td", {class: "chart"}, q ? spark(q.spark) : "—"), h("td", {class: {Positive: "up", Cautious: "down"}[latest[r.ticker]?.verdict] || "muted", textContent: VIEW[latest[r.ticker]?.verdict]?.[0] || "Not checked yet"})))))));
}

function popover(label, iconName, build) {
  const pop = h("div", {class: "pop right", hidden: true});
  const wrap = h("div", {class: "more"}, h("button", {class: "btn", onclick: () => { pop.hidden = !pop.hidden; if (!pop.hidden) build(pop); }}, icon(iconName), label), pop);
  return wrap;
}

async function stock(ticker) {
  const page = freshPage();
  const saved = JSON.parse(sessionStorage.getItem("company") || "null");
  const owned = user ? await holdings() : [];
  const held = owned.find(r => r.ticker === ticker);
  let company = saved && saved.ticker === ticker ? saved : held;
  const picker = owned.length ? h("select", {ariaLabel: "Your stocks", onchange: e => { const c = owned.find(r => r.ticker === e.target.value); if (c) openCompany(c); }},
    h("option", {value: "", textContent: "…or pick one of your stocks"}),
    [...owned].sort((a, b) => a.name.localeCompare(b.name)).map(r => h("option", {value: r.ticker, selected: r.ticker === ticker, textContent: r.name}))) : null;
  page.replaceChildren(h("div", {class: "row"}, h("div", {class: "grow"}, companySearch()), picker && h("div", {style: "width:320px;max-width:100%"}, picker)));
  if (!ticker) { page.append(h("div", {class: "info"}, icon("search"), " Type a company's name above to see everything about it.")); return; }
  const body = h("div", {class: "skel", textContent: "Loading…"});
  page.append(body);
  let info;
  try {
    info = await api("company?t=" + encodeURIComponent(ticker));
    if (!company || !company.screener_id) {
      const hit = (await api("search?q=" + encodeURIComponent(ticker))).find(c => c.ticker === ticker);
      company = {ticker, name: info.name, screener_id: hit?.screener_id};
    }
  } catch (e) { body.replaceWith(failed(e)); return; }

  let q = null;
  try { if (company.screener_id) q = await quote(company.screener_id); } catch { /* the page still works without a price */ }
  const {meta: runMeta, snap: latestRun} = await latestSnap(ticker);
  const runCount = latestRun ? (await getDocs(query(mine("runs"), where("ticker", "==", ticker)))).size : 0;
  const lookBtn = h("button", {class: "btn primary", onclick: async () => {
    lookBtn.disabled = true; lookBtn.lastChild.textContent = "Looking into it — about a minute…";
    try { await lookInto(ticker, "Checked by you"); toast("Done."); route(); }
    catch (e) { toast(e.message); lookBtn.disabled = false; lookBtn.lastChild.textContent = "Look into this company"; }
  }}, icon("travel_explore"), h("span", {textContent: "Look into this company"}));
  const actions = h("div", {class: "row", style: "margin-left:auto"}, lookBtn);
  if (latestRun) actions.append(popover("Download", "download", pop => pop.replaceChildren(
    h("button", {class: "btn link", onclick: () => downloadReport([latestRun], "xlsx", `${ticker}_${stamp()}.xlsx`).catch(e => toast(e.message))}, icon("table_view"), "Spreadsheet (Excel)"),
    h("button", {class: "btn link", onclick: () => downloadReport([latestRun], "docx", `${ticker}_${stamp()}.docx`).catch(e => toast(e.message))}, icon("description"), "Report (Word)"))));
  if (!held) actions.append(popover("Add to my portfolio", "add", async pop => {
    const accts = await accountsList(), qty = h("input", {type: "number", min: 0, placeholder: "Leave empty to just watch it"}), avg = h("input", {type: "number", min: 0, placeholder: "Optional"}), acc = h("select", {ariaLabel: "Account"}, accts.map(a => h("option", {value: a.id, textContent: a.name})));
    pop.replaceChildren(accts.length > 1 ? h("label", {}, "Add it to", acc) : "", prefs.positions ? h("label", {}, "How many shares do you own?", qty) : "", prefs.positions ? h("label", {}, "Average price you paid (₹)", avg) : "",
      h("button", {class: "btn primary", textContent: "Add", onclick: async () => {
        await saveHolding(acc.value, {...company, name: company.name || ticker}, Number(qty.value), Number(avg.value));
        toast(`Added ${ticker}. The app will keep an eye on it for you.`); route();
      }}));
  }));
  else actions.append(h("button", {class: "btn", onclick: async () => { for (const r of owned.filter(x => x.ticker === ticker)) await deleteDoc(doc(mine("holdings"), r._id)); toast(`Removed ${ticker}.`); route(); }}, icon("remove"), "Remove from portfolio"));
  const alerts = await alertRules(ticker);
  const alertBtn = popover(alerts.length ? `Price alerts · ${alerts.length}` : "Tell me when the price moves", "add_alert", pop => {
    let op = "above";
    const level = h("input", {type: "number", min: 0, value: q ? q.price : 0});
    const seg = h("div", {class: "seg"}, ["above", "below"].map(o => h("button", {class: o === op ? "on" : "", textContent: o, onclick: e => { op = o; seg.querySelectorAll("button").forEach(b => b.classList.toggle("on", b === e.target)); }})));
    pop.replaceChildren(...alerts.map(r => h("div", {class: "row"}, h("span", {class: "grow", textContent: `${r.op[0].toUpperCase()}${r.op.slice(1)} ${inr(r.level, false, 2)}`}),
      h("button", {class: "btn link", ariaLabel: "Delete alert", onclick: async () => { await deleteDoc(doc(mine("alerts"), r.id)); route(); }}, icon("delete")))),
      h("p", {class: "muted", textContent: "Notify me when the price goes"}), seg, h("label", {}, "Level (₹)", level),
      h("button", {class: "btn primary", textContent: "Add alert", onclick: async () => {
        await addDoc(mine("alerts"), {ticker, screener_id: company.screener_id ?? null, op, level: Number(level.value), fired: false});
        toast(`Saved an alert for ${ticker} going ${op} ${inr(Number(level.value), false, 2)}.`); route();
      }}));
  });

  const chartBox = h("div", {id: "chart"}), key = h("div", {class: "muted"}, h("span", {style: "color:#2962FF", textContent: "━ average of the last 50 days"}), "   ", h("span", {style: "color:#FF9800", textContent: "━ average of the last 200 days"}));
  const seg = h("div", {class: "seg"}, ["1M", "6M", "1Y", "5Y", "Max"].map(p => h("button", {class: p === "1Y" ? "on" : "", textContent: p, onclick: e => { seg.querySelectorAll("button").forEach(b => b.classList.toggle("on", b === e.target)); draw(p); }})));
  let chart;
  async function draw(period) {
    try {
      const rows = await api(`bars?t=${encodeURIComponent(ticker)}&id=${encodeURIComponent(company.screener_id ?? "")}&period=${period}`);
      chart?.remove();
      chart = LightweightCharts.createChart(chartBox, {height: 440, layout: {background: {color: "transparent"}, textColor: "#444"},
        grid: {vertLines: {color: "rgba(128,128,128,.12)"}, horzLines: {color: "rgba(128,128,128,.12)"}},
        rightPriceScale: {borderVisible: false}, timeScale: {borderVisible: false}, crosshair: {mode: LightweightCharts.CrosshairMode.Normal}});
      if (prefs.chart === "Simple") {
        const c = rows.length && rows.at(-1)[4] >= rows[0][4] ? "#15803D" : "#DC2626";
        chart.addAreaSeries({lineColor: c, topColor: c + "33", bottomColor: c + "00", lineWidth: 2}).setData(rows.map(b => ({time: b[0], value: b[4]})));
        key.hidden = true; chart.timeScale().fitContent();
        new ResizeObserver(() => chart.applyOptions({width: chartBox.clientWidth})).observe(chartBox);
        return;
      }
      chart.addCandlestickSeries({upColor: "#26a69a", downColor: "#ef5350", borderVisible: false, wickUpColor: "#26a69a", wickDownColor: "#ef5350"})
        .setData(rows.map(b => ({time: b[0], open: b[1], high: b[2], low: b[3], close: b[4]})));
      const vol = chart.addHistogramSeries({priceFormat: {type: "volume"}, priceScaleId: ""});
      vol.priceScale().applyOptions({scaleMargins: {top: .8, bottom: 0}});
      vol.setData(rows.map(b => ({time: b[0], value: b[5] || 0, color: b[4] >= b[1] ? "rgba(38,166,154,.4)" : "rgba(239,83,80,.4)"})));
      chart.addLineSeries({color: "#2962FF", lineWidth: 1, priceLineVisible: false}).setData(rows.filter(b => b[6] !== null).map(b => ({time: b[0], value: b[6]})));
      chart.addLineSeries({color: "#FF9800", lineWidth: 1, priceLineVisible: false}).setData(rows.filter(b => b[7] !== null).map(b => ({time: b[0], value: b[7]})));
      chart.timeScale().fitContent();
      new ResizeObserver(() => chart.applyOptions({width: chartBox.clientWidth})).observe(chartBox);
    } catch (e) { chartBox.replaceChildren(failed(e)); }
  }

    const coming = h("div", {class: "row"}, h("b", {}, icon("event"), " Coming up"), h("span", {class: "muted", textContent: "Checking…"}));
  api(`calendar?t=${encodeURIComponent(ticker)}&name=${encodeURIComponent(company.name || ticker)}`).then(rows => { coming.lastChild.remove(); if (!rows.length) coming.append(h("span", {class: "muted", textContent: "Nothing announced yet."})); rows.forEach(e => coming.append(h("div", {class: "card", style: "margin:0;padding:8px 12px"}, h("b", {textContent: new Date(e.date).toLocaleDateString("en-IN", {day: "2-digit", month: "short"})}), ` · ${e.event}`, e.detail ? h("div", {class: "muted", textContent: e.detail}) : null))); }).catch(() => { coming.lastChild.textContent = "Nothing announced yet."; });
  const band = h("div");
  if (company.screener_id && latestRun) api("valuation?id=" + encodeURIComponent(company.screener_id)).then(({band: b}) => { if (!b) return; const cheap = b.percentile <= 35, dear = b.percentile >= 75;
    band.append(h("div", {class: "card row"}, metric("Price-to-earnings now", b.now.toFixed(1), delta(`${(b.now - b.median).toFixed(1)} vs its usual`, b.median - b.now), true), h("div", {class: "grow"}, h("p", {}, `Cheaper than it's been ${100 - Math.round(b.percentile)}% of the time in the last ${b.years} years `, h("span", {class: cheap ? "up" : dear ? "down" : "muted", textContent: cheap ? "· cheaper than usual" : dear ? "· dearer than usual" : "· around its usual level"})), h("div", {class: "bar"}, h("i", {style: `width:${Math.min(Math.max(b.percentile, 0), 100)}%`})), h("p", {class: "muted", textContent: `Lowest ${b.low.toFixed(1)} · usual ${b.median.toFixed(1)} · highest ${b.high.toFixed(1)}. Compared only with its own past, not with other companies — and a change can be deserved.`})))); }).catch(() => {});
  body.replaceWith(h("div", {class: "row", style: "margin-top:16px"}, h("h1", {textContent: company.name || info.name}), latestRun ? viewBadge(latestRun.ai_verdict) : null), h("p", {class: "muted", textContent: ticker}),
    h("div", {class: "row"}, h("div", {class: "grow"}, q && metric("Share price", inr(q.price, false, 2),
      delta(`${q.change >= 0 ? "+" : ""}${q.change.toFixed(2)} (${q.pct >= 0 ? "+" : ""}${q.pct.toFixed(2)}%) today`, q.change), true)), metric("Times checked", String(runCount), null, true), actions),
    h("div", {class: "row", style: "margin-top:12px"}, h("div", {class: "grow"}, coming), alertBtn),
    latestRun ? "" : h("div", {class: "info"}, icon("hourglass_top"), " Press Look into this company above to get a summary, the numbers and any warning signs."),
    seg, chartBox, key,
    ...(latestRun ? [band, h("p", {class: "muted", textContent: `Last checked ${when(runMeta.ts)}`}), resultTabs(latestRun, [pastChecks(ticker)])] : [
    h("div", {class: "metrics"}, Object.entries(info.metrics).map(([k, v]) => h("div", {class: "metric"}, h("small", {textContent: k}), h("span", {style: "font-size:18px", textContent: v})))),
    info.pros.length ? h("div", {class: "card"}, h("h3", {textContent: "Strengths"}), h("ul", {class: "pros"}, info.pros.map(t => h("li", {textContent: t})))) : null,
    info.cons.length ? h("div", {class: "card"}, h("h3", {textContent: "Weaknesses"}), h("ul", {class: "cons"}, info.cons.map(t => h("li", {textContent: t})))) : null,
    info.quarters ? h("div", {style: "margin-top:16px"}, h("h3", {textContent: "Quarterly results"}), h("div", {class: "scroll"}, h("table", {},
      h("thead", {}, h("tr", {}, info.quarters.columns.map(t => h("th", {textContent: t})))),
      h("tbody", {}, info.quarters.rows.map(r => h("tr", {}, r.map(t => h("td", {textContent: t})))))))) : null]));
  draw("1Y");
}

// --- server calls and per-user helpers ------------------------------------------
const ALL_SECTIONS = ["Summary", "Results", "Every number", "What changed", "Promises kept?", "Reports & warnings"];
const PREFS = {provider: "", model: "", base_url: "", categories: null, timeframe: "Latest only", tickers: "RELIANCE, TCS, INFY", accent: "#1F4E79",
  budget: {enabled: true, monthly: 0, per_run: 50000, warn: 80}, max_file_mb: 25, max_pdf_pages: 80, doc_workers: 4, news_items: 8, hidden: [], home: "Home", nav: "top", sections: ALL_SECTIONS, chart: "Detailed", tech: false, custom_style: true, font_scale: 1, density: "Comfortable", table_height: 380, decimals: 2, sparklines: true, context: true, positions: true, refresh_hours: 24, max_stock_pct: 20, max_sector_pct: 35, email: false, email_url: ""};
const DENSITY = {Compact: "8px", Comfortable: "12px", Spacious: "20px"};
const secret = k => { try { return localStorage.getItem(k) || ""; } catch { return ""; } };
function applyLook() {
  const root = document.documentElement.style, on = prefs.custom_style;
  root.setProperty("--accent", on ? prefs.accent : "#1F4E79");
  root.setProperty("--gap", on ? DENSITY[prefs.density] || "12px" : "12px");
  document.body.style.fontSize = on ? `${14 * prefs.font_scale}px` : "";
}
const dataBody = () => ({max_file_mb: prefs.max_file_mb, max_pdf_pages: prefs.max_pdf_pages, doc_workers: prefs.doc_workers, news_items: prefs.news_items, per_run_tokens: prefs.budget.per_run});
const monthKey = () => new Date().toISOString().slice(0, 7);
const monthUsage = async () => (await getDoc(doc(mine("usage"), monthKey()))).data()?.tokens || 0;
async function addUsage(tokens) { if (tokens) await setDoc(doc(mine("usage"), monthKey()), {tokens: await monthUsage() + tokens}); }
async function overBudget() {
  const b = prefs.budget; if (!b.enabled || !b.monthly) return false;
  const used = await monthUsage();
  if (used >= b.monthly) return true;
  if (used / b.monthly * 100 >= b.warn) toast(`You've used ${Math.round(used / b.monthly * 100)}% of your monthly AI limit.`);
  return false;
}
const emailMe = (subject, text) => call("notify", {url: prefs.email_url, token: secret("emailtoken"), subject, text});
async function addEvent(e) {
  await addDoc(mine("events"), {ts: Date.now(), read: false, body: "", ...e});
  if (prefs.email && prefs.email_url && secret("emailtoken")) emailMe(e.title, e.body || e.title).catch(() => {});
}
let prefs = {...PREFS}, META = null;
const keyOf = p => { try { return localStorage.getItem("aikey:" + p) || ""; } catch { return ""; } };
const aiBody = () => ({provider: prefs.provider, model: prefs.model, base_url: prefs.base_url, api_key: keyOf(prefs.provider)});
const aiReady = () => Boolean(prefs.provider && prefs.model);
const REPEATABLE = new Set(["search-reports", "diff", "models", "screen", "trades", "upload", "export"]);  // not analyses: they cost AI tokens
async function call(route, body) {
  let r;
  for (let tries = 0; ; tries++) {
    try { r = await fetch("/api/" + route, {method: "POST", headers: {"Content-Type": "application/json", Authorization: "Bearer " + await user.getIdToken(tries > 0)}, body: JSON.stringify(body)}); }
    catch (e) { if (REPEATABLE.has(route) && tries < 1) { await wait(800); continue; } throw e; }
    if (tries < 1 && (r.status === 401 || (REPEATABLE.has(route) && [502, 503, 504].includes(r.status)))) { await wait(300); continue; }
    break;
  }
  let b; try { b = await r.json(); } catch { throw new Error("That took too long or stopped early. Try again, or read fewer reports."); }
  if (!r.ok) throw new Error(b.error);
  return b;
}
async function loadPrefs() {
  const d = await getDoc(doc(mine("settings"), "main"));
  prefs = {...PREFS, ...(d.exists() ? d.data() : {})};
  prefs.budget = {...PREFS.budget, ...prefs.budget};
  applyLook();
}
const meta = async () => META || (META = await api("meta"));
const VIEW = {Positive: ["Looks good", "green", "trending_up"], Neutral: ["Mixed picture", "orange", "trending_flat"], Cautious: ["Be careful", "red", "trending_down"]};
const viewBadge = v => VIEW[v] ? h("span", {class: "badge " + VIEW[v][1]}, icon(VIEW[v][2]), "AI view: " + VIEW[v][0]) : null;
const viewPill = v => VIEW[v] ? h("span", {class: "badge " + VIEW[v][1]}, icon(VIEW[v][2]), VIEW[v][0]) : null;
const latestAll = async () => Object.fromEntries((await getDocs(mine("latest"))).docs.map(d => [d.id, d.data()]));
async function snapOf(runId) { const d = await getDoc(doc(mine("snaps"), runId)); return d.exists() ? JSON.parse(d.data().json) : null; }
async function latestSnap(ticker) { const d = await getDoc(doc(mine("latest"), ticker)); return d.exists() ? {meta: d.data(), snap: await snapOf(d.data().runId)} : {meta: null, snap: null}; }
async function saveRun(snap, label) {
  const events_ = snap.events || []; delete snap.events;
  const row = {ticker: snap.ticker, name: snap.company_name || snap.ticker, ts: Date.now(), label, verdict: snap.ai_verdict || null, summary: snap.ai_summary || null, screen: snap.screen || null};
  const run = await addDoc(mine("runs"), row);
  await setDoc(doc(mine("snaps"), run.id), {json: JSON.stringify(snap)});
  await setDoc(doc(mine("latest"), snap.ticker), {...row, runId: run.id});
  for (const e of events_) await addEvent(e);
  return run.id;
}
async function lookInto(ticker, label, options = {}) {
  const {snap: previous} = await latestSnap(ticker);
  const blocked = options.useAI !== false && aiReady() && await overBudget();
  if (blocked) toast("You've reached the monthly AI limit you set, so the AI summary was skipped.");
  const r = await call("analyze", {ticker, ai: options.useAI === false || blocked ? {} : aiBody(), categories: options.categories || prefs.categories, timeframe: options.timeframe || prefs.timeframe, previous, data: dataBody()});
  if (r.ok === false) throw new Error(r.error);
  r.previous_at = previous ? (await getDoc(doc(mine("latest"), ticker))).data().ts : null;
  await saveRun(r, label);
  await addUsage(r.ai_tokens || 0);
  return r;
}
// Alerts are checked while the app is open: there is no always-on server here.
async function checkAlerts(rows) {
  const rules = (await getDocs(mine("alerts"))).docs.filter(d => !d.data().fired);
  for (const d of rules) {
    const r = d.data(), sid = r.screener_id ?? rows.find(x => x.ticker === r.ticker)?.screener_id;
    if (!sid) continue;
    try {
      const q = await quote(sid);
      if (r.op === "above" ? q.price >= r.level : q.price <= r.level) {
        await setDoc(d.ref, {...r, fired: true});
        await addEvent({ticker: r.ticker, kind: "price", title: `${r.ticker}: price went ${r.op} ${inr(r.level, false, 2)}`, body: `It's now ${inr(q.price, false, 2)}.`});
      }
    } catch { /* tried again on the next visit */ }
  }
}
const events = async () => (await getDocs(mine("events"))).docs.map(d => ({...d.data(), _ref: d.ref})).sort((a, b) => b.ts - a.ts);
const EVENT_ICON = {price: "notifications_active", verdict: "psychology", filing: "description", guidance: "handshake"};
const sinceText = ts => { const m = (Date.now() - ts) / 6e4; return m < 1 ? "Just now" : m < 60 ? `${Math.floor(m)} min ago` : m < 1440 ? `${Math.floor(m / 60)} h ago` : m < 2880 ? "Yesterday" : m < 10080 ? `${Math.floor(m / 1440)} days ago` : new Date(ts).toLocaleDateString("en-IN", {day: "2-digit", month: "short"}); };
const markRead = e => { const {_ref, ...rest} = e; return setDoc(_ref, {...rest, read: true}); };
// One message: what happened, how long ago, and a way to open the company or clear it.
function eventRow(e) {
  const row = h("div", {class: "event" + (e.read ? "" : " new")}, h("span", {class: "event-ic"}, icon(EVENT_ICON[e.kind] || "notifications")),
    h("div", {class: "event-text"}, h("div", {}, h("b", {textContent: e.ticker || "Update"}), h("span", {class: "muted", textContent: "  ·  " + sinceText(e.ts)})), h("div", {textContent: String(e.title || "").replace(`${e.ticker}: `, "").replace(/^./, c => c.toUpperCase())}), e.body ? h("div", {class: "muted", textContent: e.body}) : null),
    h("div", {class: "event-actions"}, e.ticker ? h("button", {class: "btn link", onclick: () => openCompany({ticker: e.ticker, name: e.ticker})}, icon("arrow_forward"), "Open") : null,
      e.read ? null : h("button", {class: "btn link", ariaLabel: "Mark as read", title: "Mark as read", onclick: async ev => { const box = row.parentNode; await markRead(e); e.read = true; row.classList.remove("new"); ev.target.closest("button").remove(); if (box?.classList.contains("unread-only")) {
        const card = box.closest(".card"), left = box.querySelectorAll(".event").length - 1;
        if (!calm()) await Promise.race([row.animate([{opacity: 1, height: row.offsetHeight + "px"}, {opacity: 0, height: 0, paddingTop: 0, paddingBottom: 0, overflow: "hidden"}], {duration: 200, easing: "cubic-bezier(.23,1,.32,1)"}).finished, wait(260)]);  // animations pause in a background tab
        row.remove();
        if (!left) card.remove(); else card.querySelector(".badge").textContent = `${left} new`;
      } }}, icon("check"))));
  return row;
}
function download(bytes, name, type) {
  const a = h("a", {href: URL.createObjectURL(new Blob([bytes], {type})), download: name}); a.click(); URL.revokeObjectURL(a.href);
}
const csvCell = v => `"${String(v ?? "").replace(/"/g, '""')}"`.replace(/^"([=+\-@])/, `"'$1`); // never a live spreadsheet formula
const downloadCsv = (df, name) => download([df.columns, ...df.data].map(r => r.map(csvCell).join(",")).join("\n"), name, "text/csv");
async function downloadReport(snaps, kind, name) {
  const {file} = await call("export", {snapshots: snaps, kind});
  download(Uint8Array.from(atob(file), c => c.charCodeAt(0)), name, kind === "docx" ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document" : "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");
}
const stamp = () => new Date().toISOString().slice(0, 10).replace(/-/g, "");
const when = ts => new Date(ts).toLocaleString("en-IN", {day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit"});
const dfTable = (df, onRow) => h("div", {class: "scroll", style: `max-height:${prefs.table_height}px`}, h("table", {},
  h("thead", {}, h("tr", {}, df.columns.map(t => h("th", {textContent: t})))),
  h("tbody", {}, df.data.map(r => h("tr", onRow ? {class: "click", tabIndex: 0, onclick: () => onRow(r), onkeydown: e => { if (e.key === "Enter") onRow(r); }} : {}, r.map(t => h("td", {class: typeof t === "number" ? "num" : "", textContent: t === null || t === undefined || t === "" ? "—" : typeof t === "number" ? t.toLocaleString("en-IN", {maximumFractionDigits: prefs.decimals}) : t})))))));
// Saved analyses keep this table zlib-compressed (core._pack), so it fits free storage.
async function unpack(packed) {
  if (!packed?.z) return packed;
  const bytes = Uint8Array.from(atob(packed.z), c => c.charCodeAt(0));
  return JSON.parse(await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate"))).text());
}
function tabs(entries) {
  const body = h("div"), bar = h("div", {class: "tabs", role: "tablist"});
  const open = (i, btn) => { bar.querySelectorAll("button").forEach(b => { b.classList.toggle("on", b === btn); b.ariaSelected = String(b === btn); }); body.replaceChildren(); fadeIn(body); (async () => entries[i][2](body))().catch(e => body.replaceChildren(failed(e))); };
  entries.forEach(([label, ic], i) => bar.append(h("button", {role: "tab", onclick: e => open(i, e.currentTarget)}, icon(ic), label)));
  queueMicrotask(() => open(0, bar.firstChild));
  return h("div", {}, bar, body);
}
const RATIO_WORDS = {"Market Cap": "Company value", "Current Price": "Share price", "High / Low": "Year high / low", "Stock P/E": "Price / earnings", "Book Value": "Book value per share", "Dividend Yield": "Dividend yield", ROCE: "Return on capital", ROE: "Return on equity", "Face Value": "Face value"};

function resultTabs(snap, extra = []) {
  const t = snap.ticker, flags = snap.red_flags || [], serious = flags.filter(f => f.severity === "high").length;
  const allowed = e => ALL_SECTIONS.includes(e[0].split(" · ")[0]) ? prefs.sections.includes(e[0].split(" · ")[0]) : true;
  return tabs([
    ["Summary", "dashboard", outer => { const el = outer.appendChild(h("div", {class: "card"}));
      if (snap.ai_summary) el.append(h("blockquote", {textContent: snap.ai_summary}), h("p", {class: "muted"}, icon("auto_awesome"), prefs.tech ? ` Written by ${snap.ai_model} from the data below · ${snap.ai_tokens || 0} tokens.` : " Written by AI from the figures below — check anything important for yourself."));
      else if (snap.ai_model) el.append(h("p", {class: "muted"}, icon("warning"), " The AI couldn't write a summary this time. Everything else below is still correct."));
      else el.append(h("div", {class: "info"}, "Choose an AI in ", h("a", {href: "#/settings", textContent: "Settings"}), " to get a written summary."));
      el.append(h("div", {class: "ratios"}, Object.entries(snap.metrics || {}).map(([k, v]) => h("div", {}, h("span", {class: "muted", textContent: RATIO_WORDS[k] || k}), h("b", {textContent: v})))));
      const list = (title, ic, cls, items) => h("div", {class: "card grow " + cls}, h("b", {}, icon(ic), " ", title), items.length ? h("ul", {}, items.slice(0, 5).map(x => h("li", {textContent: x}))) : h("p", {class: "muted", textContent: "None listed"}));
      el.append(h("div", {class: "row", style: "align-items:stretch"}, list("Strengths", "check_circle", "pros", snap.pros || []), list("Weaknesses", "error", "cons", snap.cons || [])));
      const news = snap.news || {};
      if (news.summary || news.headlines?.length) { const tone_ = {Positive: ["green", "trending_up"], Negative: ["red", "trending_down"], Neutral: ["", "trending_flat"]}[news.sentiment]; el.append(h("div", {class: "card"}, h("div", {class: "row"}, h("b", {}, icon("newspaper"), " Recent news"), tone_ ? h("span", {class: "badge " + tone_[0]}, icon(tone_[1]), `Tone of the news: ${news.sentiment.toLowerCase()}`) : null), news.summary ? h("p", {textContent: news.summary}) : null, news.summary && news.headlines?.length ? h("p", {class: "muted"}, icon("auto_awesome"), ` AI summary of the ${news.headlines.length} headlines below`) : null, h("ul", {}, (news.headlines || []).filter(n => /^https?:\/\//.test(n.url)).map(n => h("li", {}, h("a", {href: n.url, target: "_blank", rel: "noopener noreferrer", textContent: n.title}), h("span", {class: "muted", textContent: " · " + (n.source || "")})))))); }
    }],
    ["Results", "bar_chart", async el => {
      const q = snap.quarterly_df;
      if (!q) { el.append(h("p", {class: "muted", textContent: "No quarterly results were found."})); return; }
      const series = prefix => { const row = q.data.find(r => String(r[0]).toLowerCase().startsWith(prefix)); return row ? row.slice(1).map(v => parseFloat(String(v ?? "").replace(/[,%]/g, ""))) : []; };
      const card = (label, prefix, fmt) => { const v = series(prefix).filter(Number.isFinite); if (!v.length) return null; const now = v.at(-1), was = v.at(-2) ?? now; return h("div", {class: "metric"}, h("small", {textContent: label}), h("b", {textContent: fmt(now)}), delta(`${was ? ((now / was - 1) * 100).toFixed(1) : "0"}% vs last quarter`, now - was), prefs.sparklines && v.length > 1 ? spark(v) : null); };
      const cr = v => `₹${v.toLocaleString("en-IN", {maximumFractionDigits: 0})} Cr`;
      el.append(h("div", {class: "metrics"}, card("Sales", "sales", cr) || card("Sales", "revenue", cr), card("Net profit", "net profit", cr), card("Operating margin", "opm", v => `${v.toFixed(0)}%`), card("Profit per share", "eps", v => `₹${v.toFixed(2)}`)));
      const sales = series("sales").length ? series("sales") : series("revenue"), profit = series("net profit");
      if (sales.length) { const top = Math.max(...sales.filter(Number.isFinite), ...profit.filter(Number.isFinite), 1), labels = q.columns.slice(1); el.append(h("div", {class: "card"}, h("b", {textContent: "Sales and profit"}), h("span", {class: "muted", textContent: " · ₹ crore, each quarter"}), h("div", {class: "bars", role: "img", ariaLabel: "Sales and net profit by quarter"}, labels.map((l, i) => h("div", {title: `${l}: sales ${sales[i]}, profit ${profit[i]}`}, h("div", {class: "pair"}, h("i", {style: `height:${Math.max((sales[i] || 0) / top * 100, 0)}%`}), h("i", {class: "b", style: `height:${Math.max((profit[i] || 0) / top * 100, 0)}%`})), h("small", {textContent: l})))), h("span", {class: "muted"}, h("span", {style: "color:var(--accent)", textContent: "■ Sales  "}), h("span", {style: "color:var(--orange)", textContent: "■ Net profit"})))); }
      el.append(h("div", {class: "row"}, h("b", {class: "grow", textContent: "Quarterly results"}), h("button", {class: "btn link", onclick: () => downloadCsv(q, `${t}_quarterly.csv`)}, icon("download"), "Download")), dfTable({columns: q.columns, data: q.data.filter(r => r[0] !== "Raw PDF")}));
      const {statements} = await meta();
      for (const [key, label] of statements) if (snap[key]) el.append(h("details", {class: "card"}, h("summary", {textContent: label}), dfTable(snap[key])));
    }],
    ["Every number", "table_rows", async el => {
      const c = await unpack(snap.correlated), figs = snap.figures || [], idCol = c ? c.columns.indexOf("Identified") : -1;
      const col = n => c ? c.columns.indexOf(n) : -1, checked = c ? c.data.filter(r => (r[col("In filings")] || 0) > 0 && r[col("Latest quarter")] !== null).length : 0;
      el.append(h("div", {class: "metrics"}, metric("Measures", String(c?.data?.length || 0)), metric("Numbers from reports", (snap.figure_count || 0).toLocaleString("en-IN")), metric("Reports read", String((snap.sources || []).filter(s => s.path).length)), metric("Double-checked", String(checked))));
      if (c) {
        const named = h("input", {type: "checkbox", checked: idCol >= 0, disabled: idCol < 0, style: "width:auto"}), wrap = h("div");
        const view = () => { const rows = named.checked && idCol >= 0 ? c.data.filter(r => r[idCol]) : c.data, keep = c.columns.map((_, i) => i).filter(i => i !== idCol), df = {columns: keep.map(i => c.columns[i]), data: rows.map(r => keep.map(i => r[i]))}; wrap.replaceChildren(h("p", {class: "muted", textContent: `${rows.length} of ${c.data.length} measures`}), dfTable(df), h("button", {class: "btn link", onclick: () => downloadCsv(df, `${t}_all_numbers.csv`)}, icon("download"), "Download this table")); };
        named.addEventListener("change", view);
        el.append(h("div", {class: "row"}, h("b", {class: "grow", textContent: "Every measure, from every source"}), h("label", {class: "row", style: "margin:0"}, named, "Well-known measures only")), wrap); view();
      }
      if (figs.length) {
        const cols = ["Source", "Category", "Label", "Currency", "Value", "Unit", "Page", ...(prefs.context ? ["Context"] : [])], box = h("div");
        const opts = (k, all) => h("select", {ariaLabel: all}, h("option", {value: "", textContent: all}), [...new Set(figs.map(f => f[k]).filter(Boolean))].sort().map(v => h("option", {value: v, textContent: v})));
        const src = opts("Source", "All reports"), topic = opts("Category", "All topics"), find = h("input", {type: "search", placeholder: "e.g. margin, deposits, capex", ariaLabel: "Search these numbers"});
        const draw = () => { const n = find.value.toLowerCase(), v = figs.filter(f => (!src.value || f.Source === src.value) && (!topic.value || f.Category === topic.value) && `${f.Label} ${f.Context}`.toLowerCase().includes(n)); box.replaceChildren(h("p", {class: "muted", textContent: `${v.length} of ${figs.length} numbers${figs.length < snap.figure_count ? ` (the first ${figs.length} of ${snap.figure_count} are kept, to fit free storage)` : ""}`}), dfTable({columns: cols, data: v.slice(0, 500).map(f => cols.map(k => f[k]))})); };
        [src, topic].forEach(x => x.addEventListener("change", draw)); find.addEventListener("input", draw);
        const med = a => { const b = [...a].sort((x, y) => x - y); return b[Math.floor(b.length / 2)]; };
        const summary = [...new Set(figs.map(f => f.Source))].map(source => { const g = figs.filter(f => f.Source === source), pages = g.map(f => f.Page).filter(Number.isFinite); return h("div", {}, h("p", {}, h("b", {textContent: source}), h("span", {class: "muted", textContent: ` · ${g.length} numbers${pages.length ? ` · pages ${Math.min(...pages)}–${Math.max(...pages)}` : ""}`})), dfTable({columns: ["Topic", "Numbers", "Low", "Median", "High"], data: [...new Set(g.map(f => f.Category))].map(cat => { const v = g.filter(f => f.Category === cat).map(f => Number(f.Value)).filter(Number.isFinite); return [cat, v.length, v.length ? Math.min(...v) : null, v.length ? med(v) : null, v.length ? Math.max(...v) : null]; })})); });
        el.append(h("p", {style: "margin-top:16px"}, h("b", {textContent: "Every number, exactly as printed in the reports"})), h("div", {class: "row"}, h("div", {class: "grow"}, src), h("div", {class: "grow"}, topic), h("div", {class: "grow"}, find)), box, h("details", {class: "card"}, h("summary", {}, icon("description"), " Report by report"), summary)); draw();
      }
    }],
    ["What changed", "change_circle", el => {
      el.append(h("p", {class: "muted", textContent: "What's different since the last check. A straight comparison — no AI involved."}));
      const ch = snap.changes;
      if (!ch) el.append(h("div", {class: "info", textContent: "This is the first check of this company. Next time, this tab will show exactly what moved."}));
      else if (!ch.length) el.append(h("p", {class: "muted", textContent: "Nothing has changed since the last check."}));
      else el.append(dfTable({columns: ["What", "Item", "Was", "Now"], data: ch.map(c => [c.kind, c.item, c.from, c.to])}));
    }],
    ["Promises kept?", "handshake", async el => {
      el.append(h("p", {class: "muted", textContent: "What the company's bosses promised when they last spoke to investors — and whether the results since then kept those promises."}));
      const ref = doc(mine("claims"), t), box = h("div"), note = h("p", {class: "muted"});
      const load = async () => (await getDoc(ref)).data()?.claims || [];
      const draw = claims => {
        const graded = claims.filter(c => c.status), kept = graded.filter(c => c.status === "Delivered").length, words = {Delivered: ["Kept", "green"], Missed: ["Not kept", "red"], Unclear: ["Too early to tell", ""]};
        box.replaceChildren(...(claims.length ? [h("div", {class: "metrics"}, metric("Promises found", String(claims.length)), metric("Checked so far", String(graded.length)), metric("Kept", graded.length ? `${kept}/${graded.length}` : "—")),
          ...claims.map(c => h("div", {class: "card"}, h("div", {class: "row"}, h("b", {class: "grow"}, c.metric || "", h("span", {class: "muted", textContent: ` · said in ${c.quarter} · by ${c.horizon}`})), c.status ? h("span", {class: "badge " + (words[c.status]?.[1] || ""), textContent: words[c.status]?.[0] || c.status}) : null), h("blockquote", {textContent: c.claim}), c.why ? h("p", {class: "muted", textContent: `${c.why} · checked against ${c.checked_against || c.against || ""}`}) : null))] : []));
      };
      const act = (label, ic, action, primary) => h("button", {class: "btn" + (primary ? " primary" : ""), disabled: !aiReady(), onclick: async e => {
        const b = e.currentTarget; b.disabled = true; note.textContent = "Working — this can take a minute or two…";
        try { const {claims} = await call("guidance", {ticker: t, action, ai: aiBody(), claims: await load()}); await setDoc(ref, {claims}); draw(claims); note.textContent = ""; }
        catch (err) { note.textContent = err.message; } finally { b.disabled = false; }
      }}, icon(ic), label);
      el.append(h("div", {class: "row"}, act("Find their promises", "auto_stories", "harvest", true), act("Check if they kept them", "fact_check", "check")), note, box);
      if (!aiReady()) el.append(h("div", {class: "info"}, "Choose an AI in ", h("a", {href: "#/settings", textContent: "Settings"}), " so the app can read the call and find the promises."));
      draw(await load());
    }],
    [serious ? `Reports & warnings · ${serious}` : "Reports & warnings", "folder_open", el => {
      if (snap.red_flags === null || snap.red_flags === undefined) el.append(h("p", {class: "muted", textContent: "No annual report was read in this check, so warning signs weren't looked for."}));
      else if (!flags.length) el.append(h("p", {class: "muted", textContent: "No warning signs were found in the annual report."}));
      else el.append(h("b", {textContent: "Warning signs in the annual report"}), ...flags.map(f => h("div", {class: "card"}, h("span", {class: "badge " + ({high: "red", medium: "orange"}[f.severity] || ""), textContent: f.kind}), h("span", {class: "muted", textContent: ` page ${f.page}`}), h("p", {textContent: f.text}))));
      const src = snap.sources || [];
      el.append(h("h3", {style: "margin-top:16px", textContent: "Reports used"}));
      if (!src.length) el.append(h("p", {class: "muted", textContent: "No reports were downloaded this time."}));
      else el.append(h("div", {class: "scroll"}, h("table", {}, h("thead", {}, h("tr", {}, ["Report", "Numbers found", "Pages used", "Original"].map(x => h("th", {textContent: x})))),
        h("tbody", {}, src.map(s => h("tr", {}, h("td", {textContent: s.category}), h("td", {class: "num", textContent: s.figures || 0}), h("td", {textContent: s.first_page ? `p${s.first_page}–p${s.last_page}` : s.error || "—"}), h("td", {}, /^https?:\/\//.test(s.url) ? h("a", {href: s.url, target: "_blank", rel: "noopener noreferrer", textContent: "Open ↗"}) : "—")))))));
    }],
    ...extra,
  ].filter(allowed));
}
const pastChecks = ticker => ["Past checks", "history", async el => {
  const rows = (await getDocs(query(mine("runs"), where("ticker", "==", ticker)))).docs.map(d => ({id: d.id, ...d.data()})).sort((a, b) => b.ts - a.ts);
  el.append(dfTable({columns: ["When", "AI view", "AI summary", "Why it ran"], data: rows.map(r => [when(r.ts), VIEW[r.verdict]?.[0] || "—", r.summary || "—", r.label])}, r => { const hit = rows.find(x => when(x.ts) === r[0]); if (hit) location.hash = "#/history/" + hit.id; }));
  if (rows.length > 1) {
    const pick = (i, label) => h("select", {ariaLabel: label}, rows.map((r, n) => h("option", {value: r.id, selected: n === i, textContent: when(r.ts)})));
    const a = pick(0, "Compare the check on"), b = pick(1, "with the one on"), out = h("div");
    const go = async () => { out.replaceChildren(h("p", {class: "muted", textContent: "Comparing…"})); try { const [n, o] = await Promise.all([snapOf(a.value), snapOf(b.value)]); const {changes} = await call("diff", {new: n, old: o}); out.replaceChildren(changes.length ? dfTable({columns: ["What", "Item", "Was", "Now"], data: changes.map(c => [c.kind, c.item, c.from, c.to])}) : h("p", {class: "muted", textContent: "Nothing changed between these two checks."})); } catch (e) { out.replaceChildren(failed(e)); } };
    [a, b].forEach(x => x.addEventListener("change", go));
    el.append(h("div", {class: "row", style: "margin-top:16px"}, h("label", {class: "grow"}, "Compare the check on", a), h("label", {class: "grow"}, "with the one on", b)), out); go();
  }
}];

// --- Research ----------------------------------------------------------------
async function research() {
  const page = freshPage();
  const m = await meta(), chosen = new Set(prefs.categories || m.categories);
  const tickers = h("input", {value: prefs.tickers, placeholder: "Their stock symbols, separated by commas — e.g. TCS, INFY, RELIANCE", ariaLabel: "Companies"});
  const when_ = h("select", {ariaLabel: "How far back"}, m.timeframes.map(t => h("option", {value: t, selected: t === prefs.timeframe, textContent: t})));
  const useAI = h("input", {type: "checkbox", checked: aiReady(), disabled: !aiReady(), style: "width:auto"});
  const upload = h("input", {type: "file", accept: ".csv,.xlsx", ariaLabel: "Upload a list of companies", onchange: async e => {
    const f = e.target.files[0]; if (!f) return;
    if (f.size > 3e6) return toast("That file is too large.");
    try { tickers.value = (await call("upload", {kind: "tickers", name: f.name, file: await fileB64(f)})).tickers.join(", "); } catch (err) { toast(err.message); }
  }});
  const status = h("div"), out = h("div");
  const go = h("button", {class: "btn primary", onclick: async () => {
    const list = [...new Set(tickers.value.split(",").map(t => t.trim().toUpperCase()).filter(Boolean))].slice(0, 20);
    if (!list.length) return toast("Type at least one company symbol.");
    if (!chosen.size) return toast("Pick at least one kind of report to read.");
    go.disabled = true; out.replaceChildren(); const results = [], errors = [];
    for (const [i, t] of list.entries()) {
      status.replaceChildren(h("p", {class: "muted", textContent: `Looking into ${t} (${i + 1} of ${list.length}) — about a minute each…`}));
      try { results.push(await lookInto(t, list.length > 1 ? `Analysed with ${list.length - 1} other(s)` : "Analysed", {useAI: useAI.checked, categories: [...chosen], timeframe: when_.value})); }
      catch (e) { errors.push(`${t}: ${e.message}`); }
    }
    go.disabled = false; status.replaceChildren(...errors.map(x => h("p", {class: "err", textContent: x})));
    if (!results.length) return;
    const pane = h("div"), show = s => pane.replaceChildren(resultTabs(s));
    out.append(h("div", {class: "row"}, results.length > 1 ? h("div", {class: "seg"}, results.map((r, i) => h("button", {class: i ? "" : "on", textContent: r.ticker, onclick: e => { e.currentTarget.parentNode.querySelectorAll("button").forEach(b => b.classList.toggle("on", b === e.currentTarget)); show(r); }}))) : h("b", {textContent: results[0].ticker}), h("div", {class: "grow"}),
      h("button", {class: "btn", onclick: () => downloadReport(results, "xlsx", `research_${stamp()}.xlsx`).catch(e => toast(e.message))}, icon("table_view"), "Spreadsheet"), h("button", {class: "btn", onclick: () => downloadReport(results, "docx", `research_${stamp()}.docx`).catch(e => toast(e.message))}, icon("description"), "Report")), pane);
    show(results[0]);
  }}, icon("play_arrow"), "Analyse");
  page.replaceChildren(h("h1", {textContent: "Research"}), h("p", {class: "muted", textContent: "Look into several companies at once and download everything in one spreadsheet or report. For a single company, the Stock page is quicker."}),
    h("div", {class: "card"}, h("p", {}, h("b", {textContent: "1 · Which companies?"})), tickers,
      h("p", {}, h("b", {textContent: "2 · What should it read?"})), h("div", {class: "pills"}, m.categories.map(c => h("button", {class: chosen.has(c) ? "on" : "", textContent: m.words[c] || c, onclick: e => { chosen.has(c) ? chosen.delete(c) : chosen.add(c); e.currentTarget.classList.toggle("on"); }}))),
      h("details", {class: "card"}, h("summary", {}, icon("tune"), " More options"), h("label", {style: "margin-top:8px"}, "Or upload a list of companies (a spreadsheet with a column called ticker)", upload), h("label", {}, "How far back", when_), h("label", {class: "row"}, useAI, aiReady() ? "Write AI summaries" : "Write AI summaries — choose an AI in Settings first")),
      h("p", {}, h("b", {textContent: "3 · Go"})), go), h("button", {class: "btn link", onclick: () => download("ticker\nRELIANCE\nTCS\nINFY\n", "watchlist_template.csv", "text/csv")}, icon("download"), "Need a list to upload? Download an example"), h("p", {class: "muted", textContent: "Earlier checks are on the History page."}), status, out);
}

// --- Portfolio -----------------------------------------------------------------
let pfView = "all", pfSection = "Holdings", pfLayout = "Cards", pfSort = null;
const PIE = ["#3B75AF", "#EF8636", "#519E3E", "#C53A32", "#8D69B8", "#84584E", "#D57DBE", "#7F7F7F", "#BCBC45", "#58BBCC"];
// A ring like the desktop app's: biggest slice first from the top, labels on the slices big enough to hold one.
function donut(items) {
  const NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg"), total = items.reduce((s, i) => s + i[1], 0), R = 76, C = 2 * Math.PI * R;
  svg.setAttribute("viewBox", "0 0 200 200"); svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "How your money is spread: " + [...items].sort((a, b) => b[1] - a[1]).map(([t, v]) => `${t} ${(v / total * 100).toFixed(1)}%`).join(", "));
  let start = 0;
  [...items].sort((a, b) => b[1] - a[1]).forEach(([t, v], i) => {
    const part = v / total, ring = document.createElementNS(NS, "circle"), tip = document.createElementNS(NS, "title");
    Object.entries({cx: 100, cy: 100, r: R, fill: "none", stroke: PIE[i % PIE.length], "stroke-width": 44, "stroke-dasharray": calm() ? `${part * C} ${C}` : `0 ${C}`, "stroke-dashoffset": -start * C, transform: "rotate(-90 100 100)"}).forEach(([k, x]) => ring.setAttribute(k, x));
    tip.textContent = `${t} ${(part * 100).toFixed(1)}%`; ring.append(tip); svg.append(ring); setTimeout(() => ring.setAttribute("stroke-dasharray", `${part * C} ${C}`), 30);
    if (part >= .07) {
      const angle = (start + part / 2) * 2 * Math.PI - Math.PI / 2, label = document.createElementNS(NS, "text"), x = 100 + R * Math.cos(angle), y = 100 + R * Math.sin(angle);
      Object.entries({x, y: y - 2, fill: "#fff", "font-size": t.length > 6 ? 8 : 10, "text-anchor": "middle", transform: `rotate(${(deg => deg > 90 && deg < 270 ? deg - 180 : deg)(angle * 180 / Math.PI + 90)} ${x} ${y})`}).forEach(([k, n]) => label.setAttribute(k, n));
      label.textContent = t.slice(0, 9); const pc = document.createElementNS(NS, "tspan"); pc.setAttribute("x", x); pc.setAttribute("dy", 11); pc.textContent = `${(part * 100).toFixed(1)}%`; label.append(pc); svg.append(label);
    }
    start += part;
  });
  return svg;
}
const fileB64 = async f => { let s = ""; const b = new Uint8Array(await f.arrayBuffer()); for (let i = 0; i < b.length; i += 8192) s += String.fromCharCode(...b.subarray(i, i + 8192)); return btoa(s); };
async function accountsList() {
  let list = (await getDocs(mine("accounts"))).docs.map(d => ({id: d.id, ...d.data()}));
  if (!list.length) { await setDoc(doc(mine("accounts"), "main"), {name: "My stocks"}); list = [{id: "main", name: "My stocks"}]; }
  return list;
}
const holdingId = (account, ticker) => (account === "main" ? ticker : `${account}:${ticker}`);
async function saveHolding(account, c, qty, avg) {
  await setDoc(doc(mine("holdings"), holdingId(account, c.ticker)), {ticker: c.ticker, name: c.name || c.ticker, screener_id: c.screener_id ?? null, account, qty: qty || 0, avg_price: avg || 0});
}
async function syncTrades(value = 0) {
  const trades = (await getDocs(mine("trades"))).docs.map(d => ({_id: d.id, ...d.data()}));
  const r = await call("trades", {trades, value});
  for (const p of r.positions) { const ref = doc(mine("holdings"), holdingId(p.account, p.ticker)), d = await getDoc(ref); if (d.exists()) await setDoc(ref, {...d.data(), qty: p.qty, avg_price: p.avg_price}); }
  return {...r, trades};
}
function capBucket(cr) { return cr === null || cr === undefined ? "Unknown" : cr >= 100000 ? "Large cap" : cr >= 30000 ? "Mid cap" : "Small cap"; }
async function portfolioPage() {
  const page = freshPage();
  const accounts = await accountsList(), names = Object.fromEntries(accounts.map(a => [a.id, a.name]));
  if (pfView !== "all" && !names[pfView]) pfView = "all";
  const name = h("input", {placeholder: "e.g. Long-term, Trading, Mom's account"});
  const newAcct = popover("New account", "add", pop => pop.replaceChildren(h("label", {}, "Name", name), h("button", {class: "btn primary", textContent: "Create", onclick: async () => { if (!name.value.trim()) return; await addDoc(mine("accounts"), {name: name.value.trim().slice(0, 60)}); route(); }})));
  const manage = pfView !== "all" ? popover("Manage", "edit", pop => { const rn = h("input", {value: names[pfView]}); pop.replaceChildren(h("label", {}, "Rename", rn), h("div", {class: "row"}, h("button", {class: "btn", textContent: "Save name", onclick: async () => { await setDoc(doc(mine("accounts"), pfView), {name: rn.value.trim().slice(0, 60) || names[pfView]}); route(); }}), h("button", {class: "btn", textContent: "Delete this account", onclick: async () => { if (!confirm("Delete this account and its stocks and trades?")) return; for (const c of ["holdings", "trades"]) for (const d of (await getDocs(mine(c))).docs) if ((d.data().account || "main") === pfView) await deleteDoc(d.ref); await deleteDoc(doc(mine("accounts"), pfView)); pfView = "all"; route(); }}))); }) : null;
  const acctBar = h("div", {class: "row"}, h("div", {class: "seg"}, [["all", "All accounts"], ...accounts.map(a => [a.id, a.name])].map(([id, n]) => h("button", {class: id === pfView ? "on" : "", textContent: n, onclick: () => { pfView = id; route(); }}))), newAcct, manage);
  const into = () => h("select", {ariaLabel: "Account"}, accounts.map(a => h("option", {value: a.id, selected: a.id === pfView, textContent: a.name})));
  const addTo = into(), qtyIn = h("input", {type: "number", min: 0, placeholder: "Optional", ariaLabel: "Shares you own"}), avgIn = h("input", {type: "number", min: 0, placeholder: "Optional", ariaLabel: "Average price paid"});
  const addSearch = companySearch(c => saveHolding(addTo.value, c, Number(qtyIn.value), Number(avgIn.value)).then(() => { toast(`Added ${c.ticker}.`); route(); }));
  const imp = into(), impFile = h("input", {type: "file", accept: ".csv,.xlsx", ariaLabel: "Portfolio file"}), impNote = h("div");
  const adders = h("div", {}, h("details", {class: "card"}, h("summary", {}, icon("add_circle"), " Add a stock"), h("div", {class: "row", style: "margin-top:8px"}, h("label", {class: "grow"}, "Account", addTo), prefs.positions ? h("label", {class: "grow"}, "Shares you own", qtyIn) : null, prefs.positions ? h("label", {class: "grow"}, "Average price paid (₹)", avgIn) : null), addSearch),
    h("details", {class: "card"}, h("summary", {}, icon("upload_file"), " Add many at once from a spreadsheet"), h("p", {class: "muted", textContent: "Fill in one stock per row. Only the ticker (the stock's short symbol) is needed."}),
      h("button", {class: "btn link", onclick: () => download("account,ticker,quantity,avg_price\nLong term,RELIANCE,10,1200\nLong term,TCS,5,3100\nTrading,INFY,,\n", "portfolio_template.csv", "text/csv")}, icon("download"), "Download the example"), impFile, h("label", {style: "margin-top:8px"}, "Put them in", imp),
      h("button", {class: "btn primary", onclick: async e => { const f = impFile.files[0]; if (!f) return toast("Choose a file first."); if (f.size > 3e6) return toast("That file is too large."); const b = e.currentTarget; b.disabled = true; try { const r = await call("upload", {kind: "portfolio", name: f.name, file: await fileB64(f)}); for (const row of r.rows) { let acct = imp.value; if (row.account) { let hit = accounts.find(a => a.name.toLowerCase() === row.account.toLowerCase()); if (!hit) { const made = await addDoc(mine("accounts"), {name: row.account}); hit = {id: made.id, name: row.account}; accounts.push(hit); } acct = hit.id; } await saveHolding(acct, row, row.qty, row.avg_price); } toast(`Added ${r.rows.length} stock(s).`); if (r.problems.length) impNote.replaceChildren(...r.problems.map(p => h("p", {class: "err", textContent: p}))); else route(); } catch (err) { impNote.replaceChildren(failed(err)); } b.disabled = false; }}, icon("upload"), "Import"), impNote));
  page.replaceChildren(acctBar, adders, h("p", {class: "skel", textContent: "Loading…"}));
  const every = await holdings(), rows = pfView === "all" ? every : every.filter(r => r.account === pfView), latest = await latestAll();
  await checkAlerts(every);
  const skel = page.querySelector(".skel");
  if (!rows.length) { skel.replaceWith(h("p", {class: "muted", textContent: "No stocks in this account yet — add one above."})); return; }
  const tick = [...new Set(rows.map(r => r.ticker))], byT = Object.fromEntries(rows.map(r => [r.ticker, r]));
  const quotes = Object.fromEntries(await Promise.all(tick.map(async t => [t, await quote(byT[t].screener_id).catch(() => null)])));
  let value = 0, invested = 0, costed = 0, day = 0;
  for (const r of rows) { const q = quotes[r.ticker]; if (q && r.qty) { value += r.qty * q.price; day += r.qty * q.change; if (r.avg_price) { invested += r.qty * r.avg_price; costed += r.qty * q.price; } } }
  const moves = tick.filter(t => quotes[t]).map(t => [byT[t], quotes[t].pct]).sort((a, b) => b[1] - a[1]);
  const up = moves.filter(m => m[1] > 0).length, down = moves.filter(m => m[1] < 0).length, avgMove = moves.length ? moves.reduce((s, m) => s + m[1], 0) / moves.length : 0;
  const counts = {}; for (const t of tick) { const v = VIEW[latest[t]?.verdict] ? latest[t].verdict : "none"; counts[v] = (counts[v] || 0) + 1; }
  const alloc = tick.map(t => [t, rows.filter(r => r.ticker === t).reduce((s, r) => s + (quotes[t] && r.qty ? r.qty * quotes[t].price : 0), 0)]).filter(a => a[1] > 0);
  const owned = prefs.positions && value > 0, avgText = moves.length ? `${avgMove >= 0 ? "+" : ""}${avgMove.toFixed(2)}%` : "—", upDown = metric("Up / down today", `${up} / ${down}`), avgMetric = metric("Average move today", avgText);
  const moved = ([r, p]) => h("p", {class: "move"}, r.ticker, " ", h("span", {class: tone(p), textContent: `${p >= 0 ? "+" : ""}${p.toFixed(2)}%`}));
  const header = h("div", {class: "card"}, h("h4", {}, icon("insights"), " At a glance"), h("div", {class: "metrics"}, ...(owned ? [metric("Worth today", inr(value), delta(`${inr(day, true)} today`, day)), ...(invested ? [metric("You paid", inr(invested)), metric("Gain or loss", inr(costed - invested, true), delta(`${((costed / invested - 1) * 100).toFixed(2)}%`, costed - invested))] : [upDown, avgMetric])] : [metric("Stocks", String(tick.length)), upDown, avgMetric]), metric("Checked", `${tick.filter(t => latest[t]).length} / ${tick.length}`)),
    h("div", {class: "glance"}, h("div", {}, h("b", {}, icon("trending_up"), " Biggest moves today"), moves.slice(0, 3).map(moved), [...moves].reverse().slice(0, Math.min(3, Math.max(moves.length - 3, 0))).map(moved)),
      h("div", {}, h("b", {}, icon("psychology"), " What the AI thinks"), h("div", {class: "row", style: "margin-top:8px"}, Object.entries(counts).map(([v, n]) => { const w = VIEW[v]; return h("span", {class: "badge " + (w ? w[1] : "")}, icon(w ? w[2] : "hourglass_top"), `${w ? w[0] : "Not checked yet"} · ${n}`); }))),
      owned && alloc.length ? h("div", {class: "donut"}, donut(alloc)) : h("div")));
  const unread = (await events()).filter(e => !e.read).length, sectionBox = h("div");
  const SECTIONS = {Holdings: "view_module", Performance: "show_chart", ...(prefs.positions ? {Trades: "receipt_long"} : {}), Spread: "donut_large", Alerts: "notifications", "Weekly summary": "newspaper"};
  if (!SECTIONS[pfSection]) pfSection = "Holdings";
  const WORDS = {Holdings: "My stocks", Performance: "How it's doing", Trades: "Buys & sells", Spread: "Spread", Alerts: "Alerts", "Weekly summary": "Weekly summary"};
  const bar = h("div", {class: "seg sections"}, Object.entries(SECTIONS).map(([k, i]) => h("button", {class: k === pfSection ? "on" : "", onclick: e => { pfSection = k; bar.querySelectorAll("button").forEach(b => b.classList.toggle("on", b === e.currentTarget)); return drawSection(); }}, icon(i), k === "Alerts" && unread ? `Alerts · ${unread}` : WORDS[k])));
  const edit = r => popover("Edit", "edit", pop => { const qty = h("input", {type: "number", min: 0, value: r.qty || ""}), avg = h("input", {type: "number", min: 0, value: r.avg_price || ""}), acc = into(); acc.value = r.account; pop.replaceChildren(prefs.positions ? h("label", {}, "Shares", qty) : "", prefs.positions ? h("label", {}, "Average price paid (₹)", avg) : "", h("label", {}, "Account", acc), h("div", {class: "row"}, h("button", {class: "btn primary", textContent: "Save", onclick: async () => { await deleteDoc(doc(mine("holdings"), r._id)); await saveHolding(acc.value, r, Number(qty.value), Number(avg.value)); toast("Saved."); route(); }}), h("button", {class: "btn", textContent: "Remove", onclick: async () => { await deleteDoc(doc(mine("holdings"), r._id)); toast(`Removed ${r.ticker}.`); route(); }}))); });
  const recheck = h("button", {class: "btn", onclick: async () => { recheck.disabled = true; for (const [i, t] of tick.entries()) { recheck.lastChild.textContent = `Checking ${t} (${i + 1}/${tick.length})…`; await lookInto(t, "Re-checked from Portfolio").catch(e => toast(`${t}: ${e.message}`)); } route(); }}, icon("autorenew"), h("span", {textContent: "Check them all again"}));
  const SECTION_DRAW = {
    Holdings: el => {
      const SORTS = {...(prefs.positions ? {Value: "Biggest", Gain: "Best gain"} : {}), Day: "Today's move", Name: "A–Z"};
      if (!SORTS[pfSort]) pfSort = Object.keys(SORTS)[0];
      const facts = r => { const q = quotes[r.ticker], val = q && r.qty ? r.qty * q.price : null, cost = r.qty && r.avg_price ? r.qty * r.avg_price : null, gain = val !== null && cost ? val - cost : null; return {q, val, gain, pct: gain === null ? null : gain / cost * 100}; };
      const ago = r => { const l = latest[r.ticker], mins = l ? (Date.now() - l.ts) / 6e4 : null; return mins === null ? "Not checked yet" : mins < 60 ? `${Math.floor(mins)} min ago` : mins < 1440 ? `${Math.floor(mins / 60)} h ago` : `${Math.floor(mins / 1440)} days ago`; };
      const rank = {Value: r => facts(r).val, Gain: r => facts(r).pct, Day: r => facts(r).q?.pct};
      const sorted = () => [...rows].sort((a, b) => { if (pfSort === "Name") return a.ticker.localeCompare(b.ticker); const x = rank[pfSort](a) ?? -Infinity, y = rank[pfSort](b) ?? -Infinity; return y - x; });
      const pick = (options, current, choose) => h("div", {class: "seg"}, options.map(([k, label, i]) => h("button", {class: k === current ? "on" : "", onclick: () => { choose(k); draw(); }}, i ? icon(i) : null, label)));
      const card = r => { const {q, val, gain, pct} = facts(r), v = VIEW[latest[r.ticker]?.verdict];
        return h("div", {class: "card hcard"}, h("div", {class: "row"}, h("h3", {textContent: r.ticker}), viewPill(latest[r.ticker]?.verdict)),
          h("p", {class: "muted", textContent: r.name + (pfView === "all" ? ` · ${names[r.account] || "—"}` : "")}),
          h("div", {class: "metric plain"}, h("b", {textContent: q ? inr(q.price, false, 2) : "—"}), q ? delta(`${q.change >= 0 ? "+" : ""}${q.change.toFixed(2)} (${q.pct >= 0 ? "+" : ""}${q.pct.toFixed(2)}%)`, q.change) : null), q ? spark(q.spark) : null,
          prefs.positions && r.qty ? h("div", {class: "trio"}, h("div", {}, h("span", {class: "muted", textContent: "Shares"}), h("b", {textContent: r.qty.toLocaleString("en-IN")})), h("div", {}, h("span", {class: "muted", textContent: "Value"}), h("b", {textContent: val ? inr(val) : "—"})), h("div", {}, h("span", {class: "muted", textContent: "Gain/loss"}), h("b", {class: gain === null ? "" : tone(gain), textContent: gain === null ? "—" : inr(gain, true)}), gain === null ? null : h("span", {class: tone(gain), textContent: `${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%`}))) : null,
          h("div", {class: "row foot"}, h("span", {class: "muted grow", textContent: ago(r)}), edit(r), h("button", {class: "btn link", onclick: () => openCompany(r)}, icon("arrow_forward"), "Open"))); };
      const table = () => h("div", {class: "scroll stocks", style: `max-height:${prefs.table_height + 200}px`}, h("table", {}, h("thead", {}, h("tr", {}, ["Company", "Symbol", "Account", ...(prefs.positions ? ["Shares", "Paid"] : []), "Price", "Today", ...(prefs.positions ? ["Value", "Gain or loss"] : []), "AI view", "Checked", ""].map(t => h("th", {textContent: t})))),
        h("tbody", {}, sorted().map(r => { const {q, val, gain} = facts(r), verdict = latest[r.ticker]?.verdict;
          return h("tr", {}, h("td", {}, h("a", {href: "#", textContent: r.name, onclick: e => { e.preventDefault(); openCompany(r); }})), h("td", {textContent: r.ticker}), h("td", {textContent: names[r.account] || "—"}), ...(prefs.positions ? [h("td", {class: "num", textContent: r.qty || "—"}), h("td", {class: "num", textContent: r.avg_price ? inr(r.avg_price, false, 2) : "—"})] : []), h("td", {class: "num", textContent: q ? inr(q.price, false, 2) : "—"}), h("td", {class: "num " + (q ? tone(q.pct) : ""), textContent: q ? `${q.pct >= 0 ? "+" : ""}${q.pct.toFixed(2)}%` : "—"}), ...(prefs.positions ? [h("td", {class: "num", textContent: val ? inr(val) : "—"}), h("td", {class: "num " + (gain ? tone(gain) : ""), textContent: gain ? inr(gain, true) : "—"})] : []), h("td", {class: {Positive: "up", Cautious: "down"}[verdict] || "", textContent: VIEW[verdict]?.[0] || "—"}), h("td", {class: "muted", textContent: ago(r)}), h("td", {}, edit(r))); }))));
      const draw = () => el.replaceChildren(h("div", {class: "row"}, h("h4", {}, icon("view_module"), " My stocks"), pick([["Cards", "Cards", "grid_view"], ["Table", "Table", "table_rows"]], pfLayout, k => { pfLayout = k; }), pick(Object.entries(SORTS), pfSort, k => { pfSort = k; }), h("span", {class: "grow"}), recheck),
        pfLayout === "Cards" ? h("div", {class: "hgrid"}, sorted().map(card)) : table());
      draw();
    },
    Performance: async el => {
      const qty = {}; for (const r of rows) qty[r.ticker] = (qty[r.ticker] || 0) + (prefs.positions ? r.qty || 0 : 0);
      const p = await api("performance?h=" + encodeURIComponent(Object.entries(qty).map(([t, q]) => `${t}:${q}`).join(","))), last = p.you.length - 1, chart = h("div", {style: "height:360px"});
      el.append(h("div", {class: "metrics"}, [["1 week", 5], ["1 month", 21], ["3 months", 63], ["6 months", 126], ["1 year", last]].filter(([, d]) => last >= d && d > 0).map(([label, d]) => { const y = (p.you[last] / p.you[last - d] - 1) * 100, n = (p.nifty[last] / p.nifty[last - d] - 1) * 100; return metric(label, `${y >= 0 ? "+" : ""}${y.toFixed(1)}%`, delta(`${(y - n >= 0 ? "+" : "") + (y - n).toFixed(1)} points vs the market`, y - n)); })), chart, h("p", {class: "muted", textContent: `What ₹100 put into the stocks you hold today a year ago would be worth now${Object.values(qty).some(Boolean) ? ", in the amounts you hold them" : ", split equally"}, compared with the whole market (the Nifty 50).`}));
      const lc = LightweightCharts.createChart(chart, {height: 360, layout: {background: {color: "transparent"}, textColor: "#444"}, grid: {vertLines: {color: "rgba(128,128,128,.12)"}, horzLines: {color: "rgba(128,128,128,.12)"}}, rightPriceScale: {borderVisible: false}, timeScale: {borderVisible: false}});
      lc.addLineSeries({color: "#1F4E79", lineWidth: 2, title: "Your holdings"}).setData(p.dates.map((d, i) => ({time: d, value: p.you[i]})));
      lc.addLineSeries({color: "#A8620A", lineWidth: 2, title: "Nifty 50"}).setData(p.dates.map((d, i) => ({time: d, value: p.nifty[i]})));
      lc.timeScale().fitContent();
    },
    Trades: async el => {
      const acc = into(), stock = h("select", {ariaLabel: "Stock"}, tick.map(t => h("option", {value: t, textContent: t}))), side = h("select", {ariaLabel: "Bought or sold?"}, h("option", {value: "buy", textContent: "Bought"}), h("option", {value: "sell", textContent: "Sold"}));
      const today = new Date().toISOString().slice(0, 10), qty = h("input", {type: "number", min: 0}), price = h("input", {type: "number", min: 0}), date = h("input", {type: "date", value: today, max: today}), fees = h("input", {type: "number", min: 0, value: 0});
      const r = await syncTrades(value), log = pfView === "all" ? r.trades : r.trades.filter(t => t.account === pfView);
      el.append(h("p", {class: "muted", textContent: "Write down each time you buy or sell, and the app works out how many shares you have, what you paid on average, and the profit on anything you've sold."}),
        h("details", {class: "card", open: !log.length}, h("summary", {}, icon("add_circle"), " Add a buy or sell"), h("div", {class: "row", style: "margin-top:8px"}, ...[["Account", acc], ["Stock", stock], ["Bought or sold?", side], ["Shares", qty], ["Price per share (₹)", price], ["Date", date], ["Fees (₹)", fees]].map(([l, i]) => h("label", {class: "grow"}, l, i))), h("button", {class: "btn primary", onclick: async () => { if (!(Number(qty.value) > 0 && Number(price.value) > 0 && date.value)) return toast("Fill in the shares, price and date."); await addDoc(mine("trades"), {account: acc.value, ticker: stock.value, side: side.value, qty: Number(qty.value), price: Number(price.value), date: date.value, fees: Number(fees.value) || 0}); toast(`Saved: ${side.value === "buy" ? "bought" : "sold"} ${qty.value} ${stock.value}.`); route(); }}, icon("add"), "Add")));
      if (!log.length) { el.append(h("div", {class: "info", textContent: "Nothing written down yet. Until you add some, the share counts and prices you typed in are used as they are."})); return; }
      if (r.unmatched) el.append(h("div", {class: "info", textContent: `${r.unmatched} share(s) were sold, but there's no record of buying them, so their profit can't be worked out. Add the missing buy and it will fix itself.`}));
      const realised = r.gains.reduce((s, g) => s + g.gain, 0);
      el.append(h("div", {class: "metrics"}, metric("Yearly return", r.rate === null ? "—" : `${r.rate >= 0 ? "+" : ""}${(r.rate * 100).toFixed(1)}% a year`), metric("Profit on sold shares", r.gains.length ? inr(realised, true) : "—"), metric("Buys and sells", String(log.length))));
      if (r.gains.length) {
        const years = [...new Set(r.gains.map(g => g.fy))].sort().reverse(), box = h("div");
        const show = fy => { const y = r.gains.filter(g => g.fy === fy), sum = t => y.filter(g => g.term === t).reduce((s, g) => s + g.gain, 0), df = {columns: ["Stock", "Bought", "Sold", "Qty", "Buy ₹", "Sell ₹", "Days held", "Term", "Gain ₹"], data: y.map(g => [g.ticker, g.bought, g.sold, g.qty, g.buy_price, g.sell_price, g.days, g.term, g.gain])};
          box.replaceChildren(h("div", {class: "metrics"}, metric(`Short term, ${fy}`, inr(sum("Short"), true)), metric(`Long term, ${fy}`, inr(sum("Long"), true))), dfTable(df), h("button", {class: "btn link", onclick: () => downloadCsv(df, `realised_gains_${fy}.csv`)}, icon("download"), "Download this table"), h("p", {class: "muted", textContent: "Worked out the way Indian tax rules count it (oldest shares sold first, fees included). Held over 12 months counts as long term. This is a record, not tax advice."})); };
        el.append(h("b", {textContent: "Profit on sold shares, by financial year"}), h("div", {class: "seg", style: "display:flex;width:fit-content"}, years.map((fy, i) => h("button", {class: i ? "" : "on", textContent: fy, onclick: e => { e.currentTarget.parentNode.querySelectorAll("button").forEach(b => b.classList.toggle("on", b === e.currentTarget)); show(fy); }}))), box); show(years[0]);
      }
      el.append(h("details", {class: "card"}, h("summary", {}, icon("list"), " Everything you've written down"), [...log].sort((a, b) => String(b.date).localeCompare(a.date)).map(t => h("div", {class: "row"}, h("span", {class: "grow"}, h("b", {textContent: t.ticker}), ` · ${t.side} ${t.qty} @ ${inr(t.price, false, 2)}`, h("span", {class: "muted", textContent: ` · ${t.date}${t.fees ? ` · charges ${inr(t.fees)}` : ""}`})), h("button", {class: "btn link", ariaLabel: "Delete this trade", onclick: async () => { await deleteDoc(doc(mine("trades"), t._id)); await syncTrades(); route(); }}, icon("delete"))))));
    },
    Spread: el => {
      const w = {}; for (const t of tick) w[t] = 0; for (const r of rows) { const q = quotes[r.ticker]; if (q && r.qty && prefs.positions) w[r.ticker] += r.qty * q.price; }
      const total = Object.values(w).reduce((a, b) => a + b, 0), share = t => (total ? w[t] / total : 1 / tick.length) * 100, sector = t => latest[t]?.screen?.Sector || "Unknown", sizeOf = t => capBucket(latest[t]?.screen?.["Market Cap"]);
      const sum = f => { const o = {}; for (const t of tick) o[f(t)] = (o[f(t)] || 0) + share(t); return Object.entries(o).sort((a, b) => b[1] - a[1]); };
      const heavy = tick.filter(t => share(t) > prefs.max_stock_pct), crowded = sum(sector).filter(([n, v]) => v > prefs.max_sector_pct && n !== "Unknown");
      heavy.forEach(t => el.append(h("div", {class: "info", textContent: `${t} is ${share(t).toFixed(1)}% of everything you own — more than the ${prefs.max_stock_pct}% you set as the most for one stock.`})));
      crowded.forEach(([n, v]) => el.append(h("div", {class: "info", textContent: `${n} companies are ${v.toFixed(1)}% of everything you own — more than the ${prefs.max_sector_pct}% you set as the most for one industry.`})));
      if (!heavy.length && !crowded.length) el.append(h("div", {class: "info", textContent: `Nicely spread: no stock over ${prefs.max_stock_pct}% and no industry over ${prefs.max_sector_pct}% (you can change these in Settings).`}));
      el.append(h("div", {class: "tiles"}, [...tick].sort((a, b) => share(b) - share(a)).map(t => { const p = quotes[t]?.pct ?? 0; return h("div", {class: "tile " + tone(p), style: `flex:${Math.max(share(t), 4)} 1 ${Math.max(share(t) * 5, 90)}px`, title: sector(t)}, h("b", {textContent: t}), h("span", {textContent: `${share(t).toFixed(1)}%`})); })),
        h("div", {class: "row", style: "align-items:flex-start"}, h("div", {class: "card grow"}, h("b", {textContent: "By company size"}), sum(sizeOf).map(([n, v]) => h("div", {class: "bar"}, h("span", {textContent: `${n} · ${v.toFixed(1)}%`}), h("i", {style: `width:${v}%`})))), h("div", {class: "card grow"}, h("b", {textContent: "By industry"}), sum(sector).map(([n, v]) => h("div", {class: "bar"}, h("span", {textContent: `${n} · ${v.toFixed(1)}%`}), h("i", {style: `width:${v}%`}))))),
        h("p", {class: "muted", textContent: `${total ? "Sized by what each holding is worth" : "Every stock counted equally"}. Green boxes are up today, red are down. Stocks not checked yet show as Unknown.`}));
    },
    Alerts: async el => {
      const all = await events(), rules = (await getDocs(mine("alerts"))).docs, fresh = all.filter(e => !e.read);
      el.append(h("div", {class: "card"}, h("div", {class: "row"}, h("h4", {class: "grow"}, icon("inbox"), " Messages"), fresh.length ? h("span", {class: "badge orange", textContent: `${fresh.length} new`}) : null, fresh.length ? h("button", {class: "btn link", onclick: async () => { for (const e of fresh) await markRead(e); route(); }}, icon("done_all"), "Mark all read") : null),
        all.length ? h("div", {}, all.slice(0, 60).map(eventRow)) : h("p", {class: "muted", textContent: "Nothing yet. You'll be told here when the AI's view changes, a company publishes something important, it breaks a promise, or a price you're watching is reached."})));
      el.append(h("div", {class: "card"}, h("h4", {}, icon("notifications_active"), " Price alerts"), h("p", {class: "muted", style: "margin:4px 0 0", textContent: "Checked while this site is open, and once each weekday after the market closes using the closing price."}),
        rules.length ? h("div", {}, rules.map(d => { const r = d.data(); return h("div", {class: "event" + (r.fired ? "" : " new")}, h("span", {class: "event-ic"}, icon(r.fired ? "check" : "visibility")), h("div", {class: "event-text"}, h("div", {}, h("b", {textContent: r.ticker}), ` ${r.op} ${inr(r.level, false, 2)}`), h("span", {class: "badge " + (r.fired ? "green" : ""), textContent: r.fired ? "Reached" : "Watching"})), h("div", {class: "event-actions"}, h("button", {class: "btn link", ariaLabel: `Delete the ${r.ticker} alert`, title: "Delete this alert", onclick: async () => { await deleteDoc(d.ref); route(); }}, icon("delete")))); })) : h("p", {class: "muted", textContent: "None yet. Set one from a company's page."})));
    },
    "Weekly summary": async el => {
      const week = Date.now() - 7 * 864e5, ev = (await events()).filter(e => e.ts > week), wk = tick.filter(t => quotes[t]?.spark?.length > 5).map(t => { const c = quotes[t].spark; return [t, (c.at(-1) / c.at(-6) - 1) * 100]; }).sort((a, b) => b[1] - a[1]);
      const text = [`Your stocks this week:`, ...wk.map(([t, p]) => `${t}: ${p >= 0 ? "+" : ""}${p.toFixed(1)}%`), "", "What happened:", ...(ev.length ? ev.map(e => `${e.title}`) : ["Nothing new."])].join("\n");
      el.append(h("div", {class: "row"}, h("b", {class: "grow", textContent: "The past seven days"}), h("button", {class: "btn", disabled: !(prefs.email_url && secret("emailtoken")), onclick: () => emailMe("Your weekly stock summary", text).then(() => toast("Sent.")).catch(e => toast(e.message))}, icon("mail"), "Email this to me")),
        wk.length ? dfTable({columns: ["Stock", "This week"], data: wk.map(([t, p]) => [t, `${p >= 0 ? "+" : ""}${p.toFixed(1)}%`])}) : h("p", {class: "muted", textContent: "Price history isn't available right now."}),
        h("h3", {style: "margin-top:16px", textContent: "What happened"}), ...(ev.length ? ev.map(e => h("p", {}, h("b", {textContent: e.title}), e.body ? h("span", {class: "muted", style: "display:block", textContent: e.body}) : null)) : [h("p", {class: "muted", textContent: "Nothing new this week."})]));
    },
  };
  skel.replaceWith(header, bar, sectionBox);
  // Each section draws into its own box, so a slow one can't land on top of the next.
  const drawSection = async () => { const box = h("div"); sectionBox.replaceChildren(box); fadeIn(box); try { await SECTION_DRAW[pfSection](box); } catch (e) { box.replaceChildren(failed(e)); } };
  await drawSection();
}

// --- Ask AI --------------------------------------------------------------------
let chatId = null;
async function askPage() {
  const page = freshPage();
  const docs = (await getDocs(mine("chats"))).docs.map(d => ({id: d.id, ...d.data()})).sort((a, b) => (b.updated || 0) - (a.updated || 0));
  const current = docs.find(c => c.id === chatId), [held, known] = await Promise.all([holdings(), latestAll()]), tickers = [...new Set(held.map(r => r.ticker))];
  let messages = current?.messages || [];
  const scope = new Set(), log = h("div", {class: "log"}), box = h("input", {placeholder: "Ask about a company, your stocks or their reports…", ariaLabel: "Your question", disabled: !aiReady()}), reports = h("input", {type: "checkbox", class: "switch", checked: true});
  const turn = (role, text) => { const body = h("div", {class: "txt", textContent: text}); return [h("div", {class: "msg " + role}, icon(role === "user" ? "person" : "auto_awesome"), body), body]; };
  const [first, second] = [...new Set([...tickers, "TCS", "INFY"])];
  const ideas = ["Which of my holdings look weakest right now, and why?", `How have ${first}'s margins moved over the last few quarters?`, `Compare ${first} and ${second}`, "What did the bosses say about demand on their latest investor calls?"];
  const draw = () => log.replaceChildren(...(messages.length ? messages.map(m => turn(m.role, m.text)[0]) : [h("div", {class: "welcome"}, h("h2", {textContent: "What would you like to know?"}), h("p", {class: "muted", textContent: "Answers come from your stocks and the companies' own reports."}), h("div", {class: "pills"}, ideas.map(q => h("button", {textContent: q, disabled: !aiReady(), onclick: () => { box.value = q; send(); }}))))]));
  const send = async () => {
    const question = box.value.trim(); if (!question) return;
    if (!aiReady()) return toast("Choose an AI in Settings first.");
    if (await overBudget()) return toast("You've reached the monthly AI limit you set in Settings.");
    box.value = ""; const history = messages.slice(), isNew = !chatId; messages = [...messages, {role: "user", text: question}]; draw();
    const [row, bubble] = turn("assistant", reports.checked ? "Reading…" : "Thinking…"); bubble.classList.add("thinking"); log.append(row); row.scrollIntoView({block: "nearest", behavior: calm() ? "auto" : "smooth"});
    try {
      const latest = await latestAll(), upper = question.toUpperCase(), wordsIn = t => new RegExp(`\\b${t.replace(/[^A-Z0-9]/g, "")}\\b`).test(upper), found = Object.keys(latest).filter(t => wordsIn(t) || upper.includes((latest[t].name || "").toUpperCase())), named = scope.size ? [...scope] : found;
      const details = await Promise.all((named.length ? named : Object.keys(latest)).filter(t => latest[t]).slice(0, 5).map(async t => { const s = await snapOf(latest[t].runId); return s ? `${t} (${s.company_name}). View: ${s.ai_verdict}. ${s.ai_summary || ""} Numbers: ${JSON.stringify(s.metrics)}. Strengths: ${(s.pros || []).join("; ")}. Weaknesses: ${(s.cons || []).join("; ")}.` : ""; }));
      const hold = (await holdings()).map(r => `${r.ticker}: ${r.qty || 0} shares at ${r.avg_price || "?"}`).join(", ");
      const r = await fetch("/api/ask", {method: "POST", headers: {"Content-Type": "application/json", Authorization: "Bearer " + await user.getIdToken()}, body: JSON.stringify({question, history, ai: aiBody(), data: dataBody(), filings: reports.checked ? named.slice(0, 2) : [], context: `Holdings: ${hold || "none"}\n${details.join("\n")}`})});
      if (!r.ok) { let m = "That didn't work. Try again in a minute."; try { m = (await r.json()).error; } catch { /* not JSON */ } throw new Error(m); }
      let text = ""; const reader = r.body.getReader(), dec = new TextDecoder();
      for (;;) { const {done, value} = await reader.read(); if (done) break; text += dec.decode(value, {stream: true}); bubble.classList.remove("thinking"); bubble.textContent = text; }
      messages = [...messages, {role: "assistant", text}].slice(-40);
      await addUsage(Math.ceil((question.length + text.length) / 4));
      const payload = {title: (messages[0].text || "Chat").slice(0, 60), updated: Date.now(), messages};
      if (chatId) await setDoc(doc(mine("chats"), chatId), payload); else chatId = (await addDoc(mine("chats"), payload)).id;
      if (isNew) return route();  // puts the new chat in the sidebar
    } catch (e) { messages = messages.slice(0, -1); box.value = question; toast(e.message); }
    draw();
  };
  box.addEventListener("keydown", e => { if (e.key === "Enter") send(); });
  const options = popover("Options", "tune", pop => { const all = [...new Set([...tickers, ...Object.keys(known)])].sort(); pop.replaceChildren(h("p", {class: "field", textContent: "Only these companies"}), all.length ? h("div", {class: "pills"}, all.map(t => h("button", {class: scope.has(t) ? "on" : "", textContent: t, onclick: e => { scope.has(t) ? scope.delete(t) : scope.add(t); e.currentTarget.classList.toggle("on"); }}))) : h("p", {class: "muted", textContent: "Whatever I mention"}), h("label", {class: "row", style: "margin-top:8px"}, reports, "Look in company reports too")); });
  page.replaceChildren(h("aside", {class: "chats"}, h("button", {class: "btn primary", onclick: () => { chatId = null; route(); }}, icon("edit_square"), "New chat"), docs.length ? h("p", {class: "muted", textContent: "Recent"}) : null, docs.map(c => h("button", {class: "chat" + (c.id === chatId ? " on" : ""), onclick: () => { chatId = c.id; route(); }}, icon(c.id === chatId ? "chat_bubble" : "chat_bubble_outline"), h("span", {textContent: c.title})))),
    h("div", {class: "ask"}, h("div", {class: "row"}, h("h3", {class: "grow", style: "margin:0"}, icon("auto_awesome"), " Ask AI"), options, chatId ? h("button", {class: "btn link", onclick: async () => { await deleteDoc(doc(mine("chats"), chatId)); chatId = null; route(); }}, icon("delete"), "Delete chat") : null),
      aiReady() ? "" : h("div", {class: "info"}, icon("info"), " Ask AI needs an AI turned on. Set it up in ", h("a", {href: "#/settings", textContent: "Settings"}), "."), log,
      h("div", {class: "chatbar"}, box, h("button", {class: "send", ariaLabel: "Send", disabled: !aiReady(), onclick: send}, icon("arrow_upward")))));
  draw();
}

// --- Screener --------------------------------------------------------------------
async function screenerPage() {
  const page = freshPage();
  const m = await meta(), saved = (await getDoc(doc(mine("settings"), "screens"))).data() || {};
  const q = h("textarea", {rows: 5, ariaLabel: "Search", value: m.presets["Cheap and profitable"], placeholder: "Market Cap > 5000 AND P/E < 20"});
  const sme = h("input", {type: "checkbox", style: "width:auto", onchange: () => run()}), out = h("div"), remove = h("span");
  const run = async () => {
    out.replaceChildren(h("p", {class: "muted", textContent: "Searching every listed company…"}));
    try {
      const own = Object.values(await latestAll()).map(l => l.screen).filter(Boolean);
      const r = own.length ? await call("screen", {q: q.value, sme: sme.checked, own}) : await api(`screen?q=${encodeURIComponent(q.value)}&sme=${sme.checked ? 1 : 0}`);
      const vc = r.columns.indexOf("Verdict"); if (vc >= 0) { r.rows.forEach(x => { x[vc] = VIEW[x[vc]]?.[0] || x[vc]; }); r.columns[vc] = "AI view"; }
      out.replaceChildren(h("p", {}, h("b", {textContent: `${r.count.toLocaleString("en-IN")} match${r.count === 1 ? "" : "es"}`}), h("span", {class: "muted", textContent: ` · out of ${r.total.toLocaleString("en-IN")} listed companies${r.count > 300 ? " · showing the first 300" : ""}`})),
        r.has_market ? "" : h("div", {class: "info", textContent: "Yahoo Finance is blocking this website's server, so whole-market numbers like P/E and company value aren't available right now. Only the list of companies can be searched."}),
        dfTable({columns: r.columns.map(c => c === "Ticker" ? "Symbol" : r.units[c] ? `${c} (${r.units[c]})` : c), data: r.rows}, row => openCompany({ticker: row[0], name: row[1]})),
        h("p", {class: "muted", style: "margin-top:8px", textContent: "Prices and valuations are from Yahoo Finance, refreshed daily; the rest from the last time the app checked each company. Click a row to open the company."}));
    } catch (e) { out.replaceChildren(failed(e)); }
  };
  const name = h("input", {placeholder: "e.g. Cheap compounders"});
  const presets = h("div", {class: "pills"}, [...Object.entries(m.presets), ...Object.entries(saved)].map(([n, v]) => h("button", {textContent: n, onclick: e => { presets.querySelectorAll("button").forEach(b => b.classList.toggle("on", b === e.currentTarget)); q.value = v; remove.replaceChildren(saved[n] ? h("button", {class: "btn link", onclick: async () => { const {[n]: _, ...rest} = saved; await setDoc(doc(mine("settings"), "screens"), rest); route(); }}, icon("delete"), "Delete saved") : ""); run(); }})));
  page.replaceChildren(h("h1", {textContent: "Screener"}), h("p", {class: "muted", textContent: "Search every company listed on the NSE. Pick a ready-made search on the right, or write your own."}), h("label", {class: "row"}, sme, "Include SMEs"),
    h("div", {class: "split"}, h("div", {}, q, h("p", {class: "muted", style: "margin:8px 0"}, "Write what you want, like ", h("b", {textContent: "ROE > 15"}), ", and join ideas with ", h("b", {textContent: "AND"}), " / ", h("b", {textContent: "OR"}), ". Words work too, e.g. ", h("b", {textContent: "Sector = Banks"}), "."),
      h("div", {class: "row", style: "margin:12px 0"}, h("button", {class: "btn primary", onclick: run}, icon("search"), "Search"), popover("Save this search", "bookmark_add", pop => pop.replaceChildren(h("label", {}, "Name", name), h("button", {class: "btn", textContent: "Save", onclick: async () => { if (name.value.trim()) { await setDoc(doc(mine("settings"), "screens"), {...saved, [name.value.trim().slice(0, 60)]: q.value}); route(); } }}))), remove)),
      h("div", {}, h("b", {textContent: "Ready-made searches"}), presets, h("details", {class: "card"}, h("summary", {}, icon("list"), " Words you can search by"), h("p", {class: "muted", style: "margin-top:8px", textContent: "Marked ✓ are known for every listed company; the rest only for companies the app has looked into."}), h("ul", {class: "words"}, Object.entries(m.fields).map(([f, also]) => h("li", {}, h("b", {textContent: f}), (m.market.includes(f) ? " ✓" : "") + (m.units[f] ? ` (${m.units[f]})` : ""), h("br"), h("span", {class: "muted", textContent: `also: ${also.join(", ")}`}))))))), out);
  run();
}

// --- Compare ---------------------------------------------------------------------
async function comparePage() {
  const page = freshPage();
  const held = await holdings(), picked = [...new Map(held.map(r => [r.ticker, r])).values()].slice(0, 3), out = h("div"), chips = h("div", {class: "pills"});
  const list = h("ul", {class: "hits", hidden: true}), box = h("input", {type: "search", placeholder: "Pick two to four companies", ariaLabel: "Add a company"});
  const num = v => { const n = parseFloat(String(v ?? "").replace(/[₹,%\s]|Cr\.?/g, "")); return Number.isFinite(n) ? n : null; };
  const HIGH = ["Market Cap", "ROCE", "ROE", "Dividend Yield"], LOW = ["Stock P/E"];
  const draw = async () => {
    chips.replaceChildren(...picked.map(c => h("button", {class: "on", textContent: `${c.ticker} ✕`, onclick: () => { picked.splice(picked.indexOf(c), 1); draw(); }})));
    if (picked.length < 2) { out.replaceChildren(h("p", {class: "muted", textContent: "Pick at least two."})); return; }
    out.replaceChildren(h("p", {class: "muted", textContent: "Loading…"}));
    try {
      const infos = await Promise.all(picked.map(c => api("company?t=" + encodeURIComponent(c.ticker)))), latest = await latestAll();
      const names = ["Market Cap", "Current Price", "Stock P/E", "Book Value", "Dividend Yield", "ROCE", "ROE"];
      const chart = h("div", {style: "height:320px"});
      out.replaceChildren(h("div", {class: "row", style: "align-items:stretch"}, picked.map((c, i) => h("div", {class: "card grow"}, h("b", {textContent: c.ticker}), h("p", {class: "muted", style: "margin:0 0 6px", textContent: infos[i].name}), viewPill(latest[c.ticker]?.verdict)))),
        h("div", {class: "scroll"}, h("table", {}, h("thead", {}, h("tr", {}, h("th"), picked.map(c => h("th", {textContent: c.ticker})))), h("tbody", {}, names.map(n => { const vals = infos.map(x => num(x.metrics[n])), known = vals.filter(v => v !== null && (!LOW.includes(n) || v > 0)), best = HIGH.includes(n) ? Math.max(...known) : LOW.includes(n) ? Math.min(...known) : null;
          return h("tr", {}, h("td", {textContent: RATIO_WORDS[n] || n}), infos.map((x, i) => h("td", {class: "num" + (known.length > 1 && vals[i] === best ? " lead" : ""), textContent: x.metrics[n] ?? "—"}))); })))),
        h("p", {class: "muted", textContent: "Green marks the best on each line — the highest, except price / earnings, where cheaper is better."}),
        h("p", {}, h("b", {textContent: "Share price over the past year"}), h("span", {class: "muted", textContent: " · what ₹100 invested a year ago is worth now"})), chart);
      const lc = LightweightCharts.createChart(chart, {height: 320, layout: {background: {color: "transparent"}, textColor: "#444"}, grid: {vertLines: {color: "rgba(128,128,128,.12)"}, horzLines: {color: "rgba(128,128,128,.12)"}}, rightPriceScale: {borderVisible: false}, timeScale: {borderVisible: false}});
      const colours = ["#1F4E79", "#A8620A", "#1F7A4D", "#8C2F39"];
      await Promise.all(picked.map(async (c, i) => { const sid = c.screener_id ?? (await api("search?q=" + encodeURIComponent(c.ticker))).find(x => x.ticker === c.ticker)?.screener_id; if (!sid) return; const bars = await api(`prices?id=${sid}&days=366`); lc.addLineSeries({color: colours[i], lineWidth: 2, title: c.ticker}).setData(bars.map(b => ({time: b[0], value: b[1] / bars[0][1] * 100}))); }));
      lc.timeScale().fitContent();
    } catch (e) { out.replaceChildren(failed(e)); }
  };
  let timer; box.addEventListener("input", () => { clearTimeout(timer); const t = box.value.trim(); if (!t) { list.hidden = true; return; } timer = setTimeout(async () => { const rows = await api("search?q=" + encodeURIComponent(t)).catch(() => []); list.replaceChildren(...rows.slice(0, 8).map(c => h("li", {}, h("button", {textContent: `${c.name} · ${c.ticker}`, onclick: () => { if (picked.length < 4 && !picked.some(p => p.ticker === c.ticker)) picked.push(c); box.value = ""; list.hidden = true; draw(); }})))); list.hidden = !rows.length; }, 200); });
  page.replaceChildren(h("h1", {textContent: "Compare"}), h("p", {class: "muted", textContent: "Put two to four companies side by side."}), h("div", {class: "multi"}, chips, h("div", {class: "search"}, box, list)), out);
  draw();
}

// --- Archive -----------------------------------------------------------------------
async function archivePage() {
  const page = freshPage();
  const m = await meta(), latest = await latestAll(), held = await holdings();
  const known = [...new Set([...Object.keys(latest), ...held.map(r => r.ticker)])].sort();
  const t = h("input", {value: known.slice(0, 1).join(""), placeholder: "Company symbols separated by commas, e.g. TCS, INFY", ariaLabel: "Companies"}), q = h("input", {type: "search", placeholder: "What are you looking for? e.g. new factory, debt, dividend", ariaLabel: "Search words"}), out = h("div"), chosen = new Set(), limit = h("input", {type: "number", min: 1, max: 100, value: 40, ariaLabel: "Most results per company"});
  const go = h("button", {class: "btn primary", onclick: async () => {
    const list = [...new Set(t.value.split(",").map(x => x.trim().toUpperCase()).filter(Boolean))].slice(0, 5);
    if (!list.length || !q.value.trim()) return toast("Type a company symbol and what you're looking for.");
    go.disabled = true; let hits = [];
    try {
      for (const [i, sym] of list.entries()) { out.replaceChildren(h("p", {class: "muted", textContent: `Reading ${sym}'s reports (${i + 1} of ${list.length}), then searching them…`})); hits = hits.concat((await call("search-reports", {ticker: sym, q: q.value, categories: [...chosen], limit: Number(limit.value) || 40, data: dataBody()})).hits); }
      out.replaceChildren(hits.length ? h("p", {}, h("b", {textContent: `${hits.length} match(es)`})) : h("p", {class: "muted", textContent: "Nothing found. Try fewer or simpler words."}), ...hits.slice(0, 100).map(x => h("div", {class: "card"}, h("b", {textContent: `${x.ticker || ""} · ${m.words[x.category] || x.category || ""} · page ${x.page ?? "?"}`}), h("p", {textContent: "… " + String(x.snippet || "").replace(/<[^>]+>/g, "").replace(/\*\*/g, "") + " …"}), x.text ? h("details", {}, h("summary", {}, icon("article"), " Read the whole page"), h("p", {style: "white-space:pre-wrap", textContent: x.text})) : null)));
    } catch (e) { out.replaceChildren(failed(e)); } finally { go.disabled = false; }
  }}, icon("search"), "Search");
  page.replaceChildren(h("h1", {textContent: "Archive"}), h("p", {class: "muted", textContent: "Search inside companies' latest reports, up to five at a time. This website reads them fresh each time rather than keeping them, because full reports won't fit in free storage."}), h("p", {}, h("a", {href: "#/ask-ai"}, icon("auto_awesome"), " Want an answer instead? Ask AI")),
    h("div", {class: "row"}, h("div", {class: "grow"}, q), go), h("label", {style: "margin-top:12px"}, "Companies", t), known.length ? h("div", {class: "pills"}, known.slice(0, 20).map(s => h("button", {textContent: s, onclick: () => { const cur = t.value.split(",").map(x => x.trim()).filter(Boolean); if (!cur.includes(s)) t.value = [...cur, s].join(", "); }}))) : "", h("details", {class: "card"}, h("summary", {}, icon("filter_alt"), " Narrow it down"), h("p", {class: "muted", style: "margin-top:8px", textContent: "Kind of report (all if none chosen)"}), h("div", {class: "pills"}, m.categories.map(c => h("button", {textContent: m.words[c] || c, onclick: e => { chosen.has(c) ? chosen.delete(c) : chosen.add(c); e.currentTarget.classList.toggle("on"); }}))), h("label", {}, "Most results per company", limit)), out);
}

// --- History ---------------------------------------------------------------------
let histTicker = null, histMeasure = null;
async function historyPage(id) {
  const page = freshPage();
  page.replaceChildren(h("h1", {textContent: "History"}), h("p", {class: "muted", textContent: "Every check the app has made is kept, so you can look back and see how a company has changed."}));
  const rows = (await getDocs(mine("runs"))).docs.map(d => ({id: d.id, ...d.data()})).sort((a, b) => b.ts - a.ts);
  if (!rows.length) { page.append(h("div", {class: "info"}, icon("info"), " Nothing here yet. Look into a company and it will appear here.")); return; }
  const linked = rows.find(r => r.id === id), tickers = [...new Set(rows.map(r => r.ticker))].sort();
  if (linked) histTicker = linked.ticker; else if (!tickers.includes(histTicker)) histTicker = tickers[0];
  const company = h("select", {ariaLabel: "Company", onchange: () => { histTicker = company.value; id = null; fill(); }}, tickers.map(t => h("option", {selected: t === histTicker, textContent: t}))), checkedOn = h("select", {ariaLabel: "Checked on", onchange: () => show(checkedOn.value)}), caption = h("p", {class: "muted"}), body = h("div");
  const stamped = r => new Date(r.ts).toLocaleString("en-IN", {day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false});
  const fill = () => { const list = rows.filter(r => r.ticker === histTicker), pick = id && list.some(r => r.id === id) ? id : list[0].id; checkedOn.replaceChildren(...list.map(r => h("option", {value: r.id, selected: r.id === pick, textContent: stamped(r)}))); caption.textContent = `${list.length} past check(s) of ${histTicker} · kept until you delete them.`; show(pick); };
  const show = async runId => {
    body.replaceChildren(h("p", {class: "skel", textContent: "Loading…"}));
    const list = rows.filter(r => r.ticker === histTicker), at = list.findIndex(r => r.id === runId), run = list[at], older = list[at + 1], snap = await snapOf(runId);
    if (!snap) { body.replaceChildren(h("div", {class: "info", textContent: "That check is no longer saved."})); return; }
    // ponytail: reads the 30 newest checks each time; store numbers on the run itself if free-tier reads ever run short.
    const history = (await Promise.all(list.slice(0, 30).map(async r => [r.ts, await snapOf(r.id)]))).filter(x => x[1]).reverse();
    const points = {}; for (const [ts, s] of history) for (const [k, v] of Object.entries(s.metrics || {})) { const n = parseFloat(String(v).replace(/[₹,%\s]|Cr\.?/g, "")); if (Number.isFinite(n)) (points[k] ||= []).push({time: Math.floor(ts / 1000), value: n}); }
    const measures = Object.keys(points).filter(k => points[k].length > 1), parts = [];
    if (measures.length) {
      if (!measures.includes(histMeasure)) histMeasure = measures[0];
      const chart = h("div", {style: "height:260px"}), measure = h("select", {ariaLabel: "Measure", style: "width:280px", onchange: () => { histMeasure = measure.value; line.setData(points[histMeasure]); lc.timeScale().fitContent(); }}, measures.map(k => h("option", {selected: k === histMeasure, textContent: k})));
      parts.push(h("div", {class: "card"}, h("div", {class: "row"}, h("span", {class: "grow"}, h("b", {textContent: `${histTicker} over time`}), h("span", {class: "muted", textContent: ` · from ${history.length} checks`})), measure), chart));
      var lc, line; queueMicrotask(() => { lc = LightweightCharts.createChart(chart, {height: 260, layout: {background: {color: "transparent"}, textColor: "#444"}, grid: {vertLines: {color: "rgba(128,128,128,.12)"}, horzLines: {color: "rgba(128,128,128,.12)"}}, rightPriceScale: {borderVisible: false}, timeScale: {borderVisible: false, timeVisible: true}}); line = lc.addLineSeries({color: "#1F4E79", lineWidth: 2}); line.setData(points[histMeasure]); lc.timeScale().fitContent(); });
    }
    if (older) { const changed = h("div"), badge = h("span", {class: "badge", textContent: "Comparing…"}); parts.push(h("div", {class: "card"}, h("div", {class: "row"}, h("b", {class: "grow", textContent: `What changed since ${stamped(older)}`}), badge), changed));
      snapOf(older.id).then(o => call("diff", {new: snap, old: o})).then(({changes}) => { badge.className = "badge " + (changes.length ? "orange" : ""); badge.replaceChildren(icon("change_circle"), `${changes.length} change(s)`); changed.replaceChildren(changes.length ? dfTable({columns: ["What", "Item", "Was", "Now"], data: changes.map(c => [c.kind, c.item, c.from, c.to])}) : h("p", {class: "muted", textContent: "Nothing changed between these two checks."})); }).catch(e => changed.replaceChildren(failed(e))); }
    body.replaceChildren(...parts, h("div", {class: "row", style: "margin-top:16px"}, h("h3", {class: "grow", style: "margin:0", textContent: `As it was on ${stamped(run)}`}), viewBadge(snap.ai_verdict), h("button", {class: "btn link", onclick: async () => { if (!confirm("Delete this saved check?")) return; await deleteDoc(doc(mine("runs"), runId)); await deleteDoc(doc(mine("snaps"), runId)); const l = await getDoc(doc(mine("latest"), histTicker)); if (l.data()?.runId === runId) await deleteDoc(l.ref); if (location.hash.startsWith("#/history/")) location.hash = "#/history"; else route(); }}, icon("delete"), "Delete this check")), resultTabs(snap));
  };
  page.append(h("div", {class: "pair-pick"}, h("label", {}, "Company", company), h("label", {}, "Checked on", checkedOn)), caption, body); fill();
}

// --- Settings --------------------------------------------------------------------
async function settingsPage() {
  const page = freshPage();
  const [providers, m] = await Promise.all([api("providers"), meta()]);
  const num = (v, min, max, step = 1) => h("input", {type: "number", value: v, min, max, step});
  const check = v => h("input", {type: "checkbox", class: "switch", checked: Boolean(v)});
  const line = (c, text) => h("label", {class: "row"}, c, text);
  const provider = h("select", {ariaLabel: "AI provider"}, h("option", {value: "", textContent: "No AI"}), providers.map(p => h("option", {value: p.id, selected: p.id === prefs.provider, textContent: p.label})));
  const url = h("input", {value: prefs.base_url, placeholder: "e.g. https://my-ollama-server.example.com"}), key = h("input", {type: "password", autocomplete: "off"}), model = h("select", {ariaLabel: "AI model"}, prefs.model ? h("option", {value: prefs.model, textContent: prefs.model}) : null), note = h("p", {class: "muted"});
  const sync = () => { const p = providers.find(x => x.id === provider.value); url.value = p && p.id === prefs.provider ? prefs.base_url : ""; url.placeholder = p?.base_url && !p.base_url.includes("localhost") ? p.base_url : "e.g. https://my-ollama-server.example.com"; key.value = keyOf(provider.value); };
  provider.addEventListener("change", sync); sync();
  const b = prefs.budget, bEnabled = check(b.enabled), bMonthly = num(b.monthly, 0), bPerRun = num(b.per_run, 100), bWarn = num(b.warn, 1, 100);
  const used = await monthUsage();
  const tickers = h("input", {value: prefs.tickers}), timeframe = h("select", {ariaLabel: "How far back"}, m.timeframes.map(t => h("option", {value: t, selected: t === prefs.timeframe, textContent: t}))), chosen = new Set(prefs.categories || m.categories);
  const maxFile = num(prefs.max_file_mb, 1, 50), maxPages = num(prefs.max_pdf_pages, 1, 150), workers = num(prefs.doc_workers, 1, 8), newsItems = num(prefs.news_items, 1, 20);
  const hide = new Set(prefs.hidden), sections = new Set(prefs.sections), allSections = ["Summary", "Results", "Every number", "What changed", "Promises kept?", "Reports & warnings"];
  const choice = (options, value) => { const el = h("div", {class: "seg"}); el.value = value; options.forEach(([k, label]) => el.append(h("button", {class: k === value ? "on" : "", textContent: label, onclick: e => { el.value = k; el.querySelectorAll("button").forEach(b => b.classList.toggle("on", b === e.target)); }}))); return el; };
  const chart = choice([["Detailed", "Detailed"], ["Simple", "Simple"]], prefs.chart), navPick = choice([["top", "Across the top"], ["side", "Down the side"]], prefs.nav), homePick = h("select", {ariaLabel: "Open the app on"}, [...PAGES, ...MORE].map(([t]) => h("option", {selected: t === prefs.home, textContent: t}))), tech = check(prefs.tech), custom = check(prefs.custom_style);
  const accents = {"Research blue": "#1F4E79", Ink: "#1C1F26", Forest: "#1F7A4D", Burgundy: "#8C2F39", Bronze: "#A8620A", Indigo: "#3D3A8C"};
  const accent = h("select", {ariaLabel: "Accent colour"}, Object.entries(accents).map(([n, v]) => h("option", {value: v, selected: v === prefs.accent, textContent: n})));
  const font = num(prefs.font_scale, .8, 1.4, .1), density = h("select", {ariaLabel: "Spacing"}, Object.keys(DENSITY).map(d => h("option", {selected: d === prefs.density, textContent: d}))), tableH = num(prefs.table_height, 200, 1000), decimals = num(prefs.decimals, 0, 6), sparks = check(prefs.sparklines), context = check(prefs.context);
  const positions = check(prefs.positions), refresh = num(prefs.refresh_hours, 0, 720), maxStock = num(prefs.max_stock_pct, 1, 100), maxSector = num(prefs.max_sector_pct, 1, 100);
  const emailSaved = (await getDoc(doc(mine("settings"), "email"))).exists(), emailClosed = check(emailSaved);
  const emailOn = check(prefs.email), emailUrl = h("input", {value: prefs.email_url, placeholder: "https://script.google.com/…"}), emailToken = h("input", {type: "password", autocomplete: "off", value: secret("emailtoken")});
  const pills = (items, set, label = x => x) => h("div", {class: "pills"}, items.map(x => h("button", {class: set.has(x) ? "on" : "", textContent: label(x), onclick: e => { set.has(x) ? set.delete(x) : set.add(x); e.currentTarget.classList.toggle("on"); }})));
  const inclKeys = check(false), replace = check(false), backupFile = h("input", {type: "file", accept: ".json", ariaLabel: "Backup file"});
  const COLLECTIONS = ["holdings", "accounts", "trades", "alerts", "runs", "snaps", "latest", "events", "claims", "chats", "settings", "usage"];
  const save = async () => {
    try { if (key.value.trim()) localStorage.setItem("aikey:" + provider.value, key.value.trim()); else localStorage.removeItem("aikey:" + provider.value); if (emailToken.value.trim()) localStorage.setItem("emailtoken", emailToken.value.trim()); else localStorage.removeItem("emailtoken"); } catch { toast("This browser wouldn't save your key."); }
    const n = (i, fallback) => { const v = Number(i.value); return Number.isFinite(v) && i.value !== "" ? v : fallback; };
    prefs = {...prefs, provider: provider.value, base_url: url.value.trim(), model: model.value || "", categories: [...chosen], timeframe: timeframe.value, tickers: tickers.value, accent: accent.value,
      budget: {enabled: bEnabled.checked, monthly: n(bMonthly, 0), per_run: n(bPerRun, 50000), warn: n(bWarn, 80)}, max_file_mb: n(maxFile, 25), max_pdf_pages: n(maxPages, 80), doc_workers: n(workers, 4), news_items: n(newsItems, 8), hidden: [...hide], sections: [...sections], chart: chart.value, nav: navPick.value, home: homePick.value, tech: tech.checked, custom_style: custom.checked, font_scale: n(font, 1), density: density.value, table_height: n(tableH, 380), decimals: n(decimals, 2), sparklines: sparks.checked, context: context.checked, positions: positions.checked, refresh_hours: n(refresh, 24), max_stock_pct: n(maxStock, 20), max_sector_pct: n(maxSector, 35), email: emailOn.checked, email_url: emailUrl.value.trim()};
    await setDoc(doc(mine("settings"), "main"), prefs);
    const emailDoc = doc(mine("settings"), "email");
    if (emailClosed.checked && prefs.email_url && emailToken.value.trim()) await setDoc(emailDoc, {url: prefs.email_url, token: emailToken.value.trim()});
    else if (!emailClosed.checked) await deleteDoc(emailDoc);
    applyLook(); toast("Saved."); route();
  };
  const saveBtn = () => h("button", {class: "btn primary", style: "margin-top:12px", onclick: save}, icon("save"), "Save");
  const pagesPick = pills([...PAGES, ...MORE].map(p => p[0]).filter(t => !["Home", "Settings"].includes(t)), hide), sectionsPick = pills(allSections, sections);
  const toTab = (...parts) => el => { el.append(...parts); };
  page.replaceChildren(h("h1", {textContent: "Settings"}), h("p", {class: "muted", textContent: "Everything is saved to your account. API keys stay in this browser. Press the Save button in each section to keep a change."}), tabs([
    ["Layout & look", "palette", toTab(h("h4", {style: "margin-top:12px", textContent: "Layout"}), h("div", {class: "row", style: "align-items:flex-start"}, h("div", {class: "grow"}, h("p", {class: "field", textContent: "Menu"}), navPick), h("label", {class: "grow"}, "Open the app on", homePick)),
      h("p", {class: "field", textContent: "Hide these pages from the menu"}), pagesPick, h("p", {class: "field", textContent: "Sections shown on a company's page"}), sectionsPick, h("p", {class: "field", textContent: "Price chart"}), chart, line(tech, "Show more details"),
      h("h4", {style: "margin-top:16px", textContent: "Look"}), line(custom, "Use my colour and text size below"), h("div", {class: "row"}, h("label", {class: "grow"}, "Colour", accent), h("label", {class: "grow"}, "Text size", font), h("label", {class: "grow"}, "Spacing", density)),
      h("details", {class: "card"}, h("summary", {}, icon("table_chart"), " Tables and charts"), h("div", {class: "row", style: "margin-top:8px"}, h("label", {class: "grow"}, "Table height (pixels)", tableH), h("label", {class: "grow"}, "Decimal places", decimals)), line(sparks, "Small trend charts on numbers"), line(context, "Show the sentence each number came from")), saveBtn())],
    ["AI", "auto_awesome", toTab(h("p", {class: "muted", textContent: "Use a cloud provider with your own key, or your own Ollama or self-hosted server, which must be reachable from the internet. This website can't reach a server on your own computer or home network."}),
      h("label", {}, "Provider", provider), h("label", {}, "Server address (only needed for your own server)", url), h("label", {}, "API key (kept only in this browser, and sent only when the AI is used)", key),
      h("button", {class: "btn", onclick: async () => { note.textContent = "Checking…"; try { const {models} = await call("models", {ai: {provider: provider.value, base_url: url.value.trim(), api_key: key.value.trim()}}); const ids = Object.entries(models); model.replaceChildren(...ids.map(([id, label]) => h("option", {value: id, selected: id === prefs.model, textContent: label || id}))); note.textContent = ids.length ? `Connected. ${ids.length} model(s) found.` : "Couldn't find any models. Check the address and key."; } catch (e) { note.textContent = e.message; } }}, icon("wifi_tethering"), "Check it works"), note, h("label", {}, "Model", model), saveBtn())],
    ["Alerts", "notifications", toTab(h("h4", {style: "margin-top:12px", textContent: "Email alerts"}), h("p", {class: "muted", textContent: "Uses your own Google Apps Script, the same setup as the desktop app. The password stays in this browser unless you choose otherwise below."}), line(emailOn, "Email me when something happens while this site is open"), line(emailClosed, "Also email me when a price alert is reached while the site is closed. This saves the web address and password in your account, where this website's owner could see them."), h("label", {}, "Web address from the setup", emailUrl), h("label", {}, "Password from the setup", emailToken), h("button", {class: "btn", onclick: async () => { try { await call("notify", {url: emailUrl.value.trim(), token: emailToken.value.trim(), subject: "Indian Stock Data Hub test", text: "Your email alerts work."}); toast("Sent. Check your inbox."); } catch (e) { toast(e.message); } }}, icon("send"), "Send a test email"),
      h("h4", {style: "margin-top:16px", textContent: "Portfolio"}), line(positions, "Keep track of how many shares I own and what I paid"), h("div", {class: "row"}, h("label", {class: "grow"}, "Check my stocks again every (hours, 0 = never; only while this site is open)", refresh), h("label", {class: "grow"}, "Warn me if one stock is over (%)", maxStock), h("label", {class: "grow"}, "Warn me if one industry is over (%)", maxSector)), saveBtn())],
    ["Spending", "payments", toTab(h("p", {class: "muted", textContent: `Used this month: about ${used.toLocaleString("en-IN")} tokens. Providers charge by tokens, so limits are set in tokens rather than money.`}), line(bEnabled, "Use these limits"), h("div", {class: "row"}, h("label", {class: "grow"}, "Monthly limit (tokens, 0 = none)", bMonthly), h("label", {class: "grow"}, "Limit per analysis (tokens)", bPerRun), h("label", {class: "grow"}, "Warn me at (%)", bWarn)), saveBtn())],
    ["Advanced", "tune", toTab(h("p", {class: "muted", textContent: "You shouldn't need to change anything here — the defaults work well."}), tabs([
      ["Downloading", "travel_explore", toTab(h("label", {style: "margin-top:12px"}, "Companies filled in on Research", tickers), h("p", {class: "field", textContent: "Reports to read"}), pills(m.categories, chosen, c => m.words[c] || c), h("label", {style: "margin-top:12px"}, "How far back", timeframe), h("div", {class: "row"}, h("label", {class: "grow"}, "Largest report to download (MB)", maxFile), h("label", {class: "grow"}, "Pages to read per report (over 80 may time out)", maxPages), h("label", {class: "grow"}, "Parallel downloads", workers), h("label", {class: "grow"}, "News items", newsItems)), saveBtn())],
      ["Storage & backups", "database", toTab(h("h4", {style: "margin-top:12px", textContent: "Backups"}), line(inclKeys, "Include API keys (anyone with the file can use them)"), h("button", {class: "btn", onclick: async () => { const out = {version: 1}; for (const c of COLLECTIONS) out[c] = Object.fromEntries((await getDocs(mine(c))).docs.filter(d => inclKeys.checked || !(c === "settings" && d.id === "email")).map(d => [d.id, d.data()])); if (inclKeys.checked) out.keys = {...Object.fromEntries(providers.map(p => [p.id, keyOf(p.id)])), emailtoken: secret("emailtoken")}; download(JSON.stringify(out), `stock_hub_backup_${stamp()}.json`, "application/json"); }}, icon("download"), "Download a backup"), h("p", {class: "field", style: "margin-top:12px", textContent: "Restore a backup"}), backupFile, line(replace, "Replace my current data with this backup"),
        h("button", {class: "btn", onclick: async e => { const f = backupFile.files[0]; if (!f) return toast("Choose a backup file first."); let data; try { data = JSON.parse(await f.text()); } catch { return toast("That isn't a backup file."); } if (data.version !== 1) return toast("That isn't a backup file."); if (replace.checked && !confirm("This deletes your current data and replaces it with the backup. Continue?")) return; const btn = e.currentTarget; btn.disabled = true; try { for (const c of COLLECTIONS) { if (replace.checked) for (const d of (await getDocs(mine(c))).docs) await deleteDoc(d.ref); for (const [id, v] of Object.entries(data[c] || {})) if (v && typeof v === "object" && !id.includes("/")) await setDoc(doc(mine(c), id), v); } for (const [p, v] of Object.entries(data.keys || {})) if (v) localStorage.setItem(p === "emailtoken" ? p : "aikey:" + p, v); await loadPrefs(); toast("Restored."); route(); } catch (err) { toast(err.message); btn.disabled = false; } }}, icon("upload"), "Restore"),
        h("h4", {style: "margin-top:16px", textContent: "Your account"}), h("p", {class: "muted", textContent: `Signed in as ${user.email}`}), h("div", {class: "row"}, h("button", {class: "btn", onclick: () => signOut(auth)}, icon("logout"), "Sign out"),
          h("button", {class: "btn", onclick: async () => { if (!confirm("Permanently delete your portfolio, alerts, saved checks and settings? This can't be undone.")) return; for (const c of COLLECTIONS) for (const d of (await getDocs(mine(c))).docs) await deleteDoc(d.ref); toast("Your data has been deleted."); await loadPrefs(); route(); }}, icon("delete_forever"), "Delete all my data")))],
    ]))],
  ]));
}

// --- navigation ---------------------------------------------------------------
const PAGES = [["Home", "home"], ["Stock", "candlestick_chart"], ["Portfolio", "account_balance_wallet"], ["Research", "query_stats"], ["Ask AI", "auto_awesome"]];
const MORE = [["Screener", "filter_alt"], ["Compare", "compare_arrows"], ["Archive", "search"], ["History", "history"], ["Settings", "settings"]];
const slug = t => t.toLowerCase().replace(" ", "-");
function drawNav(current) {
  const nav = document.getElementById("nav");
  const visible = ([t]) => !prefs.hidden.includes(t);
  const link = ([t, i]) => h("a", {href: "#/" + slug(t), class: slug(t) === current ? "on" : ""}, icon(i), t);
  const pop = h("div", {class: "pop", hidden: true}, MORE.filter(visible).map(link),
    h("button", {class: "btn link", onclick: () => signOut(auth)}, icon("logout"), "Sign out"));
  nav.replaceChildren(...PAGES.filter(visible).map(link), h("div", {class: "more"}, h("button", {class: "menu-btn", ariaExpanded: "false", onclick: e => { pop.hidden = !pop.hidden; e.currentTarget.ariaExpanded = String(!pop.hidden); }}, "More", icon("expand_more")), pop));
  nav.hidden = false;
}
let routeId = 0;
async function route() {
  const [, name = slug(prefs.home || "Home"), arg] = location.hash.split("/"), id = ++routeId;
  document.body.classList.toggle("side", name === "ask-ai"); document.body.classList.toggle("sidenav", prefs.nav === "side");
  drawNav(name); document.body.classList.add("loading");
  const done = () => { if (id === routeId) document.body.classList.remove("loading"); };
  const open = () => name === "home" ? home() : name === "stock" ? stock(arg ? decodeURIComponent(arg).toUpperCase() : null) : name === "research" ? research() : name === "portfolio" ? portfolioPage() : name === "ask-ai" ? askPage() : name === "screener" ? screenerPage() : name === "compare" ? comparePage() : name === "archive" ? archivePage() : name === "history" ? historyPage(arg) : name === "settings" ? settingsPage() : home();
  for (let tries = 0; ; tries++) {
    try { await open(); done(); return; } catch (e) {
      if (id !== routeId) return;  // they've moved on to another page
      if (tries < 1) { await wait(800); if (id !== routeId) return; continue; }
      shell.replaceChildren(h("div", {class: "card problem"}, icon("cloud_off"), h("div", {}, h("b", {textContent: "This page didn't load"}), h("p", {class: "muted", textContent: plainError(e)}), h("button", {class: "btn", onclick: () => route()}, icon("refresh"), "Try again"))));
      done(); return;
    }
  }
}
function signInPage() {
  document.getElementById("nav").hidden = true; document.body.classList.remove("side", "sidenav");
  const note = h("p", {class: "err"});
  shell.replaceChildren(h("div", {class: "signin"}, h("h1", {textContent: "Indian Stock Data Hub"}),
    h("p", {class: "muted", textContent: "Sign in to see your portfolio, alerts and saved analyses."}),
    h("button", {class: "btn primary", textContent: "Continue with Google", onclick: () => signInWithPopup(auth, new GoogleAuthProvider()).catch(e => { note.textContent = e.code === "auth/popup-closed-by-user" ? "" : "Sign-in didn't work. Please try again."; })}), note));
}
// The desktop app's background refresher, while this site is open: one stale stock at a time.
let refreshing = false;
async function autoRefresh() {
  if (refreshing || !user || !prefs.refresh_hours) return;
  refreshing = true;
  try {
    const held = [...new Set((await holdings()).map(r => r.ticker))], latest = await latestAll(), limit = prefs.refresh_hours * 36e5;
    for (const t of held.filter(x => !latest[x] || Date.now() - latest[x].ts > limit)) {
      if (!user) break;
      await lookInto(t, "Portfolio refresh").catch(() => {});
      await new Promise(r => setTimeout(r, 2000));
    }
  } finally { refreshing = false; }
}
window.addEventListener("hashchange", () => { if (user) route(); });
window.addEventListener("unhandledrejection", ev => toast(plainError(ev.reason), true));
window.addEventListener("offline", () => toast("You're offline. Anything you save goes through when you reconnect.", true));
window.addEventListener("online", () => { toast("Back online."); if (user && shell.querySelector(".problem")) route(); });
onAuthStateChanged(auth, async u => { user = u; if (!u) return signInPage(); await loadPrefs().catch(() => wait(800).then(loadPrefs)).catch(() => {}); route(); autoRefresh(); });
