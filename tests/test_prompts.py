"""Scores the app's prompts against a real model, so prompt changes are
measured rather than argued about.

Runs against local Ollama only: free, offline, and never touches your real
archive or spend ledger (both are redirected to a throwaway database). The
point is not to prove a big model can do this -- it is to hold the prompts to
a standard on the *small* local models the app ships with by default.

    python tests/test_prompts.py                 # default model
    python tests/test_prompts.py qwen2.5:0.5b    # the adversarial floor
    python tests/test_prompts.py --all           # every installed model

Each check is deterministic: parsed-or-not, the verdict against an expected
set, and -- the one that matters for a finance app -- whether every figure in
the answer actually came from the input.
"""
import concurrent.futures as futures
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import archive  # noqa: E402

# Before anything imports the real database: evals must not pollute the
# user's saved analyses or their spend ledger.
_tmp = tempfile.TemporaryDirectory()
archive.DB_PATH = Path(_tmp.name) / "eval.db"
archive._has_fts = None

import pandas as pd  # noqa: E402
import requests  # noqa: E402

import llm  # noqa: E402
import settings as cfg  # noqa: E402

OLLAMA = "http://localhost:11434"


def installed() -> list[str]:
    try:
        r = requests.get(f"{OLLAMA}/api/tags", timeout=3)
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def make_settings(model: str) -> dict:
    """Always local, always free, never the user's configured provider."""
    s = cfg.DEFAULTS
    import copy
    s = copy.deepcopy(s)
    s["ai"].update(enabled=True, provider="ollama", model=model, temperature=0.0,
                   num_ctx=4096, timeout=180)
    s["providers"]["ollama"]["base_url"] = OLLAMA
    s["budget"]["enabled"] = False
    return s


# --------------------------------------------------------------------------
# Fixtures. Numbers are realistic Indian-market shapes; the weak one is the
# case the README documents a small model getting wrong (7.6% ROE called
# "a strong position").
# --------------------------------------------------------------------------

WEAK = dict(
    ticker="WEAKCO", company_name="Weak Industries Ltd",
    metrics={"Market Cap": "₹12,400 Cr.", "Current Price": "₹310", "Stock P/E": "68.4",
             "Book Value": "₹142", "Dividend Yield": "0.00%", "ROCE": "8.10%", "ROE": "7.60%",
             "Debt to equity": "1.85", "Face Value": "₹10"},
    quarterly=pd.DataFrame({"Metric": ["Sales", "Operating Profit", "OPM", "Net Profit", "EPS"],
                            "Mar 2026": [1420, 158, "11%", 62, 1.55],
                            "Jun 2026": [1305, 119, "9%", 34, 0.85]}),
    pros=["Company has been maintaining a healthy dividend payout"],
    cons=["Poor return on equity of 7.6% over last 3 years",
          "Company has high debtors of 148 days", "Promoter holding has decreased"],
    expect_verdict={"Cautious", "Neutral"}, forbid=["strong position", "strong financial"])

STRONG = dict(
    ticker="STRONGCO", company_name="Strong Compounders Ltd",
    metrics={"Market Cap": "₹86,900 Cr.", "Current Price": "₹2,140", "Stock P/E": "31.2",
             "Book Value": "₹268", "Dividend Yield": "1.20%", "ROCE": "34.60%", "ROE": "28.40%",
             "Debt to equity": "0.04", "Face Value": "₹2"},
    quarterly=pd.DataFrame({"Metric": ["Sales", "Operating Profit", "OPM", "Net Profit", "EPS"],
                            "Mar 2026": [3180, 782, "25%", 545, 17.2],
                            "Jun 2026": [3510, 898, "26%", 631, 19.9]}),
    pros=["Company has delivered good profit growth of 24.8% CAGR over last 5 years",
          "Company is almost debt free", "Company has a good return on equity (ROE) track record"],
    cons=["Stock is trading at 8.0 times its book value"],
    expect_verdict={"Positive"}, forbid=[])

