"""Runs the real app headlessly: first the empty home screen, then with a
realistic analysis result injected, so every tab's rendering code executes.
Catches Streamlit API changes after a dependency upgrade.
Run: python tests/test_app_smoke.py   (no network or Ollama needed)"""
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Before anything reads it: the whole app is pointed at a throwaway data
# directory, so a test run cannot read or write the real portfolio, archive
# or spend ledger. STOCK_HUB_DATA is the same switch the app documents.
_tmp = tempfile.TemporaryDirectory()
os.environ["STOCK_HUB_DATA"] = _tmp.name

import pandas as pd  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
# `streamlit run` puts the script's folder on the path; AppTest does not.
sys.path.insert(0, str(ROOT))
APP = str(ROOT / "app.py")

import portfolio as _pf  # noqa: E402
import ui as _ui  # noqa: E402

# "3th percentile" once reached the Stock page.
assert [_ui.inr(x) for x in (426984, 999, 1000, 12345678, -48016, 0)] == \
    ["₹4,26,984", "₹999", "₹1,000", "₹1,23,45,678", "-₹48,016", "₹0"]
assert _ui.inr(2572, signed=True) == "+₹2,572" and _ui.inr(-0.4) == "₹0" and _ui.inr(1234.5, decimals=2) == "₹1,234.50"
assert [_ui.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 42.4, 100)] == \
    ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "42nd", "100th"]

# Offline: no price lookups, and no background refresher analysing the
# seeded holdings over the network the moment the app starts.
_pf.start_refresher = lambda: None
_pf.quotes = lambda stocks: {}
_pf.performance = lambda *a, **k: None
_pf.calendar = lambda *a, **k: []
_pf.history = lambda *a, **k: None
_pf.pe_history = lambda *a, **k: None
# The whole-market lists come from the NSE and Yahoo; offline stand-ins keep this test off the network.
import screener as _sc  # noqa: E402

_sc.listed = lambda day: pd.DataFrame({"Ticker": ["DEMO", "BIGCO", "TINYSME"],
                                       "Company": ["Demo Industries Ltd", "Big Company Ltd", "Tiny SME Ltd"],
                                       "SME": [False, False, True]})
_sc.market = lambda day: pd.DataFrame({"Ticker": ["BIGCO", "TINYSME"], "Market Cap": [50000.0, 80.0],
                                       "Current Price": [100.0, 20.0], "P/E": [9.0, 8.0]})

# Seeded before the first render: the Archive page caches its document count,
# so anything added later is invisible for the rest of the run.
import archive as _ar  # noqa: E402

_ar.store_document("sha-smoke", "DEMO", "Annual Report", str(ROOT / "demo.pdf"), [],
                   [(1, "Capital expenditure of Rs 1,200 crore is planned for the new plant."),
                    (2, "The authorised share capital of the Company stands at Rs 500 crore.")])

import settings as cfg  # noqa: E402

home = AppTest.from_file(APP, default_timeout=60).run()
assert not home.exception, [e.value for e in home.exception]
assert home.title[0].value.startswith("Good "), "the app should open on Home"
assert home.session_state["settings"], "settings should load"
assert home.selectbox(key="home_search").placeholder.startswith("Look up any company"), "Home needs its search box"
assert "Tiny SME Ltd · TINYSME · SME" in home.selectbox(key="home_search").options, "SMEs are in the search list"
assert "Get started" in " ".join(m.value for m in home.markdown), "an empty portfolio shows the first steps"
home.selectbox(key="home_search").set_value("BIGCO").run()
assert not home.exception and home.markdown[0].value == "# Big Company Ltd", "picking a search result opens it"
print("ok: the app opens on Home with the company search and first steps")

at = AppTest.from_file(APP, default_timeout=60).run()
at.switch_page("app_pages/research.py").run()
assert not at.exception, [e.value for e in at.exception]
assert at.title[0].value == "Research"
assert any(b.label == "Analyse" for b in at.button), "Analyse button missing"

