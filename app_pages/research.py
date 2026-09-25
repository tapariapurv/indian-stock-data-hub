"""The research desk: run an analysis and read the results."""

import time
import traceback
from datetime import datetime

import streamlit as st

import archive
import core
import exports
import llm
import settings as cfg
import ui

settings = st.session_state.settings
ai = settings["ai"]
features = settings["features"]
core.apply_settings(settings)

st.session_state.setdefault("results", [])
st.session_state.setdefault("run_started", 0.0)

ai_ready, ai_status = llm.provider_status(settings) if ai["enabled"] else (False, "AI turned off")
models = llm.list_models(ai["provider"], cfg.base_url(settings, ai["provider"]),
                         cfg.api_key(settings, ai["provider"])) if ai["enabled"] else {}

# --- Sidebar ----------------------------------------------------------------
with st.sidebar:
    st.markdown("### :material/tune: Configuration")
    with st.form("config", border=False):
        raw_tickers = st.text_area(
            "Tickers", value=settings["data"]["default_tickers"], height=80,
            help="Comma-separated screener.in symbols, e.g. RELIANCE, TCS, INFY")
        watchlist = st.file_uploader(
            "…or upload a watchlist", type=["csv", "xlsx", "xls"],
            help="A CSV or Excel file with a 'ticker' column. Anything in it is analysed "
                 "instead of the box above.")
        selected_categories = st.pills(
            "Filings to download", core.WANTED_CATEGORIES, selection_mode="multi",
            default=settings["data"]["categories"])
        timeframe_label = st.selectbox(
            "Timeframe", list(core.TIMEFRAME_OPTIONS),
            index=list(core.TIMEFRAME_OPTIONS).index(settings["data"]["timeframe"])
            if settings["data"]["timeframe"] in core.TIMEFRAME_OPTIONS else 0,
            help="How many of the most recent filings to pull per source.")
        use_ai = st.toggle("AI summaries, verdicts & news", value=ai["enabled"] and ai_ready,
                           disabled=not ai_ready)
        model = st.selectbox(
            "Model", list(models), format_func=lambda m: models.get(m, m), disabled=not models,
            index=list(models).index(ai["model"]) if ai["model"] in models else (0 if models else None),
            placeholder="No models available", help="Change providers and keys on the Settings page.")
        run_button = st.form_submit_button("Run analysis", type="primary",
                                           icon=":material/play_arrow:", width="stretch")

    st.badge(ai_status, icon=":material/memory:" if ai_ready else ":material/cloud_off:",
             color="green" if ai_ready else "gray")
    if not ai_ready and ai["enabled"]:
        st.caption("Set up a model on the **Settings** page — a local one needs nothing but Ollama, "
                   "and the user guide walks through it in about ten minutes.")
    st.link_button("User guide (PDF)", core.GUIDE_URL, icon=":material/menu_book:", width="stretch",
                   type="secondary" if ai_ready else "primary")
    st.download_button("Watchlist template (CSV)", core.watchlist_template(),
                       file_name="watchlist_template.csv", mime="text/csv",
                       icon=":material/download:", type="tertiary", width="stretch")
    st.caption(f"PDFs capped at {core.MAX_FILE_MB} MB and cached in `~/.stock-data-hub`. "
               f"Results cached for {core.CACHE_TTL_SECONDS // 60} min.")
    for missing, lib, feature in [(not core.PDFPLUMBER_AVAILABLE, "pdfplumber", "PDF extraction"),
                                  (not core.DOCX_AVAILABLE, "python-docx", "Word export"),
                                  (not core.OPENPYXL_AVAILABLE, "openpyxl", "Excel export")]:
        if missing:
            st.warning(f"`{lib}` not installed — {feature} disabled.", icon=":material/warning:")

# The model picked in the sidebar applies to this run without being saved.
run_settings = {**settings, "ai": {**ai, "enabled": bool(use_ai and model), "model": model or ai["model"]}}

tickers = [t.strip().upper() for t in raw_tickers.split(",") if t.strip()]
upload_error = None
if watchlist is not None:
    uploaded_tickers, upload_error = core.tickers_from_upload(watchlist)
    if uploaded_tickers:
        tickers = uploaded_tickers
max_per_category = core.TIMEFRAME_OPTIONS[timeframe_label]

# --- Header -----------------------------------------------------------------
st.caption(":material/monitoring: EQUITY RESEARCH DESK")
st.title("Indian stock data hub")
st.markdown(f":gray[{settings['ui']['headline']}]")

if upload_error:
    st.error(upload_error, icon=":material/error:")

