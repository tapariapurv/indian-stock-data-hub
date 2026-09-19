"""Runs the real app headlessly: first the empty home screen, then with a
realistic analysis result injected, so every tab's rendering code executes.
Catches Streamlit API changes after a dependency upgrade.
Run: python tests/test_app_smoke.py   (no network or Ollama needed)"""
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app.py")

at = AppTest.from_file(APP, default_timeout=60).run()
assert not at.exception, [e.value for e in at.exception]
assert at.title[0].value == "Indian stock data hub"
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
assert [t.label for t in at.tabs][:3] == [":material/dashboard: Overview", ":material/bar_chart: Financials",
                                          ":material/folder_open: Filings"]
print("ok: app renders the home screen and all three result tabs")
