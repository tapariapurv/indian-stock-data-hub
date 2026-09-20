"""Runs the real app headlessly: first the empty home screen, then with a
realistic analysis result injected, so every tab's rendering code executes.
Catches Streamlit API changes after a dependency upgrade.
Run: python tests/test_app_smoke.py   (no network or Ollama needed)"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
# `streamlit run` puts the script's folder on the path; AppTest does not.
sys.path.insert(0, str(ROOT))
APP = str(ROOT / "app.py")

at = AppTest.from_file(APP, default_timeout=60).run()
assert not at.exception, [e.value for e in at.exception]
assert at.title[0].value == "Indian stock data hub"
assert at.session_state["settings"], "settings should load"
assert any(b.label == "Run analysis" for b in at.button), "Run button missing"

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
for expected in ["Overview", "Financials", "All numbers", "Filings"]:
    assert any(expected in l for l in labels), f"{expected} tab missing from {labels}"
print(f"ok: app renders the home screen and every result tab ({len(labels)} tabs)")

# Every other page renders too, given the settings the entry point loads.
import settings as cfg

for page in ["archive_search.py", "history.py", "settings_page.py"]:
    page_at = AppTest.from_file(str(ROOT / "app_pages" / page), default_timeout=60)
    page_at.session_state["settings"] = cfg.load()
    page_at.run()
    assert not page_at.exception, (page, [e.value for e in page_at.exception])
print("ok: archive, history and settings pages render")

# The archive's keyword search must work with no model at all, and fast.
import re  # noqa: E402

arch = AppTest.from_file(str(ROOT / "app_pages" / "archive_search.py"), default_timeout=90)
arch.session_state["settings"] = cfg.load()
arch.run()
assert [t.label for t in arch.tabs][:2] == [":material/search: Find", ":material/forum: Ask"]
assert arch.chat_input, "the Ask tab needs a chat box"
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
