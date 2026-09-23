"""End-to-end check of the Ask AI chat, against a real model.

This is the one path the other tests never touch: it streams, it builds its
own context out of the portfolio, the saved analyses and the filings, and it
parses follow-up questions back out of the reply. Everything runs against a
throwaway database and local Ollama, so it costs nothing and cannot touch
your own data.

    python tests/test_ask.py                 # first installed model
    python tests/test_ask.py gemma4:e2b      # a particular one
    python tests/test_ask.py --context       # context building only, no model

The context checks need no model at all and always run.
"""
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Captured before the override below, so --live can still find the real
# settings file (and its API key) while everything else stays throwaway.
_REAL_DATA = os.environ.get("STOCK_HUB_DATA")

_tmp = tempfile.TemporaryDirectory()
os.environ["STOCK_HUB_DATA"] = _tmp.name

import archive  # noqa: E402

archive.DB_PATH = Path(_tmp.name) / "ask.db"
archive._has_fts = None
archive.init()

import requests  # noqa: E402

import assistant  # noqa: E402
import llm  # noqa: E402
import portfolio as pf  # noqa: E402
import settings as cfg  # noqa: E402

OLLAMA = "http://localhost:11434"

# Portfolio questions put live prices in the context. These tickers are
# invented, so the lookup would be a slow round trip to Yahoo for nothing.
pf.quotes = lambda stocks: {t: {"price": 500.0, "change": 1.0, "change_pct": 0.2} for t, _ in stocks}


def installed() -> list[str]:
    try:
        return [m["name"] for m in requests.get(f"{OLLAMA}/api/tags", timeout=3).json().get("models", [])]
    except Exception:
        return []


def make_settings(model: str) -> dict:
    import copy
    s = copy.deepcopy(cfg.DEFAULTS)
    s["ai"].update(enabled=True, provider="ollama", model=model, temperature=0.0,
                   num_ctx=2048, timeout=180, tokens_chat=700)
    s["providers"]["ollama"]["base_url"] = OLLAMA
    s["budget"]["enabled"] = False
    return s


# --------------------------------------------------------------------------
# A small but realistic world: two holdings, two analyses, two filings.
# --------------------------------------------------------------------------

def snapshot(ticker, name, verdict, roe, summary, pros, cons, headline):
    return json.dumps({
        "ticker": ticker, "company_name": name, "sector": "Testing", "ai_verdict": verdict,
        "ai_summary": summary,
        "metrics": {"Market Cap": "₹86,900 Cr.", "Current Price": "₹2,140", "Stock P/E": "31.2",
                    "ROE": f"{roe}%", "ROCE": "34.6%", "Debt to equity": "0.04"},
        "pros": pros, "cons": cons,
        "news": {"headlines": [{"title": headline, "source": "ET"}]},
    })


pf.add_account("Test")
ACCT = pf.accounts()[0]["id"]
for ticker, name, verdict, roe, summary, pros, cons, headline in [
    ("ALPHAC", "Alpha Compounders Ltd", "Positive", "28.4",
     "Strong returns and almost no debt.", ["Almost debt free"], ["Trading at 8x book"],
     "Alpha Compounders wins Rs 4,200 crore railway order"),
    ("BETAIND", "Beta Industries Ltd", "Cautious", "7.6",
     "Weak returns and heavy borrowing.", ["Healthy dividend payout"],
     ["Poor return on equity of 7.6%"], "Beta Industries cuts FY27 guidance"),
]:
    pf.save_holding(ACCT, ticker, name, None, 100, 500.0)
    pf._q("INSERT INTO runs (ticker, ts, label, snapshot) VALUES (?,?,?,?)",
          (ticker, time.time(), "test",
           snapshot(ticker, name, verdict, roe, summary, pros, cons, headline)))

archive.store_document("sha-alpha", "ALPHAC", "Concall Transcript", "/tmp/alpha.pdf", [],
                       [(8, "We are targeting capital expenditure of Rs 1,200 crore over the next "
                            "two years, largely towards the new plant at Dahej.")])
archive.store_document("sha-beta", "BETAIND", "Annual Report", "/tmp/beta.pdf", [],
                       [(41, "Net debt stood at Rs 3,400 crore as at the year end, against Rs 2,100 "
                             "crore in the previous year.")])

# --------------------------------------------------------------------------
# Context building -- no model needed
# --------------------------------------------------------------------------

ctx, srcs = assistant.build_context("How is ALPHAC doing?", [])
kinds = [s["kind"] for s in srcs]
assert srcs, "a named company must produce sources"
assert "analysis" in kinds, kinds
assert "Alpha Compounders" in ctx, ctx[:300]
assert "28.4" in ctx, "the ratios have to reach the prompt"

ctx_p, srcs_p = assistant.build_context("Which of my holdings look weakest?", [])
assert srcs_p and srcs_p[0]["kind"] == "portfolio", [s["kind"] for s in srcs_p]
assert "ALPHAC" in ctx_p and "BETAIND" in ctx_p, "a portfolio question must list every holding"

