"""Self-check for the screener: query parsing, and the guidance fields built
from the app's own graded promises.

Throwaway database, no network, no model.  Run: python tests/test_screener.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import archive  # noqa: E402

tmp = tempfile.TemporaryDirectory()
archive.DB_PATH = Path(tmp.name) / "test.db"
archive._has_fts = None
archive.init()

import pandas as pd  # noqa: E402

import portfolio as pf  # noqa: E402
import screener as sc  # noqa: E402


def save_company(ticker, name, roe, pe):
    snap = json.dumps({"company_name": name, "sector": "Testing",
                       "metrics": {"ROE": f"{roe}%", "Stock P/E": str(pe),
                                   "Current Price": "100", "Book Value": "50"},
                       "ai_verdict": "Positive"})
    pf._q("INSERT INTO runs (ticker, ts, label, snapshot) VALUES (?,?,?,?)",
          (ticker, time.time(), "test", snap))


def grade(ticker, statuses, offset=0):
    for i, status in enumerate(statuses, start=offset):
        pf._q("INSERT INTO guidance (ticker, quarter, metric, claim, horizon, created, status) "
              "VALUES (?,?,?,?,?,?,?)",
              (ticker, f"Q{i}", "margin", f"claim {i}", "FY27", time.time(), status))


# KEEPER delivered 3 of 4; BREAKER delivered none; QUIET was never graded.
save_company("KEEPER", "Keeper Ltd", 22, 20)
save_company("BREAKER", "Breaker Ltd", 25, 18)
save_company("QUIET", "Quiet Ltd", 30, 15)
grade("KEEPER", ["Delivered", "Delivered", "Delivered", "Missed"])
grade("BREAKER", ["Missed", "Missed"])
grade("QUIET", ["Unclear", "Unclear"])   # graded, but says nothing either way

scores = sc.guidance_scores()
assert scores["KEEPER"] == (4, 75.0), scores
assert scores["BREAKER"] == (2, 0.0), scores
assert "QUIET" not in scores, "Unclear is not evidence and must not count"

df = sc.universe(sc.version())
row = lambda t: df[df["Ticker"] == t].iloc[0]
assert row("KEEPER")["Guidance Score"] == 75.0
assert row("KEEPER")["Guidance Checked"] == 4
assert pd.isna(row("QUIET")["Guidance Score"]), "never graded means no score, not zero"

# The preset that ships with the app must actually select the right company.
found = sc.run(df, sc.parse(sc.PRESETS["Management keeps its word"]))
assert list(found["Ticker"]) == ["KEEPER"], list(found["Ticker"])

# A company with no graded promises must never be swept in by a comparison.
assert "QUIET" not in list(sc.run(df, sc.parse("Guidance Score > 0"))["Ticker"])
assert "QUIET" not in list(sc.run(df, sc.parse("Guidance Score < 50"))["Ticker"]), \
    "missing must not read as a low score"

# Both spellings people would type.
assert sc.parse("guidance hit rate > 50") == [[("Guidance Score", ">", 50.0)]]
assert sc.parse("promises graded >= 3") == [[("Guidance Checked", ">=", 3.0)]]

# Grading a promise must invalidate the cached universe, or the screener
# would keep showing yesterday's score.
before = sc.version()
grade("BREAKER", ["Delivered"], offset=90)
assert sc.version() != before, "a newly graded promise has to change the version"
assert sc.universe(sc.version())[lambda d: d["Ticker"] == "BREAKER"]["Guidance Score"].iloc[0] > 0

print("ok: guidance scores count only graded promises, and the preset finds the right company")

tmp.cleanup()
