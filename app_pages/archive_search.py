"""The archive: a search bar for finding words, a chat window for asking questions."""

import time
from pathlib import Path

import streamlit as st

import archive
import core

settings = st.session_state.settings
archive.init()
stats = st.cache_data(ttl=60, show_spinner=False)(archive.stats)()  # a disk scan; not per keystroke
ai_ready = core.ai_signature(settings)[0]

st.caption(":material/search: LOCAL FILING ARCHIVE")
st.title("Your filings")
st.markdown(":gray[Every concall transcript, investor presentation, annual report and results PDF "
            "the app has downloaded stays on this machine — searchable across companies, with no "
            "network and no per-search cost.]")

if not stats["documents"]:
    st.info("The archive is empty. Run an analysis with some filing types ticked and they will be "
            "indexed automatically.", icon=":material/info:")
    st.stop()

m1, m2, m3, m4 = st.columns(4)
m1.metric("Documents", f"{stats['documents']:,}", border=True)
m2.metric("Pages", f"{stats['pages']:,}", border=True)
m3.metric("Figures", f"{stats['figures']:,}", border=True)
m4.metric("Archive size", f"{stats['db_mb']:.1f} MB", border=True,
          help="The search index. The PDFs themselves are counted on the Settings page.")

st.page_link("app_pages/ask.py", label="Want an answer instead of matches? Ask AI", icon=":material/auto_awesome:")
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
            "Search", placeholder="Search every filing — e.g. capital expenditure, GNPA, Hong Kong",
            label_visibility="collapsed")
        searched = button.form_submit_button("Search", type="primary", icon=":material/search:",
                                             width="stretch")
        with st.expander("Narrow it down", icon=":material/filter_alt:"):
            c1, c2, c3 = st.columns([2, 2, 1])
            pick_tickers = c1.multiselect("Companies", companies, placeholder="All companies")
            pick_types = c2.multiselect("Filing type", core.WANTED_CATEGORIES,
                                        placeholder="All types")
            limit = c3.number_input("Results", 10, 200, 50, 10)

    st.caption(":material/bolt: Instant keyword search across every indexed page. No model is used, "
               "so it costs nothing and never waits on a provider. Matching words are in bold.")

    if searched and needle.strip():
        started = time.time()
        hits = archive.search(needle, pick_tickers or None, pick_types or None, int(limit))
        elapsed = time.time() - started

        if not hits:
            st.warning("No page in the archive contains those words. Try fewer, or more common, "
                       "words — the search matches any of them.", icon=":material/search_off:")
        else:
            st.markdown(f"**{len(hits)} match(es)** :gray[· found in {elapsed * 1000:.0f} ms]")
            for hit in hits[:40]:
                with st.container(border=True):
                    with st.container(horizontal=True, vertical_alignment="center"):
                        st.markdown(source_line(hit), width="stretch")
                        st.badge(Path(hit.get("path") or "").name or "—", color="gray",
                                 icon=":material/description:")
                    st.markdown(f":gray[…] {hit['snippet']} :gray[…]")
                    with st.expander("Read the whole page", icon=":material/article:"):
                        st.caption(f":material/folder: `{hit.get('path') or 'file removed'}` "
                                   f"— page {hit['page']}")
                        st.text(hit["text"][:4000])
            if len(hits) > 40:
                st.caption(f"Showing the first 40 of {len(hits)}. Narrow it down, or raise the "
                           "result limit.")
    elif searched:
        st.warning("Type something to search for.", icon=":material/edit:")


# --------------------------------------------------------------------------
# Ask: a chat window. Slower, because a model reads the passages.
# --------------------------------------------------------------------------
