"""
Shared rendering. The research page and the history page show the same
company card, so it is written once here and given a saved analysis or a
fresh one without caring which it got.
"""

import re
from datetime import datetime

import pandas as pd
import streamlit as st

import archive
import core
import llm
import settings as cfg

VERDICT_BADGE = {"Positive": ("green", ":material/trending_up:"),
                 "Neutral": ("orange", ":material/trending_flat:"),
                 "Cautious": ("red", ":material/trending_down:")}
SENTIMENT_BADGE = {"Positive": ("green", ":material/trending_up:"),
                   "Mixed": ("orange", ":material/trending_flat:"),
                   "Negative": ("red", ":material/trending_down:")}
STATUS_BADGE = {"Delivered": ("green", ":material/check_circle:"),
                "Missed": ("red", ":material/cancel:"),
                "Unclear": ("gray", ":material/help:")}


def as_text(df: pd.DataFrame) -> pd.DataFrame:
    """screener.in columns mix numbers with text like "26%", which Arrow can't
    type; show them as the site does. (Exports parse the numbers properly.)"""
    return df.map(lambda v: "" if pd.isna(v) else str(v))


def md_escape(text: str) -> str:
    """Headlines go inside [link text]; brackets, $ (LaTeX) and * would break it."""
    return re.sub(r"([\[\]$*_`])", r"\\\1", str(text))


def quarterly_series(qdf, metric: str) -> pd.Series:
    """One quarterly row (e.g. "Sales") as numbers, indexed by quarter label."""
    if qdf is None or qdf.empty:
        return pd.Series(dtype=float)
    rows = qdf[qdf["Metric"].astype(str).str.startswith(metric)]
    if rows.empty:
        return pd.Series(dtype=float)
    return rows.iloc[0].drop("Metric").map(core._to_number).dropna().astype(float)


def trend_metric(qdf, metric: str, label: str, fmt: str, points: bool = False, sparkline: bool = True):
    s = quarterly_series(qdf, metric)
    if s.empty:
        return
    delta = None
    if len(s) > 1:
        prev, last = s.iloc[-2], s.iloc[-1]
        if points:
            delta = f"{last - prev:+.1f} pp QoQ"
        elif prev:
            delta = f"{(last - prev) / abs(prev) * 100:+.1f}% QoQ"
    st.metric(label, fmt.format(s.iloc[-1]), delta, border=True,
              chart_data=s.tail(8).tolist() if sparkline else None,
              chart_type="area" if sparkline else None,
              help=f"Latest quarter ({s.index[-1]}); sparkline shows the last 8 quarters.")


def news_block(r: dict, run_started: float = 0.0):
    news = r.get("news") or {}
    if news.get("headlines"):
        with st.container(border=True):
            with st.container(horizontal=True, vertical_alignment="center"):
                st.markdown("**:material/newspaper: Recent news**", width="content")
                if news.get("sentiment"):
                    color, icon = SENTIMENT_BADGE.get(news["sentiment"], ("gray", None))
                    st.badge(f"News sentiment · {news['sentiment']}", icon=icon, color=color)
            if llm.is_usable(news.get("summary")):
                st.markdown(news["summary"])
                st.caption(f":material/memory: Summarised by `{news.get('model')}` from the "
                           f"{len(news['headlines'])} headlines below · "
                           f"{llm.token_note(news.get('tokens', 0), news.get('at', 0), run_started)}")
            st.markdown("\n".join(
                f"- [{md_escape(h['title'])}]({h['url']}) :gray[· {h['source']}"
                f"{h['published'].strftime(' · %d %b') if h.get('published') else ''}]"
                for h in news["headlines"]))
    elif "news" in r:
        st.caption(f":material/newspaper: No news in the last {core.NEWS_MAX_AGE_DAYS} days mentioning "
                   f"{r.get('company_name') or r['ticker']}.")


