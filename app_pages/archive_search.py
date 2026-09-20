"""The archive: a search bar for finding words, a chat window for asking questions."""

import time
from pathlib import Path

import streamlit as st

import archive
import core

settings = st.session_state.settings
archive.init()
stats = archive.stats()
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

tab_find, tab_ask = st.tabs([":material/search: Find", ":material/forum: Ask"])
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
with tab_ask:
    st.session_state.setdefault("archive_chat", [])
    chat = st.session_state.archive_chat

    with st.container(horizontal=True, vertical_alignment="center"):
        st.markdown("**Ask your archive** :gray[· answers come only from your own filings, "
                    "with each claim cited]", width="stretch")
        with st.popover("Options", icon=":material/tune:"):
            st.multiselect("Companies", companies, placeholder="All companies", key="ask_tickers")
            st.multiselect("Filing type", core.WANTED_CATEGORIES, placeholder="All types",
                           key="ask_types")
            st.number_input("Passages to read", 3, 20, 6, key="ask_keep",
                            help="Fewer is faster and cheaper.")
            st.toggle("Thorough", value=False, key="ask_thorough",
                      help="Two extra model calls: one to work out the wording a filing would use, "
                           "one to rank what it finds. Better on an obscure question, three times "
                           "slower.")
            st.slider("Give up after (seconds)", 15, 240, 90, 15, key="ask_patience")
        if chat and st.button("Clear", icon=":material/delete_sweep:", type="tertiary"):
            st.session_state.archive_chat = []
            st.rerun()

    if not ai_ready:
        reason = ("AI is switched off in **Settings → Models & keys**."
                  if not settings["ai"].get("enabled")
                  else "No model is reachable — check **Settings → Models & keys**.")
        st.info(f"Asking needs a model. {reason} The **Find** tab works without one.",
                icon=":material/info:")

    if not chat:
        with st.container(border=True, horizontal_alignment="center"):
            st.space("small")
            st.markdown("### :material/forum: Ask anything about your filings",
                        text_alignment="center")
            st.markdown(":gray[*What did management say about capex?* · *How are margins "
                        "trending?* · *Which companies mentioned China?*  \n"
                        "Follow-up questions keep the thread, and every answer cites the passages "
                        "it came from.]", text_alignment="center")
            st.space("small")

    for turn in chat:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])
            if turn.get("coverage"):
                with st.expander(f"Found in {turn.get('matched_pages', 0)} page(s) across "
                                 f"{len(turn['coverage'])} compan(y/ies)",
                                 icon=":material/pie_chart:"):
                    with st.container(horizontal=True):
                        for ticker, count in turn["coverage"].items():
                            st.badge(f"{ticker} · {count}", color="gray")
            if turn.get("sources"):
                with st.expander(f"{len(turn['sources'])} source(s) read in full",
                                 icon=":material/article:"):
                    for i, hit in enumerate(turn["sources"], start=1):
                        st.markdown(f"**[{i}]** {source_line(hit)}")
                        st.markdown(f":gray[{hit['snippet']}]")
                        st.caption(f":material/folder: `{hit.get('path') or '—'}` "
                                   f"— page {hit['page']}")
            if turn.get("meta"):
                st.caption(turn["meta"])

    question = st.chat_input("Ask your filings…", disabled=not ai_ready)
    if question:
        chat.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)

        with st.chat_message("assistant"):
            started = time.time()
            with st.status("Thinking…", expanded=True) as status:
                found = core.smart_search(
                    question, settings, st.session_state.ask_tickers or None,
                    st.session_state.ask_types or None, candidates=40,
                    keep=int(st.session_state.ask_keep),
                    deadline=float(st.session_state.ask_patience),
                    progress=status.write, thorough=st.session_state.ask_thorough,
                    history=chat[:-1])
                status.update(
                    label=f"{found['matched_pages']} matching page(s) across "
                          f"{len(found['coverage'])} compan(y/ies) · read {found['considered']}",
                    state="complete", expanded=False)

            sources = found["hits"][:int(st.session_state.ask_keep)]
            coverage = found.get("coverage") or {}
            headline = found.get("headline")
            if found["answer"]:
                answer = found["answer"]
            elif sources:
                answer = ("I could not get a written answer out of the model, but these passages "
                          "are the closest matches in your archive — open the sources below.")
            else:
                answer = ("Nothing in your archive matches that. Try different words, or check the "
                          "filters under Options.")
            if headline:
                # Counted, not inferred, so it leads.
                answer = f"{headline}\n\n{answer}" if found["answer"] else headline
            for note in found.get("notes", []):
                answer += f"\n\n:gray[({note})]"

            meta = (f":material/memory: `{settings['ai']['model']}` · {found['tokens']:,} tokens · "
                    f"{time.time() - started:.0f}s" if found["used_model"] else
                    ":material/bolt: keyword search only")
            st.markdown(answer)
            if found.get("coverage"):
                with st.expander(f"Found in {found['matched_pages']} page(s) across "
                                 f"{len(found['coverage'])} compan(y/ies)",
                                 icon=":material/pie_chart:"):
                    st.caption("Every company whose filings match, and how many pages each. The "
                               "answer above uses this tally for 'which companies' questions, and "
                               "the passages below for detail.")
                    with st.container(horizontal=True):
                        for ticker, count in found["coverage"].items():
                            st.badge(f"{ticker} · {count}", color="gray")
            if sources:
                with st.expander(f"{len(sources)} source(s) read in full",
                                 icon=":material/article:"):
                    for i, hit in enumerate(sources, start=1):
                        st.markdown(f"**[{i}]** {source_line(hit)}")
                        st.markdown(f":gray[{hit.get('focus') or hit['snippet']}]")
            st.caption(meta)

        chat.append({"role": "assistant", "content": answer, "sources": sources, "meta": meta,
                     "coverage": coverage, "matched_pages": found["matched_pages"]})
