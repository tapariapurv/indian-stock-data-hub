"""
Indian Stock Data Hub
=====================
A single-file, 100% local Streamlit app for researching Indian listed
companies. It scrapes public data from screener.in, downloads the latest
filings (concall transcripts, investor presentations, annual reports,
quarterly results), extracts every figure from those PDFs with a
regex-based parser, gathers recent news from public RSS feeds, and uses a
local Ollama model (optional) to write a verdict, a short analysis, a news
digest and plain-English error explanations. Exports a formatted Excel
workbook (with live formulas and charts) and a Word report.

No paid APIs, no cloud AI -- only free, open-source tooling.

Run with:  streamlit run app.py
Settings:  OLLAMA_HOST environment variable (default http://localhost:11434)
User guide: static/user_guide.pdf (also linked from the app's sidebar)
"""

import io
import os
import random
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

try:
    from docx import Document
    from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

try:
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.worksheet.hyperlink import Hyperlink
    from openpyxl.utils import get_column_letter
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False

try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

BASE_URL = "https://www.screener.in/company/{ticker}/"
REQUEST_TIMEOUT = 10
DOWNLOAD_TIMEOUT = 20
MAX_FILE_MB = 25
MAX_PDF_PAGES_SCANNED = 150  # full documents scan fast (~2s per 75 pages); no need to truncate
DOWNLOAD_DIR = Path(__file__).parent / "downloads"
CACHE_TTL_SECONDS = 3600

# Same variable Ollama itself reads, so one setting covers both. Accepts
# "host:port" or a full URL (e.g. a model server on another machine).
_ollama_host = os.environ.get("OLLAMA_HOST", "").strip() or "localhost:11434"
if _ollama_host.startswith("0.0.0.0"):  # a server *bind* address; connect via localhost
    _ollama_host = _ollama_host.replace("0.0.0.0", "localhost", 1)
OLLAMA_URL = (_ollama_host if "://" in _ollama_host else f"http://{_ollama_host}").rstrip("/")
# Most capable first. qwen2.5:0.5b is a last resort: in testing it misread weak
# metrics as strengths (e.g. called a 7.57% ROE a "strong financial position").
OLLAMA_MODEL_PREFERENCE = ["gemma4:e4b", "gemma4:e2b", "qwen2.5:0.5b"]
OLLAMA_NUM_CTX = 2048
GUIDE_URL = "app/static/user_guide.pdf"  # served from ./static (see .streamlit/config.toml)

# News sources, all verified live 19 Sep 2026. Search feeds are per-company;
# market feeds are general and get filtered to headlines naming the company.
# (Moneycontrol's RSS was stale -- last item Apr 2024 -- so it's excluded.)
NEWS_SEARCH_FEEDS = {
    "Google News": "https://news.google.com/rss/search?q={q}+share&hl=en-IN&gl=IN&ceid=IN:en",
    "Bing News": "https://www.bing.com/news/search?q={q}+share&format=rss",
}
NEWS_MARKET_FEEDS = {
    "Economic Times": "https://economictimes.indiatimes.com/markets/stocks/news/rssfeeds/2146842.cms",
    "Livemint": "https://www.livemint.com/rss/markets",
    "Business Standard": "https://www.business-standard.com/rss/markets-106.rss",
    "Hindu BusinessLine": "https://www.thehindubusinessline.com/markets/feeder/default.rss",
}
# Auto-generated price tickers, quote pages and options chatter -- not news.
NEWS_NOISE_RE = re.compile(
    r"price prediction|share price (live|today|highlights)|stock price (today|history)|(share|stock) price$"
    r"|live ?blog|live updates|intraday|\b(calls|puts)\b.*strike|high value trading"
    r"|52-week (low|high), key metrics|stock (slips|rises|falls|gains) to rs", re.I)
NEWS_BLOCKED_SOURCES = {"marketsmojo", "ad hoc news", "univest"}  # machine-generated stock blurbs
NEWS_MAX_AGE_DAYS = 14
NEWS_MAX_ITEMS = 8
NEWS_CACHE_TTL_SECONDS = 1800

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

DOC_CATEGORY_PATTERNS = [
    ("Concall Transcript", re.compile(r"concall|transcript", re.I)),
    ("Investor Presentation", re.compile(r"present", re.I)),
    ("Annual Report", re.compile(r"annual\s*report", re.I)),
    ("Quarterly Results", re.compile(r"result|quarter", re.I)),
]
WANTED_CATEGORIES = ["Concall Transcript", "Investor Presentation", "Annual Report", "Quarterly Results"]

# Financial keywords the number-extraction pass looks for -> clean label. This
# is the "template" derived from studying a real concall transcript and IR
# deck (Ujjivan SFB, Q1 FY26): generic corporate terms plus the banking/NBFC
# vocabulary that dominates Indian financial-services filings. It's applied
# uniformly to every downloaded PDF (transcript, presentation, results,
# annual report) for every ticker.
FIN_KEYWORDS = {
    r"total\s+income": "Total Income",
    r"total\s+revenue|revenue\s+from\s+operations|net\s+sales": "Revenue",
    r"net\s+profit|profit\s+after\s+tax|\bpat\b": "Net Profit (PAT)",
    r"operating\s+profit|\bebit\b(?!da)": "Operating Profit",
    r"ebitda": "EBITDA",
    r"\beps\b|earnings\s+per\s+share": "EPS",
    r"total\s+assets": "Total Assets",
    r"total\s+liabilities": "Total Liabilities",
    r"total\s+debt|borrowings": "Total Debt",
    r"cash\s+and\s+cash\s+equivalents|cash\s+&\s+bank": "Cash & Equivalents",
    r"operating\s+margin|ebitda\s+margin": "Operating Margin",
    r"net\s+profit\s+margin|pat\s+margin": "Net Profit Margin",
    r"return\s+on\s+equity|\broe\b": "ROE",
    r"return\s+on\s+capital\s+employed|\broce\b": "ROCE",
    r"debt\s+to\s+equity|debt/equity": "Debt to Equity",
    r"book\s+value": "Book Value",
    r"dividend": "Dividend",
    r"promoter\s+holding": "Promoter Holding",
    r"interest\s+coverage": "Interest Coverage",
    r"sales\s+growth": "Sales Growth",
    r"depreciation": "Depreciation",
    r"tax\s+expense|provision\s+for\s+tax": "Tax",
    # Banking / NBFC / small-finance-bank specific (common in concalls & IR decks)
    r"gross\s+loan\s+book|loan\s+book": "Loan Book",
    r"secured\s+loan": "Secured Loan Book",
    r"unsecured\s+loan": "Unsecured Loan Book",
    r"\bgnpa\b": "GNPA",
    r"\bnnpa\b": "NNPA",
    r"collection\s+efficiency": "Collection Efficiency",
    r"total\s+deposits": "Total Deposits",
    r"\bcasa\b": "CASA",
    r"retail\s+td": "Retail TD",
    r"disbursement": "Disbursement",
    r"\baum\b": "AUM",
    r"net\s+interest\s+margin|\bnim\b": "NIM",
    r"cost\s+to\s+income": "Cost to Income",
    r"provision\s+coverage|\bpcr\b": "PCR",
    r"capital\s+adequacy|\bcrar\b": "CRAR",
    r"branches": "Branches",
    r"employees": "Employees",
    r"customers": "Customers",
    r"\batms?\b": "ATMs",
    r"yield": "Yield",
    r"cost\s+of\s+funds": "Cost of Funds",
    r"net\s+worth": "Net Worth",
    r"credit\s+cost": "Credit Cost",
    r"return\s+on\s+assets|\broa\b": "ROA",
}

# One numeric token, with optional leading currency symbol and trailing unit.
NUMBER_RE = re.compile(
    r"(?P<prefix>₹|Rs\.?|INR|\$)?\s*"
    r"(?P<value>-?\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|-?\d+\.\d+|-?\d{3,})"
    r"\s*(?P<unit>%|bps|bp\b|x\b|cr\.?|crore|lakh|lac|mn|bn|million|billion)?",
    re.I,
)
# Strips dates/quarter labels ("July 24, 2025", "Q1 FY'26") before number-scanning
# a line, so calendar noise never gets mistaken for a financial figure.
DATE_NOISE_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b"
    r"|\bq[1-4]\s?fy'?\d{2,4}\b|\bfy'?\d{2,4}\b|\b\d{1,2}:\d{2}\b",
    re.I,
)
NOISE_LINE_RE = re.compile(r"^\W*$|^page\s+\d+|^\d{1,4}$", re.I)


# --------------------------------------------------------------------------
# Networking helpers
# --------------------------------------------------------------------------

def _headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }


def _fetch_html(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
        return resp.text if resp.status_code == 200 else None
    except requests.RequestException:
        return None


def _to_number(raw) -> float | None:
    """Best-effort clean-and-convert of a scraped/PDF text token to float."""
    if raw is None:
        return None
    s = str(raw).replace(",", "").replace("%", "").strip()
    if s in ("", "-", "—", "nan", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# Control characters (except tab/newline/CR) and lone UTF-16 surrogates are
# invalid in the XML that both .xlsx and .docx are built from. openpyxl only
# guards against a narrow subset of these; text pulled from scraped HTML or
# PDF fonts (which can contain broken/private-use glyphs) can still slip
# illegal codepoints past it, producing a file Excel/Word flag as
# "unreadable content" and partially discard. Sanitizing every piece of text
# once, right where it's scraped/extracted, keeps every downstream export safe.
_ILLEGAL_XML_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def sanitize_text(value, max_len: int = 30000):
    if not isinstance(value, str):
        return value
    return _ILLEGAL_XML_RE.sub("", value)[:max_len]


def sanitize_df_strings(df: pd.DataFrame | None) -> pd.DataFrame | None:
    if df is None:
        return None
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].map(lambda v: sanitize_text(v) if isinstance(v, str) else v)
    return df

def _count_downloaded(r: dict) -> int:
    return sum(1 for infos in (r.get("downloaded") or {}).values() for info in infos if info.get("path"))



# --------------------------------------------------------------------------
# Smart, bandwidth-conscious PDF downloading
# --------------------------------------------------------------------------

def smart_download(url: str, dest_path: Path) -> tuple[bool, str | None]:
    if dest_path.exists() and dest_path.stat().st_size > 0:
        return True, None
    try:
        head = requests.head(url, headers=_headers(), timeout=REQUEST_TIMEOUT, allow_redirects=True)
        content_type = head.headers.get("Content-Type", "").lower()
        content_length = int(head.headers.get("Content-Length", 0) or 0)
        if content_length and content_length > MAX_FILE_MB * 1024 * 1024:
            return False, f"Skipped (file is {content_length / 1e6:.1f} MB, over the {MAX_FILE_MB} MB limit)."
        if content_type and "pdf" not in content_type and not url.lower().endswith(".pdf"):
            return False, f"Skipped (not a PDF: content-type '{content_type}')."

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        with requests.get(url, headers=_headers(), timeout=DOWNLOAD_TIMEOUT, stream=True) as resp:
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code}"
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > MAX_FILE_MB * 1024 * 1024:
                        f.close()
                        dest_path.unlink(missing_ok=True)
                        return False, f"Aborted -- exceeded {MAX_FILE_MB} MB while streaming."
                    f.write(chunk)
        return True, None
    except requests.RequestException as exc:
        return False, str(exc)


TIMEFRAME_OPTIONS = {"Latest only": 1, "Last 2 available": 2, "Last 4 available": 4, "All available": None}


def pick_documents_by_selection(documents: list[dict], selected_categories: list[str], max_per_category: int | None) -> dict:
    """screener.in lists filings newest-first, so document order is used as
    the recency signal: keep the first N per selected category (None = all)."""
    picked: dict[str, list[dict]] = {}
    for doc in documents:
        cat = doc["category"]
        if cat not in selected_categories:
            continue
        bucket = picked.setdefault(cat, [])
        if max_per_category is None or len(bucket) < max_per_category:
            bucket.append(doc)
    return picked


# --------------------------------------------------------------------------
# PDF number-extraction algorithm
# --------------------------------------------------------------------------

def _looks_like_year(value_str: str, unit: str) -> bool:
    """Bare 4-digit numbers with no unit/comma/decimal are almost always a
    calendar year picked up from legal boilerplate ("Companies Act, 2013"),
    never a financial figure -- filter them out."""
    if unit or "," in value_str or "." in value_str:
        return False
    return len(value_str) == 4 and value_str.isdigit() and 1947 <= int(value_str) <= 2099


