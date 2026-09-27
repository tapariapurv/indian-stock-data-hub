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
import disclosures
import llm
import settings as cfg

VERDICT_BADGE = {"Positive": ("green", ":material/trending_up:"),
                 "Neutral": ("orange", ":material/trending_flat:"),
                 "Cautious": ("red", ":material/trending_down:")}
SENTIMENT_BADGE = {"Positive": ("green", ":material/trending_up:"),
                   "Mixed": ("orange", ":material/trending_flat:"),
                   "Negative": ("red", ":material/trending_down:")}
# The model's one-word view, said the way a friend would say it.
VIEW_WORDS = llm.VIEW_WORDS
PROMISE_WORDS = {"Delivered": "Kept", "Missed": "Not kept", "Unclear": "Too early to tell"}
FLAG_WORDS = {"Auditor": "Auditor concern", "Accounting policy": "Accounting rules changed",
              "Related-party loans": "Loans to related companies", "Contingent liability": "Pending claim"}
STATUS_BADGE = {"Delivered": ("green", ":material/check_circle:"),
                "Missed": ("red", ":material/cancel:"),
                "Unclear": ("gray", ":material/help:")}


def inr(x: float, signed: bool = False, decimals: int = 0) -> str:
    """Rupees the Indian way: ₹4,26,984, and -₹48,016 rather than ₹-48,016.
    The sign is a plain hyphen because st.metric reads it to colour a change."""
    whole, _, frac = f"{abs(x):.{decimals}f}".partition(".")
    if len(whole) > 3:
        whole = re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", whole[:-3]) + "," + whole[-3:]
    sign = "-" if x < 0 and float(f"{abs(x):.{decimals}f}") else ("+" if signed and x > 0 else "")
    return f"{sign}₹{whole}" + (f".{frac}" if frac else "")


def tech(settings: dict | None = None) -> bool:
    """Behind-the-scenes details (costs, model names, file paths) are shown only on request."""
    settings = settings or st.session_state.get("settings") or {}
    return bool(settings.get("ui", {}).get("tech_details"))


def plain_title(title: str) -> str:
    """Messages saved before the plain-English wording, said the new way."""
    title = re.sub(r"AI verdict (\w+) → (\w+)",
                   lambda m: f"the AI's view changed to “{VIEW_WORDS.get(m[2], m[2])}”", title)
    return title.replace("guidance missed", "a promise wasn't kept").replace("concall transcript", "investor call")


def open_company(company: dict) -> None:
    """Go to the Stock page for {ticker, name, screener_id}."""
    st.session_state.company = company
    st.session_state.stock = company["ticker"]
    st.switch_page("app_pages/stock.py")


@st.fragment
def company_search(key: str,
                   placeholder: str = "Look up any company — type a name or symbol, e.g. tata motors or TMCV") -> None:
    """The one search box used everywhere: a list of every NSE-listed company that
    typing filters by name or symbol, in any case. Picking one opens its Stock page."""
    import portfolio as pf  # late: both import streamlit caches that need the app running
    import screener as sc
    names = sc.listed(pf.day_slot())
    with st.container(horizontal=True, vertical_alignment="center"):
        sme = st.session_state.get(f"{key}_sme", True)
        shown = names if sme else names[~names["SME"]]
        label = dict(zip(shown["Ticker"], shown["Company"] + " · " + shown["Ticker"]
                         + shown["SME"].map({True: " · SME", False: ""})))
        label.update({t: f"{h['name']} · {t}" for t, h in pf.tracked().items() if t not in label})
        # A symbol that isn't in the list (a BSE-only company, say) can still be typed and opened.
        st.selectbox("Look up a company", list(label), index=None, key=key, placeholder=placeholder,
                     format_func=lambda t: label.get(t, t), accept_new_options=True, filter_mode="contains", label_visibility="collapsed",
                     on_change=_picked, args=(key, label), width="stretch")
        st.checkbox("Include SMEs", value=True, key=f"{key}_sme", width="content",
                    help="Small and medium companies listed on NSE Emerge. Smaller, riskier and less traded.")
    if names.empty:
        st.caption("Couldn't load the list of companies — check your internet connection. "
                   "You can still type a company's exact symbol and press Enter.")
    if st.session_state.pop("_open_company", False):
        st.switch_page("app_pages/stock.py")