def company_card(r: dict, run_started: float = 0.0, show_news: bool = True):
    """The overview card: verdict, analysis, ratios, strengths, risks, news."""
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.markdown(f"## {r['ticker']}", width="content")
            if r.get("ai_verdict"):
                color, icon = VERDICT_BADGE.get(r["ai_verdict"], ("gray", None))
                st.badge(f"AI verdict · {r['ai_verdict']}", icon=icon, color=color)
            if r.get("restored"):
                st.badge("Saved analysis", icon=":material/history:", color="gray")
        st.caption(r.get("company_name") or "")

        if llm.is_usable(r.get("ai_summary")):
            st.markdown(f"> {r['ai_summary']}")
            st.caption(f":material/memory: Written by `{r.get('ai_model')}` from the scraped data below · "
                       f"{llm.token_note(r.get('ai_tokens', 0), r.get('ai_at', 0), run_started)}")
        elif r.get("ai_model"):
            why = r.get("ai_error")
            st.caption(f":material/warning: `{r['ai_model']}` gave no usable summary — "
                       + (f"the provider said: {why}. " if why else
                          "it repeated the instructions or wrote nothing, so the answer was "
                          "discarded rather than shown. ")
                       + "Try another model, or raise the reply limit in "
                         "**Settings → Generation limits**.")

        if r.get("metrics"):
            items = list(r["metrics"].items())
            for start in range(0, len(items), 5):
                for col, (k, v) in zip(st.columns(5), items[start:start + 5]):
                    col.markdown(f":gray[{k}]  \n**{v}**")

        if r.get("pros") or r.get("cons"):
            pc1, pc2 = st.columns(2)
            with pc1.container(border=True, height="stretch"):
                st.markdown("**:green[:material/check_circle: Strengths]**")
                st.markdown("\n".join(f"- {p}" for p in (r.get("pros") or [])[:5]) or ":gray[None listed]")
            with pc2.container(border=True, height="stretch"):
                st.markdown("**:red[:material/error: Risks]**")
                st.markdown("\n".join(f"- {c}" for c in (r.get("cons") or [])[:5]) or ":gray[None listed]")

        if show_news:
            news_block(r, run_started)


NUMBER_COLUMNS = ["Latest quarter", "Previous quarter", "Filing latest", "Filing low",
                  "Filing median", "Filing high"]


def correlated_table(df: pd.DataFrame, settings: dict, key: str):
    """Every metric with each source of it side by side."""
    if df is None or df.empty:
        st.caption("No numbers to correlate for this company yet.")
        return
    ui = settings["ui"]
    decimals = int(ui.get("decimals", 2))
    df = df.drop(columns=["Identified"], errors="ignore").copy()
    # Round the values rather than handing the grid a printf format: a printf
    # format prints an empty number as the word "None" in every blank cell,
    # which made a mostly-empty column look like an error.
    for column in NUMBER_COLUMNS:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce").round(decimals)
    st.dataframe(
        df, hide_index=True, height=int(ui.get("table_height", 380)), key=key,
        column_config={
            "Metric": st.column_config.TextColumn(pinned=True, width="medium"),
            "Group": st.column_config.TextColumn(width="small"),
            "Screener": st.column_config.TextColumn("Screener ratio", width="small"),
            "QoQ": st.column_config.NumberColumn(format="percent", width="small"),
            "In filings": st.column_config.NumberColumn(width="small",
                help="How many times this metric was found across the downloaded PDFs."),
            "Unit": st.column_config.TextColumn(width="small"),
            "Sources": st.column_config.TextColumn(width="medium"),
            "Pages": st.column_config.TextColumn(width="small",
                help="The pages of the filings this metric was found on."),
            **{c: st.column_config.NumberColumn(format="localized") for c in NUMBER_COLUMNS},
        })


def figures_table(df: pd.DataFrame, settings: dict, key: str):
    """Every raw figure, exactly as pulled from the PDFs."""
    if df is None or df.empty:
        st.caption("No figures were extracted from this company's filings.")
        return
    ui = settings["ui"]
    order = ["Source", "Category", "Label", "Currency", "Value", "Unit", "Page"]
    if ui.get("show_context_column", True):
        order.append("Context")
    st.dataframe(
        df, hide_index=True, height=int(ui.get("table_height", 380)), key=key, column_order=order,
        column_config={
            "Value": st.column_config.NumberColumn(format="localized"),
            "Page": st.column_config.NumberColumn(width="small"),
            "Currency": st.column_config.TextColumn(width="small"),
            "Unit": st.column_config.TextColumn(width="small"),
            "Context": st.column_config.TextColumn(width="large"),
        })


CHANGE_ICON = {"Ratio": ":material/percent:", "Strength": ":material/add_circle:",
               "Risk": ":material/warning:", "AI verdict": ":material/psychology:",
               "New filing": ":material/description:", "News": ":material/newspaper:"}


def changes_table(changes: list[dict], previous_ts: float | None = None):
    if not changes:
        st.caption("Nothing has changed since the previous saved analysis.")
        return
    df = pd.DataFrame(changes)
    df["kind"] = df["kind"].map(lambda k: f"{CHANGE_ICON.get(k, '')} {k}".strip())
    st.dataframe(df.rename(columns={"kind": "What", "item": "Item", "from": "Was", "to": "Now"}),
                 hide_index=True,
                 column_config={"What": st.column_config.TextColumn(width="small"),
                                "Item": st.column_config.TextColumn(width="large")})