quarterly = pd.DataFrame({
    "Metric": ["Sales", "Expenses", "Operating Profit", "OPM %", "Net Profit", "EPS in Rs", "Raw PDF"],
    "Mar 2026": [100, 80, 20, "20%", 12, 1.5, None],
    "Jun 2026": [110, 85, 25, "23%", 15, 1.8, None],
})
result = {
    "ticker": "DEMO", "ok": True, "company_name": "Demo Industries Ltd", "error": None, "error_friendly": None,
    "metrics": {"Market Cap": "₹1,000Cr.", "Current Price": "₹100", "ROE": "12.5%"},
    "quarterly_df": quarterly, "shareholding_df": quarterly.head(2),
    "pros": ["Healthy margins"], "cons": ["High valuation [sic] $5"],
    "documents": [{"category": "Annual Report", "text": "AR 2026", "url": "https://example.com/a.pdf"}],
    "downloaded": {"Annual Report": [{"path": "a.pdf", "error": None, "url": "https://example.com/a.pdf"}]},
    "figures": [{"Source": "Annual Report", "Label": "Revenue", "Category": "Profitability", "Value": 110.0,
                 "Currency": "₹", "Unit": "cr", "Page": 3, "Context": "Revenue 110"}],
    "ai_summary": "Demo summary.", "ai_verdict": "Positive", "ai_model": "demo-model", "ai_tokens": 400, "ai_at": 0,
    "news": {"headlines": [{"title": "Demo wins [big] order", "source": "ET", "url": "https://example.com/n",
                            "published": datetime.now(timezone.utc)}],
             "summary": "Things look fine.", "sentiment": "Mixed", "tokens": 300, "model": "demo-model", "at": 0},
}
failed = {"ticker": "NOPE", "ok": False, "company_name": None, "error": "HTTP 404", "error_friendly": None}
at.session_state["results"] = [result, failed]
at.run()
assert not at.exception, [e.value for e in at.exception]
labels = [t.label for t in at.tabs]
for expected in ["Summary", "Results", "Every number", "Reports & warnings"]:
    assert any(expected in l for l in labels), f"{expected} tab missing from {labels}"
print(f"ok: the analyse page renders every result tab in plain words ({len(labels)} tabs)")

# Layout settings: the start page, hidden pages and a side menu all take effect.
lay = AppTest.from_file(APP, default_timeout=60)
lay.session_state["settings"] = {**cfg.load(), "ui": {**cfg.load()["ui"], "home": "Screener",
                                                      "hidden_pages": ["Compare", "Home"], "nav": "sidebar"}}
lay.run()
assert not lay.exception, [e.value for e in lay.exception]
assert lay.title and lay.title[0].value == "Screener", "the chosen start page should open"
print("ok: layout settings pick the start page, hide pages (never Home or Settings) and move the menu")

# Every other page renders too, given the settings the entry point loads.

for page in ["home.py", "archive_search.py", "history.py", "settings_page.py", "ask.py", "screener_page.py", "compare.py",
             "portfolio_page.py", "stock.py"]:
    page_at = AppTest.from_file(APP, default_timeout=120).run()   # through app.py: pages link to each other
    page_at.switch_page(f"app_pages/{page}").run()
    assert not page_at.exception, (page, [e.value for e in page_at.exception])
print("ok: archive, history and settings pages render")

# The archive's keyword search must work with no model at all, and fast.
import re  # noqa: E402

arch = AppTest.from_file(APP, default_timeout=90).run()
arch.switch_page("app_pages/archive_search.py").run()
assert not arch.exception, [e.value for e in arch.exception]  # asking now lives on its own Ask AI page
if arch.text_input:                      # skipped when the archive is empty
    arch.text_input[0].set_value("capital")
    arch.button[0].click().run()
    assert not arch.exception, [e.value for e in arch.exception]
    found = re.search(r"\*\*(\d+) match\(es\)\*\* :gray\[· found in (\d+) ms\]",
                      " ".join(m.value for m in arch.markdown))
    if found:
        assert int(found.group(2)) < 3000, f"keyword search took {found.group(2)} ms"
        print(f"ok: keyword search returned {found.group(1)} matches in {found.group(2)} ms, "
              "no model involved")
    else:
        print("ok: keyword search ran (nothing matched in this archive)")

# --- A portfolio with trades in it -------------------------------------------
# The sections that only appear once you track quantities have to render too,
# and the money on screen has to match what the maths says.
_pf.add_account("Smoke account")
_acct = _pf.accounts()[0]["id"]
_pf.save_holding(_acct, "DEMO", "Demo Industries Ltd", None, None, None)
_pf.add_trade(_acct, "DEMO", "buy", 100, 250.0, "2024-05-01", fees=20.0)
_pf.add_trade(_acct, "DEMO", "buy", 50, 300.0, "2025-05-01")
_pf.add_trade(_acct, "DEMO", "sell", 60, 400.0, "2026-05-01", fees=30.0)

