"""A company's own page: price chart, the latest analysis, and every analysis before it.
Works for any listed company, held or not."""

import json
from datetime import datetime

import pandas as pd
import streamlit as st

import alerts
import archive
import core
import exports
import portfolio as pf
import ui

settings = st.session_state.settings
ss = st.session_state


@st.cache_data(max_entries=20, show_spinner="Preparing your download…")
def _export(run_id: int, ext: str) -> bytes:
    """Built once per saved analysis, not on every rerun."""
    result = core.restore_snapshot(archive.get_run(run_id)["snapshot"])
    return (exports.build_excel_workbook if ext == "xlsx" else exports.build_word_document)([result])


held = pf.tracked()
company = ss.get("company")
if ss.get("stock") in held and (not company or company["ticker"] != ss["stock"]):
    company = {"ticker": ss["stock"], **held[ss["stock"]]}  # a link that names only the symbol

left, right = st.columns([3, 1.2], vertical_alignment="top")
with left:
    ui.company_search("company_search")
if held:
    def _pick_held():
        t = ss.company_quick
        if t:
            ss.company, ss.stock = {"ticker": t, **held[t]}, t

    # The picker always shows the company on screen (blank for one you don't own).
    shown = company["ticker"] if company and company["ticker"] in held else None
    if ss.get("company_quick") != shown:
        ss.company_quick = shown
    right.selectbox("Your stocks", sorted(held, key=lambda t: held[t]["name"]), key="company_quick",
                    label_visibility="collapsed", on_change=_pick_held, index=None,
                    format_func=lambda t: held[t]["name"], placeholder="…or pick one of your stocks")
if not company:
    st.info("Type a company's name above to see everything about it.", icon=":material/search:")
    st.stop()
ss.company, ss.stock = company, company["ticker"]
ticker, sid = company["ticker"], company.get("screener_id")

# --- Header --------------------------------------------------------------------
quote = pf.quotes(((ticker, sid),)).get(ticker)
runs = archive.list_runs(ticker, limit=200)
latest_run = archive.get_run(runs[0]["id"]) if runs else None
latest = core.restore_snapshot(latest_run["snapshot"]) if runs else None

with st.container(horizontal=True, vertical_alignment="center"):
    st.markdown(f"# {company.get('name') or ticker}", width="content")
    if latest and latest.get("ai_verdict"):
        color, icon = ui.VERDICT_BADGE.get(latest["ai_verdict"], ("gray", None))
        st.badge(f"AI view: {ui.VIEW_WORDS.get(latest['ai_verdict'], latest['ai_verdict'])}", icon=icon, color=color)
st.caption(ticker)

h1, h2, h3 = st.columns([1, 1, 2], vertical_alignment="center")
if quote:
    h1.metric("Share price", f"₹{quote['price']:,.2f}", f"{quote['change']:+,.2f} ({quote['change_pct']:+.2f}%) today")
h2.metric("Times checked", len(runs))
with h3.container(horizontal=True, horizontal_alignment="right"):
    if st.button("Look into this company", icon=":material/travel_explore:", type="primary",
                 help="Reads the company's latest numbers, reports and news" + (" again" if runs else "")
                      + ". Takes about a minute."):
        with st.spinner(f"Looking into {company.get('name') or ticker} — this takes about a minute…"):
            r = pf.analyze(ticker, settings, label="Checked by you")
        st.toast("Done." if r["ok"] else "That didn't work — try again in a minute.",
                 icon=":material/check_circle:" if r["ok"] else ":material/error:")
        st.rerun()
    if ticker not in held:
        with st.popover("Add to my portfolio", icon=":material/add:"):
            accounts = pf.accounts()
            if not accounts:
                pf.add_account("My stocks")
                accounts = pf.accounts()
            names = {a["id"]: a["name"] for a in accounts}
            into = st.selectbox("Add it to", list(names), format_func=names.get) if len(names) > 1 else accounts[0]["id"]
            qty = st.number_input("How many shares do you own?", min_value=0.0, step=1.0, value=None,
                                  placeholder="Leave empty to just watch it")
            avg = st.number_input("Average price you paid (₹)", min_value=0.0, step=1.0, value=None,
                                  placeholder="Optional")
            if st.button("Add", type="primary", width="stretch"):
                pf.save_holding(into, ticker, company.get("name") or ticker, sid, qty, avg)
                st.toast(f"Added {ticker}. The app will keep an eye on it for you.", icon=":material/check_circle:")
                st.rerun()
    if latest:
        stamp = datetime.now().strftime("%Y%m%d")
        with st.popover("Download", icon=":material/download:"):
            for label, ext, icon in (("Spreadsheet (Excel)", "xlsx", ":material/table_view:"),
                                     ("Report (Word)", "docx", ":material/description:")):
                try:
                    st.download_button(label, _export(runs[0]["id"], ext), file_name=f"{ticker}_{stamp}.{ext}",
                                       icon=icon, width="stretch")
                except Exception:
                    st.caption(f"The {label.split(' (')[0].lower()} couldn't be made this time.")

