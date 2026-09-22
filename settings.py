"""
User settings: one JSON file, deep-merged over the defaults below.

Everything the app lets you customise lives here -- AI provider and keys,
scraping limits, feature toggles and the look of the UI. The file is written
with owner-only permissions and is gitignored, because it can hold API keys.

Environment variables always win over the stored value for API keys, so a
shared machine can keep keys out of the file entirely.
"""

import copy
import json
import os
from pathlib import Path

# Everything you create -- settings, portfolios, saved analyses, filings --
# lives outside the app folder, so updating or replacing the app never
# touches it. STOCK_HUB_DATA moves it elsewhere.
DATA_DIR = Path(os.environ.get("STOCK_HUB_DATA") or Path.home() / ".stock-data-hub")
DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_PATH = DATA_DIR / "settings.json"
_OLD_SETTINGS = Path(__file__).parent / "settings.json"  # where it lived before
if _OLD_SETTINGS.exists() and not SETTINGS_PATH.exists() and not os.environ.get("STOCK_HUB_DATA"):
    _OLD_SETTINGS.replace(SETTINGS_PATH)

WANTED_CATEGORIES = ["Concall Transcript", "Investor Presentation", "Annual Report", "Quarterly Results"]

# Provider id -> (label, env var holding the key, default base URL, needs a key?)
PROVIDERS = {
    "ollama":     ("Ollama (local)",        "OLLAMA_API_KEY",     "http://localhost:11434", False),
    "anthropic":  ("Anthropic (Claude)",    "ANTHROPIC_API_KEY",  "https://api.anthropic.com", True),
    "openai":     ("OpenAI",                "OPENAI_API_KEY",     "https://api.openai.com/v1", True),
    "google":     ("Google (Gemini)",       "GOOGLE_API_KEY",     "https://generativelanguage.googleapis.com/v1beta", True),
    "openrouter": ("OpenRouter",            "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", True),
    "custom":     ("Custom / self-hosted",  "CUSTOM_API_KEY",     "", False),
}

ACCENTS = {
    "Research blue": "#1F4E79", "Ink": "#1C1F26", "Forest": "#1F7A4D",
    "Burgundy": "#8C2F39", "Bronze": "#A8620A", "Indigo": "#3D3A8C",
}

DEFAULTS = {
    "ai": {
        "enabled": True,
        "provider": "ollama",
        "model": "",
        "temperature": 0.2,
        "num_ctx": 2048,
        "timeout": 60,
        "tokens_analysis": 220,   # JSON wrapping needs a little more room than plain text
        "tokens_news": 110,
        "tokens_error": 60,
        "tokens_guidance": 320,
        "tokens_synthesis": 260,
        "tokens_chat": 1200,      # Ask AI replies: room for a real answer, streamed
    },
    # Per-provider connection details. Keys stay empty when you use env vars.
    # Prices are per million tokens and start at 0: the app will not invent a
    # price list that goes stale. Copy the numbers from your provider's
    # pricing page and the spend figures become real money.
    "providers": {pid: {"base_url": base, "api_key": "", "price_in": 0.0, "price_out": 0.0}
                  for pid, (_, _, base, _) in PROVIDERS.items()},
    # Nothing here applies to a local model: it is free, and recorded as free.
    "budget": {
        "enabled": True,
        "currency": "USD",
        "max_tokens_per_run": 50000,   # a single run can never run away
        "monthly_tokens": 0,           # 0 = no token cap
        "monthly_cost": 5.0,           # 0 = no cost cap
        "warn_at": 80,                 # warn at this % of a cap
    },
    "data": {
        "max_file_mb": 25,
        "max_pdf_pages": 150,
        "cache_ttl_min": 60,
        "news_cache_ttl_min": 30,
        "news_days": 14,
        "news_items": 8,
        "request_timeout": 10,
        "download_timeout": 20,
        "doc_workers": 4,
        "delay_min": 1.0,
        "delay_max": 2.0,
        "categories": list(WANTED_CATEGORIES),
        "timeframe": "Latest only",
        "default_tickers": "RELIANCE, TCS, INFY",
    },
    "features": {
        "news": True,
        "detailed_numbers": True,
        "archive": True,
        "archive_ai": True,   # let the model search and answer, not just keyword-match
        "history": True,
        "guidance": True,
    },
    # Every held stock is re-analysed this often in the background; 0 = only
    # once, when it is added.
    "portfolio": {"refresh_hours": 24, "track_positions": True, "max_stock_pct": 20, "max_sector_pct": 35},
    # Where alerts and the weekly digest go: "mac" (a macOS notification) and/or
    # "email" (sent by your own Google Apps Script web app, to you only).
    "notify": {"channels": ["mac"], "email_url": "", "email_token": "",
               "verdict": True, "filing": True, "guidance": True, "price": True,
               "digest": True, "digest_day": 0},
    # How long saved work is kept. 0 = keep forever; a purge runs at startup.
    "storage": {
        "keep_runs_days": 180,       # saved analyses you can reopen from History
        "keep_documents_days": 90,   # downloaded filing PDFs on disk
        "keep_text_days": 365,       # searchable transcript text in the archive
        "max_runs_per_ticker": 40,
        "auto_purge": True,
    },
    "ui": {
        "custom_style": True,
        "accent": "#1F4E79",
        "font_scale": 1.0,
        "density": "Comfortable",
        "sparklines": True,
        "chart_colors": ["#1F4E79", "#C08A3E"],
        "table_height": 380,
        "decimals": 2,
        "show_context_column": True,
        "tabs": ["Overview", "Financials", "All numbers", "What changed", "Guidance", "Filings"],
        "history_page_size": 25,
        "headline": "Scrape screener.in, pull the latest filings, extract every figure from them, "
                    "and summarise with the model of your choice.",
    },
}

