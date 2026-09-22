"""A held stock's own page: live chart, latest analysis, and every analysis before it."""

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


@st.cache_data(max_entries=20, show_spinner="Building export…")
def _export(run_id: int, ext: str) -> bytes:
    """Built once per saved analysis, not on every rerun."""
    result = core.restore_snapshot(archive.get_run(run_id)["snapshot"])
    return (exports.build_excel_workbook if ext == "xlsx" else exports.build_word_document)([result])


held = pf.tracked()
if not held:
    st.info("Add a stock on the **Portfolio** page and its page appears here.", icon=":material/info:")
    st.stop()

tickers = sorted(held)
default = st.session_state.get("stock")
ticker = st.selectbox("Stock", tickers, index=tickers.index(default) if default in tickers else 0,
                      format_func=lambda t: f"{t} · {held[t]['name']}", label_visibility="collapsed", key="stock_pick")
st.session_state.stock = ticker
sid = held[ticker]["screener_id"]

# --- Header --------------------------------------------------------------------
quote = pf.quotes(((ticker, sid),)).get(ticker)
runs = archive.list_runs(ticker, limit=200)
latest = core.restore_snapshot(archive.get_run(runs[0]["id"])["snapshot"]) if runs else None

with st.container(horizontal=True, vertical_alignment="center"):
    st.markdown(f"# {ticker}", width="content")
    if latest and latest.get("ai_verdict"):
        color, icon = ui.VERDICT_BADGE.get(latest["ai_verdict"], ("gray", None))
        st.badge(f"AI verdict · {latest['ai_verdict']}", icon=icon, color=color)
st.caption(held[ticker]["name"])

h1, h2, h3 = st.columns([1, 1, 2], vertical_alignment="center")
if quote:
    h1.metric("Price", f"₹{quote['price']:,.2f}", f"{quote['change']:+,.2f} ({quote['change_pct']:+.2f}%)")
h2.metric("Analyses saved", len(runs))
with h3.container(horizontal=True, horizontal_alignment="right"):
    if st.button("Re-analyse now", icon=":material/refresh:", type="primary"):
        with st.spinner(f"Analysing {ticker} — waits for any background run to finish first…"):
            r = pf.analyze(ticker, settings, label="Manual refresh")
        st.toast("Done." if r["ok"] else f"Failed: {r.get('error')}",
                 icon=":material/check_circle:" if r["ok"] else ":material/error:")
        st.rerun()
    if latest:
        stamp = datetime.now().strftime("%Y%m%d")
        for label, ext, icon in (("Excel", "xlsx", ":material/table_view:"),
                                 ("Word", "docx", ":material/description:")):
            try:
                st.download_button(label, _export(runs[0]["id"], ext), file_name=f"{ticker}_{stamp}.{ext}",
                                   icon=icon)
            except Exception as exc:
                st.caption(f"{label} export failed: {exc}")

# --- Upcoming events and price alerts ---------------------------------------------
events = pf.calendar(ticker, held[ticker]["name"], pf.day_slot())
with st.container(horizontal=True, vertical_alignment="center", gap="small"):
    st.markdown(":material/event: **Upcoming**", width="content")
    if not events:
        st.caption("No dated events announced yet.")
    for e in events[:4]:
        days = (e["date"] - datetime.now(pf.IST).date()).days
        with st.container(border=True, width="content"):
            st.markdown(f"**{e['date']:%d %b}** · {e['event']}  \n:gray[{'today' if days == 0 else f'in {days} d'}"
                        + (f" · {e['detail']}" if e["detail"] else "") + "]")
    st.space("stretch")
    mine = alerts.rules(ticker)
    with st.popover(f"Price alerts · {len(mine)}" if mine else "Price alert", icon=":material/add_alert:"):
        for r in mine:
            c1, c2 = st.columns([4, 1], vertical_alignment="center")
            c1.markdown(f"{r['op'].title()} ₹{r['level']:,.2f}" + (" · :orange[triggered]" if r["fired"] else ""))
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
    msg = (f"Analysing now in the background…" if pf.STATUS["running"] == ticker
           else "Queued for its first full analysis — it runs in the background, one stock at a time.")
    st.info(msg, icon=":material/hourglass_top:")

# --- TradingView-style chart ---------------------------------------------------
period = st.segmented_control("Period", list(pf.PERIOD_DAYS), default="1Y", required=True,
                              label_visibility="collapsed", key="stock_period")