CASES = [WEAK, STRONG]

NEWS_BAD = ("Weak Industries Ltd", [
    {"title": "Weak Industries cuts FY27 revenue guidance after weak monsoon demand", "source": "Mint"},
    {"title": "SEBI opens probe into Weak Industries' related-party transactions", "source": "ET"},
    {"title": "Brokerage downgrades Weak Industries to Sell, cuts target to Rs 240", "source": "BS"},
], {"Negative"})

NEWS_GOOD = ("Strong Compounders Ltd", [
    {"title": "Strong Compounders wins Rs 4,200 crore order from Indian Railways", "source": "ET"},
    {"title": "Strong Compounders Q1 profit jumps 16% on margin expansion", "source": "Mint"},
    {"title": "Analysts raise Strong Compounders target price to Rs 2,600", "source": "BS"},
], {"Positive"})

TRANSCRIPT = """
Thank you all for joining the Q1 FY27 earnings call of Strong Compounders Limited.
We expect our EBITDA margin to reach 28% by the end of FY27, up from 26% today.
The management is pleased with the quarter and thanks the team for their efforts.
We are targeting capital expenditure of Rs 1,200 crore over the next two years for the new plant.
It was a reasonably good quarter overall and we remain cautiously optimistic.
We plan to reduce our net debt to zero by March 2027 using internal accruals.
I will now hand over to the operator for questions.
"""

PASSAGES = [
    {"ticker": "STRONGCO", "category": "Annual Report", "page": 42,
     "text": "The authorised share capital of the Company stands at Rs 500 crore divided into equity shares."},
    {"ticker": "STRONGCO", "category": "Concall Transcript", "page": 8,
     "text": "We are targeting capital expenditure of Rs 1,200 crore over the next two years, "
             "largely towards the new plant at Dahej which will add 40% to installed capacity."},
    {"ticker": "STRONGCO", "category": "Investor Presentation", "page": 15,
     "text": "Human capital remains our greatest asset, with 12,000 employees across 14 locations."},
]

# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

NUM_RE = re.compile(r"\d[\d,]*\.?\d*")


def numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in NUM_RE.findall(text or "")}


def unsourced(answer: str, source: str) -> set[str]:
    """Figures in the answer that are not in the input.

    ponytail: only numbers >= 100 are checked. Smaller ones are usually
    margins or growth rates the model computed correctly from two inputs
    (100 -> 112 really is "12%"), and flagging those buries the real
    hallucinations. Raise the floor if fabricated percentages show up.
    """
    have = numbers(source)
    out = set()
    for n in numbers(answer):
        try:
            if float(n) >= 100 and n not in have:
                out.add(n)
        except ValueError:
            pass
    return out


class Report:
    def __init__(self, model):
        self.model, self.rows, self.t0 = model, [], time.time()

    def add(self, name, ok, detail="", tokens=0, secs=0.0):
        self.rows.append((name, bool(ok), detail, tokens, secs))

    def show(self) -> tuple[int, int]:
        passed = sum(1 for _, ok, *_ in self.rows if ok)
        print(f"\n  {'':2} {'check':34} {'tokens':>7} {'secs':>6}  detail")
        print("  " + "-" * 86)
        for name, ok, detail, tokens, secs in self.rows:
            print(f"  {'PASS' if ok else 'FAIL':4} {name:34} {tokens:>7} {secs:>6.1f}  {detail[:110]}")
        total_tokens = sum(r[3] for r in self.rows)
        print(f"  {'-' * 86}\n  {self.model}: {passed}/{len(self.rows)} passed · "
              f"{total_tokens:,} tokens · {time.time() - self.t0:.0f}s total")
        return passed, len(self.rows)


def timed(fn, *a, **k):
    t = time.time()
    out = fn(*a, **k)
    return out, time.time() - t


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------

