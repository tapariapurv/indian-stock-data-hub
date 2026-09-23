"""
The Ask AI chat: context in, a streamed, cited answer out.

For each question it gathers three kinds of grounding, cheapest first --
your portfolio, the newest saved analysis of any company you name, and the
best-matching passages from your filings -- numbers them as sources, and
asks the model to answer from them, citing [n]. Follow-up suggestions come
back in the same call, so a turn costs one model request.
"""

import json
import re
import time

import archive
import core
import llm
import portfolio as pf

SYSTEM = """You are the research assistant inside Indian Stock Data Hub, a private app for tracking Indian listed companies.

How to answer:
- Lead with the direct answer in one or two sentences, then the detail. Use Markdown: short paragraphs, bullet points, **bold** for key figures, and a table when comparing companies or periods.
- Every company-specific fact must come from the SOURCES and be cited like [2]. Copy figures exactly, with their units (₹ Cr means crore rupees).
- If the sources do not cover something, say so plainly. You may add general market or accounting knowledge, but label it as general knowledge and never invent a company figure.
- Be concise: no preamble, no restating the question, no disclaimers beyond one short line when you give an opinion.
- Finish with exactly one final line: FOLLOWUPS: <question> | <question> | <question>
  (three short follow-up questions the user might ask next, about these companies)."""
MARKER = "FOLLOWUPS:"
KEEP = 6              # passages the model finally reads
CANDIDATES = 20       # passages retrieved before any ranking
RERANK_ABOVE = 10     # ask the model to choose only when there is a real choice
PORTFOLIO_WORDS = re.compile(r"\b(my|portfolio|holdings?|i own|we own|which of|all of)\b", re.I)
REFERS_BACK = re.compile(r"\b(it|its|it's|they|their|them|this|that|these|those|both|compare|compared|vs|versus|same)\b", re.I)


def _known() -> dict[str, str]:
    """ticker -> company name, for everything held or analysed."""
    rows = pf._q("SELECT ticker, json_extract(snapshot, '$.company_name') AS name FROM runs "
                 "WHERE id IN (SELECT MAX(id) FROM runs GROUP BY ticker)")
    known = {r["ticker"]: r["name"] or r["ticker"] for r in rows}
    known.update({t: v["name"] for t, v in pf.tracked().items() if t not in known})
    return known


def mentioned(text: str, known: dict[str, str]) -> list[str]:
    words = set(re.findall(r"[A-Za-z0-9&-]+", text.upper()))
    low = f" {text.lower()} "
    hits = [t for t, name in known.items()
            if t in words or (name and len(name.split()[0]) > 3 and f" {name.split()[0].lower()}" in low)]
    return hits[:4]


def _latest(ticker: str) -> dict | None:
    row = pf._q("SELECT id, ts FROM runs WHERE ticker=? ORDER BY ts DESC LIMIT 1", (ticker,))
    if not row:
        return None
    return {"ts": row[0]["ts"], **core.restore_snapshot(archive.get_run(row[0]["id"])["snapshot"])}


def _analysis_text(snap: dict) -> str:
    lines = [f"Company: {snap.get('company_name')} ({snap.get('ticker')}); sector: {snap.get('sector') or 'n/a'}",
             f"AI verdict: {snap.get('ai_verdict') or 'none'}. Summary: {snap.get('ai_summary') or 'none'}",
             "Ratios: " + "; ".join(f"{k} {v}" for k, v in (snap.get("metrics") or {}).items())]
    qdf = snap.get("quarterly_df")
    if qdf is not None and not qdf.empty:
        keep = qdf[qdf.iloc[:, 0].astype(str).str.match(
            r"(Sales|Revenue|Net Profit|OPM|EPS|Operating Profit|Financing|Interest|Margin)", case=False)]
        cols = [qdf.columns[0], *qdf.columns[-5:]]
        lines.append("Quarterly results (₹ Cr):\n" + keep[cols].to_string(index=False))
    if snap.get("pros"):
        lines.append("Strengths: " + "; ".join(snap["pros"][:3]))
    if snap.get("cons"):
        lines.append("Risks: " + "; ".join(snap["cons"][:3]))
    heads = (snap.get("news") or {}).get("headlines") or []
    if heads:
        lines.append("Recent headlines: " + "; ".join(h.get("title", "") for h in heads[:4]))
    return "\n".join(lines)


