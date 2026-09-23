"""Self-check for the archive, retention and batch input.

Runs against a throwaway database, so your real archive is untouched.
No network and no model needed.  Run: python tests/test_archive.py
"""
import io
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import archive  # noqa: E402

tmp = tempfile.TemporaryDirectory()
archive.DB_PATH = Path(tmp.name) / "test.db"
archive._has_fts = None
assert archive.init() in (True, False)

import core  # noqa: E402
import settings as cfg  # noqa: E402

# --- the extraction cache and full-text index --------------------------------
pages = [(1, "Management expects margins to improve to 18% by FY27."),
         (2, "Gross loan book grew to INR 33,287 crores this quarter.")]
figures = [{"Source": "Concall Transcript", "Label": "Loan Book", "Category": "Loan/Asset Book",
            "Value": 33287.0, "Currency": "INR", "Unit": "cr", "Page": 2, "Context": pages[1][1]}]
archive.store_document("sha-1", "DEMO", "Concall Transcript", "/tmp/demo.pdf", figures, pages)

cached = archive.cached_figures("sha-1")
assert cached and cached[0]["Label"] == "Loan Book", "a parsed file must come back from the cache"
assert archive.cached_figures("never-seen") is None, "an unknown file must miss the cache"
assert "margins" in archive.document_text("DEMO", "Concall Transcript")

hits = archive.search("loan book")
assert hits and hits[0]["ticker"] == "DEMO" and hits[0]["page"] == 2, hits
assert not archive.search("nothinglikethisexists")
# A question with punctuation must not be read as search syntax.
archive.search('what about "margins" (FY27)?')
assert archive.search("margins", tickers=["OTHER"]) == [], "the ticker filter must apply"

# --- saved analyses and what changed -----------------------------------------
quarterly = pd.DataFrame({"Metric": ["Sales", "Net Profit"],
                          "Mar 2026": [100, 12], "Jun 2026": [110, 15]})
result = {"ticker": "DEMO", "ok": True, "company_name": "Demo Ltd",
          "metrics": {"ROE": "7.71%"}, "quarterly_df": quarterly, "pros": ["Healthy margins"],
          "cons": [], "documents": [], "downloaded": {}, "figures": figures,
          "ai_summary": "Fine.", "ai_verdict": "Neutral", "ai_model": "test", "ai_tokens": 10}

first = core.save_run(result, "first")
assert first, "a successful result must be saved"
later = dict(result, metrics={"ROE": "9.10%"}, cons=["New risk"], ai_verdict="Cautious")
_, changes = core.changes_since_last(later)
kinds = {c["kind"] for c in changes}
assert {"Ratio", "Risk", "AI verdict"} <= kinds, kinds
assert any(c["from"] == "7.71%" and c["to"] == "9.10%" for c in changes)

restored = core.restore_snapshot(archive.get_run(first)["snapshot"])
assert restored["quarterly_df"] is not None and restored["restored"]
assert restored["correlated"] is not None and not restored["correlated"].empty

# --- guidance ----------------------------------------------------------------
claims = [{"metric": "Margin", "claim": "improve to 18%", "horizon": "FY27"}]
assert archive.save_claims("DEMO", "Jun 2026", claims) == 1
assert archive.save_claims("DEMO", "Jun 2026", claims) == 0, "the same claim must not double up"
claim_id = archive.claims_for("DEMO")[0]["id"]
archive.set_claim_status(claim_id, "Delivered", "Margin reached 18.4%.", "Sep 2026")
assert archive.claims_for("DEMO")[0]["status"] == "Delivered"

# --- retention ---------------------------------------------------------------
for i in range(5):
    archive.save_run("KEEP", f"run {i}", {"ticker": "KEEP", "metrics": {}})
archive.purge({"keep_runs_days": 0, "keep_documents_days": 0, "keep_text_days": 0,
               "max_runs_per_ticker": 2})
assert len(archive.list_runs("KEEP")) == 2, "the per-company cap must apply"
assert archive.list_runs("DEMO"), "0 days must mean keep forever"

# --- batch input -------------------------------------------------------------
csv = io.BytesIO(b"ticker,notes\nRELIANCE,big\ntcs,\n,\nnot a ticker!,\nRELIANCE,dup\n")
csv.name = "watchlist.csv"
tickers, error = core.tickers_from_upload(csv)
assert tickers == ["RELIANCE", "TCS"] and error is None, (tickers, error)

other = io.BytesIO(b"Symbol\nINFY\n")
other.name = "w.csv"
assert core.tickers_from_upload(other)[0] == ["INFY"], "a 'Symbol' column must work too"
assert core.tickers_from_upload(io.BytesIO(b"x\n\n"))[0] == [], "junk must not raise"

