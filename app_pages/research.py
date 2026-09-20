"""The research desk: run an analysis and read the results."""

import time
import traceback
from datetime import datetime

import pandas as pd
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
    st.caption(f"PDFs capped at {core.MAX_FILE_MB} MB and cached under `downloads/`. "
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
                    r["correlated"] = core.correlate_numbers(r)
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

TAB_ICONS = {"Overview": ":material/dashboard:", "Financials": ":material/bar_chart:",
             "All numbers": ":material/table_rows:", "What changed": ":material/change_circle:",
             "Guidance": ":material/handshake:", "Filings": ":material/folder_open:"}
FEATURE_TAB = {"All numbers": "detailed_numbers", "What changed": "history", "Guidance": "guidance"}
wanted = [t for t in settings["ui"]["tabs"] if t in TAB_ICONS
          and features.get(FEATURE_TAB.get(t, ""), True)] or ["Overview"]
tabs = dict(zip(wanted, st.tabs([f"{TAB_ICONS[t]} {t}" for t in wanted])))
run_started = st.session_state.run_started


def pick_company(pool: dict, key: str):
    """The company switcher used by every per-company tab."""
    if len(pool) == 1:
        return next(iter(pool.values()))
    choice = st.segmented_control("Company", list(pool), default=next(iter(pool)), required=True,
                                  label_visibility="collapsed", key=key)
    return pool[choice or next(iter(pool))]


by_ticker = {r["ticker"]: r for r in ok_results}

# --- Overview ----------------------------------------------------------------
if "Overview" in tabs:
    with tabs["Overview"]:
        answers = [(a.get("at", 0), a.get("tokens", 0)) for r in results
                   for a in ({"at": r.get("ai_at", 0), "tokens": r.get("ai_tokens", 0)}, r.get("news") or {})
                   if a.get("tokens")]
        if answers:
            fresh = sum(t for at, t in answers if at >= run_started)
            reused = sum(1 for at, _ in answers if at < run_started)
            where = ("Nothing was sent to the cloud." if not llm.is_paid(run_settings, ai["provider"])
                     else f"Sent to {cfg.PROVIDERS[ai['provider']][0]}.")
            spent = archive.usage_since(run_started)["cost"] if run_started else 0.0
            price = f" · {cfg.money(settings, spent)}" if spent else ""
            st.caption(f":material/memory: {fresh:,} tokens this run (prompt + output, as reported by the "
                       f"provider){price}"
                       + (f"; {reused} answer(s) reused from cache at no cost" if reused else "")
                       + f". {where}")
        for r in ok_results:
            ui.company_card(r, run_started, show_news=features["news"])
        for r in failed_results:
            with st.container(border=True):
                st.markdown(f"**{r['ticker']}** :red-badge[:material/error: Failed]")
                st.caption(r.get("error_friendly") or r.get("error"))
                if r.get("error_friendly"):
                    with st.expander("Technical details", icon=":material/code:"):
                        st.code(r.get("error") or "")

# --- Financials --------------------------------------------------------------
if "Financials" in tabs:
    with tabs["Financials"]:
        pool = {t: r for t, r in by_ticker.items() if r.get("quarterly_df") is not None}
        if not pool:
            st.caption("No quarterly financial tables were found for the scraped tickers.")
        else:
            r = pick_company(pool, "fin_ticker")
            qdf = r["quarterly_df"]
            spark = settings["ui"]["sparklines"]
            t1, t2, t3, t4 = st.columns(4)
            with t1:
                ui.trend_metric(qdf, "Sales", "Sales", "₹{:,.0f} Cr", sparkline=spark)
            with t2:
                ui.trend_metric(qdf, "Net Profit", "Net profit", "₹{:,.0f} Cr", sparkline=spark)
            with t3:
                ui.trend_metric(qdf, "OPM", "Operating margin", "{:.0f}%", points=True, sparkline=spark)
            with t4:
                ui.trend_metric(qdf, "EPS", "EPS", "₹{:,.2f}", sparkline=spark)

            chart = pd.DataFrame({"Sales": ui.quarterly_series(qdf, "Sales"),
                                  "Net profit": ui.quarterly_series(qdf, "Net Profit")})
            if not chart.empty:
                with st.container(border=True):
                    st.markdown("**Sales vs net profit** :gray[· ₹ Cr per quarter]")
                    st.bar_chart(chart.rename_axis("Quarter").reset_index(), x="Quarter",
                                 y=["Sales", "Net profit"], stack=False, sort=False,
                                 color=settings["ui"]["chart_colors"], x_label="", y_label="", height=300)

            with st.container(horizontal=True, vertical_alignment="bottom"):
                st.markdown("**Quarterly results**", width="stretch")
                st.download_button("CSV", data=qdf.to_csv(index=False).encode("utf-8"),
                                   file_name=f"{r['ticker']}_quarterly.csv", mime="text/csv",
                                   icon=":material/download:", type="tertiary", key=f"csv_{r['ticker']}")
            st.dataframe(ui.as_text(qdf[qdf["Metric"] != "Raw PDF"]), hide_index=True,
                         column_config={"Metric": st.column_config.TextColumn(pinned=True)})

            for key, label in core.STATEMENT_TABLES:
                table = r.get(key)
                if table is not None and not table.empty:
                    with st.expander(label, icon=":material/table_chart:"):
                        st.dataframe(ui.as_text(table), hide_index=True,
                                     column_config={"Metric": st.column_config.TextColumn(pinned=True)})

# --- All numbers -------------------------------------------------------------
if "All numbers" in tabs:
    with tabs["All numbers"]:
        if not by_ticker:
            st.caption("Nothing to show yet.")
        else:
            r = pick_company(by_ticker, "detail_ticker")
            correlated = r.get("correlated")
            if correlated is None:
                correlated = core.correlate_numbers(r)
            figures = core.figures_frame(r)

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Metrics tracked", len(correlated), border=True)
            c2.metric("Figures from filings", f"{len(figures):,}", border=True)
            c3.metric("Filings read", core._count_downloaded(r), border=True)
            c4.metric("Corroborated by 2+ sources", int(
                ((correlated["In filings"] > 0) & correlated["Latest quarter"].notna()).sum())
                if not correlated.empty else 0, border=True,
                help="Metrics where a filing and the quarterly results both report a value.")

            with st.container(horizontal=True, vertical_alignment="center"):
                st.markdown("**Every metric, every source** :gray[· one row per metric]", width="stretch")
                named_only = st.toggle(
                    "Recognised metrics only", value=True, key=f"named_{r['ticker']}",
                    disabled="Identified" not in correlated.columns,
                    help="Off shows every label the extractor produced, including the rough ones "
                         "it could not map to a known metric.")
            # Saved analyses from an older version may not carry the flag.
            can_filter = not correlated.empty and "Identified" in correlated.columns
            view_corr = correlated[correlated["Identified"]] if named_only and can_filter else correlated
            st.caption(f"{len(view_corr):,} of {len(correlated):,} metrics")
            ui.correlated_table(view_corr, settings, key=f"corr_{r['ticker']}")
            st.download_button("Download correlated table (CSV)",
                               data=(view_corr.to_csv(index=False).encode("utf-8")
                                     if not view_corr.empty else b""),
                               file_name=f"{r['ticker']}_all_numbers.csv", mime="text/csv",
                               icon=":material/download:", type="tertiary",
                               disabled=view_corr.empty, key=f"corrcsv_{r['ticker']}")

            if not figures.empty:
                st.markdown("**Every figure, as printed in the filings**")
                fc1, fc2, fc3 = st.columns([2, 2, 3])
                sources = fc1.multiselect("Source", sorted(figures["Source"].dropna().unique()),
                                          key=f"fsrc_{r['ticker']}", placeholder="All sources")
                groups = fc2.multiselect("Category", sorted(figures["Category"].dropna().unique()),
                                         key=f"fcat_{r['ticker']}", placeholder="All categories")
                needle = fc3.text_input("Search labels and context", key=f"fq_{r['ticker']}",
                                        placeholder="e.g. margin, deposits, capex")
                view = figures
                if sources:
                    view = view[view["Source"].isin(sources)]
                if groups:
                    view = view[view["Category"].isin(groups)]
                if needle:
                    mask = (view["Label"].str.contains(needle, case=False, na=False)
                            | view["Context"].str.contains(needle, case=False, na=False))
                    view = view[mask]
                st.caption(f"{len(view):,} of {len(figures):,} figures")
                ui.figures_table(view, settings, key=f"figs_{r['ticker']}")

                with st.expander("Per filing, in detail", icon=":material/description:"):
                    for source, group in figures.groupby("Source"):
                        st.markdown(f"**{source}** :gray[· {len(group):,} figures · "
                                    f"pages {int(group['Page'].min())}–{int(group['Page'].max())}]")
                        summary = (group.groupby("Category")["Value"]
                                   .agg(["count", "min", "median", "max"]).reset_index()
                                   .rename(columns={"count": "Figures", "min": "Low",
                                                    "median": "Median", "max": "High"}))
                        st.dataframe(summary, hide_index=True)

# --- What changed ------------------------------------------------------------
if "What changed" in tabs:
    with tabs["What changed"]:
        st.caption("Compared with the last saved analysis of the same company. "
                   "Plain comparison — no model is involved, so nothing here is invented.")
        any_previous = False
        for r in ok_results:
            if r.get("previous_at") is None:
                continue
            any_previous = True
            with st.container(border=True):
                when = datetime.fromtimestamp(r["previous_at"]).strftime("%d %b %Y, %H:%M")
                with st.container(horizontal=True, vertical_alignment="center"):
                    st.markdown(f"**{r['ticker']}**", width="content")
                    st.badge(f"{len(r.get('changes') or [])} change(s) since {when}",
                             color="orange" if r.get("changes") else "gray",
                             icon=":material/change_circle:")
                ui.changes_table(r.get("changes") or [])
        if not any_previous:
            st.info("This is the first saved analysis for these companies. Run them again later and "
                    "this tab will show exactly what moved.", icon=":material/info:")

# --- Guidance ----------------------------------------------------------------
if "Guidance" in tabs:
    with tabs["Guidance"]:
        st.caption("What management committed to on the earnings call, and whether the numbers "
                   "reported since actually delivered it.")
        if not by_ticker:
            st.caption("Nothing to show yet.")
        else:
            r = pick_company(by_ticker, "guide_ticker")
            has_transcript = bool(archive.document_text(r["ticker"], "Concall Transcript"))
            with st.container(horizontal=True, vertical_alignment="center"):
                if st.button("Read the latest transcript", icon=":material/auto_stories:",
                             disabled=not (has_transcript and run_settings["ai"]["enabled"]),
                             key=f"harvest_{r['ticker']}", type="primary"):
                    with st.spinner("Pulling out management's commitments…"):
                        saved, tokens = core.harvest_guidance(r, run_settings)
                    st.toast(f"{saved} new commitment(s) filed · {tokens:,} tokens",
                             icon=":material/handshake:")
                if st.button("Check against results", icon=":material/fact_check:",
                             disabled=not run_settings["ai"]["enabled"], key=f"check_{r['ticker']}"):
                    with st.spinner("Grading past commitments against reported numbers…"):
                        checked, tokens = core.check_guidance(r, run_settings)
                    st.toast(f"{checked} commitment(s) graded · {tokens:,} tokens",
                             icon=":material/fact_check:")
            if not has_transcript:
                st.info("No concall transcript has been downloaded for this company yet. Tick "
                        "**Concall Transcript** in the sidebar and run the analysis again.",
                        icon=":material/info:")
            elif not run_settings["ai"]["enabled"]:
                st.info("Set up a model to read transcripts — extraction is what small local models "
                        "do well.", icon=":material/info:")

            claims = archive.claims_for(r["ticker"])
            if claims:
                graded = [c for c in claims if c.get("status")]
                delivered = sum(1 for c in graded if c["status"] == "Delivered")
                g1, g2, g3 = st.columns(3)
                g1.metric("Commitments on file", len(claims), border=True)
                g2.metric("Graded", len(graded), border=True)
                g3.metric("Delivered", f"{delivered}/{len(graded)}" if graded else "—", border=True,
                          help="How often this management has done what it said it would.")
                for c in claims:
                    with st.container(border=True):
                        with st.container(horizontal=True, vertical_alignment="center"):
                            st.markdown(f"**{c['metric']}** :gray[· said in {c['quarter']} · "
                                        f"by {c['horizon']}]", width="stretch")
                            if c.get("status"):
                                color, icon = ui.STATUS_BADGE.get(c["status"], ("gray", None))
                                st.badge(c["status"], color=color, icon=icon)
                        st.markdown(f"> {c['claim']}")
                        if c.get("why"):
                            st.caption(f":material/fact_check: {c['why']} "
                                       f":gray[· checked against {c.get('checked_against')}]")
                if st.button("Clear this company's commitments", icon=":material/delete:",
                             type="tertiary", key=f"clr_{r['ticker']}"):
                    archive.delete_claims(r["ticker"])
                    st.rerun()
            elif has_transcript:
                st.caption("Nothing on file yet — read the transcript to get started.")

# --- Filings -----------------------------------------------------------------
if "Filings" in tabs:
    with tabs["Filings"]:
        for r in ok_results:
            with st.container(border=True):
                st.markdown(f"### {r['ticker']}")
                if r.get("downloaded"):
                    ui.sources_table(core._sources_of(r), key=f"src_{r['ticker']}")
                docs = r.get("documents") or []
                if docs:
                    with st.expander(f"All {len(docs)} document links on the screener.in page",
                                     icon=":material/link:"):
                        st.dataframe(
                            pd.DataFrame(docs).rename(columns={"category": "Category", "text": "Title",
                                                               "url": "Link"}),
                            hide_index=True,
                            column_config={"Title": st.column_config.TextColumn(width="large"),
                                           "Link": st.column_config.LinkColumn(display_text="Open ↗")})
                else:
                    st.caption("No PDF / BSE / NSE document links found on this page.")
