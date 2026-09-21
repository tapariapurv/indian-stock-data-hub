"""Settings: models and keys, scraping limits, appearance, and what is kept."""

import os
from datetime import datetime

import pandas as pd

import streamlit as st

import archive
import core
import llm
import portfolio
import settings as cfg

s = st.session_state.settings

st.caption(":material/settings: CONFIGURATION")
st.title("Settings")
st.markdown(":gray[Everything here is stored in `settings.json` next to the app. API keys are saved with "
            "owner-only permissions, and an environment variable always wins over a saved key — so on a "
            "shared machine you need never write one down.]")

tab_ai, tab_spend, tab_data, tab_look, tab_store = st.tabs(
    [":material/memory: Models & keys", ":material/payments: Spending",
     ":material/travel_explore: Data & scraping", ":material/palette: Appearance",
     ":material/database: Storage & history"])

# --------------------------------------------------------------------------
# Models and providers
# --------------------------------------------------------------------------
with tab_ai:
    ai = s["ai"]
    st.markdown("**Use AI**")
    enabled = st.toggle(
        "Use a model for verdicts, analyses, news digests, transcripts and archive answers",
        value=ai["enabled"],
        help="Everything else — scraping, extraction, exports, keyword search — works without one.")
    if not enabled:
        st.caption(":material/info: With this off, every AI feature in the app is disabled, "
                   "including the answers on the Archive page.")

    provider_ids = list(cfg.PROVIDERS)
    provider = st.selectbox(
        "Provider", provider_ids, index=provider_ids.index(ai["provider"]),
        format_func=lambda p: cfg.PROVIDERS[p][0],
        help="Ollama runs on your own machine and costs nothing. The rest are paid APIs and send "
             "your scraped data to that company.")
    label, env_var, default_base, needs_key = cfg.PROVIDERS[provider]

    with st.container(border=True):
        st.markdown(f"**{label}**")
        base = st.text_input(
            "Server address", value=s["providers"][provider]["base_url"] or default_base,
            placeholder=default_base or "https://my-model-server.local/v1",
            help="Point this anywhere that speaks the OpenAI API — LM Studio, vLLM, llama.cpp, "
                 "a colleague's server, or a provider not listed here.")
        env_set = bool(os.environ.get(env_var, "").strip())
        if env_set:
            st.success(f"Using the `{env_var}` environment variable. Nothing is stored on disk.",
                       icon=":material/lock:")
            key_input = s["providers"][provider]["api_key"]
        else:
            stored = s["providers"][provider]["api_key"]
            key_input = st.text_input(
                "API key", value=stored, type="password",
                placeholder="paste your key" if needs_key else "not needed for a local server",
                help=f"Or set the `{env_var}` environment variable instead and leave this empty.")
            if needs_key and not key_input:
                st.warning("This provider needs a key before it can list models.",
                           icon=":material/key_off:")

        if needs_key or any(cfg.prices(s, provider)):
            st.markdown(":gray[What this provider charges, per million tokens. Copy the numbers from "
                        "its pricing page — the app will not guess, and leaving them at 0 simply "
                        "means spending is tracked in tokens rather than money.]")
            p1, p2 = st.columns(2)
            price_in = p1.number_input("Input price / 1M tokens", 0.0, 10000.0,
                                       float(s["providers"][provider].get("price_in") or 0.0), 0.10,
                                       format="%.2f")
            price_out = p2.number_input("Output price / 1M tokens", 0.0, 10000.0,
                                        float(s["providers"][provider].get("price_out") or 0.0), 0.10,
                                        format="%.2f")
        else:
            price_in = price_out = 0.0
            st.caption(":material/check_circle: This provider runs on your own hardware, so it is "
                       "free and never counted against a budget.")

        if st.button("Test connection", icon=":material/link:"):
            llm.list_models.clear()
        found = llm.list_models(provider, cfg.base_url({**s, "providers": {**s["providers"],
                                provider: {"base_url": base, "api_key": key_input}}}, provider),
                                os.environ.get(env_var, "").strip() or key_input)
        if found:
            st.success(f"Connected · {len(found)} model(s) available", icon=":material/check_circle:")
        else:
            st.error("Could not reach that provider, or it returned no models.",
                     icon=":material/error:")
            if provider == "ollama":
                st.caption("Start Ollama and check http://localhost:11434 says \"Ollama is running\". "
                           "Chapter 4 of the user guide has the full walkthrough.")

    model_options = list(found) or ([ai["model"]] if ai["model"] else [])
    model = st.selectbox(
        "Model", model_options, format_func=lambda m: found.get(m, m),
        index=model_options.index(ai["model"]) if ai["model"] in model_options
        else (0 if model_options else None),
        disabled=not model_options, placeholder="No models available",
        help="Bigger models write better analysis; smaller ones are faster and cheaper. "
             "Local models also need the RAM.")

    with st.expander("Generation limits", icon=":material/tune:"):
        st.caption("Reply lengths, in tokens. These are the only thing standing between a tidy "
                   "three-sentence answer and a provider bill, so they are deliberately tight.")
        g1, g2, g3 = st.columns(3)
        tokens_analysis = g1.number_input("Company analysis", 60, 1000, int(ai["tokens_analysis"]), 10)
        tokens_news = g2.number_input("News digest", 40, 600, int(ai["tokens_news"]), 10)
        tokens_error = g3.number_input("Error explanation", 20, 300, int(ai["tokens_error"]), 10)
        g4, g5, g6 = st.columns(3)
        tokens_guidance = g4.number_input("Transcript reading", 80, 1200, int(ai["tokens_guidance"]), 20)
        tokens_synthesis = g5.number_input("Archive answers", 80, 1200, int(ai["tokens_synthesis"]), 20)
        temperature = g6.slider("Temperature", 0.0, 1.0, float(ai["temperature"]), 0.05,
                                help="Low keeps answers close to the data. 0.2 is a good default.")
        g7, g8 = st.columns(2)
        num_ctx = g7.number_input("Ollama context window", 512, 32768, int(ai["num_ctx"]), 512,
                                  help="Prompts here stay under ~700 tokens; a bigger window only "
                                       "costs RAM and load time.")
        timeout = g8.number_input("Request timeout (seconds)", 10, 600, int(ai["timeout"]), 5)

    if st.button("Save model settings", type="primary", icon=":material/save:"):
        s["ai"].update(enabled=enabled, provider=provider, model=model or "",
                       temperature=float(temperature), num_ctx=int(num_ctx), timeout=int(timeout),
                       tokens_analysis=int(tokens_analysis), tokens_news=int(tokens_news),
                       tokens_error=int(tokens_error), tokens_guidance=int(tokens_guidance),
                       tokens_synthesis=int(tokens_synthesis))
        s["providers"][provider] = {"base_url": base.strip(),
                                    "api_key": "" if env_set else key_input.strip(),
                                    "price_in": float(price_in), "price_out": float(price_out)}
        cfg.save(s)
        st.toast("Model settings saved.", icon=":material/check_circle:")