bars = pf.history(ticker, sid, period)
if bars is None or bars.empty:
    st.caption("No price history available for this stock.")
else:
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
<div style="font:12px sans-serif;color:#888;margin-top:4px">
  <span style="color:#2962FF">━ 50-day</span> &nbsp; <span style="color:#FF9800">━ 200-day</span></div>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>
<script>
const D = {json.dumps(series)}, el = document.getElementById('c');
const dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
const chart = LightweightCharts.createChart(el, {{
  height: 440, layout: {{background: {{color: 'transparent'}}, textColor: dark ? '#ccc' : '#444'}},
  grid: {{vertLines: {{color: 'rgba(128,128,128,.12)'}}, horzLines: {{color: 'rgba(128,128,128,.12)'}}}},
  rightPriceScale: {{borderVisible: false}}, timeScale: {{borderVisible: false}},
  crosshair: {{mode: LightweightCharts.CrosshairMode.Normal}}}});
chart.addCandlestickSeries({{upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
  wickUpColor: '#26a69a', wickDownColor: '#ef5350'}}).setData(D.candles);
const vol = chart.addHistogramSeries({{priceFormat: {{type: 'volume'}}, priceScaleId: ''}});
vol.priceScale().applyOptions({{scaleMargins: {{top: 0.8, bottom: 0}}}});
vol.setData(D.volume);
chart.addLineSeries({{color: '#2962FF', lineWidth: 1, priceLineVisible: false}}).setData(D.sma50);
chart.addLineSeries({{color: '#FF9800', lineWidth: 1, priceLineVisible: false}}).setData(D.sma200);
chart.timeScale().fitContent();
new ResizeObserver(() => chart.applyOptions({{width: el.clientWidth}})).observe(el);
</script>""", height=470)

if not latest:
    st.stop()

# --- The analysis --------------------------------------------------------------
tab_over, tab_fin, tab_news, tab_docs, tab_hist = st.tabs(
    [":material/dashboard: Overview", ":material/bar_chart: Financials", ":material/newspaper: News",
     ":material/folder_open: Filings", ":material/history: Analysis history"])

with tab_over:
    st.caption(f"Latest analysis: {datetime.fromtimestamp(runs[0]['ts']).strftime('%d %b %Y, %H:%M')}")
    ui.company_card(latest, show_news=False)

with tab_fin:
    for key, title in [("quarterly_df", "Quarterly results")] + core.STATEMENT_TABLES:
        if latest.get(key) is not None:
            st.markdown(f"**{title}**")
            st.dataframe(ui.as_text(latest[key]), hide_index=True)
    if latest.get("correlated") is not None:
        st.markdown("**Every figure, lined up**")
        ui.correlated_table(latest["correlated"], settings, key="stock_corr")

with tab_news:
    ui.news_block(latest)

with tab_docs:
    ui.sources_table(latest.get("sources") or [], key="stock_sources")
    docs = latest.get("documents") or []
    if docs:
        st.markdown("**All filings listed on screener.in**")
        st.dataframe(pd.DataFrame(docs)[["category", "text", "url"]], hide_index=True,
                     column_config={"url": st.column_config.LinkColumn("Link", display_text="Open")})

with tab_hist:
    verdicts = pf._q("SELECT id, ts, label, json_extract(snapshot, '$.ai_verdict') AS verdict, "
                     "json_extract(snapshot, '$.ai_summary') AS summary FROM runs WHERE ticker=? "
                     "ORDER BY ts DESC", (ticker,))
    hist = pd.DataFrame(verdicts)
    hist["When"] = pd.to_datetime(hist["ts"], unit="s")
    st.dataframe(hist[["When", "verdict", "summary", "label"]].rename(
        columns={"verdict": "Verdict", "summary": "AI analysis", "label": "Trigger"}), hide_index=True,
        column_config={"When": st.column_config.DatetimeColumn(format="D MMM YYYY, HH:mm")})
    if len(runs) > 1:
        labels = {r["id"]: datetime.fromtimestamp(r["ts"]).strftime("%d %b %Y, %H:%M") for r in runs}
        c1, c2 = st.columns(2)
        new_id = c1.selectbox("Compare", list(labels), format_func=labels.get)
        old_id = c2.selectbox("against", list(labels), index=1, format_func=labels.get)
        new, old = archive.get_run(new_id), archive.get_run(old_id)
        ui.changes_table(archive.diff_snapshots(old["snapshot"], new["snapshot"]), old["ts"])