def _picked(key: str, label: dict) -> None:
    """Runs before the rerun, so the search box can be emptied and the stock picker moved to match."""
    import portfolio as pf
    ticker = (st.session_state[key] or "").strip()
    if not ticker:
        return
    ticker = ticker if ticker in label else ticker.upper()
    held = pf.tracked().get(ticker)
    company = {"ticker": ticker, **held} if held else \
        {"ticker": ticker, "name": label.get(ticker, ticker).split(" · ")[0], "screener_id": None}
    st.session_state[key] = None
    st.session_state.company, st.session_state.stock = company, ticker
    st.session_state._open_company = True


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
            delta = f"{last - prev:+.1f} points vs last quarter"
        elif prev:
            delta = f"{(last - prev) / abs(prev) * 100:+.1f}% vs last quarter"
    st.metric(label, fmt.format(s.iloc[-1]), delta, border=True,
              chart_data=s.tail(8).tolist() if sparkline else None,
              chart_type="area" if sparkline else None,
              help=f"Latest quarter ({s.index[-1]}); the small chart shows the last 8 quarters.")


def news_block(r: dict, run_started: float = 0.0):
    news = r.get("news") or {}
    if news.get("headlines"):
        with st.container(border=True):
            with st.container(horizontal=True, vertical_alignment="center"):
                st.markdown("**:material/newspaper: Recent news**", width="content")
                if news.get("sentiment"):
                    color, icon = SENTIMENT_BADGE.get(news["sentiment"], ("gray", None))
                    st.badge(f"Tone of the news: {news['sentiment'].lower()}", icon=icon, color=color)
            if llm.is_usable(news.get("summary")):
                st.markdown(news["summary"])
                if tech():
                    st.caption(f":material/memory: Summarised by `{news.get('model')}` from the "
                               f"{len(news['headlines'])} headlines below · "
                               f"{llm.token_note(news.get('tokens', 0), news.get('at', 0), run_started)}")
                else:
                    st.caption(f":material/auto_awesome: AI summary of the {len(news['headlines'])} headlines below")
            line = lambda h: (f"- [{md_escape(h['title'])}]({h['url']}) :gray[· {h['source']}"
                              f"{h['published'].strftime(' · %d %b') if h.get('published') else ''}]")
            st.markdown("\n".join(line(h) for h in news["headlines"][:4]))
            if len(news["headlines"]) > 4:
                with st.expander(f"{len(news['headlines']) - 4} more headlines"):
                    st.markdown("\n".join(line(h) for h in news["headlines"][4:]))
    elif "news" in r:
        st.caption(f":material/newspaper: No news in the last {core.NEWS_MAX_AGE_DAYS} days mentioning "
                   f"{r.get('company_name') or r['ticker']}.")


# Screener.in's ratio names, said in everyday words.
RATIO_WORDS = {"Market Cap": "Company value", "Current Price": "Share price", "High / Low": "Year high / low",
               "Stock P/E": "Price / earnings", "Book Value": "Book value per share",
               "Dividend Yield": "Dividend yield", "ROCE": "Return on capital", "ROE": "Return on equity",
               "Face Value": "Face value"}
RATIO_HELP = {"Stock P/E": "How many years of today's profit the share price equals. Lower is cheaper.",
              "ROCE": "Profit made for every ₹100 the business uses. Higher is better.",
              "ROE": "Profit made for every ₹100 the owners have put in. Higher is better.",
              "Dividend Yield": "Cash paid out each year for every ₹100 of shares.",
              "Book Value": "What each share would be worth if the company sold everything and paid its debts."}