def build_context(question: str, history: list[dict], scope: list[str] | None = None,
                  use_filings: bool = True, settings: dict | None = None) -> tuple[str, list[dict]]:
    """(numbered source text for the prompt, source list for the UI)."""
    known = _known()
    tickers = scope or mentioned(question, known)
    about_portfolio = bool(PORTFOLIO_WORDS.search(question))
    if not scope and (REFERS_BACK.search(question) or not (tickers or about_portfolio)):
        # A follow-up ("and its margins vs AXISBANK?") keeps the companies already in play.
        for turn in reversed(history):
            earlier = [t for t in mentioned(turn["content"], known) if t not in tickers]
            if earlier:
                tickers = (tickers + earlier)[:4]
                break
    sources, blocks = [], []

    def add(kind, label, text, **extra):
        sources.append({"kind": kind, "label": label, **extra})
        blocks.append(f"[{len(sources)}] {label}\n{text}")

    held = pf.tracked()
    if held and (about_portfolio or not tickers):
        import screener
        uni = screener.universe(screener.version()).set_index("Ticker")
        quotes = pf.quotes(tuple(sorted((t, v["screener_id"]) for t, v in held.items())))

        def cell(t, col, fmt):
            v = uni.at[t, col] if t in uni.index else None
            return fmt.format(v) if v is not None and v == v and v != "" else "n/a"
        rows = ["Ticker | Company | AI verdict | Price ₹ | Today | P/E | ROE % | ROCE % | Sales YoY % | Profit YoY %"]
        for t, v in list(held.items())[:40]:
            q = quotes.get(t)
            rows.append(" | ".join([t, v["name"], cell(t, "Verdict", "{}") if cell(t, "Verdict", "{}") != "n/a" else "not yet analysed",
                                    f"{q['price']:,.2f}" if q else "n/a", f"{q['change_pct']:+.2f}%" if q else "n/a",
                                    cell(t, "P/E", "{:.1f}"), cell(t, "ROE", "{:.1f}"), cell(t, "ROCE", "{:.1f}"),
                                    cell(t, "Sales Growth", "{:+.1f}"), cell(t, "Profit Growth", "{:+.1f}")]))
        add("portfolio", f"Your portfolio · {len(held)} stocks",
            "These are the stocks the user holds (their own portfolio), one row each:\n" + "\n".join(rows))
    for t in tickers:
        snap = _latest(t)
        if snap:
            add("analysis", f"{t} · latest analysis, {time.strftime('%d %b %Y', time.localtime(snap['ts']))}",
                _analysis_text(snap), ticker=t)
    if use_filings:
        terms = [w for w in re.findall(r"[a-z0-9][a-z0-9'&.-]*", question.lower())
                 if len(w) > 2 and w not in archive.STOPWORDS]
        hits = archive.search(question, tickers or None, limit=CANDIDATES)
        # The gap between a question and a filing is vocabulary, not logic:
        # ask about "capex" and the transcript says "capital expenditure", so
        # a plain keyword search finds nothing at all. The Archive page has
        # always expanded the wording; this one did not, which is why the
        # chat could answer "no mention of that" about a filing it held.
        # Only on a poor result, so the usual question still costs one call.
        extra: list[str] = []
        if len(hits) < 3 and settings and len(question.split()) > 2 and core.ai_signature(settings)[0]:
            extra, _ = llm.expand_query(question, core._wide_context(settings))
            if extra:
                found = archive.search(question, tickers or None, limit=CANDIDATES, extra_terms=extra)
                seen = {(h["ticker"], h["page"], h["category"]) for h in hits}
                hits += [h for h in found if (h["ticker"], h["page"], h["category"]) not in seen]
        # With plenty of candidates, let the model throw out the coincidental
        # matches -- the page that says "share capital" when the question was
        # about capital expenditure. Only worth a call when there is real
        # choice to make, so a small archive still answers in one request.
        if len(hits) > RERANK_ABOVE and settings and core.ai_signature(settings)[0]:
            order, _ = llm.rerank_passages(question, hits, core._wide_context(settings), keep=KEEP)
            hits = [hits[i] for i in order] or hits
        for hit in hits[:KEEP]:
            add("filing", f"{hit['ticker']} · {hit['category']} · page {hit['page']}",
                core._focus(hit["text"], terms + extra), ticker=hit["ticker"], page=hit["page"],
                path=hit.get("path"))
    return "\n\n".join(blocks), sources


