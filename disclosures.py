"""Code-only reads of a company's filings, free and instant: red flags in the
annual report, and which exchange announcements deserve a look. No model is
involved, so nothing here can be invented -- every flag quotes its page."""

import re

# A match inside a denial is not a flag: every directors' report says the
# auditors made "no qualification, reservation or adverse remark", and every
# audit committee charter reviews "changes in accounting policies, if any".
NEGATED = re.compile(r"\b(no|not|nil|none|neither|nor|without|free from|if any)\b", re.I)

# (kind, severity, pattern) matched against sentences. Phrased to miss the
# boilerplate every audit report carries ("...whether a material uncertainty
# exists...", "...ability to continue as a going concern").
SENTENCE_FLAGS = [
    ("Auditor", "high", r"qualified opinion|adverse opinion|disclaimer of opinion"
                        r"|except for the (?:possible )?effects? of the matters?"),
    ("Auditor", "medium", r"emphasis of matter|material uncertainty related to going concern"),
    ("Accounting policy", "medium", r"change[sd]? in (?:the )?(?:significant )?accounting polic"
                                    r"|change[sd]? in accounting estimate"
                                    r"|restate(?:d|ment) (?:of )?(?:the )?(?:financial|comparative|previous|prior)"),
]
# (kind, severity, pattern) matched against single lines, where the amount sits.
LINE_FLAGS = [
    ("Contingent liability", "low", r"contingent liabilit|not acknowledged as debts?"
                                    r"|(?:tax|gst|income[- ]tax|excise|customs) demand|show[- ]cause notice"),
]
# A loan row inside a related-party note: the label is on the line, the party
# usually on a heading above it, so the page (not the line) must be the RPT note.
RPT_PAGE = re.compile(r"related part", re.I)
RPT_LOAN = re.compile(r"^\W*(?:loans?|inter[- ]corporate deposits?|icds?|guarantees?)\b"
                      r"(?!.*\b(?:repaid|repayment|received|taken|borrowed)\b)", re.I)
NUM = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?")
SEVERITY = {"high": 0, "medium": 1, "low": 2}
PER_KIND = 5


def _amounts(line: str) -> list[float]:
    """Numbers on a line, ignoring note refs and years; a lone dash is nil."""
    out = []
    for raw in NUM.findall(re.sub(r"(?<!\S)[-–—](?!\S)", " 0.00 ", line)):
        neg = raw.startswith("(") or raw.startswith("-")
        n = float(raw.strip("()-").replace(",", "") or 0)
        if n.is_integer() and (n < 100 or 1900 <= n <= 2100) and "," not in raw and "." not in raw:
            continue
        out.append(-n if neg else n)
    return out


def red_flags(pages: list[tuple[int, str]]) -> list[dict]:
    """Flags from an annual report's page texts, worst first:
    {kind, severity, page, text}."""
    found, seen = [], set()

    def add(kind, severity, page, text):
        key = (kind, text[:80].lower())
        if key not in seen and sum(f["kind"] == kind for f in found) < PER_KIND:
            seen.add(key)
            found.append({"kind": kind, "severity": severity, "page": page, "text": text.strip()[:300]})

    for page, text in pages:
        for sentence in re.split(r"(?<=[.;:])\s+", re.sub(r"\s+", " ", text)):
            for kind, severity, pattern in SENTENCE_FLAGS:
                if re.search(pattern, sentence, re.I) and not NEGATED.search(sentence):
                    add(kind, severity, page, sentence)
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        for line in lines:
            for kind, severity, pattern in LINE_FLAGS:
                # An amount, or it is a heading or an index entry ("Contingent liabilities ....").
                # No negation check: "not acknowledged as debts" is the flag itself.
                if re.search(pattern, line, re.I) and "...." not in line and _amounts(line):
                    add(kind, severity, page, line)
        if RPT_PAGE.search(text):
            for line in lines:
                nums = _amounts(line)
                # ponytail: assumes the last two columns are this year, last year
                # (the Schedule III layout); a table ordered otherwise is misread.
                if RPT_LOAN.match(line) and len(nums) >= 2:
                    now, before = nums[-2], nums[-1]
                    if now > 0 and now >= 2 * max(before, 0):
                        why = f"up {now / before:.1f}x on last year" if before > 0 else "new this year"
                        add("Related-party loans", "high", page, f"{line} ({why})")
    return sorted(found, key=lambda f: SEVERITY[f["severity"]])


# (label, severity, pattern) for an announcement's title, first match wins.
# Leadership before orders: "resignation ... order" is about the resignation.
ANNOUNCEMENT_TAGS = [
    ("Auditor change", "high", r"auditor.{0,40}(?:resign|cessation|change|appoint)"
                                r"|(?:resign|cessation|change|appoint)\w*.{0,40}auditor"),
    ("Leadership change", "high", r"resign|cessation|step(?:s|ped)? down|change in (?:kmp|management|directors?)"
                                  r"|appointment of .{0,40}(?:ceo|cfo|managing director|chief)"),
    ("Tax / legal", "high", r"demand|show[- ]cause|penalt|litigation|search|raid|fraud|default|insolvency"
                            r"|nclt|sebi order|forensic|arbitration|court"),
    ("Capacity / capex", "medium", r"capacity|expansion|capex|new plant|commission|greenfield|brownfield"),
    ("Deal / order", "medium", r"\border|contract|acqui|merger|amalgamation|demerger|stake|joint venture|divest"),
    ("Results & dividends", "medium", r"financial results|dividend|buy-?back|bonus|split|fund ?rais|qip|"
                                    r"rights issue|preferential|credit rating|pledge"),
]
ROUTINE = re.compile(r"newspaper|trading window|certificate|investor meet|analysts? (?:/ )?investor|"
                     r"duplicate share|loss of share|shareholding pattern|intimation of (?:schedule|record)", re.I)


def tag_announcement(title: str) -> tuple[str, str]:
    """(label, severity) for an announcement, from its title alone."""
    for label, severity, pattern in ANNOUNCEMENT_TAGS:
        if re.search(pattern, title, re.I) and not (severity != "high" and ROUTINE.search(title)):
            return label, severity
    return "Routine", "low"


def is_announcement(doc: dict) -> bool:
    """An exchange announcement PDF -- not the exchange's stock page, and not an
    annual report, call transcript or slide deck that happens to be hosted there."""
    url = doc.get("url", "").lower()
    return (("annpdfopen" in url or "nsearchives" in url)
            and doc.get("category") not in ("Annual Report", "Concall Transcript", "Investor Presentation")
            and not BARE_LINK.match(doc.get("text", "")))


# Screener lists a call's slides and recordings as bare one-word links.
BARE_LINK = re.compile(r"^\W*(ppt|transcript|recording|audio|video|notes?|rec)\W*$", re.I)
