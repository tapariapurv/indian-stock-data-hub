"""The archive: a search bar for finding words, a chat window for asking questions."""

import time
from pathlib import Path

import streamlit as st

import archive
import core
import ui

settings = st.session_state.settings
archive.init()
stats = st.cache_data(ttl=60, show_spinner=False)(archive.stats)()  # a disk scan; not per keystroke
ai_ready = core.ai_signature(settings)[0]

st.title("Archive")
st.caption("Search every company report the app has downloaded — instantly, and for free.")

if not stats["documents"]:
    st.info("Nothing to search yet. Look into a company and its reports are added here automatically.",
            icon=":material/info:")
    st.stop()

if ui.tech(settings):
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Documents", f"{stats['documents']:,}", border=True)
    m2.metric("Pages", f"{stats['pages']:,}", border=True)
    m3.metric("Numbers", f"{stats['figures']:,}", border=True)
    m4.metric("Search index size", f"{stats['db_mb']:.1f} MB", border=True,
              help="The reports themselves are counted in Settings → Advanced → Storage & backups.")
else:
    st.caption(f"{stats['documents']:,} documents · {stats['pages']:,} pages")
st.page_link("app_pages/ask.py", label="Want an answer instead? Ask AI", icon=":material/auto_awesome:")
tab_find = st.container()
companies = archive.indexed_tickers()


def source_line(hit: dict) -> str:
    return f"**{hit['ticker']}** · {hit['category']} · page {hit['page']}"


# --------------------------------------------------------------------------
# Find: a search bar. No model, so it answers instantly.
# --------------------------------------------------------------------------
with tab_find:
    with st.form("find", border=False):
        bar, button = st.columns([6, 1], vertical_alignment="bottom")
        needle = bar.text_input(
            "Search", placeholder="What are you looking for? e.g. new factory, debt, dividend",
            label_visibility="collapsed")
        searched = button.form_submit_button("Search", type="primary", icon=":material/search:",
                                             width="stretch")
        with st.expander("Narrow it down", icon=":material/filter_alt:"):
            c1, c2, c3 = st.columns([2, 2, 1])
            pick_tickers = c1.multiselect("Companies", companies, placeholder="All companies")
            pick_types = c2.multiselect("Kind of report", core.WANTED_CATEGORIES,
                                        placeholder="All kinds")
            limit = c3.number_input("How many results", 10, 200, 50, 10)

    if searched and needle.strip():
        started = time.time()
        hits = archive.search(needle, pick_tickers or None, pick_types or None, int(limit))
        elapsed = time.time() - started

        if not hits:
            st.warning("Nothing found. Try fewer or simpler words.", icon=":material/search_off:")
        else:
            st.markdown(f"**{len(hits)} match(es)** :gray[· found in {elapsed * 1000:.0f} ms]")
            for hit in hits[:40]:
                with st.container(border=True):
                    with st.container(horizontal=True, vertical_alignment="center"):
                        st.markdown(source_line(hit), width="stretch")
                        if ui.tech(settings):
                            st.badge(Path(hit.get("path") or "").name or "—", color="gray",
                                     icon=":material/description:")
                    st.markdown(f":gray[…] {hit['snippet']} :gray[…]")
                    with st.expander("Read the whole page", icon=":material/article:"):
                        if ui.tech(settings):
                            st.caption(f":material/folder: `{hit.get('path') or 'file removed'}` "
                                       f"— page {hit['page']}")
                        st.text(hit["text"][:4000])
            if len(hits) > 40:
                st.caption(f"Showing the first 40 of {len(hits)}. Add more words to narrow it down.")
    elif searched:
        st.warning("Type something to search for.", icon=":material/edit:")


# --------------------------------------------------------------------------
# Ask: a chat window. Slower, because a model reads the passages.
# --------------------------------------------------------------------------