# --- Upcoming events and price alerts ---------------------------------------------
events = pf.calendar(ticker, company.get("name") or ticker, pf.day_slot())
with st.container(horizontal=True, vertical_alignment="center", gap="small"):
    st.markdown(":material/event: **Coming up**", width="content")
    if not events:
        st.caption("Nothing announced yet.")
    for e in events[:4]:
        days = (e["date"] - datetime.now(pf.IST).date()).days
        with st.container(border=True, width="content"):
            st.markdown(f"**{e['date']:%d %b}** · {e['event']}  \n:gray[{'today' if days == 0 else f'in {days} d'}"
                        + (f" · {e['detail']}" if e["detail"] else "") + "]")
    st.space("stretch")
    mine = alerts.rules(ticker)
    with st.popover(f"Price alerts · {len(mine)}" if mine else "Tell me when the price moves",
                    icon=":material/add_alert:"):
        for r in mine:
            c1, c2 = st.columns([4, 1], vertical_alignment="center")
            c1.markdown(f"{r['op'].title()} ₹{r['level']:,.2f}" + (" · :orange[reached]" if r["fired"] else ""))
            if c2.button("", icon=":material/delete:", key=f"del_rule_{r['id']}", type="tertiary"):
                alerts.delete_rule(r["id"])
                st.rerun()
        op = st.segmented_control("Notify me when the price goes", ["above", "below"], default="above", required=True)
        level = st.number_input("Level (₹)", min_value=0.0, value=float(quote["price"]) if quote else 0.0, step=1.0)
        if st.button("Add alert", type="primary", width="stretch"):
            alerts.add_rule(ticker, op, level)
            st.toast(f"You'll be notified when {ticker} goes {op} ₹{level:,.2f}.", icon=":material/add_alert:")
            st.rerun()

if not latest:
    msg = ("Looking into this company now…" if pf.STATUS["running"] == ticker
           else "Waiting its turn — the app looks into one company at a time." if ticker in held
           else "Press **Look into this company** above to get a summary, the numbers and any warning signs.")
    st.info(msg, icon=":material/hourglass_top:")

# --- TradingView-style chart ---------------------------------------------------
period = st.segmented_control("Period", list(pf.PERIOD_DAYS), default="1Y", required=True,
                              label_visibility="collapsed", key="stock_period")
bars = pf.history(ticker, sid, period)
if bars is None or bars.empty:
    st.caption("No price history available for this company.")
else:
    simple = settings["ui"]["chart_style"] == "Simple"
    t = bars.index.strftime("%Y-%m-%d")
    series = {
        "candles": [{"time": d, "open": o, "high": h, "low": l, "close": c}
                    for d, o, h, l, c in zip(t, bars["Open"], bars["High"], bars["Low"], bars["Close"])],
        "volume": [{"time": d, "value": float(v) if pd.notna(v) else 0,
                    "color": "rgba(38,166,154,.4)" if c >= o else "rgba(239,83,80,.4)"}
                   for d, v, o, c in zip(t, bars.get("Volume", pd.Series(0, bars.index)), bars["Open"], bars["Close"])],
        "sma50": [{"time": d, "value": v} for d, v in zip(t, bars["SMA50"]) if pd.notna(v)],
        "sma200": [{"time": d, "value": v} for d, v in zip(t, bars["SMA200"]) if pd.notna(v)],
    }
    st.iframe(f"""
<div id="c" style="height:440px"></div>
<div id="k" style="font:12px sans-serif;color:#888;margin-top:4px">
  <span style="color:#2962FF">━ average of the last 50 days</span> &nbsp;
  <span style="color:#FF9800">━ average of the last 200 days</span></div>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>
<script>
const D = {json.dumps(series)}, el = document.getElementById('c'), SIMPLE = {json.dumps(simple)};
const dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
const chart = LightweightCharts.createChart(el, {{
  height: 440, layout: {{background: {{color: 'transparent'}}, textColor: dark ? '#ccc' : '#444'}},
  grid: {{vertLines: {{color: 'rgba(128,128,128,.12)'}}, horzLines: {{color: 'rgba(128,128,128,.12)'}}}},
  rightPriceScale: {{borderVisible: false}}, timeScale: {{borderVisible: false}},
  crosshair: {{mode: LightweightCharts.CrosshairMode.Normal}}}});
if (SIMPLE) {{
  const up = D.candles.length && D.candles.at(-1).close >= D.candles[0].close, c = up ? '#15803D' : '#DC2626';
  chart.addAreaSeries({{lineColor: c, topColor: c + '33', bottomColor: c + '00', lineWidth: 2}})
    .setData(D.candles.map(b => ({{time: b.time, value: b.close}})));
  document.getElementById('k').style.display = 'none';
}} else {{
chart.addCandlestickSeries({{upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
  wickUpColor: '#26a69a', wickDownColor: '#ef5350'}}).setData(D.candles);
const vol = chart.addHistogramSeries({{priceFormat: {{type: 'volume'}}, priceScaleId: ''}});
vol.priceScale().applyOptions({{scaleMargins: {{top: 0.8, bottom: 0}}}});
vol.setData(D.volume);
chart.addLineSeries({{color: '#2962FF', lineWidth: 1, priceLineVisible: false}}).setData(D.sma50);
chart.addLineSeries({{color: '#FF9800', lineWidth: 1, priceLineVisible: false}}).setData(D.sma200);
}}
chart.timeScale().fitContent();
new ResizeObserver(() => chart.applyOptions({{width: el.clientWidth}})).observe(el);
</script>""", height=470)