ctx_f, srcs_f = assistant.build_context("what is the capital expenditure plan?", [])
assert any(s["kind"] == "filing" for s in srcs_f), [s["kind"] for s in srcs_f]
assert "1,200 crore" in ctx_f, "the matching filing passage must reach the prompt"

# Filing search can be switched off, and then costs nothing.
_, srcs_off = assistant.build_context("what is the capex plan?", [], use_filings=False)
assert not any(s["kind"] == "filing" for s in srcs_off), "the toggle must actually stop the search"

# Scope pins the conversation to chosen companies.
_, srcs_scope = assistant.build_context("how is it doing?", [], scope=["BETAIND"])
assert {s.get("ticker") for s in srcs_scope if s.get("ticker")} == {"BETAIND"}, srcs_scope

# A follow-up with no company name keeps the company already under discussion.
history = [{"role": "user", "content": "How is ALPHAC doing?"},
           {"role": "assistant", "content": "Alpha Compounders is doing well [1]."}]
_, srcs_follow = assistant.build_context("and its margins?", history)
assert any(s.get("ticker") == "ALPHAC" for s in srcs_follow), \
    f"a follow-up lost the company: {[s.get('ticker') for s in srcs_follow]}"

# "Which companies mentioned X?" cannot be answered from the handful of
# passages the model reads. Nine companies mention Dahej here, and only six
# passages are ever shown, so without a whole-archive tally the answer is
# confidently six.
for n in range(9):
    archive.store_document(f"sha-many-{n}", f"MANY{n}", "Annual Report", "/tmp/m.pdf", [],
                           [(2, f"Our Dahej facility expanded capacity by {n + 5}% this year.")])
ctx_t, srcs_t = assistant.build_context("which companies mentioned Dahej?", [])
tally = [s for s in srcs_t if s["kind"] == "tally"]
assert tally, f"no whole-archive tally: {[s['kind'] for s in srcs_t]}"
# Ten, not nine: ALPHAC's transcript names Dahej too, and the tally finds it.
assert "10 companies" in ctx_t, ctx_t[ctx_t.find("Counted across"):][:200]
for name in [f"MANY{n}" for n in range(9)] + ["ALPHAC"]:
    assert name in ctx_t, f"{name} missing from the tally"
# A narrative question must not drag a tally in.
_, srcs_narrative = assistant.build_context("what did they say about Dahej capacity?", [])
assert not any(s["kind"] == "tally" for s in srcs_narrative), "a tally is only for counting questions"
print("ok: a 'which companies' question is counted across the whole archive, not the sample")

# Sources are numbered from 1 and the prompt's [n] markers line up with the chips.
marks = [int(m) for m in re.findall(r"^\[(\d+)\]", ctx_p, re.M)]
assert marks == list(range(1, len(srcs_p) + 1)), (marks, len(srcs_p))
print(f"ok: context is built from portfolio, analyses and filings; "
      f"scope, the filings toggle and follow-ups all hold")

# --------------------------------------------------------------------------
# The reply itself
# --------------------------------------------------------------------------

def ask(question, settings, history=None, **kw):
    holder = {}
    text = "".join(assistant.answer(question, history or [], settings, holder, **kw))
    return text, holder