# --- settings ----------------------------------------------------------------
s = cfg.load()
assert s["ai"]["provider"] in cfg.PROVIDERS
assert cfg.base_url(s, "ollama").startswith("http"), cfg.base_url(s, "ollama")
assert cfg.base_url({"providers": {"custom": {"base_url": "0.0.0.0:1234"}}}, "custom") \
    == "http://localhost:1234", "a bind address must be dialled over localhost"
assert cfg.style_css({"custom_style": False}) == "", "the escape hatch must emit no CSS"
assert "--primary-color" in cfg.style_css(s["ui"])

print(f"ok: archive caches, searches ({len(hits)} hit), saves history, diffs "
      f"({len(changes)} changes), tracks guidance, purges, and reads watchlists")

# --- search quality ----------------------------------------------------------
# The retrieval step must survive a natural question: a filing almost never
# contains every word someone types, so terms are OR-ed, not AND-ed.
assert archive.search("what did management say about the loan book?"), \
    "a natural question must still find the passage"
assert archive.search("margins", extra_terms=["operating margin", "profitability"]), \
    "model-suggested terms must widen the search, not break it"
# Stopwords alone must not match every page in the archive.
assert archive._fts_query("what about the") == "", "a question of stopwords yields no query"

# Without a model, smart_search degrades to plain retrieval rather than failing.
plain = core.smart_search("loan book", {**cfg.load(), "ai": {**cfg.load()["ai"], "enabled": False}})
assert plain["hits"] and plain["answer"] is None and plain["tokens"] == 0, plain
# --- coverage, common-word filtering and match windows -----------------------
# The chat once answered "which companies mentioned China?" with two of the
# twenty-three in the archive, because it saw six passages and because
# "companies" and "mentioned" matched nearly every page.
# Frequency filtering needs a corpus big enough for frequencies to mean
# something, so this builds one: "companies" is everywhere, "China" is rare.
filler = [(i, f"Page {i}: companies in this sector reported steady results.")
          for i in range(1, 41)]
archive.store_document("sha-FILLER", "DELTA", "Annual Report", "/tmp/DELTA.pdf", [], filler)
for ticker, text in [("ALPHA", "Our China sourcing improved margins this year."),
                     ("BETA", "Demand from China was weak across companies we track."),
                     ("GAMMA", "No mention of that market; companies here are domestic.")]:
    archive.store_document(f"sha-{ticker}", ticker, "Annual Report", f"/tmp/{ticker}.pdf", [],
                           [(1, text)])

pages, tally = archive.coverage("which companies mentioned china?")
assert set(tally) == {"ALPHA", "BETA"}, tally
assert pages == 2, pages
assert "GAMMA" not in tally, "a page that only says 'companies' must not count as a China match"

sentence = core.coverage_sentence("which companies mentioned china?", pages, tally)
assert sentence and "2 companies" in sentence and "ALPHA" in sentence, sentence
assert core.coverage_sentence("what did they say about margins?", pages, tally) is None, \
    "a narrative question is for the model, not a tally"

# The model must be shown the part of the page that matched, not its opening.
page = "Opening boilerplate. " * 40 + "Capital expenditure will be Rs 5,000 crore next year."
focus = core._focus(page, ["capital expenditure"], width=200)
assert "5,000 crore" in focus and len(focus) < len(page) / 2, focus
assert "capital expenditure" in focus.lower(), focus
print("ok: coverage counts the whole archive and passages show the matching part")

print("ok: natural-language retrieval works with and without a model")

# --- pdfplumber is loaded only when a PDF is actually opened -----------------
# It costs ~22 MB resident, and most sessions never parse one. The flag has
# to be right without paying that, and the real import has to still happen.
assert core.PDFPLUMBER_AVAILABLE, "pdfplumber is installed, so the flag must say so"
assert "pdfplumber" not in sys.modules, "importing core must not pull pdfplumber in"

guide = ROOT / "static" / "user_guide.pdf"
if guide.exists():
    import logging

    logging.getLogger("pdfminer").setLevel(logging.ERROR)  # font warnings on this PDF are noise
    read_pages = []
    figures_found = core.extract_all_numbers(guide, "Annual Report", pages_out=read_pages)
    assert "pdfplumber" in sys.modules, "opening a PDF must import it"
    assert len(read_pages) > 5, f"only {len(read_pages)} pages read"
    assert figures_found, "no figures extracted from a document full of numbers"
    print(f"ok: pdfplumber loads only on demand, then reads {len(read_pages)} pages "
          f"and {len(figures_found)} figures")
else:
    print("ok: pdfplumber stays unimported (no sample PDF to parse)")

tmp.cleanup()
