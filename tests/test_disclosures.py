"""Self-check for the red-flag scanner and the announcement tagger.

No network, no model.  Run: python tests/test_disclosures.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import disclosures as d  # noqa: E402

kinds = lambda pages: {(f["kind"], f["severity"]) for f in d.red_flags(pages)}

# Boilerplate every annual report carries must stay silent.
clean = [(40, "The Auditors' Report does not contain any qualification, reservation, adverse remark or "
              "emphasis of matter. We conclude on whether a material uncertainty exists related to events "
              "that may cast doubt on the Company's ability to continue as a going concern. "
              "There has been no change in accounting policies during the year.")]
assert d.red_flags(clean) == [], d.red_flags(clean)
# Seen in real reports: a charter's "if any", an index line, a tax asset that is not a loan.
noise = [(142, "The committee reviews all significant changes in accounting policies during the year, if any."),
         (143, "2.23 Contingent liabilities and commitments ........................ 12"),
         (108, "Related party disclosures\nAdvances Income Tax (Net of Provision) 124 -")]
assert d.red_flags(noise) == [], d.red_flags(noise)

qualified = [(182, "Basis for Qualified Opinion\nIn our opinion, except for the possible effects of the matter "
                   "described above, the financial statements give a true and fair view."),
             (183, "Emphasis of Matter\nWe draw attention to Note 42 regarding the pending litigation.")]
assert ("Auditor", "high") in kinds(qualified) and ("Auditor", "medium") in kinds(qualified), kinds(qualified)
assert d.red_flags(qualified)[0]["severity"] == "high", "worst flag first"
assert d.red_flags(qualified)[0]["page"] == 182, "a flag keeps its page"

policy = [(90, "During the year the Company changed its accounting policy. The change in accounting policy "
               "for revenue recognition has been applied retrospectively.")]
assert ("Accounting policy", "medium") in kinds(policy), kinds(policy)

# Related-party loans: this year, last year are the last two columns.
rpt = [(210, "Note 38 Related party disclosures\nPromoter Holdings Pvt Ltd\nLoans given 1,250.00 300.00\n"
             "Loans repaid 900.00 100.00\nRemuneration 45.20 40.10")]
flags = d.red_flags(rpt)
assert [f["kind"] for f in flags] == ["Related-party loans"], flags
assert "4.2x" in flags[0]["text"], flags[0]["text"]
steady = [(210, "Related party transactions\nLoans given 310.00 300.00")]
assert d.red_flags(steady) == [], "a flat loan book is not a spike"
new_loan = [(210, "Related party transactions\nInter-corporate deposits placed 500.00 -")]
assert "new this year" in d.red_flags(new_loan)[0]["text"]
assert d._amounts("Note 38 (2025) 1,250.00 (300.00)") == [1250.0, -300.0], d._amounts("Note 38 (2025) 1,250.00 (300.00)")

contingent = [(230, "Contingent liabilities\nClaims against the Company not acknowledged as debts 812.40 640.10\n"
                    "Income tax demand under appeal 120.00 95.00")]
assert len(d.red_flags(contingent)) == 2 and kinds(contingent) == {("Contingent liability", "low")}

assert len(d.red_flags([(1, "Emphasis of matter on item %d." % i) for i in range(20)])) == d.PER_KIND, "capped per kind"
print("ok: red-flag scanner")

tag = lambda t: d.tag_announcement(t)
assert tag("Resignation of Chief Financial Officer") == ("Leadership change", "high")
assert tag("Announcement under Regulation 30 (LODR)-Resignation of Statutory Auditors")[0] == "Auditor change"
assert tag("Receipt of Income Tax demand order for AY 2022-23") == ("Tax / legal", "high")
assert tag("Commissioning of new plant with 2 MTPA capacity")[0] == "Capacity / capex"
assert tag("Bagging of large order from NHAI")[0] == "Deal / order"
assert tag("Announcement under Regulation 30 (LODR)-Newspaper Publication25 Sep- Newspaper Advertisement") \
    == ("Routine", "low")
assert tag("Board Meeting Intimation for Approval Of The Financial Results")[0] == "Results & dividends"
assert not d.is_announcement({"url": "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=t.pdf",
                              "category": "Concall Transcript"}), "a call transcript is not an announcement"
assert tag("Certificate under Reg. 74 (5) of SEBI (DP) Regulations") == ("Routine", "low")
assert d.is_announcement({"url": "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=x.pdf"})
assert not d.is_announcement({"url": "https://example.com/ar.pdf"})
assert not d.is_announcement({"url": "https://www.bseindia.com/stock-share-price/x/y/532540/"}), "a stock page"
assert not d.is_announcement({"url": "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=p.pdf", "text": "PPT"}), \
    "a call's slide deck is not an announcement"
assert not d.is_announcement({"url": "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=a.pdf",
                              "category": "Annual Report"}), "an annual report is not an announcement"
print("ok: announcement tagger")
