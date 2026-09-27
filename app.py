"""
Indian Stock Data Hub
=====================
A Streamlit research desk for Indian listed companies. It scrapes public
data from screener.in, downloads the latest filings (concall transcripts,
investor presentations, annual reports, quarterly results), extracts every
figure from those PDFs with a regex parser, gathers recent news from public
RSS feeds, and asks a model of your choosing -- a local one by default -- to
write a verdict, a short analysis and a news digest.

Everything it learns is kept in a local archive, so filings are parsed once,
can be searched across companies, and every analysis can be reopened later
to see what has changed since.

Run with:   streamlit run app.py
Settings:   the Settings page in the app (stored in settings.json)
User guide: static/user_guide.pdf, linked from the sidebar
"""

import streamlit as st

import archive
import core
import llm
import portfolio
import settings as cfg

st.set_page_config(page_title="Indian Stock Data Hub", page_icon=":material/monitoring:",
                   layout="wide", initial_sidebar_state="expanded")

if "settings" not in st.session_state:
    st.session_state.settings = cfg.load()
    # Retention runs once per session, not per rerun.
    if st.session_state.settings["storage"].get("auto_purge"):
        try:
            archive.purge(st.session_state.settings["storage"])
        except Exception:
            pass  # housekeeping must never stop the app from starting

    try:
        core.compact_runs()
    except Exception:
        pass  # housekeeping must never stop the app from starting

    try:
        archive.drop_unusable(llm.is_usable)
    except Exception:
        pass  # cleaning old answers must never stop the app from starting

    # Pick a model if none is saved, so every page -- not just Research --
    # knows one is available.
    if llm.ensure_model(st.session_state.settings):
        cfg.save(st.session_state.settings)

# A session that outlives a code update still holds the old settings; fill in
# any default added since, so new pages never hit a missing key.
st.session_state.settings = cfg._merge(cfg.DEFAULTS, st.session_state.settings)
portfolio.start_refresher()

st.html(cfg.style_css(st.session_state.settings["ui"]))

u = st.session_state.settings["ui"]
hidden = set(u["hidden_pages"]) - cfg.ALWAYS_SHOWN
pages = {}
for file, title, icon, section in cfg.PAGES:
    if title not in hidden:
        pages.setdefault(section, []).append(
            st.Page(f"app_pages/{file}", title=title, icon=icon, url_path=title.lower().replace(" ", "-"),
                    default=title == (u["home"] if u["home"] not in hidden else "Home")))
if u["nav"] == "sidebar":  # a side menu has room for every page in the open
    pages = {"": [p for group in pages.values() for p in group]}
st.navigation(pages, position=u["nav"]).run()