def company_card(r: dict, run_started: float = 0.0, show_news: bool = True, header: bool = True):
    """The overview card: verdict, analysis, ratios, strengths, risks, news.
    `header` is off on the Stock page, which already shows the name and view."""
    with st.container(border=True):
        if header:
            with st.container(horizontal=True, vertical_alignment="center"):
                st.markdown(f"## {r['ticker']}", width="content")
                if r.get("ai_verdict"):
                    color, icon = VERDICT_BADGE.get(r["ai_verdict"], ("gray", None))
                    st.badge(f"AI view: {VIEW_WORDS.get(r['ai_verdict'], r['ai_verdict'])}", icon=icon, color=color)
                if r.get("restored"):
                    st.badge("From an earlier check", icon=":material/history:", color="gray")
            st.caption(r.get("company_name") or "")

        if llm.is_usable(r.get("ai_summary")):
            st.markdown(f"> {r['ai_summary']}")
            if tech():
                st.caption(f":material/memory: Written by `{r.get('ai_model')}` from the data below · "
                           f"{llm.token_note(r.get('ai_tokens', 0), r.get('ai_at', 0), run_started)}")
            else:
                st.caption(":material/auto_awesome: Written by AI from the figures below — check anything "
                           "important for yourself.")
        elif r.get("ai_model"):
            why = r.get("ai_error")
            st.caption(":material/warning: The AI couldn't write a summary this time"
                       + (f" ({why})" if why and tech() else "")
                       + ". Everything else below is still correct. Try again later, or pick a different "
                         "AI in **Settings**.")

        if r.get("metrics"):
            items = list(r["metrics"].items())
            for start in range(0, len(items), 5):
                for col, (k, v) in zip(st.columns(5), items[start:start + 5]):
                    col.markdown(f":gray[{k if tech() else RATIO_WORDS.get(k, k)}]  \n**{v}**", help=RATIO_HELP.get(k))

        if r.get("pros") or r.get("cons"):
            pc1, pc2 = st.columns(2)
            with pc1.container(border=True, height="stretch"):
                st.markdown("**:green[:material/check_circle: Strengths]**")
                st.markdown("\n".join(f"- {p}" for p in (r.get("pros") or [])[:5]) or ":gray[None listed]")
            with pc2.container(border=True, height="stretch"):
                st.markdown("**:red[:material/error: Weaknesses]**")
                st.markdown("\n".join(f"- {c}" for c in (r.get("cons") or [])[:5]) or ":gray[None listed]")

        if show_news:
            news_block(r, run_started)


NUMBER_COLUMNS = ["Latest quarter", "Previous quarter", "Filing latest", "Filing low",
                  "Filing median", "Filing high"]


def correlated_table(df: pd.DataFrame, settings: dict, key: str):
    """Every metric with each source of it side by side."""
    if df is None or df.empty:
        st.caption("No numbers for this company yet.")
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
            "Screener": st.column_config.TextColumn("Website figure", width="small"),
            "QoQ": st.column_config.NumberColumn("vs last quarter", format="percent", width="small"),
            "In filings": st.column_config.NumberColumn("Times in reports", width="small",
                help="How many times this number appears across the company's reports."),
            "Unit": st.column_config.TextColumn(width="small"),
            "Sources": st.column_config.TextColumn(width="medium"),
            "Pages": st.column_config.TextColumn(width="small",
                help="The pages of the reports this number was found on."),
            **{c: st.column_config.NumberColumn(format="localized") for c in NUMBER_COLUMNS},
        })


def figures_table(df: pd.DataFrame, settings: dict, key: str):
    """Every raw figure, exactly as pulled from the PDFs."""
    if df is None or df.empty:
        st.caption("No numbers were found in this company's reports.")
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
               "Risk": ":material/warning:", "AI view": ":material/psychology:", "AI verdict": ":material/psychology:",
               "New filing": ":material/description:", "News": ":material/newspaper:"}


