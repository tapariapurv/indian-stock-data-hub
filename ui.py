"""
Shared rendering. The research page and the history page show the same
company card, so it is written once here and given a saved analysis or a
fresh one without caring which it got.
"""

import re

import pandas as pd
import streamlit as st

import core
import llm

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
            if news.get("summary"):
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

        if r.get("ai_summary"):
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