port = AppTest.from_file(APP, default_timeout=120).run()
port.switch_page("app_pages/portfolio_page.py").run()
assert not port.exception, [e.value for e in port.exception]

sections = [c for c in port.segmented_control if "Buys & sells" in (c.options or [])]
assert sections, f"Trades section missing from {[c.options for c in port.segmented_control]}"
sections[0].set_value("Trades").run()
assert not port.exception, [e.value for e in port.exception]

_text = " ".join(m.value for m in port.markdown)
assert "Profit on sold shares, by financial year" in _text, _text[:400]
_metrics = {m.label: m.value for m in port.metric}
assert "Yearly return" in _metrics, _metrics
assert _metrics["Buys and sells"] == "3", _metrics
# 60 sold out of the oldest lot at 250.2: (400 - 0.5 charges - 250.2) * 60.
_gains, _unmatched = _pf.fifo_gains(_pf.trades(_acct, "DEMO"))
assert not _unmatched and abs(sum(g["gain"] for g in _gains) - 8958.0) < 1.0, _gains
assert _metrics["Profit on sold shares"] == "₹8,958", _metrics
print(f"ok: portfolio renders with trades, and the page agrees with the maths "
      f"({_metrics['Profit on sold shares']} realised)")

# The stock page renders for a held stock, not just an empty portfolio --
# with its saved analyses shown the way the Research page shows a fresh one.
import core as _core  # noqa: E402

_ar.store_document("sha-a", "DEMO", "Annual Report", "a.pdf", result["figures"], [])
_core.save_run(result, label="smoke")
_core.save_run({**result, "ai_verdict": "Negative"}, label="smoke")
stock = AppTest.from_file(APP, default_timeout=120).run()
stock.session_state["stock"] = "DEMO"
stock.switch_page("app_pages/stock.py").run()
assert not stock.exception, [e.value for e in stock.exception]
labels = [t.label for t in stock.tabs]
for name in ("Summary", "Results", "Every number", "Reports & warnings", "Past checks"):
    assert any(name in label for label in labels), f"stock page lacks the Research tab {name!r}: {labels}"
shown = {m.label: m.value for m in stock.metric}
assert shown.get("Numbers from reports") == "1", f"saved figures not found by filing: {shown}"
print("ok: the company page renders a held stock with the same tabs and figures")

# A company you don't hold still gets its page, with a way to look into it and add it.
other = AppTest.from_file(APP, default_timeout=120).run()
other.session_state["company"] = {"ticker": "NEWCO", "name": "New Company Ltd", "screener_id": None}
other.switch_page("app_pages/stock.py").run()
assert not other.exception, [e.value for e in other.exception]
_buttons = [b.label for b in other.button]
assert "Look into this company" in _buttons, _buttons
assert any(n.label == "How many shares do you own?" for n in other.number_input), "no way to add it to the portfolio"
print("ok: any company gets a page, even one you don't hold")

# The "your stocks" picker starts blank, so picking the first stock in it still opens that stock.
pick = AppTest.from_file(APP, default_timeout=120).run()
pick.switch_page("app_pages/stock.py").run()
assert pick.selectbox(key="company_quick").value is None, "the picker must not pre-select a stock"
pick.selectbox(key="company_quick").set_value("DEMO").run()
assert not pick.exception and pick.markdown[0].value == "# Demo Industries Ltd", [m.value for m in pick.markdown][:2]
print("ok: picking one of your stocks opens it")

# The Screener covers every listed company, and leaves SMEs out until asked.
scr = AppTest.from_file(APP, default_timeout=120).run()
scr.switch_page("app_pages/screener_page.py").run()
scr.text_area(key="screen_query").set_value("Market Cap > 0").run()
assert not scr.exception, [e.value for e in scr.exception]
assert "**2 matches** :gray[· out of 2 listed companies]" in [m.value for m in scr.markdown], [m.value for m in scr.markdown]
scr.checkbox(key="screen_sme").check().run()
assert "**3 matches** :gray[· out of 3 listed companies]" in [m.value for m in scr.markdown], [m.value for m in scr.markdown]
print("ok: the Screener searches every listed company, with SMEs only when ticked")

_tmp.cleanup()
