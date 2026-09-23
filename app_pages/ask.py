"""Ask AI: a chat that knows your portfolio, your analyses and your filings."""

import streamlit as st

import assistant
import core
import portfolio as pf

settings = st.session_state.settings
ai_ready = core.ai_signature(settings)[0]
ss = st.session_state
ss.setdefault("chat_id", None)
ss.setdefault("chat", [])

# --- Sidebar: conversations -------------------------------------------------------
with st.sidebar:
    if st.button("New chat", icon=":material/edit_square:", type="primary", width="stretch"):
        ss.chat_id, ss.chat = None, []
        st.rerun()
    recent = assistant.chats()
    if recent:
        st.caption("Recent")
        for c in recent:
            if st.button(c["title"], key=f"chat_{c['id']}", type="tertiary", width="stretch",
                         icon=":material/chat_bubble:" if c["id"] == ss.chat_id else ":material/chat_bubble_outline:"):
                ss.chat_id, ss.chat = c["id"], assistant.load(c["id"])
                st.rerun()

# --- Header -------------------------------------------------------------------------
with st.container(horizontal=True, vertical_alignment="center"):
    st.markdown("### :material/auto_awesome: Ask AI", width="content")
    st.space("stretch")
    with st.popover("Scope", icon=":material/tune:"):
        known = assistant._known()
        st.multiselect("Only these companies", sorted(known), key="ask_scope", placeholder="Whatever I mention",
                       format_func=lambda t: f"{t} · {known[t]}")
        st.toggle("Search my filings", value=True, key="ask_filings",
                  help="Read matching passages from downloaded concalls, presentations and reports.")
    if ss.chat_id and st.button("Delete chat", icon=":material/delete:", type="tertiary"):
        assistant.delete(ss.chat_id)
        ss.chat_id, ss.chat = None, []
        st.rerun()

if not ai_ready:
    st.info("Ask AI needs a model — set one up in **Settings → Models & keys**. A local Ollama model is free "
            "and keeps everything on this computer.", icon=":material/info:")


def show_sources(sources: list[dict], key: str):
    """Numbered source chips under an answer, Perplexity-style; each opens to show where it came from."""
    if not sources:
        return
    with st.container(horizontal=True, gap="small"):
        for i, s in enumerate(sources[:8], start=1):
            icon = {"portfolio": ":material/account_balance_wallet:", "analysis": ":material/query_stats:",
                    "filing": ":material/description:", "tally": ":material/functions:",
                    }.get(s["kind"], ":material/source:")
            with st.popover(f"{i} · {s['label'].split(' · ')[0]}", icon=icon, type="tertiary"):
                st.markdown(f"**[{i}]** {s['label']}")
                if s.get("path"):
                    st.caption(f":material/folder: `{s['path']}` · page {s.get('page')}")


def ask(question: str):
    ss.pending = question


# --- Conversation ----------------------------------------------------------------------
typed = st.chat_input("Ask about a company, your portfolio or your filings…", disabled=not ai_ready)
if not ss.chat and not typed:
    held = list(pf.tracked())
    with st.container(horizontal_alignment="center"):
        st.space("large")
        st.markdown("## What would you like to know?", text_alignment="center")
        st.markdown(":gray[Answers come from your portfolio, your saved analyses and your downloaded filings — "
                    "every fact cited.]", text_alignment="center")
        a, b = (held + ["TCS", "INFY"])[:2]
        ideas = ["Which of my holdings look weakest right now, and why?",
                 f"How have {a}'s margins moved over the last few quarters?",
                 f"Compare {a} and {b}",
                 "What did management say about demand in the latest concalls?"]
        choice = st.pills("Try", ideas, label_visibility="collapsed", key="ask_idea")
        if choice:
            ask(choice)

for i, turn in enumerate(ss.chat):
    with st.chat_message(turn["role"], avatar=":material/person:" if turn["role"] == "user" else ":material/auto_awesome:"):
        st.markdown(turn["content"])
        if turn["role"] == "assistant":
            show_sources(turn.get("sources") or [], f"s{i}")
            if turn.get("meta"):
                st.caption(turn["meta"])
            if i == len(ss.chat) - 1 and turn.get("followups"):
                pick = st.pills("Follow up", turn["followups"], label_visibility="collapsed", key=f"fu_{i}")
                if pick:
                    ask(pick)

question = typed or ss.pop("pending", None)
if question and ai_ready:
    history = [{"role": t["role"], "content": t["content"]} for t in ss.chat]
    ss.chat.append({"role": "user", "content": question})
    with st.chat_message("user", avatar=":material/person:"):
        st.markdown(question)
    with st.chat_message("assistant", avatar=":material/auto_awesome:"):
        with st.status("Reading your sources…", expanded=False) as status:
            holder = {"progress": lambda msg: status.update(label=msg)}
            stream = assistant.answer(question, history, settings, holder, ss.get("ask_scope") or None,
                                      ss.get("ask_filings", True))
            status.update(label=f"Reading your sources and asking {settings['ai']['model']}…")
            first = next(stream, None)  # covers retrieval, any fallbacks, and the model's thinking
            status.update(label=f"{holder.get('model')} is answering from {len(holder.get('sources') or [])} source(s)",
                          state="complete" if first else "error")

        def chained():
            if first:
                yield first
            yield from stream

        st.write_stream(chained())
        sources = holder.get("sources") or []
    meta = (f":material/memory: `{holder.get('model')}`"
            + (f" · stood in for `{settings['ai']['model']}`, which was busy" if holder.get("fallback") else "")
            + f" · {holder.get('seconds', 0):.0f}s · {len(sources)} source(s)")
    ss.chat.append({"role": "assistant", "content": holder.get("text") or
                    f":material/error: No answer — {holder.get('error')}. Try again in a moment.",
                    "sources": sources, "followups": holder.get("followups") or [], "meta": meta})
    ss.chat_id = assistant.save(ss.chat_id, ss.chat)
    st.rerun()  # redraw with the sources and follow-up suggestions under the answer