if run_button:
    if not tickers:
        st.toast("Enter at least one ticker, or upload a watchlist.", icon=":material/warning:")
    elif not selected_categories:
        st.toast("Select at least one filing type.", icon=":material/warning:")
    else:
        try:
            progress = st.progress(0.0, text="Starting…")
            with st.spinner(f"Scraping, downloading and analysing {len(tickers)} ticker(s)…"):
                st.session_state.run_started = time.time()
                results = core.scrape_all(tickers, run_settings, tuple(selected_categories),
                                          max_per_category, progress=progress)
                # Compare with the last saved analysis *before* saving this one.
                for r in results:
                    if not r.get("ok"):
                        continue
                    previous, changes = core.changes_since_last(r)
                    r["changes"], r["previous_at"] = changes, previous["ts"] if previous else None
                    if features["history"]:
                        core.save_run(r, label=f"{len(tickers)} ticker run")
                st.session_state.results = results
            progress.empty()
            ok_count = sum(1 for r in st.session_state.results if r["ok"])
            if ok_count:
                st.toast(f"Completed {ok_count} ticker(s).", icon=":material/check_circle:")
            if len(st.session_state.results) - ok_count:
                st.toast(f"{len(st.session_state.results) - ok_count} ticker(s) failed.",
                         icon=":material/error:")
        except Exception as exc:
            st.error(f"Run failed: {exc}", icon=":material/error:")
            st.code(traceback.format_exc())

if llm.LAST_BLOCK:
    st.warning(f"**Spending limit reached.** {llm.LAST_BLOCK} Everything except the AI summaries "
               "still ran normally.", icon=":material/payments:")

budget = llm.budget_state(run_settings)
if budget["enabled"] and budget["paid"]:
    for pct, label in ((budget["cost_pct"], "monthly budget"), (budget["token_pct"], "token budget")):
        if settings["budget"]["warn_at"] <= pct < 100:
            st.info(f"You have used {pct:.0f}% of your {label} this month. "
                    "The Settings page shows where it went.", icon=":material/info:")

results = st.session_state.results

if not results:
    with st.container(border=True, horizontal_alignment="center"):
        st.space("small")
        st.markdown("### :material/query_stats: No analysis yet", text_alignment="center")
        st.markdown(
            ":gray[Add tickers in the sidebar — or upload a watchlist — choose the filings you care "
            "about, then hit **Run analysis**.  \nYou'll get a snapshot per company, quarterly trends, "
            "every figure extracted from the PDFs, and Excel / Word reports.]", text_alignment="center")
        st.space("small")
    if features["history"] and archive.list_runs(limit=1):
        st.caption("Past analyses are on the **History** page, and everything downloaded so far is "
                   "searchable on the **Archive** page.")
    st.stop()

ok_results = [r for r in results if r["ok"]]
failed_results = [r for r in results if not r["ok"]]

# --- KPI strip + exports -----------------------------------------------------
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Tickers", len(results), border=True)
k2.metric("Successful", len(ok_results), border=True)
k3.metric("Failed", len(failed_results), border=True)
k4.metric("Filings downloaded", sum(core._count_downloaded(r) for r in ok_results), border=True)
k5.metric("Figures extracted", f"{sum(len(r.get('figures') or []) for r in ok_results):,}", border=True)

# Streamlit reruns the whole script on any widget interaction. Rebuilding a
# multi-thousand-row workbook every time is slow and risks handing the browser
# a half-written file, so it is built once per distinct result set.
fingerprint = tuple(
    (r["ticker"], r["ok"], len(r.get("figures") or []),
     0 if r.get("quarterly_df") is None else len(r["quarterly_df"]), r.get("ai_verdict"), r.get("ai_summary"),
     tuple(h["url"] for h in (r.get("news") or {}).get("headlines", [])), (r.get("news") or {}).get("summary"))
    for r in results)
if st.session_state.get("_export_fp") != fingerprint:
    st.session_state["_export_fp"] = fingerprint
    for key in ("xlsx", "docx"):
        st.session_state[f"_{key}_bytes"] = st.session_state[f"_{key}_error"] = None


def _cached_export(key: str, builder):
    if st.session_state.get(f"_{key}_bytes") is None and st.session_state.get(f"_{key}_error") is None:
        try:
            st.session_state[f"_{key}_bytes"] = builder(results)
        except Exception as exc:
            st.session_state[f"_{key}_error"] = str(exc)
    return st.session_state.get(f"_{key}_bytes"), st.session_state.get(f"_{key}_error")


stamp = datetime.now().strftime("%Y%m%d_%H%M")
with st.container(horizontal=True, horizontal_alignment="right"):
    if core.OPENPYXL_AVAILABLE:
        xlsx, xlsx_err = _cached_export("xlsx", exports.build_excel_workbook)
        if xlsx_err:
            st.error(f"Could not build Excel workbook: {xlsx_err}")
        else:
            st.download_button("Excel workbook", data=xlsx, file_name=f"stock_report_{stamp}.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               icon=":material/table_view:")
    if core.DOCX_AVAILABLE:
        docx, docx_err = _cached_export("docx", exports.build_word_document)
        if docx_err:
            st.error(f"Could not build Word document: {docx_err}")
        else:
            st.download_button("Word report", data=docx, file_name=f"stock_report_{stamp}.docx",
                               mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                               icon=":material/description:")

ui.result_tabs(results, settings, run_settings, st.session_state.run_started)
