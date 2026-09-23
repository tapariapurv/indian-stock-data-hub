"""
Data layer: scraping, filing downloads, PDF number extraction and news.

Pure data work -- no Streamlit UI beyond caching decorators, so the same
functions serve the research page, the archive search and the exports.

Extraction results are cached in the local archive by file hash, so a filing
that has been parsed once is never parsed again, however many times it is
re-analysed.
"""

import base64
import hashlib
import io
import json
import zlib
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from importlib.util import find_spec
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx

import archive
import llm
import settings as cfg

# Asked, not imported. These three exist only to grey out a button in the UI,
# and importing them to find out cost ~26 MB of resident memory in every
# session -- including one that never opens a PDF or builds a report. The
# real import happens where the work does: pdfplumber below, openpyxl and
# python-docx inside exports.py.
PDFPLUMBER_AVAILABLE = find_spec("pdfplumber") is not None
OPENPYXL_AVAILABLE = find_spec("openpyxl") is not None
DOCX_AVAILABLE = find_spec("docx") is not None

WANTED_CATEGORIES = cfg.WANTED_CATEGORIES
GUIDE_URL = "app/static/user_guide.pdf"  # served from ./static (see .streamlit/config.toml)

# A pooled session: one TCP/TLS handshake is reused across every scrape,
# download and feed fetch instead of one per request.
SESSION = requests.Session()
SESSION.mount("https://", requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=16))

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

BASE_URL = "https://www.screener.in/company/{ticker}/"
DOWNLOAD_DIR = archive.DATA_ROOT

# Defaults come from the user's settings at import. The cache TTLs below are
# baked into decorators, so changing those two takes a restart (the settings
# page says so, and offers a "clear caches now" button that works instantly);
# everything else is re-read by apply_settings() on every run.
_S = cfg.load()
REQUEST_TIMEOUT = _S["data"]["request_timeout"]
DOWNLOAD_TIMEOUT = _S["data"]["download_timeout"]
MAX_FILE_MB = _S["data"]["max_file_mb"]
MAX_PDF_PAGES_SCANNED = _S["data"]["max_pdf_pages"]
CACHE_TTL_SECONDS = int(_S["data"]["cache_ttl_min"]) * 60


def apply_settings(s: dict) -> None:
    """Push the current settings into this module's limits before a run."""
    global REQUEST_TIMEOUT, DOWNLOAD_TIMEOUT, MAX_FILE_MB, MAX_PDF_PAGES_SCANNED
    global NEWS_MAX_AGE_DAYS, NEWS_MAX_ITEMS
    d = s["data"]
    REQUEST_TIMEOUT, DOWNLOAD_TIMEOUT = d["request_timeout"], d["download_timeout"]
    MAX_FILE_MB, MAX_PDF_PAGES_SCANNED = d["max_file_mb"], d["max_pdf_pages"]
    NEWS_MAX_AGE_DAYS, NEWS_MAX_ITEMS = d["news_days"], d["news_items"]


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
NEWS_MAX_AGE_DAYS = _S["data"]["news_days"]
NEWS_MAX_ITEMS = _S["data"]["news_items"]
NEWS_CACHE_TTL_SECONDS = int(_S["data"]["news_cache_ttl_min"]) * 60

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
        resp = SESSION.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
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
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        # One streamed GET: its headers answer the size/type checks before
        # any of the body is read, so no separate HEAD round trip is needed.
        with SESSION.get(url, headers=_headers(), timeout=DOWNLOAD_TIMEOUT, stream=True) as resp:
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code}"
            content_type = resp.headers.get("Content-Type", "").lower()
            content_length = int(resp.headers.get("Content-Length", 0) or 0)
            if content_length and content_length > MAX_FILE_MB * 1024 * 1024:
                return False, f"Skipped (file is {content_length / 1e6:.1f} MB, over the {MAX_FILE_MB} MB limit)."
            if content_type and "pdf" not in content_type and not url.lower().endswith(".pdf"):
                return False, f"Skipped (not a PDF: content-type '{content_type}')."
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


