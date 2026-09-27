"""Settings: models and keys, scraping limits, appearance, and what is kept."""

import os
from datetime import datetime

import pandas as pd

import streamlit as st

import archive
import core
import llm
import alerts
import portfolio
import settings as cfg
import ui

s = st.session_state.settings

st.title("Settings")
with st.container(horizontal=True, vertical_alignment="center"):
    st.caption("Everything is saved on this computer. Press the **Save** button in each section to keep a change.")
    st.link_button("User guide", core.GUIDE_URL, icon=":material/menu_book:", type="tertiary")

tab_look, tab_ai, tab_note, tab_spend, tab_adv = st.tabs(
    [":material/palette: Layout & look", ":material/auto_awesome: AI", ":material/notifications: Alerts",
     ":material/payments: Spending", ":material/tune: Advanced"])
with tab_adv:
    st.caption("You shouldn't need to change anything here — the defaults work well.")
    tab_data, tab_store, tab_perf = st.tabs(
        [":material/travel_explore: Downloading", ":material/database: Storage & backups",
         ":material/speed: Speed & memory"])

# --------------------------------------------------------------------------
# Models and providers
# --------------------------------------------------------------------------
with tab_ai:
    ai = s["ai"]
    st.info("**The easiest choice is free:** install *Ollama* (a free app) and pick it below — the AI then runs "
            "on this computer and nothing is sent anywhere. The user guide walks you through it, or paste "
            "the setup prompt from the guide into any AI assistant and it will do it for you.",
            icon=":material/lightbulb:")
    st.link_button("Open the user guide", core.GUIDE_URL, icon=":material/menu_book:")
    enabled = st.toggle(
        "Use AI for summaries, explanations and answers", value=ai["enabled"],
        help="Everything else — prices, numbers, warning signs, search — works without it.")
    if not enabled:
        st.caption(":material/info: With this off, summaries, *Explain* and *Ask AI* are switched off.")

    provider_ids = list(cfg.PROVIDERS)
    provider = st.selectbox(
        "Which AI service", provider_ids, index=provider_ids.index(ai["provider"]),
        format_func=lambda p: cfg.PROVIDERS[p][0] + (" — free, runs on this computer" if p == "ollama" else ""),
        help="Ollama is free and private. The others are paid services run by AI companies.")
    label, env_var, default_base, needs_key = cfg.PROVIDERS[provider]

    with st.container(border=True):
        st.markdown(f"**{label}**")
        base = st.text_input(
            "Address (leave as it is)", value=s["providers"][provider]["base_url"] or default_base,
            placeholder=default_base or "https://my-model-server.local/v1",
            help="Point this anywhere that speaks the OpenAI API — LM Studio, vLLM, llama.cpp, "
                 "a colleague's server, or a provider not listed here.")
        env_set = bool(os.environ.get(env_var, "").strip())
        if env_set:
            st.success("Your key is already set up on this computer.",
                       icon=":material/lock:")
            key_input = s["providers"][provider]["api_key"]
        else:
            stored = s["providers"][provider]["api_key"]
            key_input = st.text_input(
                "Key", value=stored, type="password",
                placeholder="paste the key from the AI company's website" if needs_key else "not needed",
                help="A password-like code the AI company gives you when you sign up. It's kept on this computer.")
            if needs_key and not key_input:
                st.warning("This service needs a key. Get one from its website and paste it above.",
                           icon=":material/key_off:")

        if needs_key or any(cfg.prices(s, provider)):
            st.markdown(":gray[Optional: what this service charges, copied from its pricing page (price per "
                        "million 'tokens' — roughly words). Fill these in to see your spending in money.]")
            p1, p2 = st.columns(2)
            price_in = p1.number_input("Price for what you send", 0.0, 10000.0,
                                       float(s["providers"][provider].get("price_in") or 0.0), 0.10,
                                       format="%.2f")
            price_out = p2.number_input("Price for what it writes", 0.0, 10000.0,
                                        float(s["providers"][provider].get("price_out") or 0.0), 0.10,
                                        format="%.2f")
        else:
            price_in = price_out = 0.0
            st.caption(":material/check_circle: This runs on your own computer, so it's free.")

        if st.button("Check it works", icon=":material/link:"):
            llm.list_models.clear()
        found = llm.list_models(provider, cfg.base_url({**s, "providers": {**s["providers"],
                                provider: {"base_url": base, "api_key": key_input}}}, provider),
                                os.environ.get(env_var, "").strip() or key_input)
        if found:
            st.success(f"Working — {len(found)} AI model(s) ready.", icon=":material/check_circle:")
        else:
            st.error("Couldn't connect.", icon=":material/error:")
            if provider == "ollama":
                st.caption("Make sure the Ollama app is open, then press **Check it works** again. "
                           "The user guide shows each step.")

    model_options = list(found) or ([ai["model"]] if ai["model"] else [])
    model = st.selectbox(
        "Which AI model", model_options, format_func=lambda m: found.get(m, m),
        index=model_options.index(ai["model"]) if ai["model"] in model_options
        else (0 if model_options else None),
        disabled=not model_options, placeholder="None found yet — check it works above",
        help="Bigger models write better but are slower (and cost more on paid services).")

    with st.expander("Advanced: answer lengths", icon=":material/tune:"):
        st.caption("How long each kind of AI answer may be (in tokens — roughly words). Short limits keep "
                   "answers tidy and costs low.")
        g1, g2, g3 = st.columns(3)
        tokens_analysis = g1.number_input("Company analysis", 60, 1000, int(ai["tokens_analysis"]), 10)
        tokens_news = g2.number_input("News digest", 40, 600, int(ai["tokens_news"]), 10)
        tokens_error = g3.number_input("Error explanation", 20, 300, int(ai["tokens_error"]), 10)
        g4, g5, g6 = st.columns(3)
        tokens_guidance = g4.number_input("Transcript reading", 80, 1200, int(ai["tokens_guidance"]), 20)
        tokens_synthesis = g5.number_input("Archive answers", 80, 1200, int(ai["tokens_synthesis"]), 20)
        temperature = g6.slider("Temperature", 0.0, 1.0, float(ai["temperature"]), 0.05,
                                help="Low keeps answers close to the data. 0.2 is a good default.")
        g7, g8, g9 = st.columns(3)
        tokens_chat = g9.number_input("Chat replies (Ask AI)", 200, 8000, int(ai.get("tokens_chat", 1200)), 100,
                                      help="Ask AI streams its answer, so a longer limit costs nothing until used.")
        num_ctx = g7.number_input("Ollama context window", 512, 32768, int(ai["num_ctx"]), 512,
                                  help="Prompts here stay under ~700 tokens; a bigger window only "
                                       "costs RAM and load time.")
        timeout = g8.number_input("Request timeout (seconds)", 10, 600, int(ai["timeout"]), 5)

    if st.button("Save AI settings", type="primary", icon=":material/save:"):
        s["ai"].update(enabled=enabled, provider=provider, model=model or "",
                       temperature=float(temperature), num_ctx=int(num_ctx), timeout=int(timeout),
                       tokens_analysis=int(tokens_analysis), tokens_news=int(tokens_news),
                       tokens_error=int(tokens_error), tokens_guidance=int(tokens_guidance),
                       tokens_synthesis=int(tokens_synthesis), tokens_chat=int(tokens_chat))
        s["providers"][provider] = {"base_url": base.strip(),
                                    "api_key": "" if env_set else key_input.strip(),
                                    "price_in": float(price_in), "price_out": float(price_out)}
        cfg.save(s)
        st.toast("AI settings saved.", icon=":material/check_circle:")