if not latest:
    st.stop()

# --- The analysis --------------------------------------------------------------
# Where today's P/E sits in the company's own history. Shown only when
# there is enough of it, and silent for a loss-making company where a
# P/E means nothing.
band = pf.pe_position(pf.pe_history(sid, pf.day_slot()))
if band:
    cheap = band["percentile"] <= 35
    dear = band["percentile"] >= 75
    with st.container(border=True):
        b1, b2 = st.columns([1, 2.2], vertical_alignment="center")
        b1.metric("Price-to-earnings now", f"{band['now']:,.1f}",
                  f"{band['now'] - band['median']:+.1f} vs its usual", border=False,
                  delta_color="inverse", help="How many years of today's profit the share price equals. "
                                              "Lower means cheaper.")
        with b2:
            st.markdown(
                f"Cheaper than it's been **{100 - round(band['percentile'])}%** of the time in the last {band['years']:g} years "
                + (":green[· cheaper than usual]" if cheap else
                   ":red[· dearer than usual]" if dear else ":gray[· around its usual level]"))
            st.progress(min(max(band["percentile"] / 100, 0.0), 1.0))
            if ui.tech(settings):
                st.caption(f"{ui.ordinal(band['percentile'])} percentile of its own P/E range.")
            st.caption(f"Lowest {band['low']:,.1f} · usual {band['median']:,.1f} · "
                       f"highest {band['high']:,.1f}. Compared only with its own past, not with other "
                       f"companies — and a change can be deserved.")

# The same view as the Research page. A saved analysis keeps its figures in
# the archive, not in the snapshot, so they are looked up by filing.
latest["figures"] = [f for d in (latest.get("downloaded") or {}).values() for doc in d
                     if doc.get("path") for f in archive.cached_figures(doc["path"], "path") or []]
if len(runs) > 1:
    previous = archive.get_run(runs[1]["id"])
    latest["previous_at"] = previous["ts"]
    latest["changes"] = archive.diff_snapshots(previous["snapshot"], latest_run["snapshot"])
st.caption(f"Last checked {datetime.fromtimestamp(runs[0]['ts']).strftime('%d %b %Y, %H:%M')}")
tab_hist = ui.result_tabs([latest], settings, settings, runs[0]["ts"],
                          extra={"Past checks": ":material/history:"}, own_page=True)["Past checks"]

with tab_hist:
    verdicts = pf._q("SELECT id, ts, label, json_extract(snapshot, '$.ai_verdict') AS verdict, "
                     "json_extract(snapshot, '$.ai_summary') AS summary FROM runs WHERE ticker=? "
                     "ORDER BY ts DESC", (ticker,))
    hist = pd.DataFrame(verdicts)
    hist["When"] = pd.to_datetime(hist["ts"], unit="s")
    st.dataframe(hist[["When", "verdict", "summary", "label"]].rename(
        columns={"verdict": "AI view", "summary": "AI summary", "label": "Why it ran"}), hide_index=True,
        column_config={"When": st.column_config.DatetimeColumn(format="D MMM YYYY, HH:mm")})
    if len(runs) > 1:
        labels = {r["id"]: datetime.fromtimestamp(r["ts"]).strftime("%d %b %Y, %H:%M") for r in runs}
        c1, c2 = st.columns(2)
        new_id = c1.selectbox("Compare the check on", list(labels), format_func=labels.get)
        old_id = c2.selectbox("with the one on", list(labels), index=1, format_func=labels.get)
        new, old = archive.get_run(new_id), archive.get_run(old_id)
        ui.changes_table(archive.diff_snapshots(old["snapshot"], new["snapshot"]), old["ts"])