def _is_heading_line(line: str) -> bool:
    """A line with letters but no digits, of plausible title length -- used
    as a fallback label source for IR-deck slides where the metric name sits
    on its own line above the numbers (a layout no single-line regex can see)."""
    return bool(line) and not re.search(r"\d", line) and 2 <= len(line) <= 70


def extract_all_numbers(pdf_path: Path, source_category: str = "") -> list[dict]:
    """Extract EVERY financial number from a PDF, deterministically (no LLM
    involved in finding numbers -- only code, so nothing is missed or
    invented). For each match we keep the exact page, a cleaned label, and
    the full original line as context, so even when the label heuristic is
    imperfect the true source text is always preserved.

    This is the general-purpose template built from analyzing a real
    concall transcript (numbers embedded in prose, e.g. "grew to
    INR33,287 crores") and IR presentation (label-then-value slide layout,
    e.g. "Gross Loan Book" / "₹ 33,287 Cr") -- it is applied unchanged to
    every filing type (transcript, presentation, quarterly results, annual
    report) for every ticker.
    """
    if not PDFPLUMBER_AVAILABLE or not pdf_path.exists():
        return []

    findings, seen = [], set()
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages[:MAX_PDF_PAGES_SCANNED], start=1):
                text = page.extract_text() or ""
                lines = [l.strip() for l in text.split("\n") if l.strip()]
                heading_window: list[str] = []  # last 2 non-numeric lines on this page

                for raw_line in lines:
                    if NOISE_LINE_RE.match(raw_line):
                        continue
                    line_clean = DATE_NOISE_RE.sub(" ", raw_line)

                    if _is_heading_line(raw_line):
                        heading_window = (heading_window + [raw_line])[-2:]
                        continue

                    # If the heading row above lists N metric names and this
                    # value row has exactly N numbers, align them 1:1 in order
                    # (handles side-by-side metric columns in IR decks).
                    line_numbers = list(NUMBER_RE.finditer(line_clean))
                    ordinal_labels: dict[int, str] = {}
                    if heading_window:
                        heading_text = " ".join(heading_window).lower()
                        hits = sorted(
                            (m.start(), canon)
                            for pattern, canon in FIN_KEYWORDS.items()
                            for m in re.finditer(pattern, heading_text)
                        )
                        ordered = [c for _, c in hits]
                        if ordered and len(ordered) == len(line_numbers):
                            ordinal_labels = dict(enumerate(ordered))

                    for idx, m in enumerate(line_numbers):
                        value_str = m.group("value")
                        unit = (m.group("unit") or "").lower()
                        currency = (m.group("prefix") or "").replace("Rs.", "Rs")
                        if _looks_like_year(value_str, unit):
                            continue

                        num = _to_number(value_str)
                        if num is None:
                            continue
                        has_signal = bool(unit) or bool(currency) or "." in value_str or "," in value_str
                        long_enough = len(value_str.replace(",", "").replace(".", "")) >= 3

                        label = ordinal_labels.get(idx)
                        if not label:
                            for span in [raw_line] + list(reversed(heading_window)):
                                for pattern, canon in FIN_KEYWORDS.items():
                                    if re.search(pattern, span.lower()):
                                        label = canon
                                        break
                                if label:
                                    break

                        if not (has_signal or long_enough or label):
                            continue  # skip bare small noise (bullets, footnote refs, list markers)

                        if not label:
                            before = raw_line[: m.start()].strip(" :-–—\t•")
                            label = (before[-40:] if before else raw_line[m.end():].strip(" :-–—\t•")[:40])
                            label = label or (heading_window[-1] if heading_window else "Unlabeled")
                        label = label.strip(" :-–—\t•")[:60] or "Unlabeled"

                        key = (label, value_str, unit, page_num)
                        if key in seen:
                            continue
                        seen.add(key)
                        findings.append({
                            "Source": source_category, "Label": sanitize_text(label), "Category": "",
                            "Value": num, "Currency": sanitize_text(currency), "Unit": sanitize_text(unit),
                            "Page": page_num, "Context": sanitize_text(raw_line[:160]),
                        })
    except Exception:
        return findings  # partial results are fine -- never crash the run
    return findings


# --------------------------------------------------------------------------
# Local Ollama integration (the ONLY model used, and it runs 100% locally)
# --------------------------------------------------------------------------

@st.cache_data(ttl=60, show_spinner=False)
def get_ollama_models() -> dict[str, str]:
    """Every generative model installed in the local Ollama, best first
    (preference list order, then the rest by size, largest first), mapped
    to a display label like "gemma4:e2b · 5.1B · 7.2 GB"."""
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        if resp.status_code != 200:
            return {}
        models = [m for m in resp.json().get("models", []) if "embed" not in m["name"]]
    except requests.RequestException:
        return {}
    rank = {name: i for i, name in enumerate(OLLAMA_MODEL_PREFERENCE)}
    models.sort(key=lambda m: (rank.get(m["name"], len(rank)), -m.get("size", 0)))
    return {
        m["name"]: " · ".join(filter(None, [m["name"], (m.get("details") or {}).get("parameter_size"),
                                             f"{m.get('size', 0) / 1e9:.1f} GB"]))
        for m in models
    }


def _ollama_call(prompt: str, model: str, num_predict: int) -> tuple[str | None, int]:
    """Returns (response text, tokens used = prompt + generated)."""
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            # think=False: "thinking" models (e.g. gemma4) otherwise spend the whole
            # num_predict budget on hidden reasoning and return an empty response.
            # num_ctx: our prompts are < 700 tokens; the model default (often 16k+)
            # only costs RAM and load time.
            json={"model": model, "prompt": prompt, "stream": False, "think": False,
                  "options": {"temperature": 0.2, "num_predict": num_predict, "num_ctx": OLLAMA_NUM_CTX}},
            timeout=45,
        )
        if resp.status_code != 200:
            return None, 0
        data = resp.json()
        return data.get("response", "").strip() or None, data.get("prompt_eval_count", 0) + data.get("eval_count", 0)
    except requests.RequestException:
        return None, 0


def _drop_cut_off_sentence(text: str) -> str:
    """If num_predict cut the reply mid-sentence, keep only complete sentences."""
    text = text.strip()
    if text.endswith((".", "!", "?")):
        return text
    end = max(text.rfind(". "), text.rfind("! "), text.rfind("? "))
    return text[: end + 1] if end > 0 else text


AI_QUARTERLY_ROWS = ("Sales", "Operating Profit", "OPM", "Net Profit", "EPS")


def ollama_analyze(ticker: str, company_name: str, metrics: dict, quarterly_df: pd.DataFrame | None,
                    pros: list[str], cons: list[str], model: str) -> dict:
    """One combined call: verdict + short summary, built ONLY from data we
    already scraped (never invented).

    Input is deliberately the structured screener.in data -- key ratios plus
    the last two quarters -- not the regex-extracted PDF figures. Those are
    great for the searchable figures table but too noisy to feed a model:
    first-occurrence "Dividend 5000" was a TDS threshold, "Net Profit 216" a
    page number. Clean inputs also mean ~40% fewer prompt tokens."""
    if not metrics and quarterly_df is None and not pros and not cons:
        return {"summary": None, "verdict": None, "tokens": 0}

    ratios = "; ".join(f"{k} {v}" for k, v in metrics.items() if k != "Face Value") or "none"
    quarter_lines = []
    if quarterly_df is not None and not quarterly_df.empty and len(quarterly_df.columns) >= 3:
        prev_q, last_q = quarterly_df.columns[-2], quarterly_df.columns[-1]
        for _, row in quarterly_df.iterrows():
            if str(row["Metric"]).startswith(AI_QUARTERLY_ROWS):
                quarter_lines.append(f"{row['Metric']} {row[prev_q]} -> {row[last_q]}")
        quarters = f"Quarterly, Rs Cr ({prev_q} -> {last_q}): " + "; ".join(quarter_lines) if quarter_lines else ""
    else:
        quarters = ""
    prompt = (
        f"Equity analyst review of {company_name} ({ticker}). Use ONLY this data; never invent numbers; no markdown.\n"
        f"Ratios: {ratios}\n{quarters}\n"
        f"Pros: {'; '.join(p[:120] for p in pros[:4]) or 'none'}\n"
        f"Cons: {'; '.join(c[:120] for c in cons[:4]) or 'none'}\n"
        "Reply exactly:\nVERDICT: <Positive|Neutral|Cautious>\nSUMMARY: <3 sentences on financial position and outlook, citing key numbers>"
    )
    # Verdict comes FIRST so it survives even if num_predict cuts the summary short.
    raw, tokens = _ollama_call(prompt, model, num_predict=180)
    if not raw:
        return {"summary": None, "verdict": None, "tokens": tokens}
    clean = raw.replace("*", "")

    verdict_match = re.search(r"verdict[:\s]+.*?\b(Positive|Neutral|Cautious)\b", clean, re.I)
    summary_match = re.search(r"SUMMARY:\s*(.*)", clean, re.S | re.I)
    verdict = verdict_match.group(1).capitalize() if verdict_match else None
    summary = _drop_cut_off_sentence(summary_match.group(1) if summary_match else clean)
    return {"summary": summary or None, "verdict": verdict, "tokens": tokens}


@st.cache_data(ttl=CACHE_TTL_SECONDS, max_entries=200, show_spinner=False)
def cached_analysis(ticker, company_name, metrics, quarterly_df, pros, cons, model) -> dict:
    """Keyed on exactly what the model sees, so changing filing options (which
    don't feed the prompt) never re-spends tokens. Raises on failure because
    st.cache_data doesn't cache exceptions -- a blip in Ollama isn't remembered."""
    analysis = ollama_analyze(ticker, company_name, metrics, quarterly_df, pros, cons, model)
    if not analysis["summary"]:
        raise RuntimeError(f"{model} returned no summary")
    return {**analysis, "at": time.time()}


def ollama_explain_error(ticker: str, error: str, model: str) -> tuple[str | None, int]:
    """Turn a raw exception/HTTP-style error into one plain-English sentence
    a non-technical user can act on -- used wherever a scrape/download step
    fails, so the app never just shows a stack trace."""
    if not error:
        return None, 0
    prompt = (
        f"In ONE short plain-English sentence, tell a non-technical investor what likely went wrong "
        f"fetching stock data for '{ticker}' and a quick next step. Technical detail: {error[:300]}"
    )
    return _ollama_call(prompt, model, num_predict=60)


# --------------------------------------------------------------------------
# News: public RSS feeds (no API keys), filtered to the company, AI-summarised
# --------------------------------------------------------------------------

def _rss_items(url: str, source: str) -> list[dict]:
    try:
        resp = requests.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.content, "xml")
    except Exception:
        return []  # one dead feed must never sink the others
    items = []
    for it in soup.find_all("item"):
        title = it.title.get_text(strip=True) if it.title else ""
        src_el = it.find("source") or it.find("News:Source") or it.find("Source")
        src = (src_el.get_text(strip=True) if src_el else "") or source
        if title.endswith(f" - {src}"):  # Google News appends " - Publisher"
            title = title[: -len(src) - 3]
        try:
            published = parsedate_to_datetime(it.pubDate.get_text(strip=True))
            published = published if published.tzinfo else published.replace(tzinfo=timezone.utc)
        except Exception:
            published = None
        link = it.link.get_text(strip=True) if it.link else ""
        if "bing.com/news/apiclick" in link:  # unwrap Bing's click-tracking redirect
            link = parse_qs(urlparse(link).query).get("url", [link])[0]
        items.append({"title": sanitize_text(title), "source": sanitize_text(src),
                      "published": published, "url": sanitize_text(link)})
    return items


def _title_words(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 2}


def fetch_company_news(ticker: str, company_name: str) -> list[dict]:
    """Recent headlines mentioning the company, from company-specific news
    searches plus the main Indian market-news feeds, fetched in parallel.
    Filters out auto-generated price/options chatter and collapses the same
    story reported by several outlets."""
    name = re.sub(r"\b(ltd|limited)\.?$", "", company_name or "", flags=re.I).strip(" .,") or ticker
    mentions = re.compile(rf"\b({re.escape(name)}|{re.escape(ticker)})\b", re.I)
    query = quote_plus(f'"{name}"')
    jobs = [(url.format(q=query), src) for src, url in NEWS_SEARCH_FEEDS.items()] + \
           [(url, src) for src, url in NEWS_MARKET_FEEDS.items()]
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        batches = list(pool.map(lambda job: _rss_items(*job), jobs))

    cutoff = datetime.now(timezone.utc) - timedelta(days=NEWS_MAX_AGE_DAYS)
    kept, kept_words = [], []
    for item in (i for batch in batches for i in batch):
        if (not item["title"] or not mentions.search(item["title"]) or NEWS_NOISE_RE.search(item["title"])
                or item["source"].lower() in NEWS_BLOCKED_SOURCES
                or (item["published"] and item["published"] < cutoff)):
            continue
        words = _title_words(item["title"])
        if any(len(words & k) >= 0.5 * min(len(words), len(k)) for k in kept_words):
            continue  # same story from another outlet
        kept_words.append(words)
        kept.append(item)
    kept.sort(key=lambda i: i["published"] or cutoff, reverse=True)
    return kept[:NEWS_MAX_ITEMS]