# --------------------------------------------------------------------------
# Spending
# --------------------------------------------------------------------------
with tab_spend:
    b = s["budget"]
    state = llm.budget_state(s)
    provider_label = cfg.PROVIDERS[s["ai"]["provider"]][0]

    if not state["paid"]:
        st.success(f"You're using **{provider_label}**, which is free. The limits below only matter "
                   "if you switch to a paid AI service.", icon=":material/savings:")
    month = archive.usage_since(cfg.month_start())
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Spent this month", cfg.money(s, month["cost"]), border=True,
              help="Based on the prices you entered for each provider.")
    s2.metric("Words used (tokens)", f"{month['tokens']:,}", border=True)
    s3.metric("AI requests", f"{month['calls']:,}", border=True)
    s4.metric("Remaining", cfg.money(s, max(0.0, state["cost_cap"] - month["cost"]))
              if state["cost_cap"] else "No cap", border=True)

    if state["cost_cap"]:
        st.progress(min(1.0, month["cost"] / state["cost_cap"]),
                    text=(f"{cfg.money(s, month['cost'])} of {cfg.money(s, state['cost_cap'])} "
                          f"this month ({state['cost_pct']:.0f}%)").replace("$", r"\$"))  # $…$ is LaTeX
    if state["token_cap"]:
        st.progress(min(1.0, month["tokens"] / state["token_cap"]),
                    text=f"{month['tokens']:,} of {state['token_cap']:,} tokens "
                         f"({state['token_pct']:.0f}%)")

    st.markdown("**Limits**")
    st.caption("The app stops before going over a limit, so you can never overspend by accident. "
               "0 means no limit.")
    enabled_budget = st.toggle("Use these limits", value=b["enabled"])
    c1, c2, c3 = st.columns(3)
    currency = c1.selectbox("Currency", list(cfg.CURRENCIES),
                            index=list(cfg.CURRENCIES).index(b.get("currency", "USD"))
                            if b.get("currency") in cfg.CURRENCIES else 0,
                            help="Only a label — enter prices in the same currency.")
    monthly_cost = c2.number_input(f"Monthly limit ({cfg.CURRENCIES.get(currency, '')})",
                                   0.0, 100000.0, float(b["monthly_cost"]), 1.0, format="%.2f")
    monthly_tokens = c3.number_input("Monthly limit (tokens)", 0, 100_000_000,
                                     int(b["monthly_tokens"]), 10_000)
    c4, c5 = st.columns(2)
    per_run = c4.number_input("Limit per analysis (tokens)", 0, 10_000_000, int(b["max_tokens_per_run"]), 1_000,
                              help="Stops one big job — like analysing 200 companies — using up a month's budget.")
    warn_at = c5.slider("Warn me at", 10, 100, int(b["warn_at"]), 5,
                        help="Percentage of a limit at which the app starts warning you.")

    if st.button("Save spending limits", type="primary", icon=":material/save:"):
        b.update(enabled=enabled_budget, currency=currency, monthly_cost=float(monthly_cost),
                 monthly_tokens=int(monthly_tokens), max_tokens_per_run=int(per_run),
                 warn_at=int(warn_at))
        cfg.save(s)
        st.toast("Spending limits saved.", icon=":material/check_circle:")

    breakdown = archive.usage_breakdown(cfg.month_start())
    if breakdown:
        st.markdown("**Where it went this month**")
        st.dataframe(
            pd.DataFrame(breakdown).rename(columns={
                "provider": "Provider", "model": "Model", "kind": "Used for", "calls": "Calls",
                "tokens_in": "Tokens in", "tokens_out": "Tokens out", "cost": "Cost"}),
            hide_index=True,
            column_config={"Cost": st.column_config.NumberColumn(format="%.4f"),
                           "Provider": st.column_config.TextColumn(width="small"),
                           "Calls": st.column_config.NumberColumn(width="small")})
        daily = archive.usage_daily(cfg.month_start())
        if len(daily) > 1:
            chart = pd.DataFrame(daily).set_index("day")
            st.bar_chart(chart[["cost"]] if state["cost_cap"] or month["cost"] else chart[["tokens"]],
                         color=s["ui"]["chart_colors"][0], x_label="", y_label="", height=220)
        if st.button("Clear the spending log", icon=":material/delete_sweep:", type="tertiary"):
            archive.clear_usage()
            st.rerun()
    else:
        st.caption("No AI used this month.")