def extract_all_numbers(pdf_path: Path, source_category: str = "",
                        pages_out: list | None = None) -> list[dict]:
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

    import pdfplumber  # here, not at module level: see the availability flags above

    findings, seen = [], set()
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages[:MAX_PDF_PAGES_SCANNED], start=1):
                text = page.extract_text() or ""
                page.close()  # drop this page's parsed layout now, not when the whole PDF closes
                if pages_out is not None:
                    pages_out.append((page_num, text))
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



def _rss_items(url: str, source: str) -> list[dict]:
    try:
        resp = SESSION.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
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




# --------------------------------------------------------------------------
# PDF extraction, cached in the archive by file hash
# --------------------------------------------------------------------------

# Parsing is pure Python, so the GIL runs one parse at a time anyway: parallel
# parses only multiplied memory (~200 MB per annual report) and thrashed.
# Downloads stay parallel; each file is parsed as soon as the parser is free.
_PARSE_LOCK = threading.Lock()


def extract_and_index(path: Path, ticker: str, category: str) -> list[dict]:
    """Figures from one filing, parsed at most once ever.

    A filing that has been seen before comes straight back from the archive;
    a new one is opened once and its page text is stored for search at the
    same time, so nothing is read twice.
    """
    if not path.exists():
        return []
    try:
        sha = archive.file_sha(path)
    except OSError:
        return extract_all_numbers(path, source_category=category)

    cached = archive.cached_figures(sha)
    if cached is not None:
        return cached

    pages: list[tuple[int, str]] = []
    with _PARSE_LOCK:
        figures = extract_all_numbers(path, source_category=category, pages_out=pages)
    for f in figures:
        f["Category"] = classify_figure_category(f["Label"])
    try:
        archive.store_document(sha, ticker, category, str(path), figures, pages)
    except Exception:
        pass  # the archive is an optimisation, never a reason to fail a run
    return figures


# --------------------------------------------------------------------------
# News
# --------------------------------------------------------------------------

@st.cache_data(ttl=NEWS_CACHE_TTL_SECONDS, max_entries=100, show_spinner=False)
def get_news(ticker: str, company_name: str, ai_sig: tuple, _settings: dict) -> dict:
    """Cached apart from the scrape, so changing filing options never
    re-fetches headlines or re-spends tokens summarising the same ones."""
    headlines = fetch_company_news(ticker, company_name)
    news = {"headlines": headlines, "summary": None, "sentiment": None, "tokens": 0, "model": None}
    if ai_sig[0] and headlines:
        news.update(llm.summarize_news(company_name, headlines, _settings), at=time.time())
        news["model"] = llm.LAST_MODEL or _settings["ai"]["model"]
    return news


# --------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------

@st.cache_data(ttl=CACHE_TTL_SECONDS, max_entries=60, show_spinner=False)  # ~0.5 MB of HTML each
def fetch_company_page(ticker: str) -> str | None:
    return _fetch_html(BASE_URL.format(ticker=ticker))


@st.cache_data(ttl=CACHE_TTL_SECONDS, max_entries=300, show_spinner=False)
def cached_analysis(ticker, company_name, metrics, quarterly_df, pros, cons, ai_sig, _settings) -> dict:
    """Keyed on exactly what the model sees, so changing filing options (which
    never reach the prompt) costs nothing. Raises on failure because
    st.cache_data doesn't cache exceptions -- a blip isn't remembered."""
    analysis = llm.analyze(ticker, company_name, metrics, quarterly_df, pros, cons, _settings)
    if not analysis["summary"]:
        raise RuntimeError("the model returned no summary")
    return {**analysis, "at": time.time(), "model": llm.LAST_MODEL or _settings["ai"]["model"]}


def ai_signature(settings: dict) -> tuple:
    ai = settings["ai"]
    return (bool(ai.get("enabled") and ai.get("model")), ai.get("provider"), ai.get("model"))


def _blank_result(ticker: str) -> dict:
    return {"ticker": ticker, "ok": False, "error": None, "error_friendly": None, "company_name": None,
            "metrics": {}, "quarterly_df": None, "shareholding_df": None, "pros": [], "cons": [],
            "documents": [], "downloaded": {}, "figures": [], "ai_summary": None, "ai_verdict": None,
            "ai_model": None, "ai_tokens": 0}