def check(label: str, s: dict) -> int:
    failures = 0

    def ok(name, passed, detail=""):
        nonlocal failures
        print(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not passed else ""))
        failures += not passed

    print(f"\n{'=' * 78}\n  Ask AI · {label}\n{'=' * 78}")

    streamed, h = ask("How is ALPHAC doing?", s)
    answer = h.get("text") or ""
    ok("answers at all", bool(answer.strip()), h.get("error") or "empty")
    ok("streams the same text it stores", streamed.strip() == answer.strip(),
       f"streamed {len(streamed)} vs stored {len(answer)}")
    ok("never shows the FOLLOWUPS marker", assistant.MARKER not in streamed, streamed[-120:])
    ok("cites a source", bool(re.search(r"\[\d+\]", answer)), answer[:150])
    ok("reports which model answered", bool(h.get("model")), str(h.get("model")))
    ok("returns the sources it used", bool(h.get("sources")), str(h.get("sources")))
    ok("suggests follow-ups", len(h.get("followups") or []) >= 1, str(h.get("followups")))
    ok("follow-ups are questions, not fragments",
       all(q.endswith("?") and len(q) > 8 for q in (h.get("followups") or [])), str(h.get("followups")))
    ok("cites nothing that does not exist",
       all(1 <= int(n) <= len(h.get("sources") or []) for n in re.findall(r"\[(\d+)\]", answer)),
       f"{re.findall(r'[(]?\\[(\\d+)\\]', answer)} vs {len(h.get('sources') or [])} sources")

    # The portfolio question: it has to use the portfolio, and pick the weak one.
    _, hp = ask("Which of my two holdings looks weakest, and why?", s)
    weak = (hp.get("text") or "")
    ok("uses the portfolio source", any(x["kind"] == "portfolio" for x in hp.get("sources") or []),
       str([x["kind"] for x in hp.get("sources") or []]))
    ok("names the weaker holding", "BETAIND" in weak.upper() or "BETA" in weak.upper(), weak[:150])

    # A follow-up in a real conversation.
    convo = [{"role": "user", "content": "How is ALPHAC doing?"},
             {"role": "assistant", "content": answer[:400] or "Alpha Compounders is doing well [1]."}]
    _, hf = ask("and what is its capex plan?", s, history=convo)
    follow = hf.get("text") or ""
    ok("a follow-up still answers", bool(follow.strip()), hf.get("error") or "empty")
    ok("a follow-up finds the filing", "1,200" in follow or "1200" in follow, follow[:150])

    # The wording gap: the filing says "capital expenditure", the question
    # says "capex". A plain keyword search finds nothing, so the chat used to
    # answer "no mention of that" about a filing it was holding.
    _, hx = ask("what is the capex plan?", s)
    capex = hx.get("text") or ""
    ok("finds a filing through different wording",
       any(x["kind"] == "filing" for x in hx.get("sources") or []),
       str([x["kind"] for x in hx.get("sources") or []]))
    ok("answers the capex question with the real figure", "1,200" in capex or "1200" in capex, capex[:150])

    # With many candidates, the model picks which passages actually answer.
    # The archive is padded with pages that repeat the question's words in
    # another sense, which is exactly what ranking is for.
    for n in range(12):
        archive.store_document(
            f"sha-noise-{n}", "ALPHAC", "Annual Report", "/tmp/noise.pdf", [],
            [(100 + n, f"The authorised share capital of the Company stands at Rs {500 + n} crore, "
                       f"divided into equity shares of Rs 10 each. Human capital remains our "
                       f"greatest asset across {12 + n} locations.")])
    started = time.time()
    _, hr = ask("what is the capital expenditure plan?", s)
    took = time.time() - started
    ranked = hr.get("text") or ""
    filings = [x for x in hr.get("sources") or [] if x["kind"] == "filing"]
    ok("ranking keeps the answer to a few passages", 0 < len(filings) <= assistant.KEEP, str(len(filings)))
    ok("ranking still finds the real capex passage",
       any(x.get("page") == 8 for x in filings), str([x.get("page") for x in filings]))
    ok("still answers with the figure after ranking", "1,200" in ranked or "1200" in ranked, ranked[:150])
    print(f"        (that turn took {took:.0f}s, ranking {assistant.CANDIDATES} candidates)")

    # Nothing in the archive about this: it must say so rather than invent.
    _, hn = ask("What did ALPHAC say about its Antarctic division?", s)
    none = (hn.get("text") or "").lower()
    ok("says when it does not know",
       any(p in none for p in ("no ", "not ", "does not", "doesn't", "no mention", "nothing")),
       none[:150])
    ok("invents no Antarctic figures", "antarctic" not in none or "[" in none, none[:150])
    return failures


if __name__ == "__main__":
    if "--live" in sys.argv:
        # Whatever Settings actually points at. Spends real money on a paid
        # key, which is why it is opt-in -- and the Google streaming path
        # (system folded into the first turn) exists nowhere else.
        real = Path(_REAL_DATA or Path.home() / ".stock-data-hub") / "settings.json"
        if not real.exists():
            print(f"No settings file at {real} — nothing to test live against.")
            _tmp.cleanup()
            sys.exit(0)
        live = cfg._merge(cfg.DEFAULTS, json.loads(real.read_text()))
        live["ai"]["temperature"] = 0.0
        live["budget"]["enabled"] = False   # this is a test, not a run against the month's cap
        print(f"\nLIVE: {live['ai']['provider']} · {live['ai']['model']}")
        bad = check(f"{live['ai']['provider']}/{live['ai']['model']}", live)
        print(f"\n{'ok: Ask AI works on the configured provider' if not bad else f'{bad} check(s) FAILED'}")
        _tmp.cleanup()
        sys.exit(1 if bad else 0)
    if "--context" in sys.argv:
        print("\nok: context checks only")
        _tmp.cleanup()
        sys.exit(0)
    have = installed()
    if not have:
        print("\nOllama is not running — context checks passed; skipping the model checks.")
        _tmp.cleanup()
        sys.exit(0)
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    total = 0
    for m in (args or have[:1]):
        if m not in have:
            print(f"{m} is not installed (have: {', '.join(have)})")
            continue
        total += check(m, make_settings(m))
    print(f"\n{'=' * 78}\n{'ok: Ask AI behaves like a chatbot should' if not total else f'{total} check(s) FAILED'}")
    _tmp.cleanup()
    sys.exit(1 if total else 0)