# --------------------------------------------------------------------------
# Data and scraping
# --------------------------------------------------------------------------
with tab_data:
    d = s["data"]
    st.markdown("**What to read for each company**")
    default_tickers = st.text_input("Companies filled in on *Research*", value=d["default_tickers"])
    categories = st.pills("Reports to read", core.WANTED_CATEGORIES, selection_mode="multi",
                          default=d["categories"])
    timeframe = st.selectbox("How far back", list(core.TIMEFRAME_OPTIONS),
                             index=list(core.TIMEFRAME_OPTIONS).index(d["timeframe"])
                             if d["timeframe"] in core.TIMEFRAME_OPTIONS else 0)

    st.markdown("**Downloads**")
    c1, c2, c3 = st.columns(3)
    max_file_mb = c1.number_input("Largest report to download (MB)", 1, 200, int(d["max_file_mb"]))
    max_pages = c2.number_input("Pages to read per report", 10, 1000, int(d["max_pdf_pages"]),
                                help="Raise this to catch warning signs deep in long annual reports. "
                                     "Each extra 75 pages adds about 2 seconds, once per report.")
    workers = c3.number_input("Parallel downloads", 1, 12, int(d["doc_workers"]),
                              help="Filings download at the same time. 4 is plenty; higher mostly "
                                   "annoys the servers you are downloading from.")
    c4, c5, c6 = st.columns(3)
    request_timeout = c4.number_input("Page timeout (s)", 3, 60, int(d["request_timeout"]))
    download_timeout = c5.number_input("Download timeout (s)", 5, 300, int(d["download_timeout"]))
    delay = c6.slider("Pause between companies (s)", 0.0, 5.0,
                      (float(d["delay_min"]), float(d["delay_max"])), 0.5,
                      help="Politeness. Leave some gap — the sites here are free and public.")

    st.markdown("**News**")
    n1, n2, n3 = st.columns(3)
    news_days = n1.number_input("Headline age (days)", 1, 90, int(d["news_days"]))
    news_items = n2.number_input("Headlines per company", 1, 30, int(d["news_items"]))
    news_ttl = n3.number_input("News cache (min)", 1, 1440, int(d["news_cache_ttl_min"]))
    cache_ttl = st.number_input("Result cache (min)", 1, 1440, int(d["cache_ttl_min"]),
                                help="How long a scraped page counts as fresh. Both cache settings "
                                     "take effect when the app restarts; the button below clears "
                                     "what is cached right now.")

    st.markdown("**Features**")
    f = s["features"]
    FEATURE_NAMES = [("News", "news"), ("Every number", "detailed_numbers"),
                     ("Document search", "archive"), ("AI help in document search", "archive_ai"),
                     ("Keep past analyses", "history"), ("Promise tracking", "guidance")]
    picked = st.pills(
        "Turn parts of the app on or off", [name for name, _ in FEATURE_NAMES],
        selection_mode="multi",
        default=[name for name, key in FEATURE_NAMES if f.get(key, True)])
    st.caption("**AI help in document search** lets the AI find better matches. Off, it's a plain word search.")

    if st.button("Save data settings", type="primary", icon=":material/save:"):
        d.update(default_tickers=default_tickers, categories=list(categories) or core.WANTED_CATEGORIES,
                 timeframe=timeframe, max_file_mb=int(max_file_mb), max_pdf_pages=int(max_pages),
                 doc_workers=int(workers), request_timeout=int(request_timeout),
                 download_timeout=int(download_timeout), delay_min=float(delay[0]),
                 delay_max=float(delay[1]), news_days=int(news_days), news_items=int(news_items),
                 news_cache_ttl_min=int(news_ttl), cache_ttl_min=int(cache_ttl))
        f.update(**{key: name in picked for name, key in FEATURE_NAMES})
        cfg.save(s)
        core.apply_settings(s)
        st.toast("Saved.", icon=":material/check_circle:")

