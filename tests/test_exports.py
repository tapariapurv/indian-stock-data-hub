"""Self-check for the Excel/Word exports. Run: python tests/test_exports.py

Guards the bug Excel reported as "Removed Records: Formula from
/xl/worksheets/sheet4.xml": scraped text starting with "=" was being written
as a formula. Every <f> in the workbook must be one the exporter built itself.
"""
import io
import re
import sys
import zipfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import exports as app_mod
from docx import Document

app = {"build_excel_workbook": app_mod.build_excel_workbook,
       "build_word_document": app_mod.build_word_document,
       "_W_ORDER": app_mod._W_ORDER, "Document": Document}

TRUSTED_FORMULA = re.compile(r"^(COUNTA|COUNTIF|SUM|IFERROR)\(|^'[^']+'![A-Z]+\d+$")

quarterly = pd.DataFrame({
    "Metric": ["Sales", "Expenses", "Operating Profit", "OPM %", "Net Profit", "EPS in Rs", "Raw PDF"],
    "Mar 2026": [100, 80, 20, "20%", 12, 1.5, None],
    "Jun 2026": [110, 85, 25, "23%", 15, 1.8, None],
})
hostile = [  # the real-world trigger, plus a deliberate injection attempt
    "= 175 559 3,286 2,552 – 2,752 262 72 190",
    '=HYPERLINK("http://evil.example","click")',
]
ok = {
    "ticker": "TEST", "ok": True, "company_name": "=Test Industries Ltd", "error": None, "error_friendly": None,
    "metrics": {"Market Cap": "₹16,59,630Cr.", "Current Price": "₹1,226", "High / Low": "₹1,612/1,226",
                "Stock P/E": "42.3", "ROE": "7.71%", "Face Value": "₹10.0"},
    "quarterly_df": quarterly, "shareholding_df": None,
    "pros": ["=1+1 healthy payout"], "cons": ["Low ROE"],
    "documents": [], "downloaded": {"Annual Report": [{"path": "x.pdf", "error": None, "url": "https://example.com/a.pdf"}]},
    "figures": [{"Source": "Annual Report", "Label": h, "Category": "Other", "Value": 1.0, "Currency": "",
                 "Unit": "", "Page": 1, "Context": h} for h in hostile],
    "ai_summary": "=Summary that starts with an equals sign.", "ai_verdict": "Neutral", "ai_model": "test-model",
    "news": {"headlines": [{"title": "=Headline", "source": "ET", "published": None, "url": "https://example.com"}],
             "summary": "News digest.", "sentiment": "Mixed", "model": "test-model"},
}
failed = {"ticker": "BAD", "ok": False, "company_name": None, "error": "HTTP 404", "error_friendly": None}

from core import correlate_numbers
correlated = correlate_numbers(ok)
assert not correlated.empty, "correlated table should not be empty"
assert (correlated["Metric"] == "ROE").any(), "screener ratios should reach the correlated table"
assert (correlated["In filings"] > 0).any(), "PDF figures should reach the correlated table"

xlsx = app["build_excel_workbook"]([ok, failed])
with zipfile.ZipFile(io.BytesIO(xlsx)) as z:
    sheets = [n for n in z.namelist() if n.startswith("xl/worksheets/sheet")]
    formulas = [f for n in sheets for f in re.findall(r"<f>([^<]*)</f>", z.read(n).decode())]
    xml = "".join(z.read(n).decode() for n in sheets)
bad = [f for f in formulas if not TRUSTED_FORMULA.match(f.replace("&quot;", '"'))]
assert formulas, "expected the exporter's own formulas"
assert not bad, f"scraped text leaked into formulas: {bad}"
assert "HYPERLINK" in xml and "evil.example" in xml, "hostile text should survive as plain text"

# Excel offers to "repair" a sheet that has both a worksheet-level autoFilter
# and a Table over the same range -- the Table already provides the filters.
with zipfile.ZipFile(io.BytesIO(xlsx)) as z:
    table_refs = {}
    for name in [n for n in z.namelist() if n.startswith("xl/tables/")]:
        xml = z.read(name).decode()
        table_refs[re.search(r'displayName="([^"]+)"', xml).group(1)] = \
            re.search(r'<table [^>]*ref="([^"]+)"', xml).group(1)
    assert table_refs, "expected the exporter's tables"
    assert len(set(table_refs)) == len(table_refs), f"duplicate table names: {table_refs}"
    for name in [n for n in z.namelist() if n.startswith("xl/worksheets/sheet")]:
        sheet_xml = z.read(name).decode()
        sheet_filter = re.search(r'<autoFilter ref="([^"]+)"', sheet_xml)
        if sheet_filter and sheet_filter.group(1) in table_refs.values():
            raise AssertionError(
                f"{name}: worksheet autoFilter duplicates a Table over {sheet_filter.group(1)}; "
                "Excel treats that as damage")
print(f"ok: {len(table_refs)} table(s), no filter conflicts")

docx = app["build_word_document"]([ok, failed])
app["Document"](io.BytesIO(docx))

# Word rejects property elements that are out of schema order ("unreadable content").
from lxml import etree
order = app["_W_ORDER"]
with zipfile.ZipFile(io.BytesIO(docx)) as z:
    parts = [n for n in z.namelist() if re.match(r"word/(document|footer\d*|header\d*|styles)\.xml$", n)]
    checked = 0
    for name in parts:
        for el in etree.fromstring(z.read(name)).iter():
            ranks = order.get(etree.QName(el).localname)
            if not ranks or not isinstance(el.tag, str):
                continue
            seq = [ranks.index(c.tag) for c in el if c.tag in ranks]
            assert seq == sorted(seq), f"{name}: <{etree.QName(el).localname}> children out of schema order"
            checked += 1
print(f"ok: {len(correlated)} metrics correlated; {len(formulas)} formulas, all built by the exporter; Word document opens; "
      f"{checked} property blocks in schema order")