def _fail(result: dict, message: str, settings: dict) -> dict:
    result["error"] = message
    if ai_signature(settings)[0]:
        friendly, result["ai_tokens"] = llm.explain_error(result["ticker"], message, settings)
        result["ai_at"] = time.time()
        result["error_friendly"] = sanitize_text(friendly)
    return result


def scrape_ticker(ticker: str, settings: dict, selected_categories: tuple[str, ...],
                  max_per_category: int | None, on_name=None) -> dict:
    ticker = (ticker or "").strip().upper()
    result = _blank_result(ticker)
    if not ticker:
        result["error"] = "Empty ticker."
        return result

    html = fetch_company_page(ticker)
    if html is None:
        return _fail(result, f"Could not fetch page for '{ticker}' (network error or ticker not found).", settings)
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        return _fail(result, f"Failed to parse HTML for '{ticker}': {exc}", settings)

    title_tag = soup.select_one("h1")
    result["company_name"] = sanitize_text(title_tag.get_text(strip=True)) if title_tag else ticker
    if on_name:
        on_name(result["company_name"])  # lets news start while filings download

    for tag in soup.select('a[title="Sector"], a[title="Industry"]'):
        result[tag["title"].lower()] = sanitize_text(tag.get_text(strip=True))

    try:
        for li in soup.select("#top-ratios li"):
            name_el, value_el = li.select_one(".name"), li.select_one(".value")
            if name_el and value_el:
                result["metrics"][sanitize_text(name_el.get_text(strip=True))] = \
                    sanitize_text(value_el.get_text(strip=True))
    except Exception:
        pass

    try:
        result["quarterly_df"] = sanitize_df_strings(_extract_table(soup, "quarters"))
    except Exception as exc:
        result["error"] = (result["error"] or "") + f" | Quarterly table parse issue: {exc}"
    for key, section in (("profit_loss_df", "profit-loss"), ("balance_sheet_df", "balance-sheet"),
                         ("cash_flow_df", "cash-flow"), ("ratios_df", "ratios"),
                         ("shareholding_df", "shareholding")):
        try:
            result[key] = sanitize_df_strings(_extract_table(soup, section))
        except Exception:
            result[key] = None  # bonus data -- never fatal

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
            documents.append({"category": category, "text": sanitize_text(link_text) or "(untitled link)",
                              "url": sanitize_text(href)})
        result["documents"] = documents
    except Exception as exc:
        result["error"] = (result["error"] or "") + f" | Document scan issue: {exc}"

    # Downloads run in parallel -- they are pure network waits, and this is
    # where most of a run's wall-clock time used to go.
    picked = pick_documents_by_selection(result["documents"], list(selected_categories), max_per_category)
    jobs = []
    for category, docs in picked.items():
        safe_cat = re.sub(r"[^A-Za-z0-9]+", "_", category)
        result["downloaded"][category] = []
        for i, doc in enumerate(docs):
            suffix = "" if i == 0 else f"_{i + 1}"
            # Named by URL, so a newly published filing is downloaded while one
            # already on disk is never fetched (or parsed) twice.
            tag = hashlib.sha1(doc["url"].encode()).hexdigest()[:10]
            jobs.append((category, doc, DOWNLOAD_DIR / ticker / f"{safe_cat}{suffix}_{tag}.pdf"))
    if jobs:
        workers = max(1, min(int(settings["data"]["doc_workers"]), len(jobs)))

        def fetch_and_parse(job):
            category, doc, dest = job
            ok, err = smart_download(doc["url"], dest)
            # Parsed the moment it lands, while the other files are still
            # downloading. Deterministic, code-only: nothing depends on a model.
            return ok, err, extract_and_index(dest, ticker, category) if ok else []

        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(fetch_and_parse, jobs))
        figures = []
        for (category, doc, dest), (ok, err, found) in zip(jobs, outcomes):
            result["downloaded"][category].append(
                {"path": str(dest) if ok else None, "error": err, "url": doc["url"]})
            figures.extend(found)
        result["figures"] = figures

    if ai_signature(settings)[0]:
        try:
            analysis = cached_analysis(ticker, result["company_name"], result["metrics"],
                                       result["quarterly_df"], result["pros"], result["cons"],
                                       ai_signature(settings), settings)
        except RuntimeError:
            analysis = {"summary": None, "verdict": None, "tokens": 0, "at": time.time()}
        result["ai_at"] = analysis["at"]
        result["ai_summary"] = sanitize_text(analysis["summary"])
        result["ai_verdict"] = analysis["verdict"]
        result["ai_model"] = analysis.get("model") or settings["ai"]["model"]
        result["ai_tokens"] = analysis["tokens"]
        if not result["ai_summary"]:
            # Why it failed, so the card can say more than "no summary".
            result["ai_error"] = llm.LAST_ERROR

    result["ok"] = True
    return result