# --------------------------------------------------------------------------
# Appearance
# --------------------------------------------------------------------------
with tab_look:
    u = s["ui"]
    st.markdown("**Layout**")
    l1, l2 = st.columns(2)
    nav = l1.segmented_control("Menu", ["top", "sidebar"], default=u["nav"], required=True,
                               format_func={"top": "Across the top", "sidebar": "Down the side"}.get)
    titles = [title for _, title, _, _ in cfg.PAGES]
    home = l2.selectbox("Open the app on", titles, index=titles.index(u["home"]) if u["home"] in titles else 0)
    hidden = st.multiselect("Hide these pages from the menu", [t for t in titles if t not in cfg.ALWAYS_SHOWN],
                            default=[t for t in u["hidden_pages"] if t in titles],
                            placeholder="Show every page", help="Home and Settings are always shown.")
    tabs_picked = st.pills("Sections shown on a company's page", cfg.ALL_TABS, selection_mode="multi",
                           default=[t for t in u["tabs"] if t in cfg.ALL_TABS], format_func=ui.TAB_LABEL.get)
    chart_style = st.segmented_control("Price chart", ["Detailed", "Simple"], default=u["chart_style"],
                                       required=True, help="Detailed shows daily candles, trading volume and "
                                                           "50- and 200-day averages. Simple shows one price line.")
    tech_details = st.toggle("Show more details", value=u["tech_details"],
                             help="Everything the app knows: extra counts and figures, the original financial terms, "
                                  "AI costs and model names, file locations and full error messages.")

    st.markdown("**Look**")
    custom_style = st.toggle("Use my colour and text size below", value=u["custom_style"],
                             help="Turn off to go back to the standard look.")
    a1, a2 = st.columns(2)
    accent_name = a1.selectbox("Colour", list(cfg.ACCENTS),
                               index=list(cfg.ACCENTS.values()).index(u["accent"])
                               if u["accent"] in cfg.ACCENTS.values() else 0)
    accent = a2.color_picker("or pick your own", value=cfg.ACCENTS.get(accent_name, u["accent"]))
    b1, b2 = st.columns(2)
    font_scale = b1.slider("Text size", 0.8, 1.4, float(u["font_scale"]), 0.05)
    density = b2.select_slider("Spacing", list(cfg.DENSITIES), value=u["density"])

    with st.expander("Tables and charts", icon=":material/table_chart:"):
        c1, c2, c3 = st.columns(3)
        table_height = c1.number_input("Table height (pixels)", 200, 1200, int(u["table_height"]), 20)
        decimals = c2.number_input("Decimal places", 0, 6, int(u["decimals"]))
        sparklines = c3.toggle("Small trend charts on numbers", value=u["sparklines"])
        d1, d2 = st.columns(2)
        chart_one = d1.color_picker("Chart colour 1", value=u["chart_colors"][0])
        chart_two = d2.color_picker("Chart colour 2", value=u["chart_colors"][1])
        show_context = st.toggle("Show the sentence each number came from", value=u["show_context_column"],
                                 help="Handy for checking a number is real.")

    if st.button("Save layout & look", type="primary", icon=":material/save:"):
        u.update(nav=nav, home=home, hidden_pages=list(hidden), chart_style=chart_style, tech_details=tech_details,
                 custom_style=custom_style, accent=accent, font_scale=float(font_scale),
                 density=density, table_height=int(table_height), decimals=int(decimals),
                 sparklines=sparklines, chart_colors=[chart_one, chart_two],
                 show_context_column=show_context, tabs=list(tabs_picked) or cfg.ALL_TABS)
        cfg.save(s)
        st.rerun()