def changes_table(changes: list[dict], previous_ts: float | None = None):
    if not changes:
        st.caption("Nothing has changed since the last check.")
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
        st.caption("No reports were downloaded this time.")
        return
    rows = []
    for s in sources:
        if s.get("first_page") and s.get("last_page"):
            pages = (f"p{s['first_page']}" if s["first_page"] == s["last_page"]
                     else f"p{s['first_page']}–p{s['last_page']}")
        else:
            pages = "—"
        rows.append({
            "Report": core.CATEGORY_WORDS.get(s.get("category"), s.get("category") or "—"),
            "File": s.get("file") or "(not downloaded)",
            "Numbers found": s.get("figures") or 0,
            "Pages used": pages,
            "Saved at": s.get("path") or (s.get("error") or "—"),
            "Original": s.get("url") or None,
        })
    table = pd.DataFrame(rows)
    if not tech():
        table = table.drop(columns=["File", "Saved at"])
    st.dataframe(
        table, hide_index=True, key=key,
        column_config={
            "Report": st.column_config.TextColumn(width="small"),
            "Numbers found": st.column_config.NumberColumn(width="small",
                help="How many numbers were read from this report."),
            "Pages used": st.column_config.TextColumn(width="small",
                help="The pages those numbers came from."),
            "Saved at": st.column_config.TextColumn(width="large",
                help="Where the file is kept on this computer."),
            "Original": st.column_config.LinkColumn(display_text="Open ↗", width="small"),
        })
    st.caption(":material/open_in_new: Open the original report to check any number on the page shown.")


# --- An analysis, in tabs ---------------------------------------------------

TAB_ICONS = {"Overview": ":material/dashboard:", "Financials": ":material/bar_chart:",
             "All numbers": ":material/table_rows:", "What changed": ":material/change_circle:",
             "Guidance": ":material/handshake:", "Filings": ":material/folder_open:"}
# What each tab is called on screen. The keys stay as they are, because saved settings name them.
TAB_LABEL = {"Overview": "Summary", "Financials": "Results", "All numbers": "Every number",
             "What changed": "What changed", "Guidance": "Promises kept?", "Filings": "Reports & warnings"}
FEATURE_TAB = {"All numbers": "detailed_numbers", "What changed": "history", "Guidance": "guidance"}

SEVERITY_COLOR = {"high": "red", "medium": "orange", "low": "gray"}


@st.cache_data(ttl=600, max_entries=50, show_spinner=False)
def red_flags_of(ticker: str) -> list[dict] | None:
    """None when no annual report has been read yet."""
    pages = archive.document_pages(ticker, "Annual Report")
    return disclosures.red_flags(pages) if pages else None


def pick_company(pool: dict, key: str):
    """The company switcher used by every per-company tab."""
    if len(pool) == 1:
        return next(iter(pool.values()))
    choice = st.segmented_control("Company", list(pool), default=next(iter(pool)), required=True,
                                  label_visibility="collapsed", key=key)
    return pool[choice or next(iter(pool))]