# One analysis at a time across the whole app (Research page and the portfolio
# refresher), so a background refresh never doubles the load on the machine.
# ponytail: global lock; a second user waits for the first run to finish.
RUN_LOCK = threading.Lock()


def scrape_all(tickers: list[str], settings: dict, selected_categories: tuple[str, ...],
               max_per_category: int | None, progress=None) -> list[dict]:
    with RUN_LOCK:
        return _scrape_all(tickers, settings, selected_categories, max_per_category, progress)


def _scrape_all(tickers, settings, selected_categories, max_per_category, progress) -> list[dict]:
    apply_settings(settings)
    llm.start_run()
    delay = (settings["data"]["delay_min"], settings["data"]["delay_max"])
    want_news = settings["features"]["news"]
    results, total = [], len(tickers)
    # News is fetched (and summarised) on one side thread while the same
    # company's filings download and parse, instead of after them.
    ctx, news = get_script_run_ctx(), {}
    with ThreadPoolExecutor(max_workers=1,
                            initializer=lambda: add_script_run_ctx(threading.current_thread(), ctx)) as side:
        for i, ticker in enumerate(tickers):
            if progress:
                progress.progress(i / max(total, 1), text=f"Processing {ticker}…")
            started = time.time()
            start_news = (lambda name, t=ticker.strip().upper(): news.__setitem__(
                t, side.submit(get_news, t, name or t, ai_signature(settings), settings))) if want_news else None
            results.append(scrape_ticker(ticker, settings, selected_categories, max_per_category, start_news))
            # Only pause when we actually hit the network; a cached ticker is free.
            if time.time() - started > 0.05 and i < total - 1:
                time.sleep(random.uniform(*delay))
        if progress and news:
            progress.progress(0.95, text="Finishing news…")
        for data in results:
            if data["ok"] and data["ticker"] in news:
                data["news"] = news[data["ticker"]].result()
    if progress:
        progress.progress(1.0, text="Done.")
    return results


# --------------------------------------------------------------------------
# Correlated numbers: every figure the app holds, lined up metric by metric
# --------------------------------------------------------------------------

# screener.in's ratio names -> the canonical labels used everywhere else, so
# a site ratio, a quarterly row and a figure lifted out of a PDF all land on
# the same line of the detail table.
SCREENER_TO_CANON = {
    "ROE": "ROE", "ROCE": "ROCE", "Book Value": "Book Value", "Dividend Yield": "Dividend",
    "Market Cap": "Market Cap", "Current Price": "Share Price", "Stock P/E": "P/E",
    "Face Value": "Face Value", "High / Low": "52-week High / Low",
}
QUARTER_TO_CANON = {
    "Sales": "Revenue", "Revenue": "Revenue", "Expenses": "Expenses",
    "Operating Profit": "Operating Profit", "OPM": "Operating Margin", "OPM %": "Operating Margin",
    "Net Profit": "Net Profit (PAT)", "EPS": "EPS", "EPS in Rs": "EPS",
    "Interest": "Interest", "Depreciation": "Depreciation", "Tax": "Tax", "Tax %": "Tax",
    "Other Income": "Other Income", "Profit before tax": "Profit Before Tax",
}