def ollama_summarize_news(company_name: str, headlines: list[dict], model: str) -> dict:
    """Headlines only (no article bodies): ~15 tokens each, and enough for a
    2-sentence digest. Fetching and feeding full articles would cost 50-100x
    the tokens for a marginally better summary."""
    if not headlines:
        return {"summary": None, "sentiment": None, "tokens": 0}
    lines = "\n".join(f"- {h['title']} ({h['source']})" for h in headlines)
    prompt = (
        f"Recent headlines about {company_name}:\n{lines}\n"
        "Using ONLY these headlines, no markdown, reply exactly:\n"
        "SENTIMENT: <Positive|Mixed|Negative>\nSUMMARY: <2 sentences on what is happening with the company>"
    )
    raw, tokens = _ollama_call(prompt, model, num_predict=110)
    if not raw:
        return {"summary": None, "sentiment": None, "tokens": tokens}
    clean = raw.replace("*", "")
    sentiment = re.search(r"sentiment[:\s]+.*?\b(Positive|Mixed|Negative)\b", clean, re.I)
    summary = re.search(r"SUMMARY:\s*(.*)", clean, re.S | re.I)
    return {"summary": sanitize_text(_drop_cut_off_sentence(summary.group(1) if summary else clean)) or None,
            "sentiment": sentiment.group(1).capitalize() if sentiment else None, "tokens": tokens}


@st.cache_data(ttl=NEWS_CACHE_TTL_SECONDS, show_spinner=False)
def get_news(ticker: str, company_name: str, use_ollama: bool, ollama_model: str | None) -> dict:
    """Cached separately from scrape_ticker so changing filing options never
    re-fetches news or re-spends model tokens on the same headlines."""
    headlines = fetch_company_news(ticker, company_name)
    news = {"headlines": headlines, "summary": None, "sentiment": None, "tokens": 0, "model": None}
    if use_ollama and ollama_model and headlines:
        news.update(ollama_summarize_news(company_name, headlines, ollama_model), model=ollama_model, at=time.time())
    return news


CANONICAL_LABEL_CATEGORY = {
    "Total Income": "Profitability", "Revenue": "Profitability", "Net Profit (PAT)": "Profitability",
    "Operating Profit": "Profitability", "EBITDA": "Profitability", "EPS": "Profitability",
    "Total Assets": "Capital & Liquidity", "Total Liabilities": "Capital & Liquidity",
    "Total Debt": "Capital & Liquidity", "Cash & Equivalents": "Capital & Liquidity",
    "Operating Margin": "Profitability", "Net Profit Margin": "Profitability", "ROE": "Profitability",
    "ROCE": "Profitability", "Debt to Equity": "Capital & Liquidity", "Book Value": "Capital & Liquidity",
    "Dividend": "Profitability", "Promoter Holding": "Operational", "Interest Coverage": "Capital & Liquidity",
    "Sales Growth": "Growth/Guidance", "Depreciation": "Profitability", "Tax": "Profitability",
    "Loan Book": "Loan/Asset Book", "Secured Loan Book": "Loan/Asset Book", "Unsecured Loan Book": "Loan/Asset Book",
    "GNPA": "Asset Quality", "NNPA": "Asset Quality", "Collection Efficiency": "Asset Quality",
    "Total Deposits": "Deposits & Funding", "CASA": "Deposits & Funding", "Retail TD": "Deposits & Funding",
    "Disbursement": "Loan/Asset Book", "AUM": "Loan/Asset Book", "NIM": "Profitability",
    "Cost to Income": "Profitability", "PCR": "Asset Quality", "CRAR": "Capital & Liquidity",
    "Branches": "Operational", "Employees": "Operational", "Customers": "Operational", "ATMs": "Operational",
    "Yield": "Profitability", "Cost of Funds": "Deposits & Funding", "Net Worth": "Capital & Liquidity",
    "Credit Cost": "Asset Quality", "ROA": "Profitability",
}
# Broader, looser keyword net for the long tail of fallback labels code
# couldn't map to a specific metric -- still 100% deterministic (no model
# call), which is what keeps categorization instant even over thousands of
# figures. Order matters: first matching bucket wins.
CATEGORY_FALLBACK_PATTERNS = [
    ("Loan/Asset Book", re.compile(r"loan|advance|disburs|aum|book", re.I)),
    ("Deposits & Funding", re.compile(r"deposit|casa|funding|borrow", re.I)),
    ("Asset Quality", re.compile(r"npa|slippage|write.?off|provision|stress|delinquen", re.I)),
    ("Profitability", re.compile(r"profit|income|revenue|margin|yield|ebitda|eps|nim", re.I)),
    ("Capital & Liquidity", re.compile(r"capital|crar|liquidity|net\s*worth|cash|cost\s+of\s+fund", re.I)),
    ("Growth/Guidance", re.compile(r"grow|guidance|outlook|target|yoy|qoq|y-o-y|q-o-q", re.I)),
    ("Operational", re.compile(r"branch|employee|customer|atm|staff|headcount", re.I)),
]


def classify_figure_category(label: str) -> str:
    """Deterministic (code-only) categorization -- always fast and always
    populated, unlike a per-label model call which we tested and found
    unreliable for a 0.5B-parameter model at this kind of bulk structured
    task (it drifted into echoing the category list instead of mapping
    items). The model is used instead for what it's actually good at:
    the narrative summary below."""
    if label in CANONICAL_LABEL_CATEGORY:
        return CANONICAL_LABEL_CATEGORY[label]
    for category, pattern in CATEGORY_FALLBACK_PATTERNS:
        if pattern.search(label):
            return category
    return "Other"


# --------------------------------------------------------------------------
# Scraping + parsing (cached so repeat views / tab switches cost nothing)
# --------------------------------------------------------------------------

def _extract_table(soup, selector_id) -> pd.DataFrame | None:
    section = soup.select_one(f"#{selector_id}")
    table = section.find("table") if section else None
    if table is None:
        return None
    dfs = pd.read_html(io.StringIO(str(table)))
    if not dfs:
        return None
    df = dfs[0]
    first_col = df.columns[0]
    df = df.rename(columns={first_col: "Metric"})
    df["Metric"] = df["Metric"].astype(str).str.replace(r"\s*\+\s*$", "", regex=True).str.strip()
    return df.dropna(how="all").reset_index(drop=True)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def scrape_ticker(ticker: str, use_ollama: bool, ollama_model: str | None,
                   selected_categories: tuple[str, ...] = tuple(WANTED_CATEGORIES),
                   max_per_category: int | None = 1) -> dict:
    ticker = ticker.strip().upper()
    result = {
        "ticker": ticker, "ok": False, "error": None, "error_friendly": None, "company_name": None,
        "metrics": {}, "quarterly_df": None, "shareholding_df": None, "pros": [], "cons": [],
        "documents": [], "downloaded": {}, "figures": [], "ai_summary": None, "ai_verdict": None, "ai_model": None, "ai_tokens": 0,
    }
    if not ticker:
        result["error"] = "Empty ticker."
        return result

    html = _fetch_html(BASE_URL.format(ticker=ticker))
    if html is None:
        result["error"] = f"Could not fetch page for '{ticker}' (network error or ticker not found)."
        if use_ollama and ollama_model:
            friendly, result["ai_tokens"] = ollama_explain_error(ticker, result["error"], ollama_model)
            result["ai_at"] = time.time()
            result["error_friendly"] = sanitize_text(friendly)
        return result

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        result["error"] = f"Failed to parse HTML for '{ticker}': {exc}"
        if use_ollama and ollama_model:
            friendly, result["ai_tokens"] = ollama_explain_error(ticker, result["error"], ollama_model)
            result["ai_at"] = time.time()
            result["error_friendly"] = sanitize_text(friendly)
        return result

    title_tag = soup.select_one("h1")
    result["company_name"] = sanitize_text(title_tag.get_text(strip=True)) if title_tag else ticker

    try:
        for li in soup.select("#top-ratios li"):
            name_el, value_el = li.select_one(".name"), li.select_one(".value")
            if name_el and value_el:
                result["metrics"][sanitize_text(name_el.get_text(strip=True))] = sanitize_text(value_el.get_text(strip=True))
    except Exception:
        pass

    try:
        result["quarterly_df"] = sanitize_df_strings(_extract_table(soup, "quarters"))
    except Exception as exc:
        result["error"] = (result["error"] or "") + f" | Quarterly table parse issue: {exc}"

    try:
        result["shareholding_df"] = sanitize_df_strings(_extract_table(soup, "shareholding"))
    except Exception:
        pass  # bonus data -- never fatal

    try:
        result["pros"] = [sanitize_text(li.get_text(strip=True)) for li in soup.select("div.pros li")]
        result["cons"] = [sanitize_text(li.get_text(strip=True)) for li in soup.select("div.cons li")]
    except Exception:
        pass

    try:
        seen_urls, documents = set(), []
        for a in soup.find_all("a", href=True):
            href, link_text = a["href"].strip(), a.get_text(strip=True)
            is_doc_link = ".pdf" in href.lower() or "bseindia.com" in href.lower() or "nseindia.com" in href.lower()
            if not is_doc_link or href in seen_urls:
                continue
            seen_urls.add(href)
            category = "Other Filing"
            haystack = f"{link_text} {href}"
            for cat_name, pattern in DOC_CATEGORY_PATTERNS:
                if pattern.search(haystack):
                    category = cat_name
                    break
            documents.append({"category": category, "text": sanitize_text(link_text) or "(untitled link)", "url": sanitize_text(href)})
        result["documents"] = documents
    except Exception as exc:
        result["error"] = (result["error"] or "") + f" | Document scan issue: {exc}"

    picked_docs = pick_documents_by_selection(result["documents"], list(selected_categories), max_per_category)
    all_figures = []
    for category, docs in picked_docs.items():
        safe_cat = re.sub(r"[^A-Za-z0-9]+", "_", category)
        result["downloaded"][category] = []
        for i, doc in enumerate(docs):
            suffix = "" if i == 0 else f"_{i + 1}"
            dest = DOWNLOAD_DIR / ticker / f"{safe_cat}{suffix}.pdf"
            ok, err = smart_download(doc["url"], dest)
            result["downloaded"][category].append({"path": str(dest) if ok else None, "error": err, "url": doc["url"]})
            if ok:
                # Deterministic, code-only pass: guarantees every number in the
                # PDF is captured -- nothing here depends on the model.
                all_figures.extend(extract_all_numbers(dest, source_category=category))
    for f in all_figures:
        f["Category"] = classify_figure_category(f["Label"])
    result["figures"] = all_figures

    if use_ollama and ollama_model:
        try:
            analysis = cached_analysis(ticker, result["company_name"], result["metrics"], result["quarterly_df"],
                                       result["pros"], result["cons"], ollama_model)
        except RuntimeError:
            analysis = {"summary": None, "verdict": None, "tokens": 0, "at": time.time()}
        result["ai_at"] = analysis["at"]
        result["ai_summary"] = sanitize_text(analysis["summary"])
        result["ai_verdict"] = analysis["verdict"]
        result["ai_model"] = ollama_model
        result["ai_tokens"] = analysis["tokens"]

    result["ok"] = True
    return result


def scrape_all(tickers: list[str], use_ollama: bool, ollama_model: str | None,
                selected_categories: tuple[str, ...], max_per_category: int | None,
                delay_range=(1.0, 2.0)) -> list[dict]:
    results = []
    progress = st.progress(0.0, text="Starting...")
    total = len(tickers)
    for i, ticker in enumerate(tickers):
        progress.progress(i / max(total, 1), text=f"Processing {ticker}...")
        started = time.time()
        data = scrape_ticker(ticker, use_ollama, ollama_model, selected_categories, max_per_category)
        if data["ok"]:
            progress.progress((i + 0.8) / max(total, 1), text=f"Gathering news for {ticker}...")
            data["news"] = get_news(ticker, data["company_name"] or ticker, use_ollama, ollama_model)
        elapsed = time.time() - started
        results.append(data)
        if elapsed > 0.05 and i < total - 1:
            time.sleep(random.uniform(*delay_range))
    progress.progress(1.0, text="Done.")
    progress.empty()
    return results


