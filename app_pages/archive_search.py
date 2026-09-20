"""Search everything ever downloaded — across companies, entirely offline."""

from pathlib import Path

import pandas as pd
import streamlit as st

import archive
import core

settings = st.session_state.settings
has_fts = archive.init()
stats = archive.stats()
ai_ready = core.ai_signature(settings)[0]
ai_wanted = settings["features"].get("archive_ai", True)

st.caption(":material/search: LOCAL FILING ARCHIVE")
st.title("Search your filings")
st.markdown(":gray[Every concall transcript, investor presentation, annual report and results PDF the app "
            "has downloaded stays on this machine and stays searchable — across companies, with no "
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

with st.form("archive_search"):
    question = st.text_input("Ask across every filing",
                             placeholder="e.g. what did management say about capex plans?")
    f1, f2, f3 = st.columns([2, 2, 1])
    tickers = f1.multiselect("Companies", archive.indexed_tickers(), placeholder="All companies")
    categories = f2.multiselect("Filing type", core.WANTED_CATEGORIES, placeholder="All types")
    keep = f3.number_input("Passages to read", min_value=3, max_value=20, value=6,
                           help="How many of the best passages the model reads before answering. "
                                "Fewer is faster and cheaper.")
    thorough = st.toggle(
        "Thorough search", value=False,
        help="Adds two more model calls: one to work out the vocabulary a filing would use, one "
             "to rank what it finds. Better on an obscure question, but three times slower and "
             "three times the tokens. A fast local model barely notices; a slow hosted one does.")
    patience = st.slider("Give up after (seconds)", 15, 180, 60, 15,
                         help="A busy provider can take minutes. When this runs out you get the "
                              "passages found so far instead of a spinning page.")
    use_model = st.toggle(
        "Let the model do the searching and answer", value=ai_ready and ai_wanted,
        disabled=not ai_ready,
        help="It suggests the wording a filing would really use, discards the coincidental "
             "matches, then answers from what is left — citing each passage.")
    searched = st.form_submit_button("Search", type="primary", icon=":material/search:")

if not ai_ready:
    reason = ("AI is switched off in **Settings → Models & keys**."
              if not settings["ai"].get("enabled")
              else "No model is reachable — check **Settings → Models & keys** and press "
                   "*Test connection*.")
    st.info(f"This is plain keyword search right now. {reason}", icon=":material/info:")
elif not ai_wanted:
    st.caption(":material/info: AI answers are switched off under **Settings → Data & scraping → "
               "Features**. Switch the toggle above on for this search only.")
if not has_fts:
    st.caption(":material/info: This Python's SQLite has no FTS5 module, so search falls back to a "
               "plain scan. It still works; it is just slower on a large archive.")

if not searched or not question.strip():
    st.stop()

search_settings = settings if use_model else {**settings, "ai": {**settings["ai"], "enabled": False}}
with st.status("Searching your filings…", expanded=True) as status:
    found = core.smart_search(question, search_settings, tickers or None, categories or None,
                              candidates=24, keep=int(keep), deadline=float(patience),
                              progress=lambda line: status.write(line), thorough=thorough)
    status.update(label=f"Read {found['considered']} passages · {found['tokens']:,} tokens",
                  state="complete", expanded=False)

for note in found.get("notes", []):
    st.caption(f":material/info: {note.capitalize()} — the passages below are still ranked by "
               "relevance, and a shorter question or a faster model usually fixes it.")

if not found["hits"]:
    st.warning("Nothing in the archive matches that. Try different words, or widen the filters.",
               icon=":material/search_off:")
    st.stop()

if found["answer"]:
    with st.container(border=True):
        st.markdown(f"**:material/lightbulb: {question}**")
        st.markdown(found["answer"])
        st.caption(f":material/memory: Answered by `{settings['ai']['model']}` from the "
                   f"{min(len(found['hits']), int(keep))} passages it judged most relevant, out of "
                   f"{found['considered']} retrieved · {found['tokens']:,} tokens. "
                   "The numbers in [brackets] are the passages below.")
elif found["used_model"]:
    st.caption(f":material/warning: The model ranked the results but returned no written answer "
               f"({found['tokens']:,} tokens). The passages below are still ordered best first.")

if found["terms"]:
    with st.container(horizontal=True, vertical_alignment="center"):
        st.markdown(":gray[Also searched for:]", width="content")
        for term in found["terms"]:
            st.badge(term, color="gray", icon=":material/search:")

rows = []
for i, hit in enumerate(found["hits"], start=1):
    path = Path(hit.get("path") or "")
    rows.append({"#": i, "Company": hit["ticker"], "Filing": hit["category"], "Page": hit["page"],
                 "Passage": (hit["snippet"] or "").replace("\n", " "),
                 "File": path.name or "—", "Saved at": str(path) if path.name else "—"})

st.markdown(f"**{len(found['hits'])} passage(s)** :gray[· best first]")
st.dataframe(
    pd.DataFrame(rows), hide_index=True, height=int(settings["ui"]["table_height"]),
    column_config={"#": st.column_config.NumberColumn(width="small"),
                   "Company": st.column_config.TextColumn(width="small"),
                   "Page": st.column_config.NumberColumn(width="small"),
                   "Passage": st.column_config.TextColumn(width="large"),
                   "File": st.column_config.TextColumn(width="small"),
                   "Saved at": st.column_config.TextColumn(width="medium",
                                                           help="Where the PDF sits on your disk.")})

with st.expander("Read the full pages", icon=":material/article:"):
    for i, hit in enumerate(found["hits"][:int(keep)], start=1):
        st.markdown(f"**[{i}] {hit['ticker']} · {hit['category']} · page {hit['page']}**")
        if hit.get("path"):
            st.caption(f":material/folder: `{hit['path']}` — page {hit['page']}")
        st.text(hit["text"][:2000])
        st.divider()