def check_analyze(case, s, rep):
    (out, secs) = timed(llm.analyze, case["ticker"], case["company_name"], case["metrics"],
                        case["quarterly"], case["pros"], case["cons"], s)
    tag = case["ticker"]
    summary, verdict = out["summary"], out["verdict"]
    rep.add(f"analyze/{tag}: returns a summary", summary, (summary or "nothing")[:90],
            out["tokens"], secs)
    rep.add(f"analyze/{tag}: verdict in {sorted(case['expect_verdict'])}",
            verdict in case["expect_verdict"], f"got {verdict!r}")
    source = (" ".join(f"{k} {v}" for k, v in case["metrics"].items())
              + " " + case["quarterly"].to_string()
              + " " + " ".join(case["pros"] + case["cons"]))
    bad = unsourced(summary or "", source)
    rep.add(f"analyze/{tag}: no invented figures", not bad, f"unsourced: {sorted(bad)}" if bad else "")
    low = (summary or "").lower()
    hit = [p for p in case["forbid"] if p in low]
    rep.add(f"analyze/{tag}: no false praise", not hit, f"said: {hit}" if hit else "")
    return out


def check_news(fixture, s, rep):
    name, heads, expect = fixture
    (out, secs) = timed(llm.summarize_news, name, heads, s)
    tag = "bad" if "Negative" in expect else "good"
    rep.add(f"news/{tag}: returns a digest", out["summary"], (out["summary"] or "nothing")[:90],
            out["tokens"], secs)
    rep.add(f"news/{tag}: sentiment in {sorted(expect)}", out["sentiment"] in expect,
            f"got {out['sentiment']!r}")
    bad = unsourced(out["summary"] or "", " ".join(h["title"] for h in heads))
    rep.add(f"news/{tag}: no invented figures", not bad, f"unsourced: {sorted(bad)}" if bad else "")


def check_guidance(s, rep):
    (res, secs) = timed(llm.extract_guidance, "Strong Compounders Ltd", "Q1 FY27", TRANSCRIPT, s)
    claims, tokens = res
    joined = " ".join(f"{c['metric']} {c['claim']} {c['horizon']}" for c in claims).lower()
    rep.add("guidance: finds commitments", bool(claims), f"{len(claims)} found", tokens, secs)
    for want, label in (("28", "margin target"), ("1,200", "capex"), ("debt", "debt")):
        rep.add(f"guidance: captures {label}", want.replace(",", "") in joined.replace(",", ""),
                joined[:90])
    rep.add("guidance: skips the vague line", "pleased with the quarter" not in joined, joined[:90])

    (res2, secs2) = timed(llm.judge_guidance, "Strong Compounders Ltd",
                          {"metric": "EBITDA margin", "claim": "EBITDA margin to reach 28% by FY27",
                           "horizon": "FY27"},
                          "Q2 FY27 reported: OPM 22%, down from 26%. Net profit fell 8%.", s)
    verdict, tokens2 = res2
    rep.add("guidance: grades a miss as Missed", verdict["status"] == "Missed",
            f"got {verdict['status']!r}: {(verdict['why'] or '')[:60]}", tokens2, secs2)


def check_retrieval(s, rep):
    (res, secs) = timed(llm.expand_query, "what is the capex plan?", s)
    terms, tokens = res
    joined = " ".join(terms).lower()
    rep.add("expand: suggests search terms", len(terms) >= 2, f"{terms}", tokens, secs)
    rep.add("expand: includes the formal term", "capital expenditure" in joined, joined[:90])

    many = PASSAGES + [dict(p, page=p["page"] + 100) for p in PASSAGES] + \
           [dict(p, page=p["page"] + 200) for p in PASSAGES]
    (res2, secs2) = timed(llm.rerank_passages, "what is the capex plan?", many, s, 3)
    order, tokens2 = res2
    rep.add("rerank: puts the capex passage first",
            bool(order) and many[order[0]]["category"] == "Concall Transcript",
            f"order {order[:4]}", tokens2, secs2)

    (res3, secs3) = timed(llm.synthesize_search, "what is the capex plan?", PASSAGES, s)
    answer, tokens3 = res3
    rep.add("answer: returns prose", answer, (answer or "nothing")[:90], tokens3, secs3)
    rep.add("answer: uses the real figure", "1,200" in (answer or "") or "1200" in (answer or ""),
            (answer or "")[:90])
    rep.add("answer: cites a passage", bool(re.search(r"\[\d\]", answer or "")), (answer or "")[:90])
    bad = unsourced(answer or "", " ".join(p["text"] for p in PASSAGES))
    rep.add("answer: no invented figures", not bad, f"unsourced: {sorted(bad)}" if bad else "")