# --------------------------------------------------------------------------
# Shared export helpers -- same "research desk" palette as the app theme
# --------------------------------------------------------------------------

INK, ACCENT, MUTED, LINE, PAPER, WHITE = "1C1F26", "1F4E79", "6B6F78", "E3E0D8", "F6F4EF", "FFFFFF"
GOOD, WARN, BAD, GOLD = "1F7A4D", "A8620A", "B4382F", "C08A3E"
TONE_COLOR = {"Positive": GOOD, "Neutral": WARN, "Mixed": WARN, "Cautious": BAD, "Negative": BAD}
TINT = {GOOD: "E5F2EA", WARN: "FBF1E1", BAD: "F7E6E4", MUTED: "EEEDEA"}
SERIF, SANS = "Georgia", "Calibri"  # both ship with Windows and macOS Office


def _parse_ratio(raw):
    """screener.in ratio text -> (number, Excel number format); text that isn't
    one number (e.g. High / Low "₹1,612/1,226") comes back as (text, None)."""
    s = str(raw or "").strip()
    num = None if "/" in s else _to_number(re.sub(r"₹|Cr\.?", "", s))
    if num is None:
        return s, None
    if s.endswith("%"):
        return num / 100, "0.00%"
    if "Cr" in s:
        return num, '"₹"#,##0" Cr"'
    if s.startswith("₹"):
        return num, '"₹"#,##0' if float(num).is_integer() else '"₹"#,##0.00'
    return num, "#,##0.0#"


def _quarterly_rows(qdf):
    """(quarter labels, [(metric, values, is_pct)]) with % rows as fractions."""
    if qdf is None or qdf.empty:
        return [], []
    quarters = [c for c in qdf.columns if c != "Metric"]
    rows = []
    for _, row in qdf.iterrows():
        metric = str(row["Metric"])
        if metric.lower().startswith("raw pdf"):
            continue
        is_pct = "%" in metric
        values = [_to_number(row[q]) for q in quarters]
        rows.append((metric, [v / 100 if is_pct and v is not None else v for v in values], is_pct))
    return quarters, rows


def _qoq(values, is_pct):
    """Latest-quarter change: percentage points for margin rows, % otherwise."""
    if len(values) < 2 or values[-1] is None or values[-2] is None:
        return None
    if is_pct:
        return (values[-1] - values[-2]) * 100
    return (values[-1] - values[-2]) / abs(values[-2]) if values[-2] else None


COST_ROWS = ("Expenses", "Interest", "Depreciation", "Tax")  # a rise here is bad news


def _qoq_color(metric, change):
    if not change:
        return MUTED
    return GOOD if (change > 0) != metric.startswith(COST_ROWS) else BAD


def _report_model(results):
    return next((r.get("ai_model") for r in results if r.get("ai_model")), None)


# --------------------------------------------------------------------------
# Excel export
# --------------------------------------------------------------------------

def _xl_put(ws, row, col, value=None, *, formula=False, size=10, bold=False, italic=False, color=INK,
            font=SANS, fill=None, h=None, v="center", wrap=False, fmt=None, border=None, link=None):
    """The ONLY way the exporter writes a cell. openpyxl stores any string that
    starts with "=" as a formula: scraped PDF text like "= 175 559 3,286" became
    invalid formulas that Excel "repaired" away (and a hostile page could inject
    working ones). Scraped text is therefore always stored as text; only the
    formulas this module builds itself pass formula=True."""
    cell = ws.cell(row=row, column=col, value=value)
    if isinstance(value, str) and value.startswith("=") and not formula:
        cell.data_type = "s"
    cell.font = Font(name=font, size=size, bold=bold, italic=italic, color=color,
                     underline="single" if link else None)
    cell.alignment = Alignment(horizontal=h, vertical=v, wrap_text=wrap)
    if fill:
        cell.fill = PatternFill("solid", start_color=fill, end_color=fill)
    if fmt:
        cell.number_format = fmt
    if border:
        cell.border = border
    if link and link.startswith("#"):  # jump within the workbook
        cell.hyperlink = Hyperlink(ref=cell.coordinate, location=link[1:])
    elif link:
        cell.hyperlink = link
    return cell


def _xl_merge(ws, row, col1, col2, value=None, **style):
    cell = _xl_put(ws, row, col1, value, **style)
    for c in range(col1 + 1, col2 + 1):  # keep fill/borders continuous across the merge
        _xl_put(ws, row, c, None, fill=style.get("fill"), border=style.get("border"))
    if col2 > col1:
        ws.merge_cells(start_row=row, start_column=col1, end_row=row, end_column=col2)
    return cell