def sources_table(sources: list[dict], key: str | None = None):
    """Where every filing behind an analysis lives, and which pages it gave up.

    A saved analysis is only trustworthy if you can get back to the paper it
    came from, so each row carries the file on disk, the pages figures were
    found on, and a link to the original document.
    """
    if not sources:
        st.caption("No filings were downloaded in this analysis.")
        return
    rows = []
    for s in sources:
        if s.get("first_page") and s.get("last_page"):
            pages = (f"p{s['first_page']}" if s["first_page"] == s["last_page"]
                     else f"p{s['first_page']}–p{s['last_page']}")
        else:
            pages = "—"
        rows.append({
            "Filing": s.get("category") or "—",
            "File": s.get("file") or "(not downloaded)",
            "Figures": s.get("figures") or 0,
            "Pages used": pages,
            "Saved at": s.get("path") or (s.get("error") or "—"),
            "Original": s.get("url") or None,
        })
    st.dataframe(
        pd.DataFrame(rows), hide_index=True, key=key,
        column_config={
            "Filing": st.column_config.TextColumn(width="small"),
            "Figures": st.column_config.NumberColumn(width="small",
                help="How many numbers were extracted from this document."),
            "Pages used": st.column_config.TextColumn(width="small",
                help="The span of pages those figures came from."),
            "Saved at": st.column_config.TextColumn(width="large",
                help="The full path to the PDF on this machine."),
            "Original": st.column_config.LinkColumn(display_text="Open ↗", width="small"),
        })
    st.caption(":material/folder: Copy a path to open the PDF, then jump to the page shown. "
               "Per-metric pages are in the **Pages** column of the numbers table.")


# --- An analysis, in tabs ---------------------------------------------------

TAB_ICONS = {"Overview": ":material/dashboard:", "Financials": ":material/bar_chart:",
             "All numbers": ":material/table_rows:", "What changed": ":material/change_circle:",
             "Guidance": ":material/handshake:", "Filings": ":material/folder_open:"}
FEATURE_TAB = {"All numbers": "detailed_numbers", "What changed": "history", "Guidance": "guidance"}

def pick_company(pool: dict, key: str):
    """The company switcher used by every per-company tab."""
    if len(pool) == 1:
        return next(iter(pool.values()))
    choice = st.segmented_control("Company", list(pool), default=next(iter(pool)), required=True,
                                  label_visibility="collapsed", key=key)
    return pool[choice or next(iter(pool))]



def result_tabs(results: list[dict], settings: dict, run_settings: dict, run_started: float = 0.0,
                extra: dict[str, str] | None = None) -> dict:
    """The Research page's tabs for a set of analyses. The Stock page shows a
    held stock's latest analysis through the same function, so the two pages
    always show the same thing. `extra` adds tabs (name -> icon) after these;
    their containers are returned for the caller to fill."""
    features, ai = settings["features"], settings["ai"]
    ok_results = [r for r in results if r["ok"]]
    failed_results = [r for r in results if not r["ok"]]
    extra = extra or {}
    wanted = [t for t in settings["ui"]["tabs"] if t in TAB_ICONS
              and features.get(FEATURE_TAB.get(t, ""), True)] or ["Overview"]
    icons = {**TAB_ICONS, **extra}
    tabs = dict(zip(wanted + list(extra), st.tabs([f"{icons[t]} {t}" for t in wanted + list(extra)])))

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
                company_card(r, run_started, show_news=features["news"])
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
                    trend_metric(qdf, "Sales", "Sales", "₹{:,.0f} Cr", sparkline=spark)
                with t2:
                    trend_metric(qdf, "Net Profit", "Net profit", "₹{:,.0f} Cr", sparkline=spark)
                with t3:
                    trend_metric(qdf, "OPM", "Operating margin", "{:.0f}%", points=True, sparkline=spark)
                with t4:
                    trend_metric(qdf, "EPS", "EPS", "₹{:,.2f}", sparkline=spark)

                chart = pd.DataFrame({"Sales": quarterly_series(qdf, "Sales"),
                                      "Net profit": quarterly_series(qdf, "Net Profit")})
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
                st.dataframe(as_text(qdf[qdf["Metric"] != "Raw PDF"]), hide_index=True,
                             column_config={"Metric": st.column_config.TextColumn(pinned=True)})

                for key, label in core.STATEMENT_TABLES:
                    table = r.get(key)
                    if table is not None and not table.empty:
                        with st.expander(label, icon=":material/table_chart:"):
                            st.dataframe(as_text(table), hide_index=True,
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
                correlated_table(view_corr, settings, key=f"corr_{r['ticker']}")
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
                    figures_table(view, settings, key=f"figs_{r['ticker']}")

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
                    changes_table(r.get("changes") or [])
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
                                    color, icon = STATUS_BADGE.get(c["status"], ("gray", None))
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
                        sources_table(core._sources_of(r), key=f"src_{r['ticker']}")
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

    return {name: tabs[name] for name in extra}