# --------------------------------------------------------------------------
# Storage, retention and history
# --------------------------------------------------------------------------
with tab_store:
    stats = archive.stats()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Saved analyses", f"{stats['runs']:,}", border=True)
    m2.metric("Filings on disk", f"{stats['pdf_count']:,}", f"{stats['pdf_mb']:.0f} MB",
              delta_color="off", border=True)
    m3.metric("Indexed pages", f"{stats['pages']:,}", border=True)
    m4.metric("Archive database", f"{stats['db_mb']:.1f} MB", border=True)
    if stats["oldest_run"]:
        st.caption(f"Oldest saved analysis: "
                   f"{datetime.fromtimestamp(stats['oldest_run']).strftime('%d %B %Y')}.")

    st.markdown("**How long to keep things**")
    st.caption("Set any of these to 0 to keep that kind of data forever.")
    st_ = s["storage"]
    r1, r2, r3 = st.columns(3)
    keep_runs = r1.number_input("Saved analyses (days)", 0, 3650, int(st_["keep_runs_days"]),
                                help="What the History page can show you.")
    keep_docs = r2.number_input("Filing PDFs (days)", 0, 3650, int(st_["keep_documents_days"]),
                                help="The downloaded files. Deleting them frees the most space; "
                                     "they are re-downloaded on demand.")
    keep_text = r3.number_input("Searchable text (days)", 0, 3650, int(st_["keep_text_days"]),
                                help="The transcript text behind the Archive page. Keep this longer "
                                     "than the PDFs — it is small and it is what search reads.")
    r4, r5 = st.columns(2)
    max_runs = r4.number_input("Most analyses kept per company", 0, 1000,
                               int(st_["max_runs_per_ticker"]),
                               help="A cap that applies whatever the age limit says. 0 means no cap.")
    auto_purge = r5.toggle("Apply these limits automatically at startup", value=st_["auto_purge"])

    if st.button("Save retention settings", type="primary", icon=":material/save:"):
        st_.update(keep_runs_days=int(keep_runs), keep_documents_days=int(keep_docs),
                   keep_text_days=int(keep_text), max_runs_per_ticker=int(max_runs),
                   auto_purge=auto_purge)
        cfg.save(s)
        st.toast("Retention settings saved.", icon=":material/check_circle:")

    st.markdown("**Housekeeping**")
    h1, h2 = st.columns(2)
    if h1.button("Apply retention limits now", icon=":material/mop:", width="stretch"):
        removed = archive.purge(s["storage"])
        st.toast(f"Removed {removed['runs']} analysis/analyses, {removed['pdfs']} PDF(s) and "
                 f"{removed['documents']} indexed document(s).", icon=":material/check_circle:")
        st.rerun()
    if h2.button("Clear cached results", icon=":material/cached:", width="stretch",
                 help="Forces the next run to re-fetch pages and news. Downloaded filings and "
                      "saved analyses are untouched."):
        st.cache_data.clear()
        st.toast("Caches cleared.", icon=":material/check_circle:")

    st.markdown("**Back up or move your data**")
    st.caption(f"Everything lives in `{cfg.DATA_DIR}` — outside the app folder, so updating the app never "
               "touches it. A backup is one zip with your settings, portfolios, saved analyses and search "
               "index; import it here, on this or another computer, to get everything back.")
    b1, b2 = st.columns(2)
    with b1.container(border=True):
        keys = st.checkbox("Include API keys", help="Off by default: a backup file is easy to share by accident.")
        pdfs = st.checkbox("Include filing PDFs", help="Much bigger. Without them, filings are re-downloaded "
                                                       "when needed; the extracted numbers and text are kept either way.")
        if st.button("Prepare backup", icon=":material/archive:", width="stretch"):
            st.session_state.backup_zip = archive.export_bundle(keys, pdfs)
        if st.session_state.get("backup_zip"):
            st.download_button("Download backup", st.session_state.backup_zip, type="primary", width="stretch",
                               file_name=f"stock_data_hub_{datetime.now():%Y%m%d_%H%M}.zip", mime="application/zip",
                               icon=":material/download:", on_click=lambda: st.session_state.pop("backup_zip"))
    with b2.container(border=True):
        backup = st.file_uploader("Restore from a backup", type=["zip"])
        sure = st.checkbox("Replace my current data with this backup")
        if st.button("Restore", icon=":material/settings_backup_restore:", width="stretch",
                     disabled=not (backup and sure)):
            try:
                msg = archive.import_bundle(backup)
                st.session_state.settings = cfg.load()
                st.cache_data.clear()
                st.toast(msg, icon=":material/check_circle:")
            except ValueError as exc:
                st.error(str(exc), icon=":material/error:")

    with st.expander("Delete everything", icon=":material/warning:"):
        st.markdown("This removes every saved analysis, every indexed page and every commitment on "
                    "file. The downloaded PDFs stay on disk; retention handles those. "
                    "**It cannot be undone.**")
        confirm = st.checkbox("I understand this deletes all saved history")
        if st.button("Delete all saved data", type="primary", icon=":material/delete_forever:",
                     disabled=not confirm):
            archive.clear_all()
            st.toast("Saved data deleted.", icon=":material/delete:")
            st.rerun()

    with st.expander("Reset all settings to defaults", icon=":material/restart_alt:"):
        st.caption("Settings only — your saved analyses and downloads are not touched. "
                   "API keys saved in the file are removed.")
        if st.button("Reset settings", icon=":material/restart_alt:"):
            cfg.reset()
            st.session_state.settings = cfg.load()
            st.rerun()