def run(model: str) -> tuple[int, int]:
    s = make_settings(model)
    rep = Report(model)
    print(f"\n{'=' * 90}\n  {model}\n{'=' * 90}")
    # Independent calls, run together: Ollama queues them, but the wall-clock
    # win is real when a model is slow to first token.
    with futures.ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(check_analyze, c, s, rep) for c in CASES]
        jobs += [pool.submit(check_news, f, s, rep) for f in (NEWS_BAD, NEWS_GOOD)]
        jobs.append(pool.submit(check_guidance, s, rep))
        jobs.append(pool.submit(check_retrieval, s, rep))
        for j in futures.as_completed(jobs):
            j.result()
    rep.rows.sort(key=lambda r: r[0])
    return rep.show()


def offline_checks():
    """The parts that need no model at all, so they run on every machine."""
    v = llm.verdict_from_metrics
    assert v(WEAK["metrics"], WEAK["quarterly"]) == "Cautious", "7.6% ROE is not Positive"
    assert v(STRONG["metrics"], STRONG["quarterly"]) == "Positive", v(STRONG["metrics"], STRONG["quarterly"])
    assert v({"ROE": "25%"}, None) == "Neutral", "ROE alone must not carry a Positive"
    assert v({"ROE": "25%", "Debt to equity": "2.4"}, None) == "Cautious", "leverage outranks ROE"
    assert v({}, None) is None, "no ratios means no rule-based verdict"
    assert v({"Market Cap": "₹5 Cr."}, None) is None, "market cap decides nothing on its own"
    assert v({"ROE": "15%"}, None) == "Neutral", "a middling ROE with nothing else is Neutral"

    # Parsing, against the replies that actually broke it.
    nested = ('{"verdict": "Positive", "summary": "Good.", '
              '"key_ratios": {"Sales": 3510, "OPM": 26}}')
    assert llm._json_reply(nested, "verdict")["verdict"] == "Positive", "nesting must not win"
    cut = '{\n "verdict": "Cautious",\n "summary": "Margins fell and debt is high'
    assert llm._json_salvage(cut)["verdict"] == "Cautious", "a truncated reply must still parse"
    assert llm._field('"verdict": "Positive"', "verdict", "Positive|Neutral|Cautious") == "Positive", \
        "JSON punctuation must not hide the field"
    assert llm._strip_markers("[END] Real answer here.") == "Real answer here."
    assert llm._two_fields(cut, "verdict", "Positive|Neutral|Cautious", "summary")[0] == "Cautious"
    print("ok: verdict rules and reply parsing (no model needed)")


if __name__ == "__main__":
    offline_checks()
    have = installed()
    if not have:
        print("Ollama is not running at localhost:11434 — start it, or `ollama pull qwen2.5:0.5b`.")
        sys.exit(0)
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    models = have if "--all" in sys.argv else (args or have[:1])
    total_ok = total = 0
    for m in models:
        if m not in have:
            print(f"{m} is not installed (have: {', '.join(have)})")
            continue
        ok, n = run(m)
        total_ok += ok
        total += n
    print(f"\n{'=' * 90}\nTOTAL {total_ok}/{total}")
    _tmp.cleanup()