def _q_value(qdf, metric_prefix: str, offset: int = -1):
    """One quarterly row's value, `offset` columns from the right."""
    if qdf is None or qdf.empty:
        return None, None
    rows = qdf[qdf["Metric"].astype(str).str.startswith(metric_prefix)]
    if rows.empty or len(qdf.columns) < abs(offset) + 1:
        return None, None
    column = qdf.columns[offset]
    return _to_number(rows.iloc[0][column]), column


def correlate_numbers(r: dict) -> pd.DataFrame:
    """One row per metric, every source of it side by side.

    This is the detail behind the summary: the value screener.in publishes,
    the value the last two quarters reported, and what the filings
    themselves said -- count, latest, and the spread across mentions, so a
    single odd number in a PDF is obvious rather than hidden in an average.
    """
    rows: dict[str, dict] = {}

    def row(label: str) -> dict:
        return rows.setdefault(label, {
            "Metric": label, "Group": classify_figure_category(label), "Screener": None,
            "Latest quarter": None, "Previous quarter": None, "QoQ": None,
            "In filings": 0, "Filing latest": None, "Filing low": None,
            "Filing median": None, "Filing high": None, "Unit": "", "Sources": "", "Pages": ""})

    for name, value in (r.get("metrics") or {}).items():
        row(SCREENER_TO_CANON.get(name, name))["Screener"] = value

    qdf = r.get("quarterly_df")
    if qdf is not None and not qdf.empty:
        for metric in qdf["Metric"].astype(str):
            canon = QUARTER_TO_CANON.get(metric.rstrip(" %"), metric)
            latest, latest_q = _q_value(qdf, metric, -1)
            previous, _ = _q_value(qdf, metric, -2)
            entry = row(canon)
            entry["Latest quarter"], entry["Previous quarter"] = latest, previous
            if latest is not None and previous:
                entry["QoQ"] = (latest - previous) / abs(previous)
            if latest_q:
                entry["Unit"] = entry["Unit"] or ("%" if metric.strip().endswith("%") else "₹ Cr")

    figures = r.get("figures") or []
    if figures:
        fdf = pd.DataFrame(figures)
        for label, group in fdf.groupby("Label"):
            values = pd.to_numeric(group["Value"], errors="coerce").dropna()
            if values.empty:
                continue
            entry = row(str(label))
            entry["In filings"] = int(len(values))
            entry["Filing latest"] = float(values.iloc[0])
            entry["Filing low"], entry["Filing high"] = float(values.min()), float(values.max())
            entry["Filing median"] = float(values.median())
            entry["Unit"] = entry["Unit"] or (group["Unit"].dropna().iloc[0] if group["Unit"].notna().any() else "")
            entry["Sources"] = ", ".join(sorted({str(s) for s in group["Source"].dropna()}))
            pages = sorted({int(x) for x in pd.to_numeric(group["Page"], errors="coerce").dropna()})
            entry["Pages"] = ", ".join(str(x) for x in pages[:10]) + (" …" if len(pages) > 10 else "")

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(list(rows.values()))
    # Keep the numeric columns numeric: a column holding both floats and None
    # falls back to object dtype, and Streamlit then prints a literal "None"
    # in every empty cell instead of leaving it blank.
    for column in ("Latest quarter", "Previous quarter", "QoQ", "In filings",
                   "Filing latest", "Filing low", "Filing median", "Filing high"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["Screener"] = df["Screener"].fillna("").astype(str)
    # Metrics with the most corroboration first: they are the ones worth reading.
    df["_rank"] = (df["Screener"].notna().astype(int) + df["Latest quarter"].notna().astype(int)
                   + (df["In filings"] > 0).astype(int))
    df["Identified"] = ((df["Group"] != "Other") | (df["Screener"] != "")
                        | df["Latest quarter"].notna())
    return df.sort_values(["_rank", "In filings", "Metric"], ascending=[False, False, True]) \
             .drop(columns="_rank").reset_index(drop=True)


def figures_frame(r: dict) -> pd.DataFrame:
    figures = r.get("figures") or []
    return pd.DataFrame(figures) if figures else pd.DataFrame()


STATEMENT_TABLES = [("profit_loss_df", "Profit & loss"), ("balance_sheet_df", "Balance sheet"),
                    ("cash_flow_df", "Cash flow"), ("ratios_df", "Ratios"),
                    ("shareholding_df", "Shareholding")]


# --------------------------------------------------------------------------
# Saving a run, and comparing it with last time
# --------------------------------------------------------------------------

def _df_to_json(df) -> dict | None:
    if df is None or not hasattr(df, "columns") or df.empty:
        return None
    return {"columns": [str(c) for c in df.columns], "data": df.astype(object).where(df.notna(), None).values.tolist()}


def _pack(obj) -> dict | None:
    """Compress a big table inside a saved analysis (roughly 10x smaller). The
    small fields beside it stay plain JSON, so SQL can still read a verdict."""
    return None if obj is None else {"z": base64.b64encode(zlib.compress(json.dumps(obj).encode(), 6)).decode()}


def _df_from_json(obj) -> pd.DataFrame | None:
    if obj and "z" in obj:
        obj = json.loads(zlib.decompress(base64.b64decode(obj["z"])))
    if not obj or not obj.get("columns"):
        return None
    return pd.DataFrame(obj["data"], columns=obj["columns"])


def _sources_of(result: dict) -> list[dict]:
    """Where each filing landed on disk, and which pages figures came from --
    so a saved analysis can always be traced back to the page it came from."""
    pages_by_category: dict[str, list[int]] = {}
    for f in result.get("figures") or []:
        page = f.get("Page")
        if isinstance(page, (int, float)):
            pages_by_category.setdefault(str(f.get("Source") or ""), []).append(int(page))

    sources = []
    for category, infos in (result.get("downloaded") or {}).items():
        pages = sorted(set(pages_by_category.get(category, [])))
        for info in infos:
            path = info.get("path") or ""
            sources.append({
                "category": category,
                "file": Path(path).name if path else "",
                "path": str(Path(path).resolve()) if path else "",
                "url": info.get("url") or "",
                "error": info.get("error") or "",
                "figures": len(pages_by_category.get(category, [])),
                "first_page": pages[0] if pages else None,
                "last_page": pages[-1] if pages else None,
            })
    return sources


def snapshot_of(result: dict) -> dict:
    """A saved analysis: everything except the raw figures, which stay in the
    archive keyed by file hash and would otherwise dominate the database."""
    news = dict(result.get("news") or {})
    news["headlines"] = [{**h, "published": h["published"].isoformat() if h.get("published") else None}
                         for h in (news.get("headlines") or [])]
    snap = {k: result.get(k) for k in
            ("ticker", "ok", "error", "error_friendly", "company_name", "metrics", "pros", "cons",
             "documents", "downloaded", "ai_summary", "ai_verdict", "ai_model", "ai_tokens", "ai_at",
             "sector", "industry")}
    snap["news"] = news
    snap["figure_count"] = len(result.get("figures") or [])
    snap["sources"] = _sources_of(result)
    for key, _ in [("quarterly_df", "")] + [(k, "") for k, _ in STATEMENT_TABLES]:
        snap[key] = _df_to_json(result.get(key))
    correlated = result.get("correlated")
    if correlated is None:  # built once per analysis and kept, not rebuilt per compare/save
        correlated = result["correlated"] = correlate_numbers(result)
    snap["correlated"] = _pack(_df_to_json(correlated))
    return snap


def compact_runs() -> int:
    """One-off: compress the figure table in analyses saved before packing
    existed, then give the space back. Lossless; a no-op once done."""
    with archive.connect() as conn:
        rows = conn.execute("SELECT id, snapshot FROM runs WHERE json_type(snapshot, '$.correlated') = 'object' "
                            "AND json_type(snapshot, '$.correlated.z') IS NULL").fetchall()
        for row in rows:
            snap = json.loads(row["snapshot"])
            snap["correlated"] = _pack(snap["correlated"])
            conn.execute("UPDATE runs SET snapshot=? WHERE id=?", (json.dumps(snap, default=str), row["id"]))
    if rows:
        with archive.connect() as conn:
            conn.execute("VACUUM")
    return len(rows)


def restore_snapshot(snap: dict) -> dict:
    """A saved analysis turned back into something the pages can render."""
    result = dict(snap)
    for key in ["quarterly_df", "correlated"] + [k for k, _ in STATEMENT_TABLES]:
        result[key] = _df_from_json(snap.get(key))
    news = dict(snap.get("news") or {})
    headlines = []
    for h in news.get("headlines") or []:
        published = h.get("published")
        try:
            published = datetime.fromisoformat(published) if published else None
        except (TypeError, ValueError):
            published = None
        headlines.append({**h, "published": published})
    news["headlines"] = headlines
    result["news"] = news
    result["figures"] = []
    result["restored"] = True
    return result


def save_run(result: dict, label: str = "") -> int | None:
    if not result.get("ok"):
        return None
    try:
        return archive.save_run(result["ticker"], label, snapshot_of(result))
    except Exception:
        return None  # history is a convenience, never a reason to fail a run


def changes_since_last(result: dict) -> tuple[dict | None, list[dict]]:
    """(the previous saved analysis, what changed since) for one company."""
    previous = archive.previous_run(result["ticker"], time.time())
    if not previous:
        return None, []
    return previous, archive.diff_snapshots(previous["snapshot"], snapshot_of(result))


# --------------------------------------------------------------------------
# Guidance: what management promised, checked against what was reported
# --------------------------------------------------------------------------

def actuals_text(result: dict, quarters: int = 3) -> str:
    """The reported numbers a promise gets graded against -- scraped, not recalled."""
    qdf = result.get("quarterly_df")
    if qdf is None or qdf.empty or len(qdf.columns) < 2:
        return ""
    columns = list(qdf.columns)[-quarters:]
    lines = []
    for _, row in qdf.iterrows():
        metric = str(row["Metric"])
        if metric.startswith(llm.AI_QUARTERLY_ROWS):
            lines.append(f"{metric}: " + ", ".join(f"{c} {row[c]}" for c in columns))
    ratios = "; ".join(f"{k} {v}" for k, v in (result.get("metrics") or {}).items())
    return "\n".join(lines) + (f"\nCurrent ratios: {ratios}" if ratios else "")


def quarter_label(result: dict) -> str:
    qdf = result.get("quarterly_df")
    if qdf is not None and not qdf.empty and len(qdf.columns) > 1:
        return str(qdf.columns[-1])
    return datetime.now().strftime("%b %Y")


def harvest_guidance(result: dict, settings: dict) -> tuple[int, int]:
    """Pull management's forward-looking claims out of the latest concall
    transcript and file them. Returns (claims saved, tokens used)."""
    text = archive.document_text(result["ticker"], "Concall Transcript")
    if not text:
        return 0, 0
    claims, tokens = llm.extract_guidance(
        result.get("company_name") or result["ticker"], quarter_label(result), text, settings)
    return archive.save_claims(result["ticker"], quarter_label(result), claims), tokens


def check_guidance(result: dict, settings: dict, only_unchecked: bool = True) -> tuple[int, int]:
    """Grade filed claims against the numbers reported since. Only claims
    from an earlier quarter are graded -- this quarter's are not due yet."""
    actuals = actuals_text(result)
    if not actuals:
        return 0, 0
    current = quarter_label(result)
    checked = tokens = 0
    for claim in archive.claims_for(result["ticker"]):
        if only_unchecked and claim.get("status"):
            continue
        if claim.get("quarter") == current:
            continue
        verdict, used = llm.judge_guidance(result.get("company_name") or result["ticker"], claim, actuals, settings)
        tokens += used
        if verdict["status"]:
            archive.set_claim_status(claim["id"], verdict["status"], verdict["why"], current)
            checked += 1
    return checked, tokens


# --------------------------------------------------------------------------
# Batch input
# --------------------------------------------------------------------------

TICKER_RE = re.compile(r"^[A-Z0-9&.\-]{1,20}$")


def tickers_from_upload(uploaded) -> tuple[list[str], str | None]:
    """Tickers from an uploaded CSV/Excel watchlist.

    Looks for a column called ticker/symbol/scrip/code (any case); failing
    that, uses the first column. Returns (tickers, error message).
    """
    try:
        name = (getattr(uploaded, "name", "") or "").lower()
        if name.endswith((".xlsx", ".xlsm", ".xls")):
            df = pd.read_excel(uploaded)
        else:
            df = pd.read_csv(uploaded)
    except Exception as exc:
        return [], f"Could not read that file: {exc}"
    if df.empty:
        return [], "That file has no rows."

    column = next((c for c in df.columns
                   if str(c).strip().lower() in {"ticker", "tickers", "symbol", "scrip", "code", "nse code"}),
                  df.columns[0])
    seen, tickers = set(), []
    for value in df[column].dropna():
        candidate = str(value).strip().upper()
        if TICKER_RE.match(candidate) and candidate not in seen:
            seen.add(candidate)
            tickers.append(candidate)
    if not tickers:
        return [], f"No usable ticker symbols found in column '{column}'."
    return tickers, None


def watchlist_template() -> bytes:
    return pd.DataFrame({"ticker": ["RELIANCE", "TCS", "INFY"],
                         "notes": ["", "", ""]}).to_csv(index=False).encode("utf-8")


# --------------------------------------------------------------------------
# Archive search: retrieve, re-rank, answer
# --------------------------------------------------------------------------

# "Which companies...", "how many companies...", "list the companies that..."
AGGREGATE_RE = re.compile(
    r"\b(which|what|how many|list|name|any)\b[^?]{0,40}\b(compan(y|ies)|firms?|tickers?|stocks?)\b",
    re.I)


def coverage_sentence(question: str, matched_pages: int, coverage: dict) -> str | None:
    """The exact answer to "which companies...", stated from the index.

    A model reading eight passages cannot know what the other hundred say,
    and a small one will not enumerate a list from a tally either. This
    comes straight from SQL, so it is complete and always right.
    """
    if not coverage or not AGGREGATE_RE.search(question or ""):
        return None
    listed = ", ".join(f"**{ticker}** ({count})" for ticker, count in coverage.items())
    return (f"**{len(coverage)} compan{'y' if len(coverage) == 1 else 'ies'}** in your archive "
            f"match that, across {matched_pages} page(s) — with the number of matching pages each: "
            f"{listed}.")


def _focus(text: str, terms: list[str], width: int = 420, windows: int = 2) -> str:
    """The parts of a page that actually matched, not its opening lines.

    Sending the first N characters of a page was the single worst thing the
    search did: on a 2,600-character page the opening often does not contain
    the search term at all, so the model was asked to answer from text that
    had nothing to do with the question.
    """
    body = " ".join((text or "").split())
    if not body:
        return ""
    lowered = body.lower()
    spots = []
    for term in terms:
        term = term.strip().lower()
        if len(term) < 3:
            continue
        at = lowered.find(term)
        if at >= 0:
            spots.append(at)
    if not spots:
        return body[: width * windows]

    spots.sort()
    picked: list[tuple[int, int]] = []
    for spot in spots:
        start, end = max(0, spot - width // 3), min(len(body), spot + width)
        if picked and start <= picked[-1][1]:        # overlapping: widen the last one
            picked[-1] = (picked[-1][0], max(picked[-1][1], end))
        elif len(picked) < windows:
            picked.append((start, end))
    return " … ".join(body[start:end] for start, end in picked)


def _wide_context(settings: dict) -> dict:
    """Archive prompts carry whole passages, unlike the short ones elsewhere,
    so local models need a bigger window for them. Hosted providers ignore it."""
    return {**settings, "ai": {**settings["ai"],
                               "num_ctx": max(8192, int(settings["ai"]["num_ctx"])),
                               # Fail fast: three steps behind one spinner must
                               # not add up to minutes if a provider is busy.
                               "timeout": min(int(settings["ai"].get("timeout", 60)), 30)}}
