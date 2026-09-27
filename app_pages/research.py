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
CATEGORY_WORDS = core.CATEGORY_WORDS

st.session_state.setdefault("results", [])
st.session_state.setdefault("run_started", 0.0)

ai_ready, ai_status = llm.provider_status(settings) if ai["enabled"] else (False, "AI turned off")
models = llm.list_models(ai["provider"], cfg.base_url(settings, ai["provider"]),
                         cfg.api_key(settings, ai["provider"])) if ai["enabled"] else {}

# --- Header and the three steps ---------------------------------------------------
st.title("Research")
st.caption("Look into several companies at once and download everything in one spreadsheet or report. "
           "For a single company, the **Stock** page is quicker.")

with st.form("config"):
    st.markdown("**1 · Which companies?**")
    raw_tickers = st.text_input(
        "Companies", value=settings["data"]["default_tickers"], label_visibility="collapsed",
        placeholder="Their stock symbols, separated by commas — e.g. TCS, INFY, RELIANCE",
        help="A company's symbol is the short name shown under its name on the Stock page.")
    st.markdown("**2 · What should it read?**")
    selected_categories = st.pills(
        "Reports", core.WANTED_CATEGORIES, selection_mode="multi", label_visibility="collapsed",
        default=settings["data"]["categories"], format_func=lambda c: CATEGORY_WORDS.get(c, c))
    with st.expander("More options", icon=":material/tune:"):
        watchlist = st.file_uploader(
            "Or upload a list of companies", type=["csv", "xlsx", "xls"],
            help="A spreadsheet with a column called 'ticker'. It's used instead of the box above.")
        timeframe_label = st.selectbox(
            "How far back", list(core.TIMEFRAME_OPTIONS),
            index=list(core.TIMEFRAME_OPTIONS).index(settings["data"]["timeframe"])
            if settings["data"]["timeframe"] in core.TIMEFRAME_OPTIONS else 0,
            help="How many of each kind of report to read.")
        use_ai = st.toggle("Write AI summaries", value=ai["enabled"] and ai_ready, disabled=not ai_ready,
                           help=None if ai_ready else "Turn on AI in Settings first.")
        model = st.selectbox(
            "Which AI", list(models), format_func=lambda m: models.get(m, m), disabled=not models,
            index=list(models).index(ai["model"]) if ai["model"] in models else (0 if models else None),
            placeholder="None set up yet", help="Add or change AIs in Settings.")
    st.markdown("**3 · Go**")
    run_button = st.form_submit_button("Analyse", type="primary", icon=":material/play_arrow:")

st.download_button("Need a list to upload? Download an example", core.watchlist_template(),
                   file_name="watchlist_template.csv", mime="text/csv", icon=":material/download:", type="tertiary")
if ui.tech(settings):
    st.badge(ai_status, icon=":material/memory:" if ai_ready else ":material/cloud_off:",
             color="green" if ai_ready else "gray")
    st.caption(f"Reports up to {core.MAX_FILE_MB} MB are downloaded and kept in `~/.stock-data-hub`. "
               f"Results are reused for {core.CACHE_TTL_SECONDS // 60} min.")
if not ai_ready and ai["enabled"]:
    st.caption(":material/info: AI summaries are off. Turn them on in **Settings** — it's free.")
for missing, lib, feature in [(not core.PDFPLUMBER_AVAILABLE, "pdfplumber", "reading reports"),
                              (not core.DOCX_AVAILABLE, "python-docx", "Word downloads"),
                              (not core.OPENPYXL_AVAILABLE, "openpyxl", "Excel downloads")]:
    if missing:
        st.warning(f"Part of the app is missing, so {feature} is switched off. Reinstalling the app fixes it.",
                   icon=":material/warning:")

# The model picked in the form applies to this run without being saved.
run_settings = {**settings, "ai": {**ai, "enabled": bool(use_ai and model), "model": model or ai["model"]}}

tickers = [t.strip().upper() for t in raw_tickers.split(",") if t.strip()]
upload_error = None
if watchlist is not None:
    uploaded_tickers, upload_error = core.tickers_from_upload(watchlist)
    if uploaded_tickers:
        tickers = uploaded_tickers
max_per_category = core.TIMEFRAME_OPTIONS[timeframe_label]

if upload_error:
    st.error(upload_error, icon=":material/error:")

if run_button:
    if not tickers:
        st.toast("Type at least one company symbol.", icon=":material/warning:")
    elif not selected_categories:
        st.toast("Pick at least one kind of report to read.", icon=":material/warning:")
    else:
        try:
            progress = st.progress(0.0, text="Starting…")
            with st.spinner(f"Looking into {len(tickers)} compan{'y' if len(tickers) == 1 else 'ies'} — about a minute each…"):
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
                        core.save_run(r, label=f"Analysed with {len(tickers) - 1} other(s)" if len(tickers) > 1 else "Analysed")
                st.session_state.results = results
            progress.empty()
            ok_count = sum(1 for r in st.session_state.results if r["ok"])
            if ok_count:
                st.toast(f"Finished {ok_count} compan{'y' if ok_count == 1 else 'ies'}.", icon=":material/check_circle:")
            if len(st.session_state.results) - ok_count:
                st.toast(f"{len(st.session_state.results) - ok_count} couldn't be loaded.",
                         icon=":material/error:")
        except Exception as exc:
            st.error("Something went wrong. Try again in a minute.", icon=":material/error:")
            if ui.tech(settings):
                st.code(traceback.format_exc())

if llm.LAST_BLOCK:
    st.warning("**You've reached the spending limit you set** for the AI, so summaries were skipped. "
               "Everything else worked. You can change the limit in Settings.", icon=":material/payments:")

budget = llm.budget_state(run_settings)
if budget["enabled"] and budget["paid"]:
    for pct, label in ((budget["cost_pct"], "monthly budget"), (budget["token_pct"], "token budget")):
        if settings["budget"]["warn_at"] <= pct < 100:
            st.info(f"You've used {pct:.0f}% of your AI {label} this month. Settings shows where it went.",
                    icon=":material/info:")

results = st.session_state.results

if not results:
    if features["history"] and archive.list_runs(limit=1):
        st.caption("Earlier checks are on the **History** page.")
    st.stop()

ok_results = [r for r in results if r["ok"]]
failed_results = [r for r in results if not r["ok"]]
if ui.tech(settings):
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Companies", len(results), border=True)
    k2.metric("Done", len(ok_results), border=True)
    k3.metric("Couldn't load", len(failed_results), border=True)
    k4.metric("Reports read", sum(core._count_downloaded(r) for r in ok_results), border=True)
    k5.metric("Numbers found", f"{sum(len(r.get('figures') or []) for r in ok_results):,}", border=True)
else:
    st.markdown(f"**{len(ok_results)} of {len(results)} companies done** :gray[· "
                f"{sum(core._count_downloaded(r) for r in ok_results)} reports read]")

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
            st.caption("The spreadsheet couldn't be made this time.")
        else:
            st.download_button("Download spreadsheet", data=xlsx, file_name=f"stock_report_{stamp}.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               icon=":material/table_view:")
    if core.DOCX_AVAILABLE:
        docx, docx_err = _cached_export("docx", exports.build_word_document)
        if docx_err:
            st.caption("The Word report couldn't be made this time.")
        else:
            st.download_button("Download report", data=docx, file_name=f"stock_report_{stamp}.docx",
                               mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                               icon=":material/description:")

ui.result_tabs(results, settings, run_settings, st.session_state.run_started)