# --------------------------------------------------------------------------
# Spending
# --------------------------------------------------------------------------
with tab_spend:
    b = s["budget"]
    state = llm.budget_state(s)
    provider_label = cfg.PROVIDERS[s["ai"]["provider"]][0]

    if not state["paid"]:
        st.success(f"You are using **{provider_label}**, which runs on your own hardware. "
                   "It costs nothing, and none of the limits below apply to it. They take effect "
                   "the moment you switch to a paid provider.", icon=":material/savings:")
    month = archive.usage_since(cfg.month_start())
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Spent this month", cfg.money(s, month["cost"]), border=True,
              help="Based on the prices you entered for each provider.")
    s2.metric("Tokens this month", f"{month['tokens']:,}", border=True)
    s3.metric("Model calls", f"{month['calls']:,}", border=True)
    s4.metric("Remaining", cfg.money(s, max(0.0, state["cost_cap"] - month["cost"]))
              if state["cost_cap"] else "No cap", border=True)

    if state["cost_cap"]:
        st.progress(min(1.0, month["cost"] / state["cost_cap"]),
                    text=f"{cfg.money(s, month['cost'])} of {cfg.money(s, state['cost_cap'])} "
                         f"this month ({state['cost_pct']:.0f}%)")
    if state["token_cap"]:
        st.progress(min(1.0, month["tokens"] / state["token_cap"]),
                    text=f"{month['tokens']:,} of {state['token_cap']:,} tokens "
                         f"({state['token_pct']:.0f}%)")

    st.markdown("**Limits**")
    st.caption("A call that would cross a limit is refused before it is sent, so a limit can never "
               "be exceeded by accident. Set any of these to 0 to remove it.")
    enabled_budget = st.toggle("Enforce these limits", value=b["enabled"])
    c1, c2, c3 = st.columns(3)
    currency = c1.selectbox("Currency", list(cfg.CURRENCIES),
                            index=list(cfg.CURRENCIES).index(b.get("currency", "USD"))
                            if b.get("currency") in cfg.CURRENCIES else 0,
                            help="Only a label — enter prices in the same currency.")
    monthly_cost = c2.number_input(f"Monthly limit ({cfg.CURRENCIES.get(currency, '')})",
                                   0.0, 100000.0, float(b["monthly_cost"]), 1.0, format="%.2f")
    monthly_tokens = c3.number_input("Monthly token limit", 0, 100_000_000,
                                     int(b["monthly_tokens"]), 10_000)
    c4, c5 = st.columns(2)
    per_run = c4.number_input("Tokens per run", 0, 10_000_000, int(b["max_tokens_per_run"]), 1_000,
                              help="The one that saves you: a 200-company watchlist against a "
                                   "frontier model can spend a monthly budget in a single run.")
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
        st.caption("No model calls recorded this month.")