with tab_look:
    st.markdown("**Portfolio**")
    p = portfolio.prefs(s)
    track = st.toggle("Keep track of how many shares I own and what I paid", value=p["track_positions"],
                      help="Off just follows the stocks, without money. Either way, it stays on this computer.")
    l1, l2, l3 = st.columns(3)
    hours = l1.number_input("Check my stocks again every (hours)", 0, 720, int(p["refresh_hours"]),
                            help="The app re-checks each stock in the background, one at a time. 0 = only once.")
    max_stock = l2.number_input("Warn me if one stock is over (%)", 5, 100, int(p["max_stock_pct"]))
    max_sector = l3.number_input("Warn me if one industry is over (%)", 10, 100, int(p["max_sector_pct"]))
    if st.button("Save portfolio settings", type="primary", icon=":material/save:"):
        s["portfolio"] = {**p, "refresh_hours": int(hours), "track_positions": track,
                          "max_stock_pct": int(max_stock), "max_sector_pct": int(max_sector)}
        cfg.save(s)
        portfolio.wake()
        st.toast("Saved.", icon=":material/check_circle:")


with tab_note:
    n = alerts.prefs(s)
    st.markdown("**How should the app tell you?**")
    channels = st.pills("Channels", ["mac", "email"], selection_mode="multi", default=n["channels"],
                        format_func={"mac": ":material/desktop_mac: Pop-up on this Mac",
                                     "email": ":material/mail: Email"}.get, label_visibility="collapsed")
    st.caption("You'll always find them under **Portfolio → Alerts** too. Several at once arrive as one message.")
    if "email" in (channels or []):
        with st.container(border=True):
            st.markdown(":material/mail: **Email, sent from your own Google account**")
            st.caption("A one-time setup — the user guide shows each step, or ask any AI assistant to do it with "
                       "the setup prompt in the guide. Emails only ever go to you.")
            url = st.text_input("Web address from the setup", value=n["email_url"],
                                placeholder="https://script.google.com/macros/s/…/exec")
            token = st.text_input("Password from the setup", value=n["email_token"], type="password")
    else:
        url, token = n["email_url"], n["email_token"]
    st.markdown("**Tell me when…**")
    k1, k2, k3, k4 = st.columns(4)
    verdict = k1.toggle("The AI's view changes", n["verdict"])
    filing = k2.toggle("A company publishes something", n["filing"],
                       help="Including important announcements and warning signs in a new annual report.")
    guidance = k3.toggle("A promise isn't kept", n["guidance"], help="Needs AI turned on.")
    price = k4.toggle("A price I'm watching is reached", n["price"],
                      help="Set prices to watch on each company's page.")
    st.markdown("**Weekly summary**")
    d1, d2 = st.columns(2)
    digest = d1.toggle("Make a summary of my week", n["digest"])
    day = d2.selectbox("On", range(7), index=int(n["digest_day"]), format_func=alerts.DAYS.__getitem__,
                       disabled=not digest)
    b1, b2 = st.columns(2)
    if b1.button("Save alert settings", type="primary", icon=":material/save:", width="stretch"):
        s["notify"] = {**n, "channels": channels or [], "email_url": url.strip(), "email_token": token.strip(),
                       "verdict": verdict, "filing": filing, "guidance": guidance, "price": price,
                       "digest": digest, "digest_day": int(day)}
        cfg.save(s)
        st.toast("Saved.", icon=":material/check_circle:")
    if b2.button("Send me a test", icon=":material/send:", width="stretch", disabled=not channels):
        test = {**s, "notify": {**n, "channels": channels, "email_url": url.strip(), "email_token": token.strip()}}
        problems = alerts.notify("Test from Stock Data Hub", "Notifications are working.", test,
                                 alerts.email_page("Notifications are working",
                                                   "This is a test from your Indian Stock Data Hub. Alerts and your "
                                                   "weekly digest will arrive like this.", ""))
        if problems:
            st.error("  \n".join(problems), icon=":material/error:")
        else:
            st.success("Sent — check your notifications" + (" and inbox." if "email" in channels else "."),
                       icon=":material/check_circle:")