def _xl_lines(text, chars_per_line):
    return max(1, sum(-(-max(len(part), 1) // chars_per_line) for part in str(text).split("\n")))


def _xl_section(ws, row, title, last_col, note=None):
    """Serif section title with an ink rule underneath, like the app's headings."""
    rule = Border(bottom=Side(style="medium", color=ACCENT))
    _xl_merge(ws, row, 2, last_col, title, font=SERIF, size=13, bold=True, border=rule, v="bottom")
    ws.row_dimensions[row].height = 24
    if note:
        _xl_put(ws, row + 1, 2, note, size=9, italic=True, color=MUTED)
        return row + 2
    return row + 1


def _xl_pill(ws, row, col1, col2, label):
    color = TONE_COLOR.get(label, MUTED)
    _xl_merge(ws, row, col1, col2, (label or "n/a").upper(), size=10, bold=True, color=color,
              fill=TINT[color], h="center")


def _xl_sheet_setup(ws, widths, tab_color):
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = tab_color
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    for col, width in widths.items():
        ws.column_dimensions[col].width = width


def _xl_ticker_sheet(wb, r):
    """One readable report per company. Returns cell refs the Overview links to."""
    from openpyxl.chart import BarChart, Reference, Series
    from openpyxl.formatting.rule import CellIsRule

    ws = wb.create_sheet(re.sub(r"[\\/*?:\[\]]", "", r["ticker"])[:31] or "Ticker")
    quarters, qrows = _quarterly_rows(r.get("quarterly_df"))
    first_q = 4                                   # quarters start in column D
    last_col = max(17, first_q + len(quarters))   # grid runs B..Q at least
    qoq_col = first_q + len(quarters)
    _xl_sheet_setup(ws, {"A": 2, **{get_column_letter(c): 11.5 for c in range(2, last_col + 1)}},
                    TONE_COLOR.get(r.get("ai_verdict"), ACCENT if r["ok"] else BAD))
    chars_per_line = int((last_col - 1) * 11.5 * 1.15)
    thin = Border(bottom=Side(style="thin", color=LINE))
    refs = {"sheet": ws.title}

    _xl_merge(ws, 2, 2, last_col - 2, r["ticker"], font=SERIF, size=24, bold=True)
    ws.row_dimensions[2].height = 34
    _xl_merge(ws, 3, 2, last_col - 2, r.get("company_name") or "", size=11, color=MUTED)
    _xl_put(ws, 2, last_col, "← Overview", size=9, color=ACCENT, h="right", link="#'Overview'!A1")

    if not r["ok"]:
        msg = r.get("error_friendly") or r.get("error") or "Unknown error."
        _xl_merge(ws, 5, 2, last_col, f"Could not analyse this ticker: {msg}", color=BAD, fill=TINT[BAD], wrap=True)
        ws.row_dimensions[5].height = 15 * _xl_lines(msg, chars_per_line) + 10
        return refs

    row = 5
    if r.get("ai_verdict") or (r.get("news") or {}).get("sentiment"):
        _xl_put(ws, row, 2, "AI VERDICT", size=8, bold=True, color=MUTED)
        _xl_pill(ws, row, 3, 4, r.get("ai_verdict"))
        _xl_put(ws, row, 6, "NEWS SENTIMENT", size=8, bold=True, color=MUTED)
        _xl_pill(ws, row, 8, 9, (r.get("news") or {}).get("sentiment"))
        ws.row_dimensions[row].height = 20
        row += 2

    if r.get("ai_summary"):
        row = _xl_section(ws, row, "Analysis", last_col)
        _xl_merge(ws, row, 2, last_col, r["ai_summary"], size=11, fill=PAPER, wrap=True, v="top",
                  border=Border(left=Side(style="thick", color=ACCENT)))
        ws.row_dimensions[row].height = 16 * _xl_lines(r["ai_summary"], chars_per_line) + 12
        _xl_put(ws, row + 1, 2, f"Generated locally by {r.get('ai_model')} from screener.in data. Not investment advice.",
                size=8, italic=True, color=MUTED)
        row += 3

    if r.get("metrics"):
        row = _xl_section(ws, row, "Key ratios", last_col)
        items = list(r["metrics"].items())
        for i, (name, raw) in enumerate(items):
            tile_row, tile_col = row + (i // 5) * 3, 2 + (i % 5) * 3
            value, fmt = _parse_ratio(raw)
            _xl_merge(ws, tile_row, tile_col, tile_col + 2, name.upper(), size=8, bold=True, color=MUTED)
            _xl_merge(ws, tile_row + 1, tile_col, tile_col + 2, value, font=SERIF, size=15, h="left", fmt=fmt)
            ws.row_dimensions[tile_row + 1].height = 22
        row += -(-len(items) // 5) * 3 + 1

    pros, cons = (r.get("pros") or [])[:6], (r.get("cons") or [])[:6]
    if pros or cons:
        row = _xl_section(ws, row, "Strengths and risks", last_col)
        mid = 2 + (last_col - 1) // 2
        _xl_merge(ws, row, 2, mid - 1, "STRENGTHS", size=9, bold=True, color=GOOD, border=thin)
        _xl_merge(ws, row, mid + 1, last_col, "RISKS", size=9, bold=True, color=BAD, border=thin)
        row += 1
        width = int((mid - 2) * 11.5 * 1.1)
        for i in range(max(len(pros), len(cons))):
            p, c = (pros[i] if i < len(pros) else ""), (cons[i] if i < len(cons) else "")
            _xl_merge(ws, row, 2, mid - 1, f"•  {p}" if p else None, wrap=True, v="top")
            _xl_merge(ws, row, mid + 1, last_col, f"•  {c}" if c else None, wrap=True, v="top")
            ws.row_dimensions[row].height = 15 * max(_xl_lines(p, width), _xl_lines(c, width)) + 4
            row += 1
        row += 1

    news = r.get("news") or {}
    if news.get("headlines"):
        note = (f"Summarised locally by {news.get('model')} from the headlines below." if news.get("summary") else None)
        row = _xl_section(ws, row, "Recent news", last_col, note=note)
        if news.get("summary"):
            _xl_merge(ws, row, 2, last_col, news["summary"], size=10.5, fill=PAPER, wrap=True, v="top")
            ws.row_dimensions[row].height = 15 * _xl_lines(news["summary"], chars_per_line) + 10
            row += 1
        for h in news["headlines"]:
            _xl_put(ws, row, 2, h["published"].strftime("%d %b %Y") if h.get("published") else "",
                    size=9, color=MUTED, border=thin)
            _xl_merge(ws, row, 3, 4, h["source"], size=9, color=MUTED, border=thin)
            _xl_merge(ws, row, 5, last_col, h["title"], color=ACCENT, link=h.get("url") or None, border=thin)
            row += 1
        row += 1

    if qrows:
        row = _xl_section(ws, row, "Quarterly results", last_col,
                          note="₹ crore. Last column: change vs previous quarter (percentage points for margin rows).")
        _xl_merge(ws, row, 2, 3, "Metric", size=9, bold=True, color=WHITE, fill=ACCENT)
        for j, q in enumerate(quarters):
            _xl_put(ws, row, first_q + j, str(q), size=9, bold=True, color=WHITE, fill=ACCENT, h="right")
        _xl_put(ws, row, qoq_col, "QoQ", size=9, bold=True, color=WHITE, fill=ACCENT, h="right")
        header_row, data_start = row, row + 1
        prev_col, last_q_col = get_column_letter(qoq_col - 2), get_column_letter(qoq_col - 1)
        for i, (metric, values, is_pct) in enumerate(qrows):
            row += 1
            band = PAPER if i % 2 else None
            strong = metric.startswith(("Sales", "Net Profit"))
            _xl_merge(ws, row, 2, 3, metric, bold=strong, fill=band)
            fmt = "0%" if is_pct else ("#,##0.00" if metric.startswith("EPS") else "#,##0")
            for j, v in enumerate(values):
                _xl_put(ws, row, first_q + j, v, fmt=fmt, h="right", fill=band, bold=strong)
            if len(quarters) >= 2:
                f = (f"=IFERROR(({last_q_col}{row}-{prev_col}{row})*100,\"\")" if is_pct else
                     f"=IFERROR({last_q_col}{row}/{prev_col}{row}-1,\"\")")
                _xl_put(ws, row, qoq_col, f, formula=True, fill=band, h="right", bold=True,
                        fmt='+0.0" pp";-0.0" pp";0.0" pp"' if is_pct else "+0.0%;-0.0%;0.0%")
            if metric.startswith("Sales"):
                refs["sales_row"] = row
            elif metric.startswith("Net Profit"):
                refs["profit_row"] = row
        qoq_letter = get_column_letter(qoq_col)
        for is_cost in (False, True):  # costs rising is red, everything else rising is green
            cells = " ".join(f"{qoq_letter}{data_start + i}" for i, (m, _, _) in enumerate(qrows)
                             if m.startswith(COST_ROWS) == is_cost)
            if cells:
                up, down = (BAD, GOOD) if is_cost else (GOOD, BAD)
                ws.conditional_formatting.add(cells, CellIsRule(operator="greaterThan", formula=["0"], font=Font(color=up)))
                ws.conditional_formatting.add(cells, CellIsRule(operator="lessThan", formula=["0"], font=Font(color=down)))
        refs.update(qoq_col=get_column_letter(qoq_col), last_q_col=last_q_col)
        row += 2

        if "sales_row" in refs and len(quarters) >= 2:
            chart = BarChart()
            chart.type, chart.grouping, chart.overlap, chart.gapWidth = "col", "clustered", -10, 60
            chart.title = "Sales vs net profit (₹ Cr)"
            for src_row, title, color in [(refs["sales_row"], "Sales", ACCENT), (refs.get("profit_row"), "Net profit", GOLD)]:
                if src_row:
                    series = Series(Reference(ws, min_col=first_q, max_col=qoq_col - 1, min_row=src_row), title=title)
                    series.graphicalProperties.solidFill = color
                    series.graphicalProperties.line.solidFill = color
                    chart.series.append(series)
            chart.set_categories(Reference(ws, min_col=first_q, max_col=qoq_col - 1, min_row=header_row))
            chart.x_axis.delete = chart.y_axis.delete = False  # openpyxl 3.1 hides axes by default
            chart.y_axis.numFmt, chart.y_axis.majorGridlines = "#,##0", None
            chart.legend.position = "b"
            chart.height, chart.width = 7.5, 2.3 * (last_col - 1)
            ws.add_chart(chart, f"B{row}")
            row += 17

    downloaded = r.get("downloaded") or {}
    if downloaded:
        row = _xl_section(ws, row, "Filings", last_col)
        for cat, infos in downloaded.items():
            for info in infos:
                ok = bool(info.get("path"))
                _xl_merge(ws, row, 2, 4, cat, border=thin)
                _xl_merge(ws, row, 5, 8, "Downloaded" if ok else str(info.get("error") or "Skipped"),
                          color=GOOD if ok else MUTED, border=thin, size=9)
                _xl_merge(ws, row, 9, last_col, info.get("url") or "", size=9, color=ACCENT,
                          link=info.get("url") or None, border=thin)
                row += 1
        row += 1

    figs = r.get("figures") or []
    if figs:
        _xl_put(ws, row, 2, f"{len(figs):,} figures were extracted from this company's filings. "
                            "Filter them on the Figures sheet.", size=9, italic=True, color=MUTED)
        _xl_put(ws, row + 1, 2, "→ Open Figures", size=9, color=ACCENT, link="#'Figures'!A1")
    return refs


def _xl_figures_sheet(wb, results):
    """Every extracted number, all tickers, as one filterable Excel Table."""
    from openpyxl.worksheet.table import Table, TableStyleInfo

    rows = [(r["ticker"], f) for r in results for f in (r.get("figures") or [])]
    if not rows:
        return
    ws = wb.create_sheet("Figures")
    cols = [("Ticker", 12), ("Source", 20), ("Category", 18), ("Label", 32), ("Value", 14),
            ("Unit", 8), ("Currency", 9), ("Page", 7), ("Context", 90)]
    _xl_sheet_setup(ws, {get_column_letter(i + 1): w for i, (_, w) in enumerate(cols)}, MUTED)
    _xl_put(ws, 1, 1, "Figures extracted from filings", font=SERIF, size=16, bold=True)
    _xl_put(ws, 2, 1, "Found by pattern-matching the downloaded PDFs, not by AI. Labels are best-effort: "
                      "check the Context column before relying on a number.", size=9, italic=True, color=MUTED)
    ws.row_dimensions[1].height = 26
    for j, (name, _) in enumerate(cols, start=1):
        _xl_put(ws, 4, j, name, bold=True, color=WHITE, fill=ACCENT)
    for i, (ticker, f) in enumerate(rows, start=5):
        values = [ticker, f.get("Source"), f.get("Category"), f.get("Label"), f.get("Value"),
                  f.get("Unit"), f.get("Currency"), f.get("Page"), f.get("Context")]
        for j, v in enumerate(values, start=1):
            _xl_put(ws, i, j, v, fmt="#,##0.##" if j == 5 else None, size=9.5,
                    color=MUTED if j == 9 else INK)
    table = Table(displayName="ExtractedFigures", ref=f"A4:{get_column_letter(len(cols))}{4 + len(rows)}")
    table.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=True)
    ws.add_table(table)
    ws.freeze_panes = "B5"


def build_excel_workbook(results: list[dict]) -> bytes:
    if not OPENPYXL_AVAILABLE:
        buf = io.BytesIO()
        pd.DataFrame([{"note": "Install openpyxl for a formatted workbook."}]).to_excel(buf, index=False)
        return buf.getvalue()
    import openpyxl
    from openpyxl.formatting.rule import CellIsRule

    wb = openpyxl.Workbook()
    ov = wb.active
    ov.title = "Overview"
    refs = {r["ticker"]: _xl_ticker_sheet(wb, r) for r in results}
    _xl_figures_sheet(wb, results)

    columns = [("Ticker", 11), ("Company", 30), ("AI verdict", 12), ("News", 11), ("Price", 11),
               ("Market cap", 16), ("P/E", 8), ("ROE", 9), ("ROCE", 9), ("Div. yield", 10),
               ("Sales, latest qtr", 16), ("Sales QoQ", 11), ("Profit QoQ", 11), ("Filings", 9), ("Figures", 10)]
    _xl_sheet_setup(ov, {"A": 2, **{get_column_letter(i + 2): w for i, (_, w) in enumerate(columns)}}, ACCENT)
    last_col = len(columns) + 1

    model = _report_model(results)
    _xl_put(ov, 2, 2, "Indian Stock Data Hub", font=SERIF, size=24, bold=True)
    ov.row_dimensions[2].height = 36
    _xl_put(ov, 3, 2, f"Equity research report · {datetime.now():%d %b %Y, %H:%M}"
                      + (f" · AI analysis by {model}, running locally" if model else ""), size=10, color=MUTED)

    head, first, last = 9, 10, 9 + len(results)
    col = {name: i + 2 for i, (name, _) in enumerate(columns)}
    L = {name: get_column_letter(c) for name, c in col.items()}
    rng = lambda name: f"{L[name]}{first}:{L[name]}{last}"  # noqa: E731
    tiles = [("Companies", f"=COUNTA({rng('Ticker')})"),
             ("Positive verdicts", f'=COUNTIF({rng("AI verdict")},"Positive")'),
             ("Cautious verdicts", f'=COUNTIF({rng("AI verdict")},"Cautious")'),
             ("Filings downloaded", f"=SUM({rng('Filings')})"),
             ("Figures extracted", f"=SUM({rng('Figures')})")]
    tile_spans = [(2, 3), (4, 6), (7, 9), (10, 12), (13, 16)]  # ~equal widths
    for (label, formula), (c1, c2) in zip(tiles, tile_spans):
        edge = Border(left=Side(style="thick", color=ACCENT))
        _xl_merge(ov, 5, c1, c2, label.upper(), size=8, bold=True, color=MUTED, fill=PAPER, border=edge)
        _xl_merge(ov, 6, c1, c2, formula, formula=True, font=SERIF, size=20, fill=PAPER, border=edge,
                  h="left", fmt="#,##0")
    ov.row_dimensions[6].height = 32

    for name, c in col.items():
        _xl_put(ov, head, c, name, size=9, bold=True, color=WHITE, fill=ACCENT, wrap=True,
                h="left" if name in ("Ticker", "Company") else "center")
    ov.row_dimensions[head].height = 30
    thin = Border(bottom=Side(style="thin", color=LINE))
    for i, r in enumerate(results):
        row, ref = first + i, refs[r["ticker"]]
        put = lambda name, value, **kw: _xl_put(ov, row, col[name], value, border=thin, **kw)  # noqa: E731
        put("Ticker", r["ticker"], bold=True, color=ACCENT, link=f"#'{ref['sheet']}'!A1")
        put("Company", r.get("company_name") or "", wrap=True)
        if not r["ok"]:
            _xl_merge(ov, row, col["AI verdict"], last_col, "Failed: " + (r.get("error_friendly") or r.get("error") or ""),
                      color=BAD, border=thin, size=9)
            continue
        verdict = r.get("ai_verdict")
        tone = TONE_COLOR.get(verdict, MUTED)
        put("AI verdict", verdict or "–", bold=True, color=tone, fill=TINT[tone] if verdict else None, h="center")
        sentiment = (r.get("news") or {}).get("sentiment")
        put("News", sentiment or "–", color=TONE_COLOR.get(sentiment, MUTED), h="center")
        m = r.get("metrics") or {}
        for name, key in [("Price", "Current Price"), ("Market cap", "Market Cap"), ("P/E", "Stock P/E"),
                          ("ROE", "ROE"), ("ROCE", "ROCE"), ("Div. yield", "Dividend Yield")]:
            value, fmt = _parse_ratio(m.get(key, ""))
            put(name, value if value != "" else "–", fmt=fmt, h="right")
        # Live links into the company sheet, so edits there flow through here.
        sheet = f"'{ref['sheet']}'"
        if ref.get("sales_row"):
            put("Sales, latest qtr", f"={sheet}!{ref['last_q_col']}{ref['sales_row']}", formula=True,
                fmt='"₹"#,##0" Cr"', h="right")
            put("Sales QoQ", f"={sheet}!{ref['qoq_col']}{ref['sales_row']}", formula=True,
                fmt="+0.0%;-0.0%;0.0%", h="right", bold=True)
        if ref.get("profit_row"):
            put("Profit QoQ", f"={sheet}!{ref['qoq_col']}{ref['profit_row']}", formula=True,
                fmt="+0.0%;-0.0%;0.0%", h="right", bold=True)
        put("Filings", _count_downloaded(r), h="right")
        put("Figures", len(r.get("figures") or []), fmt="#,##0", h="right")
        ov.row_dimensions[row].height = 20
    for name in ("Sales QoQ", "Profit QoQ"):
        ov.conditional_formatting.add(rng(name), CellIsRule(operator="greaterThan", formula=["0"], font=Font(color=GOOD)))
        ov.conditional_formatting.add(rng(name), CellIsRule(operator="lessThan", formula=["0"], font=Font(color=BAD)))
    ov.freeze_panes = ov.cell(row=first, column=col["AI verdict"])
    _xl_put(ov, last + 2, 2, "Click a ticker to open its report. Verdicts are written by a local AI model from "
                             "scraped data and can be wrong; this is not investment advice.",
            size=8.5, italic=True, color=MUTED)

    wb.calculation.fullCalcOnLoad = True  # openpyxl can't store results; make Excel compute on open
    buffer = io.BytesIO()
    wb.save(buffer)
    data = buffer.getvalue()

    # Round-trip verification: never hand the user a file we can't re-open
    # ourselves. If openpyxl can't parse its own output, something upstream
    # (bad character, bad range, etc.) is wrong -- fail loudly here instead.
    check_wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
    for name in check_wb.sheetnames:
        for _ in check_wb[name].iter_rows():
            pass
    check_wb.close()
    return data


# --------------------------------------------------------------------------
# Word export
# --------------------------------------------------------------------------

def _w_xml(tag, **attrs):
    el = OxmlElement(tag)
    for k, v in attrs.items():
        el.set(qn(k), str(v))
    return el


# Child order of the property elements we touch, per the WordprocessingML
# schema. Word reports out-of-order properties as "unreadable content", so
# every property this module adds goes through _w_set rather than .append().
_W_ORDER = {
    "rPr": "rStyle rFonts b bCs i iCs caps smallCaps strike dstrike outline shadow emboss imprint noProof "
           "snapToGrid vanish webHidden color spacing w kern position sz szCs highlight u effect bdr shd "
           "fitText vertAlign rtl cs em lang eastAsianLayout specVanish oMath",
    "pPr": "pStyle keepNext keepLines pageBreakBefore framePr widowControl numPr suppressLineNumbers pBdr shd "
           "tabs suppressAutoHyphens kinsoku wordWrap overflowPunct topLinePunct autoSpaceDE autoSpaceDN bidi "
           "adjustRightInd snapToGrid spacing ind contextualSpacing mirrorIndents suppressOverlap jc "
           "textDirection textAlignment textboxTightWrap outlineLvl divId cnfStyle rPr sectPr pPrChange",
    "tcPr": "cnfStyle tcW gridSpan hMerge vMerge tcBorders shd noWrap tcMar textDirection tcFitText vAlign hideMark",
    "tblPr": "tblStyle tblpPr tblOverlap bidiVisual tblStyleRowBandSize tblStyleColBandSize tblW jc "
             "tblCellSpacing tblInd tblBorders shd tblLayout tblCellMar tblLook tblCaption tblDescription",
    "tcBorders": "top start left bottom end right insideH insideV tl2br tr2bl",
    "tcMar": "top start left bottom end right",
    "pBdr": "top left bottom right between bar",
}
_W_ORDER = {k: [qn(f"w:{n}") for n in v.split()] for k, v in _W_ORDER.items()}


def _w_set(parent, child):
    """Put child into a w:*Pr-style element at its schema position, replacing
    any existing element of the same kind."""
    order = _W_ORDER[parent.tag.rsplit("}", 1)[-1]]
    for old in parent.findall(child.tag):
        parent.remove(old)
    rank = order.index(child.tag)
    for sibling in parent:
        if sibling.tag in order and order.index(sibling.tag) > rank:
            sibling.addprevious(child)
            return child
    parent.append(child)
    return child


def _w_border(parent_tag, edges):
    """edges: {edge: (size_eighths_pt, color, space_pt)} -> ordered w:tcBorders / w:pBdr."""
    box = _w_xml(parent_tag)
    for edge, (size, color, space) in edges.items():
        _w_set(box, _w_xml(f"w:{edge}", **{"w:val": "single", "w:sz": size, "w:space": space, "w:color": color}))
    return box


def _w_font(target, name):
    """Set a font on a style or run, dropping theme-font attributes that
    otherwise override the name (python-docx's template uses theme fonts)."""
    target.font.name = name
    rfonts = target.element.get_or_add_rPr().get_or_add_rFonts()
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        rfonts.attrib.pop(qn(attr), None)
    rfonts.set(qn("w:eastAsia"), name)


def _w_run(par, text, size=None, bold=False, italic=False, color=INK, font=None, caps=False, spacing=None):
    run = par.add_run(text)
    run.bold, run.italic = bold, italic
    run.font.color.rgb = RGBColor.from_string(color)
    if size:
        run.font.size = Pt(size)
    if font:
        _w_font(run, font)
    if caps:
        run.font.all_caps = True
    if spacing:
        _w_set(run.element.get_or_add_rPr(), _w_xml("w:spacing", **{"w:val": spacing}))
    return run


def _w_par(container, text="", size=None, color=INK, bold=False, italic=False, align=None,
           before=0, after=4, font=None, caps=False, spacing=None, keep_next=False):
    par = container.add_paragraph()
    fmt = par.paragraph_format
    fmt.space_before, fmt.space_after = Pt(before), Pt(after)
    fmt.keep_with_next = keep_next
    if align:
        par.alignment = align
    if text:
        _w_run(par, text, size, bold, italic, color, font, caps, spacing)
    return par


def _w_pill(par, label):
    color = TONE_COLOR.get(label, MUTED)
    run = _w_run(par, f"  {(label or 'n/a').upper()}  ", size=8.5, bold=True, color=color, spacing=10)
    _w_set(run.element.get_or_add_rPr(), _w_xml("w:shd", **{"w:val": "clear", "w:color": "auto", "w:fill": TINT[color]}))


def _w_cell(cell, fill=None, borders=None, width=None, valign="center", margins=None):
    tc_pr = cell._tc.get_or_add_tcPr()
    if fill:
        _w_set(tc_pr, _w_xml("w:shd", **{"w:val": "clear", "w:color": "auto", "w:fill": fill}))
    if borders:
        _w_set(tc_pr, _w_border("w:tcBorders", {e: (sz, c, 0) for e, (sz, c) in borders.items()}))
    if margins:
        mar = _w_xml("w:tcMar")
        for edge, twips in margins.items():
            _w_set(mar, _w_xml(f"w:{edge}", **{"w:w": twips, "w:type": "dxa"}))
        _w_set(tc_pr, mar)
    if width:
        cell.width = width
    cell.vertical_alignment = {"center": WD_ALIGN_VERTICAL.CENTER, "top": WD_ALIGN_VERTICAL.TOP}[valign]
    cell.paragraphs[0].paragraph_format.space_after = Pt(0)
    return cell


def _w_table(doc, rows, widths):
    """Fixed-layout table. python-docx only sets per-cell widths; Word, Pages and
    Quick Look size columns from the table grid, so set that and the layout too."""
    table = doc.add_table(rows=rows, cols=len(widths))
    table.autofit = False
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    tbl_pr = table._tbl.tblPr
    _w_set(tbl_pr, _w_xml("w:tblW", **{"w:w": int(sum(w.twips for w in widths)), "w:type": "dxa"}))
    _w_set(tbl_pr, _w_xml("w:tblLayout", **{"w:type": "fixed"}))
    for grid_col, w in zip(table._tbl.tblGrid.gridCol_lst, widths):
        grid_col.w = w
    for row in table.rows:
        for cell, w in zip(row.cells, widths):
            cell.width = w
    return table


def _w_cell_text(cell, text, size=9.5, bold=False, color=INK, align=None, italic=False):
    par = cell.paragraphs[0]
    par.paragraph_format.space_after = Pt(0)
    if align:
        par.alignment = align
    _w_run(par, str(text), size=size, bold=bold, italic=italic, color=color)
    return par


def _w_hyperlink(par, url, text, size=9.5, color=ACCENT):
    r_id = par.part.relate_to(url, RT.HYPERLINK, is_external=True)
    link = _w_xml("w:hyperlink", **{"r:id": r_id})
    run = _w_xml("w:r")
    rpr = _w_xml("w:rPr")
    rpr.append(_w_xml("w:color", **{"w:val": color}))
    rpr.append(_w_xml("w:sz", **{"w:val": int(size * 2)}))
    run.append(rpr)
    t = _w_xml("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    run.append(t)
    link.append(run)
    par._p.append(link)


def _w_heading(doc, text, level=2, before=14):
    par = doc.add_heading(text, level=level)
    par.paragraph_format.space_before, par.paragraph_format.space_after = Pt(before), Pt(6)
    if level == 2:  # thin ink rule under section headings, echoing the app
        _w_set(par._p.get_or_add_pPr(), _w_border("w:pBdr", {"bottom": (6, ACCENT, 3)}))
    return par


def _w_setup(doc):
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)  # A4
    section.left_margin = section.right_margin = Cm(2)
    section.top_margin, section.bottom_margin = Cm(1.8), Cm(1.8)
    normal = doc.styles["Normal"]
    _w_font(normal, SANS)
    normal.font.size, normal.font.color.rgb = Pt(10.5), RGBColor.from_string(INK)
    normal.paragraph_format.space_after, normal.paragraph_format.line_spacing = Pt(4), 1.15
    for level, size in [(1, 24), (2, 13)]:
        style = doc.styles[f"Heading {level}"]
        _w_font(style, SERIF)
        style.font.size, style.font.bold = Pt(size), True
        style.font.color.rgb = RGBColor.from_string(INK)

    footer = section.footer.paragraphs[0]
    footer.paragraph_format.tab_stops.add_tab_stop(Cm(17), WD_TAB_ALIGNMENT.RIGHT)
    _w_run(footer, f"Indian Stock Data Hub · {datetime.now():%d %b %Y}\tPage ", size=8, color=MUTED)
    run = _w_run(footer, "", size=8, color=MUTED)
    for kind, text in [("begin", None), (None, "PAGE"), ("end", None)]:
        if kind:
            run._r.append(_w_xml("w:fldChar", **{"w:fldCharType": kind}))
        else:
            instr = _w_xml("w:instrText")
            instr.text = text
            instr.set(qn("xml:space"), "preserve")
            run._r.append(instr)


def _w_company(doc, r):
    eyebrow = _w_par(doc, "Company report", size=8, color=MUTED, caps=True, spacing=30, after=0)
    eyebrow.paragraph_format.page_break_before = True  # no empty break paragraph between companies
    doc.add_heading(r["ticker"], level=1).paragraph_format.space_after = Pt(0)
    _w_par(doc, r.get("company_name") or "", size=12, color=MUTED, after=8)
    if not r["ok"]:
        _w_par(doc, f"Could not analyse this ticker: {r.get('error_friendly') or r.get('error')}", color=BAD)
        return

    news = r.get("news") or {}
    if r.get("ai_verdict") or news.get("sentiment"):
        par = _w_par(doc, after=10)
        _w_run(par, "AI VERDICT  ", size=8, bold=True, color=MUTED)
        _w_pill(par, r.get("ai_verdict"))
        _w_run(par, "      NEWS SENTIMENT  ", size=8, bold=True, color=MUTED)
        _w_pill(par, news.get("sentiment"))

    if r.get("ai_summary"):
        box = _w_table(doc, 1, [Cm(17)])
        cell = _w_cell(box.cell(0, 0), fill=PAPER, borders={"left": (24, ACCENT)},
                       margins={"top": 140, "bottom": 140, "left": 220, "right": 220})
        _w_cell_text(cell, r["ai_summary"], size=10.5)
        _w_par(doc, f"Generated locally by {r.get('ai_model')} from screener.in data.", size=8, italic=True,
               color=MUTED, before=3, after=6)

    metrics = list((r.get("metrics") or {}).items())
    if metrics:
        _w_heading(doc, "Key ratios")
        per_row = 5
        grid = _w_table(doc, -(-len(metrics) // per_row) * 2, [Cm(17 / per_row)] * per_row)
        for i, (name, value) in enumerate(metrics):
            label_cell = _w_cell(grid.cell((i // per_row) * 2, i % per_row))
            value_cell = _w_cell(grid.cell((i // per_row) * 2 + 1, i % per_row),
                                 borders={"bottom": (4, LINE)} if i // per_row == 0 else None)
            _w_cell_text(label_cell, name.upper(), size=7.5, bold=True, color=MUTED)
            par = value_cell.paragraphs[0]
            _w_run(par, value, size=13, font=SERIF)
            par.paragraph_format.space_after = Pt(8)

    pros, cons = (r.get("pros") or [])[:6], (r.get("cons") or [])[:6]
    if pros or cons:
        _w_heading(doc, "Strengths and risks")
        pc = _w_table(doc, 1, [Cm(8.3), Cm(0.4), Cm(8.3)])
        for idx, (title, items, color) in [(0, ("Strengths", pros, GOOD)), (2, ("Risks", cons, BAD))]:
            cell = _w_cell(pc.cell(0, idx), fill=TINT[color], valign="top",
                           margins={"top": 120, "bottom": 120, "left": 160, "right": 160})
            _w_cell_text(cell, title.upper(), size=8, bold=True, color=color)
            for item in items or ["None listed"]:
                par = cell.add_paragraph()
                par.paragraph_format.space_before, par.paragraph_format.space_after = Pt(4), Pt(0)
                par.paragraph_format.left_indent, par.paragraph_format.first_line_indent = Cm(0.35), Cm(-0.35)
                _w_run(par, f"•  {item}", size=9.5)

    quarters, qrows = _quarterly_rows(r.get("quarterly_df"))
    if qrows:
        shown = quarters[-5:]  # 13 quarters are unreadable in portrait; Excel has all of them
        _w_heading(doc, "Quarterly results")
        _w_par(doc, f"₹ crore, last {len(shown)} quarters. Full history in the Excel workbook.", size=8,
               italic=True, color=MUTED, after=4)
        widths = [Cm(4.2)] + [Cm(2.2)] * len(shown) + [Cm(1.8)]
        qt = _w_table(doc, len(qrows) + 1, widths)
        for j, text in enumerate(["Metric", *map(str, shown), "QoQ"]):
            cell = _w_cell(qt.cell(0, j), fill=ACCENT)
            _w_cell_text(cell, text, size=8.5, bold=True, color=WHITE,
                         align=None if j == 0 else WD_ALIGN_PARAGRAPH.RIGHT)
        for i, (metric, values, is_pct) in enumerate(qrows, start=1):
            band = PAPER if i % 2 == 0 else None
            strong = metric.startswith(("Sales", "Net Profit"))
            _w_cell_text(_w_cell(qt.cell(i, 0), fill=band), metric, size=9, bold=strong)
            for j, v in enumerate(values[-len(shown):], start=1):
                text = "–" if v is None else (f"{v:.0%}" if is_pct else f"{v:,.2f}" if metric.startswith("EPS") else f"{v:,.0f}")
                _w_cell_text(_w_cell(qt.cell(i, j), fill=band), text, size=9, bold=strong, align=WD_ALIGN_PARAGRAPH.RIGHT)
            change = _qoq(values, is_pct)
            text = "–" if change is None else (f"{change:+.1f} pp" if is_pct else f"{change:+.1%}")
            color = _qoq_color(metric, change)
            _w_cell_text(_w_cell(qt.cell(i, len(shown) + 1), fill=band), text, size=9, bold=True, color=color,
                         align=WD_ALIGN_PARAGRAPH.RIGHT)

    if news.get("headlines"):
        _w_heading(doc, "Recent news")
        if news.get("summary"):
            _w_par(doc, news["summary"], after=2)
            _w_par(doc, f"Summarised locally by {news.get('model')} from the headlines below.", size=8,
                   italic=True, color=MUTED, after=6)
        for h in news["headlines"]:
            par = _w_par(doc, after=3)
            par.paragraph_format.left_indent, par.paragraph_format.first_line_indent = Cm(0.35), Cm(-0.35)
            _w_run(par, "›  ", size=9.5, color=ACCENT, bold=True)
            if h.get("url"):
                _w_hyperlink(par, h["url"], h["title"])
            else:
                _w_run(par, h["title"], size=9.5)
            date = f" · {h['published']:%d %b}" if h.get("published") else ""
            _w_run(par, f"   {h['source']}{date}", size=8.5, color=MUTED)

    downloaded = r.get("downloaded") or {}
    figs = r.get("figures") or []
    if downloaded or figs:
        _w_heading(doc, "Filings")
        for cat, infos in downloaded.items():
            for info in infos:
                par = _w_par(doc, after=2)
                _w_run(par, f"{cat}   ", size=9.5, bold=True)
                if info.get("path"):
                    _w_run(par, "Downloaded   ", size=9, color=GOOD)
                    if info.get("url"):
                        _w_hyperlink(par, info["url"], "Open source ↗", size=9)
                else:
                    _w_run(par, str(info.get("error") or "Skipped"), size=9, color=MUTED)
        if figs:
            by_cat = pd.Series([f.get("Category") or "Other" for f in figs]).value_counts()
            _w_par(doc, f"{len(figs):,} figures extracted from these filings: "
                        + ", ".join(f"{cat} {n:,}" for cat, n in by_cat.items())
                        + ". The full, filterable list is on the Figures sheet of the Excel workbook.",
                   size=8.5, italic=True, color=MUTED, before=6)


def build_word_document(results: list[dict]) -> bytes:
    doc = Document()
    _w_setup(doc)
    model = _report_model(results)

    _w_par(doc, "Equity research report", size=9, color=MUTED, caps=True, spacing=40, before=60, after=4)
    _w_par(doc, "Indian Stock Data Hub", size=30, font=SERIF, bold=True, after=2)
    tickers = ", ".join(r["ticker"] for r in results)
    rule = _w_par(doc, f"{tickers} · {datetime.now():%d %B %Y}"
                       + (f" · analysis by {model}, run locally" if model else ""), size=10.5, color=MUTED, after=18)
    _w_set(rule._p.get_or_add_pPr(), _w_border("w:pBdr", {"bottom": (18, ACCENT, 10)}))

    _w_heading(doc, "At a glance", before=6)
    cols = [("Ticker", 2.0), ("Company", 5.2), ("AI verdict", 2.3), ("News", 2.0), ("Price", 1.9), ("P/E", 1.5), ("ROE", 2.1)]
    glance = _w_table(doc, len(results) + 1, [Cm(w) for _, w in cols])
    for j, (name, _) in enumerate(cols):
        _w_cell_text(_w_cell(glance.cell(0, j), fill=ACCENT), name, size=8.5, bold=True, color=WHITE,
                     align=WD_ALIGN_PARAGRAPH.RIGHT if j >= 4 else None)
    for i, r in enumerate(results, start=1):
        line = {"bottom": (4, LINE)}
        cells = [_w_cell(glance.cell(i, j), borders=line) for j in range(len(cols))]
        for c in cells:
            c.paragraphs[0].paragraph_format.space_before = Pt(3)
            c.paragraphs[0].paragraph_format.space_after = Pt(3)
        _w_cell_text(cells[0], r["ticker"], bold=True)
        _w_cell_text(cells[1], r.get("company_name") or "", size=9)
        if not r["ok"]:
            _w_cell_text(cells[2], "Failed", bold=True, color=BAD)
            continue
        _w_pill(cells[2].paragraphs[0], r.get("ai_verdict"))
        sentiment = (r.get("news") or {}).get("sentiment")
        _w_cell_text(cells[3], sentiment or "–", size=9, color=TONE_COLOR.get(sentiment, MUTED))
        m = r.get("metrics") or {}
        for c, key in zip(cells[4:], ["Current Price", "Stock P/E", "ROE"]):
            _w_cell_text(c, m.get(key, "–"), size=9, align=WD_ALIGN_PARAGRAPH.RIGHT)

    _w_par(doc, "How this report was made", size=8, bold=True, color=MUTED, caps=True, before=18, after=2)
    _w_par(doc, "Ratios, quarterly results, pros and cons come from screener.in; filings from the exchanges; "
                "headlines from public news feeds. Verdicts and summaries are written by a language model running "
                "on this computer, using only that data. They can be wrong: this is not investment advice.",
           size=9, color=MUTED)

    for r in results:
        _w_company(doc, r)

    buffer = io.BytesIO()
    doc.save(buffer)
    data = buffer.getvalue()

    # Same round-trip guarantee as the Excel export: fail loudly here rather
    # than silently shipping a .docx Word can't fully open.
    Document(io.BytesIO(data))
    return data


# --------------------------------------------------------------------------
# Streamlit UI  (look & feel lives in .streamlit/config.toml)
# --------------------------------------------------------------------------

st.set_page_config(page_title="Indian Stock Data Hub", page_icon=":material/monitoring:", layout="wide")

SENTIMENT_BADGE = {
    "Positive": ("green", ":material/trending_up:"),
    "Mixed": ("orange", ":material/trending_flat:"),
    "Negative": ("red", ":material/trending_down:"),
}


def _as_text(df: pd.DataFrame) -> pd.DataFrame:
    """screener.in columns mix numbers with text like "26%", which Arrow can't
    type; show them as the site does. (Exports parse the numbers properly.)"""
    return df.map(lambda v: "" if pd.isna(v) else str(v))


def _md_escape(text: str) -> str:
    """Headlines go inside [link text]; brackets, $ (LaTeX) and * would break the Markdown."""
    return re.sub(r"([\[\]$*_`])", r"\\\1", text)


def _token_note(tokens: int, generated_at: float, run_started: float) -> str:
    return f"{tokens:,} tokens" if generated_at >= run_started else f"reused from cache ({tokens:,} tokens when first generated)"


VERDICT_BADGE = {
    "Positive": ("green", ":material/trending_up:"),
    "Neutral": ("orange", ":material/trending_flat:"),
    "Cautious": ("red", ":material/trending_down:"),
}


def quarterly_series(qdf: pd.DataFrame | None, metric: str) -> pd.Series:
    """One screener.in quarterly row (e.g. "Sales") as numbers, indexed by quarter label."""
    if qdf is None or qdf.empty:
        return pd.Series(dtype=float)
    rows = qdf[qdf["Metric"].str.startswith(metric)]
    if rows.empty:
        return pd.Series(dtype=float)
    return rows.iloc[0].drop("Metric").map(_to_number).dropna().astype(float)


def trend_metric(qdf, metric: str, label: str, fmt: str, points: bool = False):
    s = quarterly_series(qdf, metric)
    if s.empty:
        return
    delta = None
    if len(s) > 1:
        prev, last = s.iloc[-2], s.iloc[-1]
        if points:
            delta = f"{last - prev:+.1f} pp QoQ"
        elif prev:
            delta = f"{(last - prev) / abs(prev) * 100:+.1f}% QoQ"
    st.metric(label, fmt.format(s.iloc[-1]), delta, chart_data=s.tail(8).tolist(),
              chart_type="area", border=True, help=f"Latest quarter ({s.index[-1]}); sparkline shows the last 8 quarters.")


ollama_models = get_ollama_models()

if "results" not in st.session_state:
    st.session_state.results = []

# --- Sidebar: configuration -------------------------------------------------
with st.sidebar:
    st.markdown("### :material/tune: Configuration")
    with st.form("config", border=False):
        raw_tickers = st.text_area(
            "Tickers", value="RELIANCE, TCS, INFY", height=80,
            help="Comma-separated screener.in symbols, e.g. RELIANCE, TCS, INFY",
        )
        selected_categories = st.pills(
            "Filings to download", WANTED_CATEGORIES, selection_mode="multi", default=WANTED_CATEGORIES,
        )
        timeframe_label = st.selectbox("Timeframe", list(TIMEFRAME_OPTIONS.keys()), index=0,
            help="How many of the most recent filings to pull per source.")
        use_ollama = st.toggle("Local AI summaries, verdicts & news", value=bool(ollama_models), disabled=not ollama_models)
        ollama_model = st.selectbox("Local model", list(ollama_models), index=0 if ollama_models else None,
            format_func=ollama_models.get, disabled=not ollama_models, placeholder="No Ollama models found",
            help="Every model found in your local Ollama, most capable first. Larger models write better "
                 "analysis but need more RAM.")
        run_button = st.form_submit_button("Run analysis", type="primary", icon=":material/play_arrow:", width="stretch")

    if ollama_models:
        st.badge(f"Ollama connected · {len(ollama_models)} model(s) found", icon=":material/memory:", color="green")
    else:
        st.badge("Ollama not detected", icon=":material/cloud_off:", color="gray")
        st.caption(f"AI features need Ollama running at `{OLLAMA_URL}`. The user guide walks you through "
                   "setting it up in about 10 minutes.")
    st.link_button("User guide (PDF)", GUIDE_URL, icon=":material/menu_book:", width="stretch",
                   type="primary" if not ollama_models else "secondary")
    st.caption(f"PDFs capped at {MAX_FILE_MB} MB and cached under `downloads/`. "
               f"Results cached for {CACHE_TTL_SECONDS // 60} min.")
    for missing, lib, feature in [(not PDFPLUMBER_AVAILABLE, "pdfplumber", "PDF extraction"),
                                  (not DOCX_AVAILABLE, "python-docx", "Word export"),
                                  (not OPENPYXL_AVAILABLE, "openpyxl", "Excel formatting")]:
        if missing:
            st.warning(f"`{lib}` not installed — {feature} disabled.", icon=":material/warning:")

tickers = [t.strip().upper() for t in raw_tickers.split(",") if t.strip()]
max_per_category = TIMEFRAME_OPTIONS[timeframe_label]

# --- Header -------------------------------------------------------------------
st.caption(":material/monitoring: EQUITY RESEARCH DESK · 100% LOCAL")
st.title("Indian stock data hub")
st.markdown(":gray[Scrape screener.in, pull the latest filings, extract every figure from them, "
            "and summarise with a local model — nothing leaves your machine.]")

if run_button:
    if not tickers:
        st.toast("Enter at least one ticker.", icon=":material/warning:")
    elif not selected_categories:
        st.toast("Select at least one filing type.", icon=":material/warning:")
    else:
        try:
            with st.spinner(f"Scraping, downloading & analysing {len(tickers)} ticker(s)…"):
                st.session_state.run_started = time.time()
                st.session_state.results = scrape_all(
                    tickers, use_ollama, ollama_model, tuple(selected_categories), max_per_category)
            ok_count = sum(1 for r in st.session_state.results if r["ok"])
            fail_count = len(st.session_state.results) - ok_count
            if ok_count:
                st.toast(f"Completed {ok_count} ticker(s).", icon=":material/check_circle:")
            if fail_count:
                st.toast(f"{fail_count} ticker(s) failed.", icon=":material/error:")
        except Exception as exc:
            st.error(f"Run failed: {exc}", icon=":material/error:")
            st.code(traceback.format_exc())

results = st.session_state.results

if not results:
    with st.container(border=True, horizontal_alignment="center"):
        st.space("small")
        st.markdown("### :material/query_stats: No analysis yet", text_alignment="center")
        st.markdown(
            ":gray[Add tickers in the sidebar, choose the filings you care about, then hit **Run analysis**.  \n"
            "You'll get a snapshot per company, quarterly trends, every figure extracted from the PDFs, "
            "and Excel / Word reports.]", text_alignment="center")
        st.space("small")
    st.stop()

ok_results = [r for r in results if r["ok"]]
failed_results = [r for r in results if not r["ok"]]

# --- KPI strip + export -------------------------------------------------------
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Tickers", len(results), border=True)
k2.metric("Successful", len(ok_results), border=True)
k3.metric("Failed", len(failed_results), border=True)
k4.metric("Filings downloaded", sum(_count_downloaded(r) for r in ok_results), border=True)
k5.metric("Figures extracted", f"{sum(len(r.get('figures') or []) for r in ok_results):,}", border=True)

# Streamlit reruns this whole script on ANY widget interaction, not just
# a download click. Rebuilding a multi-thousand-row workbook on every
# single rerun is slow and, worse, risks handing the browser a file
# mid-regeneration if a rerun lands during a download. Building once
# per distinct result set and reusing the cached bytes avoids both.
fingerprint = tuple(
    (r["ticker"], r["ok"], len(r.get("figures") or []),
     0 if r.get("quarterly_df") is None else len(r["quarterly_df"]), r.get("ai_verdict"), r.get("ai_summary"),
     tuple(h["url"] for h in (r.get("news") or {}).get("headlines", [])), (r.get("news") or {}).get("summary"))
    for r in results
)
if st.session_state.get("_export_fp") != fingerprint:
    st.session_state["_export_fp"] = fingerprint
    st.session_state["_xlsx_bytes"] = st.session_state["_xlsx_error"] = None
    st.session_state["_docx_bytes"] = st.session_state["_docx_error"] = None


def _cached_export(key: str, builder):
    if st.session_state.get(f"_{key}_bytes") is None and st.session_state.get(f"_{key}_error") is None:
        try:
            st.session_state[f"_{key}_bytes"] = builder(results)
        except Exception as exc:
            st.session_state[f"_{key}_error"] = str(exc)
    return st.session_state.get(f"_{key}_bytes"), st.session_state.get(f"_{key}_error")


stamp = datetime.now().strftime("%Y%m%d_%H%M")
with st.container(horizontal=True, horizontal_alignment="right"):
    if OPENPYXL_AVAILABLE:
        xlsx, xlsx_err = _cached_export("xlsx", build_excel_workbook)
        if xlsx_err:
            st.error(f"Could not build Excel workbook: {xlsx_err}")
        else:
            st.download_button("Excel workbook", data=xlsx, file_name=f"stock_report_{stamp}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", icon=":material/table_view:")
    if DOCX_AVAILABLE:
        docx, docx_err = _cached_export("docx", build_word_document)
        if docx_err:
            st.error(f"Could not build Word document: {docx_err}")
        else:
            st.download_button("Word report", data=docx, file_name=f"stock_report_{stamp}.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document", icon=":material/description:")

tab_home, tab_quarterly, tab_docs = st.tabs(
    [":material/dashboard: Overview", ":material/bar_chart: Financials", ":material/folder_open: Filings"]
)

# --- Tab 1: Overview ------------------------------------------------------------
with tab_home:
    # An answer counts toward this run only if it was generated during it; cache hits cost nothing.
    run_started = st.session_state.get("run_started", 0)
    ai_answers = [(a.get("at", 0), a.get("tokens", 0)) for r in results
                  for a in ({"at": r.get("ai_at", 0), "tokens": r.get("ai_tokens", 0)}, r.get("news") or {})
                  if a.get("tokens")]
    fresh_tokens = sum(t for at, t in ai_answers if at >= run_started)
    reused = sum(1 for at, _ in ai_answers if at < run_started)
    if ai_answers:
        st.caption(f":material/memory: Local AI used {fresh_tokens:,} tokens this run (prompt + output, as reported "
                   f"by Ollama)" + (f"; {reused} answer(s) reused from cache at no cost" if reused else "") +
                   ". Nothing was sent to the cloud.")
    for r in ok_results:
        with st.container(border=True):
            with st.container(horizontal=True, vertical_alignment="center"):
                st.markdown(f"## {r['ticker']}", width="content")
                if r.get("ai_verdict"):
                    color, icon = VERDICT_BADGE.get(r["ai_verdict"], ("gray", None))
                    st.badge(f"AI verdict · {r['ai_verdict']}", icon=icon, color=color)
            st.caption(r.get("company_name") or "")

            if r.get("ai_summary"):
                st.markdown(f"> {r['ai_summary']}")
                st.caption(f":material/memory: Generated locally by `{r.get('ai_model')}` from the scraped data below "
                           f"· {_token_note(r.get('ai_tokens', 0), r.get('ai_at', 0), run_started)}")
            elif r.get("ai_model"):
                st.caption(f":material/warning: `{r['ai_model']}` returned no summary for this ticker.")

            if r.get("metrics"):
                items = list(r["metrics"].items())
                for start in range(0, len(items), 5):
                    for col, (k, v) in zip(st.columns(5), items[start:start + 5]):
                        col.markdown(f":gray[{k}]  \n**{v}**")

            if r.get("pros") or r.get("cons"):
                pc1, pc2 = st.columns(2)
                with pc1.container(border=True, height="stretch"):
                    st.markdown("**:green[:material/check_circle: Strengths]**")
                    st.markdown("\n".join(f"- {p}" for p in (r.get("pros") or [])[:5]) or ":gray[None listed]")
                with pc2.container(border=True, height="stretch"):
                    st.markdown("**:red[:material/error: Risks]**")
                    st.markdown("\n".join(f"- {c}" for c in (r.get("cons") or [])[:5]) or ":gray[None listed]")

            news = r.get("news") or {}
            if news.get("headlines"):
                with st.container(border=True):
                    with st.container(horizontal=True, vertical_alignment="center"):
                        st.markdown("**:material/newspaper: Recent news**", width="content")
                        if news.get("sentiment"):
                            color, icon = SENTIMENT_BADGE.get(news["sentiment"], ("gray", None))
                            st.badge(f"News sentiment · {news['sentiment']}", icon=icon, color=color)
                    if news.get("summary"):
                        st.markdown(news["summary"])
                        st.caption(f":material/memory: Summarised locally by `{news['model']}` from the "
                                   f"{len(news['headlines'])} headlines below · "
                                   f"{_token_note(news['tokens'], news.get('at', 0), run_started)}")
                    st.markdown("\n".join(
                        f"- [{_md_escape(h['title'])}]({h['url']}) :gray[· {h['source']}"
                        f"{h['published'].strftime(' · %d %b') if h['published'] else ''}]"
                        for h in news["headlines"]))
            elif "news" in r:
                st.caption(":material/newspaper: No news in the last "
                           f"{NEWS_MAX_AGE_DAYS} days mentioning {r.get('company_name') or r['ticker']}.")

            if r.get("figures"):
                with st.expander(f"Figures extracted from filings · {len(r['figures']):,}", icon=":material/data_table:"):
                    st.dataframe(
                        pd.DataFrame(r["figures"]), hide_index=True,
                        column_order=["Source", "Category", "Label", "Currency", "Value", "Unit", "Page", "Context"],
                        column_config={
                            "Value": st.column_config.NumberColumn(format="localized"),
                            "Page": st.column_config.NumberColumn(width="small"),
                            "Currency": st.column_config.TextColumn(width="small"),
                            "Unit": st.column_config.TextColumn(width="small"),
                            "Context": st.column_config.TextColumn(width="large"),
                        },
                    )

    for r in failed_results:
        with st.container(border=True):
            st.markdown(f"**{r['ticker']}** :red-badge[:material/error: Failed]")
            st.caption(r.get("error_friendly") or r.get("error"))
            if r.get("error_friendly"):
                with st.expander("Technical details", icon=":material/code:"):
                    st.code(r.get("error") or "")

# --- Tab 2: Financials ----------------------------------------------------------
with tab_quarterly:
    fin_results = {r["ticker"]: r for r in ok_results
                   if r.get("quarterly_df") is not None or r.get("shareholding_df") is not None}
    if not fin_results:
        st.caption("No quarterly financial tables were found for the scraped tickers.")
    else:
        pick = st.segmented_control("Company", list(fin_results), default=next(iter(fin_results)),
                                    required=True, label_visibility="collapsed", key="fin_ticker")
        r = fin_results[pick]
        qdf = r.get("quarterly_df")
        if qdf is not None:
            t1, t2, t3, t4 = st.columns(4)
            with t1:
                trend_metric(qdf, "Sales", "Sales", "₹{:,.0f} Cr")
            with t2:
                trend_metric(qdf, "Net Profit", "Net profit", "₹{:,.0f} Cr")
            with t3:
                trend_metric(qdf, "OPM", "Operating margin", "{:.0f}%", points=True)
            with t4:
                trend_metric(qdf, "EPS", "EPS", "₹{:,.2f}")

            chart = pd.DataFrame({"Sales": quarterly_series(qdf, "Sales"),
                                  "Net profit": quarterly_series(qdf, "Net Profit")})
            if not chart.empty:
                with st.container(border=True):
                    st.markdown("**Sales vs net profit** :gray[· ₹ Cr per quarter]")
                    chart = chart.rename_axis("Quarter").reset_index()
                    st.bar_chart(chart, x="Quarter", y=["Sales", "Net profit"], stack=False, sort=False, color=["#1F4E79", "#C08A3E"],
                                 x_label="", y_label="", height=300)

            with st.container(horizontal=True, vertical_alignment="bottom"):
                st.markdown("**Quarterly results**", width="stretch")
                st.download_button("CSV", data=qdf.to_csv(index=False).encode("utf-8"),
                    file_name=f"{r['ticker']}_quarterly.csv", mime="text/csv",
                    icon=":material/download:", type="tertiary", key=f"csv_{r['ticker']}")
            st.dataframe(_as_text(qdf[qdf["Metric"] != "Raw PDF"]), hide_index=True,
                         column_config={"Metric": st.column_config.TextColumn(pinned=True)})
        sdf = r.get("shareholding_df")
        if sdf is not None:
            with st.expander("Shareholding pattern", icon=":material/pie_chart:"):
                st.dataframe(_as_text(sdf), hide_index=True, column_config={"Metric": st.column_config.TextColumn(pinned=True)})

# --- Tab 3: Filings ---------------------------------------------------------------
with tab_docs:
    for r in ok_results:
        with st.container(border=True):
            st.markdown(f"### {r['ticker']}")
            downloaded = r.get("downloaded") or {}
            if downloaded:
                rows = [
                    {"Category": cat,
                     "Status": "✓ Downloaded" if info.get("path") else f"✕ {info.get('error')}",
                     "URL": info.get("url")}
                    for cat, infos in downloaded.items() for info in infos
                ]
                st.dataframe(pd.DataFrame(rows), hide_index=True,
                             column_config={"URL": st.column_config.LinkColumn("Source", display_text="Open ↗")})

            docs = r.get("documents") or []
            if docs:
                with st.expander(f"All {len(docs)} document links on the screener.in page", icon=":material/link:"):
                    st.dataframe(
                        pd.DataFrame(docs).rename(columns={"category": "Category", "text": "Title", "url": "Link"}),
                        hide_index=True,
                        column_config={"Title": st.column_config.TextColumn(width="large"),
                                       "Link": st.column_config.LinkColumn(display_text="Open ↗")},
                    )
            else:
                st.caption("No PDF / BSE / NSE document links found on this page.")