def answer(question: str, history: list[dict], settings: dict, holder: dict, scope=None, use_filings=True):
    """Stream the answer text; when done, holder has sources, followups, text, model, error."""
    context, holder["sources"] = build_context(question, history, scope, use_filings, settings)
    messages = [{"role": t["role"], "content": t["content"]} for t in history[-6:]]
    messages.append({"role": "user", "content": (f"SOURCES:\n{context}\n\n" if context else "SOURCES: none\n\n")
                     + f"QUESTION: {question}"})
    started, buf, sent, tried = time.time(), "", 0, []
    candidates_ = llm.candidates(settings)
    say = holder.get("progress") or (lambda _msg: None)
    for attempt in candidates_:
        if tried:
            say(f"{holder['model']} is busy — asking {attempt['ai']['model']}…")
        holder["model"] = attempt["ai"]["model"]
        for chunk in llm.stream(messages, SYSTEM, int(settings["ai"].get("tokens_chat", 1200)),
                                core._wide_context(attempt), "Chat", patient=attempt is candidates_[-1],
                                first_by=None if attempt is candidates_[-1] else 15):
            buf += chunk
            cut = buf.find(MARKER)
            safe = cut if cut >= 0 else max(sent, len(buf) - len(MARKER))  # hold back a possibly split marker
            if safe > sent:
                yield buf[sent:safe]
                sent = safe
        if buf.strip():
            break
        tried.append(f"{attempt['ai']['model']}: {llm.friendly(llm.LAST_ERROR)}")
        llm.bench(attempt["ai"]["model"])
        if not llm.is_transient(llm.LAST_ERROR):
            break  # a real error (bad key, bad request): another model will not fix it
    holder["fallback"] = bool(tried) and bool(buf.strip())
    cut = buf.find(MARKER)
    tail = buf[sent:cut] if cut >= 0 else buf[sent:]
    if tail:
        yield tail
    holder["followups"] = [q.strip(" -•?") + "?" for q in buf[cut + len(MARKER):].split("|")
                           if q.strip(" -•?")][:3] if cut >= 0 else []
    holder["text"] = (buf[:cut] if cut >= 0 else buf).strip()
    holder["error"] = None if holder["text"] else "; ".join(tried) or "the model returned nothing"
    holder["seconds"] = time.time() - started


# --- Saved chats ------------------------------------------------------------------

def chats(limit: int = 30) -> list[dict]:
    return pf._q("SELECT id, title, updated FROM chats ORDER BY updated DESC LIMIT ?", (limit,))


def load(chat_id: int) -> list[dict]:
    row = pf._q("SELECT messages FROM chats WHERE id=?", (chat_id,))
    return json.loads(row[0]["messages"]) if row else []


def save(chat_id: int | None, messages: list[dict]) -> int:
    title = next((m["content"] for m in messages if m["role"] == "user"), "New chat")[:70]
    if chat_id:
        pf._q("UPDATE chats SET messages=?, updated=? WHERE id=?", (json.dumps(messages), time.time(), chat_id))
        return chat_id
    pf._q("INSERT INTO chats (title, updated, messages) VALUES (?,?,?)", (title, time.time(), json.dumps(messages)))
    return pf._q("SELECT MAX(id) AS id FROM chats")[0]["id"]


def delete(chat_id: int) -> None:
    pf._q("DELETE FROM chats WHERE id=?", (chat_id,))