# --------------------------------------------------------------------------
# Performance: how heavy the app is allowed to be
# --------------------------------------------------------------------------
with tab_perf:
    import portfolio as pf
    import runtime

    p = pf.perf(s)
    st.markdown("**What the app does when you are not looking at it**")
    st.caption("This app is meant to sit in the background. Everything here trades speed for "
               "lightness — the defaults favour speed.")

    used = runtime.live_memory_mb() or runtime.memory_mb()
    live = pf.STATUS["running"]
    mem1, mem2, mem3 = st.columns(3)
    mem1.metric("Memory in use", f"{used:,.0f} MB" if used else "—", border=True,
                help="Memory in use right now by this app and any filing it is parsing. The target "
                     "this app is built to is under 500 MB while idle.")
    mem2.metric("Background work", "Running" if live else ("On" if p["background_refresh"] else "Off"),
                f"analysing {live}" if live else None, border=True)
    mem3.metric("Stocks tracked", len(pf.tracked()), border=True)
    if used and used > 500:
        st.warning(f"This session is using {used:,.0f} MB. Turning off chart preloading and lowering "
                   "the cache sizes below will bring it down after a restart.",
                   icon=":material/memory_alt:")

    lean = st.toggle(
        "Lean mode", value=not p["warm_charts"] and p["chart_cache"] <= 4,
        help="One switch for a light footprint: no chart preloading and small caches. Charts take "
             "a few seconds longer the first time you open one.")
    background = st.toggle(
        "Keep working in the background", value=p["background_refresh"],
        help="Re-analyses held stocks on the schedule below, checks price alerts and builds the "
             "weekly digest. Off, nothing happens unless you open a page and ask for it.")
    warm = st.toggle(
        "Preload the charting library at startup", value=p["warm_charts"], disabled=lean,
        help="Costs about 30 MB of memory and makes the first chart instant instead of taking a "
             "few seconds.")
    rank = st.toggle(
        "Let Ask AI rank passages before answering", value=p.get("rank_passages", True),
        help="With a large archive, the model picks which retrieved passages actually answer your "
             "question — 'share capital' is not capital expenditure. It costs one extra request "
             "per question, which is worth a few seconds locally and can be slower on a hosted "
             "provider. Off, the best keyword matches are used as they come.")

    st.markdown("**How much to keep in memory**")
    st.caption("Lower numbers use less memory and cause more re-fetching. These take effect when "
               "the app restarts; the button below clears what is held right now.")
    c1, c2, c3 = st.columns(3)
    chart_cache = c1.number_input("Price histories", 1, 60, int(p["chart_cache"]), disabled=lean,
                                  help="Years of daily bars each — the largest thing the app holds.")
    quote_cache = c2.number_input("Latest quotes", 10, 2000, int(p["quote_cache"]), step=10)
    search_cache = c3.number_input("Company searches", 50, 5000, int(p["search_cache"]), step=50)

    r1, r2 = st.columns(2)
    refresh_hours = r1.number_input("Re-analyse each held stock every (hours)", 0, 720,
                                    int(pf.prefs(s)["refresh_hours"]), disabled=not background,
                                    help="0 analyses a stock once, when you add it, and never again.")
    workers = r2.number_input("Filings downloaded at once", 1, 16, int(s["data"]["doc_workers"]),
                              help="More is faster but heavier, and less polite to the source site.")

    st.markdown("**Limits for analysis work**")
    st.caption(f"Filings are parsed in separate, low-priority processes, so pages stay quick while an "
               f"analysis runs. These ceilings slow the analysis down instead of the computer "
               f"(this one has {os.cpu_count() or 1} CPU cores). They apply to the next filing parsed.")
    l1, l2 = st.columns(2)
    cpu_limit = l1.slider("CPU the analysis may use", 5, 100, int(p["cpu_limit_pct"]), step=5,
                          format="%d%%",
                          help="Share of all CPU cores. Also caps how many threads a local Ollama "
                               "model may use; at 100% Ollama picks for itself.")
    ram_limit = l2.number_input("Memory limit (MB)", 0, 65536, int(p["ram_limit_mb"]), step=256,
                                help="This app plus its parse workers. Over it, new filings wait "
                                     "for memory to free up. 0 = no limit.")

    if st.button("Save performance settings", type="primary", icon=":material/save:"):
        s["performance"] = {
            "background_refresh": background,
            "warm_charts": False if lean else warm,
            "chart_cache": 4 if lean else int(chart_cache),
            "quote_cache": int(quote_cache), "search_cache": int(search_cache),
            "rank_passages": rank, "cpu_limit_pct": int(cpu_limit), "ram_limit_mb": int(ram_limit)}
        s["portfolio"] = {**s["portfolio"], "refresh_hours": int(refresh_hours)}
        s["data"]["doc_workers"] = int(workers)
        cfg.save(s)
        core.apply_settings(s)
        st.toast("Performance settings saved — restart the app for the memory limits to apply.",
                 icon=":material/check_circle:")

    if st.button("Free memory now", icon=":material/cleaning_services:",
                 help="Drops everything cached in memory. Nothing is lost: it is all re-fetched "
                      "or recomputed when next needed."):
        st.cache_data.clear()
        freed = runtime.memory_mb()
        st.toast(f"Caches cleared. Now using {freed:,.0f} MB." if freed else "Caches cleared.",
                 icon=":material/check_circle:")

    # --- Start with the computer ---------------------------------------------
    st.divider()
    st.markdown("**Start automatically**")
    installed, where = runtime.status()
    st.caption(f"Launch the app in the background whenever you log in, so alerts, the weekly digest "
               f"and background re-analysis keep running without you starting it. "
               f"{runtime.DESCRIPTION}")
    a1, a2 = st.columns([1, 2], vertical_alignment="center")
    want = a1.toggle("Start when I log in", value=installed, key="autostart_toggle")
    if want != installed:
        try:
            if want:
                runtime.enable()
                st.success(f"Set up. The app will start on your next login, and is written to "
                           f"`{where}`.", icon=":material/check_circle:")
            else:
                runtime.disable()
                st.info("Removed. The app will no longer start on its own.", icon=":material/info:")
        except Exception as exc:
            st.error(f"Could not change that: {exc}", icon=":material/error:")
    if installed:
        a2.caption(f"Installed at `{where}`")