ALL_TABS = list(DEFAULTS["ui"]["tabs"])
DENSITIES = {"Compact": 0.35, "Comfortable": 0.75, "Roomy": 1.2}


def _merge(base: dict, extra: dict) -> dict:
    """Recursive update: unknown keys in the file are kept, missing ones filled."""
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load() -> dict:
    """Stored settings over defaults. A corrupt file never blocks the app."""
    try:
        stored = json.loads(SETTINGS_PATH.read_text())
    except (OSError, ValueError):
        stored = {}
    return _merge(DEFAULTS, stored if isinstance(stored, dict) else {})


def save(settings: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
    try:
        os.chmod(SETTINGS_PATH, 0o600)  # the file can hold API keys
    except OSError:
        pass


def reset() -> None:
    SETTINGS_PATH.unlink(missing_ok=True)


def api_key(settings: dict, provider: str) -> str:
    """Environment first, so keys need never be written to disk."""
    env_var = PROVIDERS.get(provider, (None, "", None, None))[1]
    return (os.environ.get(env_var, "").strip()
            or (settings.get("providers", {}).get(provider, {}).get("api_key") or "").strip())


def base_url(settings: dict, provider: str) -> str:
    stored = (settings.get("providers", {}).get(provider, {}).get("base_url") or "").strip()
    url = stored or PROVIDERS.get(provider, ("", "", "", False))[2]
    if url.startswith("0.0.0.0"):  # a bind address; connect over localhost
        url = url.replace("0.0.0.0", "localhost", 1)
    if url and "://" not in url:
        url = f"http://{url}"
    return url.rstrip("/")


def style_css(ui: dict) -> str:
    """Accent colour, text size and row spacing, applied live.

    Deliberately three properties and nothing else: the base look stays in
    .streamlit/config.toml, so a bad value here can't leave the app unusable.
    """
    if not ui.get("custom_style", True):
        return ""
    accent = ui.get("accent") or DEFAULTS["ui"]["accent"]
    scale = max(0.8, min(1.4, float(ui.get("font_scale") or 1.0)))
    gap = DENSITIES.get(ui.get("density"), 0.75)
    return (
        "<style>"
        f":root{{--primary-color:{accent};--link-color:{accent};}}"
        f'section[data-testid="stMain"] .stMarkdown{{font-size:{scale}rem;}}'
        f'section[data-testid="stMain"] [data-testid="stVerticalBlock"]{{gap:{gap}rem;}}'
        "</style>"
    )


CURRENCIES = {"USD": "$", "INR": "₹", "EUR": "€", "GBP": "£"}


def prices(settings: dict, provider: str) -> tuple[float, float]:
    """(input, output) price per million tokens, as the user entered them."""
    row = settings.get("providers", {}).get(provider, {})
    try:
        return float(row.get("price_in") or 0.0), float(row.get("price_out") or 0.0)
    except (TypeError, ValueError):
        return 0.0, 0.0


def money(settings: dict, amount: float) -> str:
    symbol = CURRENCIES.get(settings.get("budget", {}).get("currency", "USD"), "")
    return f"{symbol}{amount:,.4f}" if 0 < amount < 0.01 else f"{symbol}{amount:,.2f}"


def month_start() -> float:
    """Midnight on the 1st of this month, local time -- the budget window."""
    import datetime as _dt
    now = _dt.datetime.now()
    return _dt.datetime(now.year, now.month, 1).timestamp()