def result_tabs(results: list[dict], settings: dict, run_settings: dict, run_started: float = 0.0,
                extra: dict[str, str] | None = None, own_page: bool = False) -> dict:
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
    # The worst red flags ride on the tab label, so they are seen without opening it.
    serious = sum(f["severity"] == "high" for r in ok_results if "Filings" in wanted
                  for f in red_flags_of(r["ticker"]) or [])
    labels = [f"{icons[t]} {TAB_LABEL.get(t, t)}" + (f" :red[· {serious} warning{'s' * (serious > 1)}]"
                                   if t == "Filings" and serious else "") for t in wanted + list(extra)]
    tabs = dict(zip(wanted + list(extra), st.tabs(labels)))

    by_ticker = {r["ticker"]: r for r in ok_results}

    # --- Overview ----------------------------------------------------------------
    if "Overview" in tabs:
        with tabs["Overview"]:
            answers = [(a.get("at", 0), a.get("tokens", 0)) for r in results
                       for a in ({"at": r.get("ai_at", 0), "tokens": r.get("ai_tokens", 0)}, r.get("news") or {})
                       if a.get("tokens")]
            if answers and tech(settings):
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
                company_card(r, run_started, show_news=features["news"], header=not own_page)
            for r in failed_results:
                with st.container(border=True):
                    st.markdown(f"**{r['ticker']}** :red-badge[:material/error: Couldn't load]")
                    st.caption(r.get("error_friendly") or "We couldn't get this company's information. Check "
                               "the name is right, then try again in a minute.")
                    if tech(settings):
                        with st.expander("Full error message", icon=":material/code:"):
                            st.code(r.get("error") or "")

    # --- Financials --------------------------------------------------------------
    if "Financials" in tabs:
        with tabs["Financials"]:
            pool = {t: r for t, r in by_ticker.items() if r.get("quarterly_df") is not None}
            if not pool:
                st.caption("No quarterly results were found for these companies.")
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
                    trend_metric(qdf, "EPS", "Profit per share", "₹{:,.2f}", sparkline=spark)

                chart = pd.DataFrame({"Sales": quarterly_series(qdf, "Sales"),
                                      "Net profit": quarterly_series(qdf, "Net Profit")})
                if not chart.empty:
                    with st.container(border=True):
                        st.markdown("**Sales and profit** :gray[· ₹ crore, each quarter]")
                        st.bar_chart(chart.rename_axis("Quarter").reset_index(), x="Quarter",
                                     y=["Sales", "Net profit"], stack=False, sort=False,
                                     color=settings["ui"]["chart_colors"], x_label="", y_label="", height=300)

                with st.container(horizontal=True, vertical_alignment="bottom"):
                    st.markdown("**Quarterly results**", width="stretch")
                    st.download_button("Download", data=qdf.to_csv(index=False).encode("utf-8"),
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
                c1.metric("Measures", len(correlated), border=True)
                c2.metric("Numbers from reports", f"{len(figures):,}", border=True)
                c3.metric("Reports read", core._count_downloaded(r), border=True)
                c4.metric("Double-checked", int(
                    ((correlated["In filings"] > 0) & correlated["Latest quarter"].notna()).sum())
                    if not correlated.empty else 0, border=True,
                    help="Measures where a report and the quarterly results agree there is a value.")

                with st.container(horizontal=True, vertical_alignment="center"):
                    st.markdown("**Every measure, from every source** :gray[· one row each]", width="stretch")
                    named_only = st.toggle(
                        "Well-known measures only", value=True, key=f"named_{r['ticker']}",
                        disabled="Identified" not in correlated.columns,
                        help="Turn off to also see rougher labels the app couldn't match to a known measure.")
                # Saved analyses from an older version may not carry the flag.
                can_filter = not correlated.empty and "Identified" in correlated.columns
                view_corr = correlated[correlated["Identified"]] if named_only and can_filter else correlated
                st.caption(f"{len(view_corr):,} of {len(correlated):,} measures")
                correlated_table(view_corr, settings, key=f"corr_{r['ticker']}")
                st.download_button("Download this table",
                                   data=(view_corr.to_csv(index=False).encode("utf-8")
                                         if not view_corr.empty else b""),
                                   file_name=f"{r['ticker']}_all_numbers.csv", mime="text/csv",
                                   icon=":material/download:", type="tertiary",
                                   disabled=view_corr.empty, key=f"corrcsv_{r['ticker']}")

                if not figures.empty:
                    st.markdown("**Every number, exactly as printed in the reports**")
                    fc1, fc2, fc3 = st.columns([2, 2, 3])
                    sources = fc1.multiselect("Report", sorted(figures["Source"].dropna().unique()),
                                              key=f"fsrc_{r['ticker']}", placeholder="All reports")
                    groups = fc2.multiselect("Topic", sorted(figures["Category"].dropna().unique()),
                                             key=f"fcat_{r['ticker']}", placeholder="All topics")
                    needle = fc3.text_input("Search", key=f"fq_{r['ticker']}",
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
                    st.caption(f"{len(view):,} of {len(figures):,} numbers")
                    figures_table(view, settings, key=f"figs_{r['ticker']}")

                    with st.expander("Report by report", icon=":material/description:"):
                        for source, group in figures.groupby("Source"):
                            st.markdown(f"**{source}** :gray[· {len(group):,} numbers · "
                                        f"pages {int(group['Page'].min())}–{int(group['Page'].max())}]")
                            summary = (group.groupby("Category")["Value"]
                                       .agg(["count", "min", "median", "max"]).reset_index()
                                       .rename(columns={"count": "Numbers", "min": "Low",
                                                        "median": "Median", "max": "High"}))
                            st.dataframe(summary, hide_index=True)

    # --- What changed ------------------------------------------------------------
    if "What changed" in tabs:
        with tabs["What changed"]:
            st.caption("What's different since the last time this company was checked. "
                       "A straight comparison — no AI involved, so nothing here is made up.")
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
                st.info("This is the first check of these companies. Next time, this tab will show "
                        "exactly what moved.", icon=":material/info:")

    # --- Guidance ----------------------------------------------------------------
    if "Guidance" in tabs:
        with tabs["Guidance"]:
            st.caption("What the company's bosses promised when they last spoke to investors — and "
                       "whether the results since then kept those promises.")
            if not by_ticker:
                st.caption("Nothing to show yet.")
            else:
                r = pick_company(by_ticker, "guide_ticker")
                has_transcript = bool(archive.document_text(r["ticker"], "Concall Transcript"))
                with st.container(horizontal=True, vertical_alignment="center"):
                    if st.button("Find their promises", icon=":material/auto_stories:",
                                 disabled=not (has_transcript and run_settings["ai"]["enabled"]),
                                 key=f"harvest_{r['ticker']}", type="primary"):
                        with st.spinner("Reading what they said…"):
                            saved, tokens = core.harvest_guidance(r, run_settings)
                        st.toast(f"{saved} new promise(s) found" + (f" · {tokens:,} tokens" if tech() else ""),
                                 icon=":material/handshake:")
                    if st.button("Check if they kept them", icon=":material/fact_check:",
                                 disabled=not run_settings["ai"]["enabled"], key=f"check_{r['ticker']}"):
                        with st.spinner("Comparing old promises with the latest results…"):
                            checked, tokens = core.check_guidance(r, run_settings)
                        st.toast(f"{checked} promise(s) checked" + (f" · {tokens:,} tokens" if tech() else ""),
                                 icon=":material/fact_check:")
                if not has_transcript:
                    st.info("We don't have a recording of this company's last investor call yet. It's picked "
                            "up automatically when the company publishes one.", icon=":material/info:")
                elif not run_settings["ai"]["enabled"]:
                    st.info("Turn on AI in **Settings** so the app can read the call and find the promises.",
                            icon=":material/info:")

                claims = archive.claims_for(r["ticker"])
                if claims:
                    graded = [c for c in claims if c.get("status")]
                    delivered = sum(1 for c in graded if c["status"] == "Delivered")
                    g1, g2, g3 = st.columns(3)
                    g1.metric("Promises found", len(claims), border=True)
                    g2.metric("Checked so far", len(graded), border=True)
                    g3.metric("Kept", f"{delivered}/{len(graded)}" if graded else "—", border=True,
                              help="How often these bosses have done what they said they would.")
                    for c in claims:
                        with st.container(border=True):
                            with st.container(horizontal=True, vertical_alignment="center"):
                                st.markdown(f"**{c['metric']}** :gray[· said in {c['quarter']} · "
                                            f"by {c['horizon']}]", width="stretch")
                                if c.get("status"):
                                    color, icon = STATUS_BADGE.get(c["status"], ("gray", None))
                                    st.badge(PROMISE_WORDS.get(c["status"], c["status"]), color=color, icon=icon)
                            st.markdown(f"> {c['claim']}")
                            if c.get("why"):
                                st.caption(f":material/fact_check: {c['why']} "
                                           f":gray[· checked against {c.get('checked_against')}]")
                    if st.button("Clear this company's promises", icon=":material/delete:",
                                 type="tertiary", key=f"clr_{r['ticker']}"):
                        archive.delete_claims(r["ticker"])
                        st.rerun()
                elif has_transcript:
                    st.caption("Nothing yet — press **Find their promises** to start.")

    # --- Filings -----------------------------------------------------------------
    if "Filings" in tabs:
        with tabs["Filings"]:
            for r in ok_results:
                with st.container(border=True):
                    st.markdown(f"### {r['ticker']}")
                    filings_scan(r, run_settings)
                    if r.get("downloaded"):
                        st.markdown("**:material/description: Reports the app read**")
                        sources_table(core._sources_of(r), key=f"src_{r['ticker']}")
                    docs = r.get("documents") or []
                    if docs:
                        with st.expander(f"All {len(docs)} documents this company has published",
                                         icon=":material/link:"):
                            st.dataframe(
                                pd.DataFrame(docs).rename(columns={"category": "Type", "text": "Title",
                                                                   "url": "Link"}),
                                hide_index=True,
                                column_config={"Title": st.column_config.TextColumn(width="large"),
                                               "Link": st.column_config.LinkColumn(display_text="Open ↗")})
                    else:
                        st.caption("No documents found for this company.")

    return {name: tabs[name] for name in extra}


def filings_scan(r: dict, run_settings: dict):
    """Red flags from the annual report, then the exchange announcements."""
    flags = red_flags_of(r["ticker"])
    st.markdown("**:material/flag: Warning signs in the annual report**")
    if flags is None:
        st.caption("The annual report hasn't been read yet. It's picked up automatically when available.")
    elif not flags:
        st.caption(":green[No warning signs found] — the auditor had no objections, the accounting rules "
                   "didn't change, there's no jump in loans to related companies, and no big pending claims.")
    for f in flags or []:
        st.markdown(f":{SEVERITY_COLOR[f['severity']]}-badge[{FLAG_WORDS.get(f['kind'], f['kind'])}] "
                    f":gray[page {f['page']}] {md_escape(f['text'])}")
    if flags:
        st.caption(f"Found by searching the first {run_settings['data']['max_pdf_pages']} pages for warning "
                   "phrases — no AI involved. Each one shows its page so you can read it in full.")

    anns = [d for d in r.get("documents") or [] if disclosures.is_announcement(d)][:15]
    if not anns:
        return
    st.markdown("**:material/campaign: Latest company announcements**")
    ai_on = run_settings["ai"]["enabled"]
    for i, d in enumerate(anns[:8]):
        announcement_row(r, d, i, ai_on, run_settings)
    if len(anns) > 8:
        with st.expander(f"{len(anns) - 8} older announcements"):
            for i, d in enumerate(anns[8:], start=8):
                announcement_row(r, d, i, ai_on, run_settings)


def announcement_row(r: dict, d: dict, i: int, ai_on: bool, run_settings: dict):
    """One line per announcement; its three-point summary opens beneath it."""
    label, severity = disclosures.tag_announcement(d["text"])
    key = f"decoded:{d['url']}"
    with st.container():
        with st.container(horizontal=True, vertical_alignment="center"):
            # Screener runs title, date and summary together; the title alone reads cleanly.
            title, *rest = re.split(r"(?<=[A-Za-z)])(?=\d{1,2} [A-Z][a-z]{2}\b)", d["text"], maxsplit=1)
            title = re.sub(r"^Announcement under Regulation 30 \(LODR\)-", "", title)
            day = re.match(r"\d{1,2} [A-Z][a-z]{2}", rest[0]) if rest else None
            when = f" :gray[· {day.group()}]" if day else ""
            st.markdown(f":{SEVERITY_COLOR[severity]}-badge[{label}] [{md_escape(title[:110])}]({d['url']}){when}",
                        width="stretch")
            if st.button("Explain", key=f"dec_{r['ticker']}_{i}", icon=":material/translate:", type="tertiary",
                         disabled=not ai_on, help="Sum it up in three simple points." if ai_on
                         else "Turn on AI in Settings to use this."):
                with st.spinner("Reading the announcement…"):
                    st.session_state[key], tokens = core.decode_announcement(r, d, run_settings)
                st.toast("Done" + (f" · {tokens:,} tokens" if tech() else ""), icon=":material/translate:")
        out = st.session_state.get(key)
        if out and out.get("error"):
            st.caption(f":material/error: {out['error']}")
        elif out:
            with st.container(border=True):
                if out["material"]:
                    st.markdown(":red-badge[Could matter for the share price]" if out["material"] == "Yes"
                                else ":gray-badge[Routine — probably nothing to worry about]")
                st.markdown("\n".join(f"- {md_escape(p)}" for p in out["points"]))


def ordinal(n: float) -> str:
    """42 -> "42nd", 13 -> "13th"."""
    n = round(n)
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"