# --------------------------------------------------------------------------
# Data and scraping
# --------------------------------------------------------------------------
with tab_data:
    d = s["data"]
    st.markdown("**Defaults for a new run**")
    default_tickers = st.text_input("Tickers", value=d["default_tickers"])
    categories = st.pills("Filings", core.WANTED_CATEGORIES, selection_mode="multi",
                          default=d["categories"])
    timeframe = st.selectbox("Timeframe", list(core.TIMEFRAME_OPTIONS),
                             index=list(core.TIMEFRAME_OPTIONS).index(d["timeframe"])
                             if d["timeframe"] in core.TIMEFRAME_OPTIONS else 0)

    st.markdown("**Downloads**")
    c1, c2, c3 = st.columns(3)
    max_file_mb = c1.number_input("Largest filing (MB)", 1, 200, int(d["max_file_mb"]))
    max_pages = c2.number_input("Pages scanned per PDF", 10, 1000, int(d["max_pdf_pages"]),
                                help="Most filings are well under this. Raising it costs about "
                                     "2 seconds per 75 extra pages, once per document ever.")
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
    FEATURE_NAMES = [("News", "news"), ("All numbers", "detailed_numbers"),
                     ("Archive search", "archive"), ("AI answers in Archive", "archive_ai"),
                     ("Saved history", "history"), ("Guidance tracking", "guidance")]
    picked = st.pills(
        "Turn parts of the app on or off", [name for name, _ in FEATURE_NAMES],
        selection_mode="multi",
        default=[name for name, key in FEATURE_NAMES if f.get(key, True)])
    st.caption("**AI answers in Archive** lets the model expand your question, rank what it finds "
               "and answer with citations. Off, the Archive page is plain keyword search.")

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
        st.toast("Data settings saved.", icon=":material/check_circle:")

# --------------------------------------------------------------------------
# Appearance
# --------------------------------------------------------------------------
with tab_look:
    u = s["ui"]
    custom_style = st.toggle(
        "Apply my appearance settings", value=u["custom_style"],
        help="Off means the app uses only the base theme in .streamlit/config.toml — the safe "
             "fallback if anything here ever looks wrong.")
    a1, a2 = st.columns(2)
    accent_name = a1.selectbox("Accent colour", list(cfg.ACCENTS),
                               index=list(cfg.ACCENTS.values()).index(u["accent"])
                               if u["accent"] in cfg.ACCENTS.values() else 0)
    accent = a2.color_picker("or pick your own", value=cfg.ACCENTS.get(accent_name, u["accent"]))
    b1, b2 = st.columns(2)
    font_scale = b1.slider("Text size", 0.8, 1.4, float(u["font_scale"]), 0.05)
    density = b2.select_slider("Spacing", list(cfg.DENSITIES), value=u["density"])

    st.markdown("**Tables and charts**")
    c1, c2, c3 = st.columns(3)
    table_height = c1.number_input("Table height (px)", 200, 1200, int(u["table_height"]), 20)
    decimals = c2.number_input("Decimal places", 0, 6, int(u["decimals"]))
    sparklines = c3.toggle("Sparklines on metrics", value=u["sparklines"])
    d1, d2 = st.columns(2)
    chart_one = d1.color_picker("Chart colour 1", value=u["chart_colors"][0])
    chart_two = d2.color_picker("Chart colour 2", value=u["chart_colors"][1])
    show_context = st.toggle("Show the source sentence next to each extracted figure",
                             value=u["show_context_column"],
                             help="The sentence a number came from. Worth keeping: it is how you "
                                  "tell a real figure from a stray one.")

    st.markdown("**Layout**")
    tabs_picked = st.pills("Tabs on the research page", cfg.ALL_TABS, selection_mode="multi",
                           default=[t for t in u["tabs"] if t in cfg.ALL_TABS])
    headline = st.text_area("Subtitle under the title", value=u["headline"], height=80)

    with st.container(border=True):
        st.markdown("**Preview**")
        st.markdown("This is how body text will look. "
                    f":gray[Muted text sits beside it.] Accent: :material/palette: `{accent}`")
        st.button("Primary button", type="primary", disabled=True)

    if st.button("Save appearance", type="primary", icon=":material/save:"):
        u.update(custom_style=custom_style, accent=accent, font_scale=float(font_scale),
                 density=density, table_height=int(table_height), decimals=int(decimals),
                 sparklines=sparklines, chart_colors=[chart_one, chart_two],
                 show_context_column=show_context,
                 tabs=list(tabs_picked) or cfg.ALL_TABS, headline=headline.strip() or u["headline"])
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


with tab_data:
    st.markdown("**Portfolio auto-refresh**")
    st.caption("Every stock held in any portfolio is re-analysed in the background this often — "
               "one stock at a time, never alongside another run. A newly added stock is analysed "
               "straight away. Set 0 to analyse each stock only once.")
    p = portfolio.prefs(s)
    hours = st.number_input("Re-analyse every (hours)", 0, 720, int(p["refresh_hours"]))
    track = st.toggle("Track quantities and buy prices", value=p["track_positions"],
                      help="Optional either way. Off hides value and P&L and just follows the stocks. "
                           "These numbers are stored only on this computer and are never sent anywhere — "
                           "not to price sources, and not to any AI model.")
    if st.button("Save portfolio settings", type="primary", icon=":material/save:"):
        s["portfolio"] = {**p, "refresh_hours": int(hours), "track_positions": track}
        cfg.save(s)
        portfolio.wake()
        st.toast("Refresh schedule saved.", icon=":material/check_circle:")
