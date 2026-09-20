"""
One small interface in front of every model provider the app supports.

Local Ollama, any OpenAI-compatible server you point it at, Anthropic,
OpenAI, Google and OpenRouter all answer through `complete()` and report
their own token usage, so the "what did this cost" caption in the UI stays
honest whichever one you pick.

Prompts are deliberately short and built only from data the app has already
scraped: clean ratios and headlines, never the raw PDF text. That keeps a
company review at roughly 400 tokens on any provider.
"""

import re
import time

import requests
import streamlit as st

import archive
import settings as cfg

_JSON = {"Content-Type": "application/json"}


def _auth_headers(provider: str, key: str) -> dict:
    if provider == "anthropic":
        return {**_JSON, "x-api-key": key, "anthropic-version": "2023-06-01"}
    if key:
        return {**_JSON, "Authorization": f"Bearer {key}"}
    return dict(_JSON)


# --------------------------------------------------------------------------
# Model discovery
# --------------------------------------------------------------------------

# Most capable first for local models. qwen2.5:0.5b is a last resort: in
# testing it misread weak metrics as strengths (a 7.57% ROE as "strong").
OLLAMA_PREFERENCE = ["gemma4:e4b", "gemma4:e2b", "qwen2.5:0.5b"]


@st.cache_data(ttl=300, show_spinner=False)
def list_models(provider: str, base_url: str, key: str) -> dict[str, str]:
    """{model id: display label} for a provider, or {} if it can't be reached.

    Cached for five minutes: the settings page has an explicit Refresh, and
    without this every rerun would re-hit the provider's model endpoint.
    """
    try:
        if provider == "ollama":
            resp = requests.get(f"{base_url}/api/tags", timeout=4)
            models = [m for m in resp.json().get("models", []) if "embed" not in m["name"]]
            rank = {name: i for i, name in enumerate(OLLAMA_PREFERENCE)}
            models.sort(key=lambda m: (rank.get(m["name"], len(rank)), -m.get("size", 0)))
            return {m["name"]: " · ".join(filter(None, [
                m["name"], (m.get("details") or {}).get("parameter_size"),
                f"{m.get('size', 0) / 1e9:.1f} GB"])) for m in models}

        if provider == "google":
            resp = requests.get(f"{base_url}/models", params={"key": key}, timeout=8)
            out = {}
            for m in resp.json().get("models", []):
                if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                    continue
                name = m["name"].split("/")[-1]
                out[name] = m.get("displayName") or name
            return out

        url = f"{base_url}/v1/models" if provider == "anthropic" else f"{base_url}/models"
        resp = requests.get(url, headers=_auth_headers(provider, key), timeout=8)
        data = resp.json().get("data", [])
        return {m["id"]: m.get("name") or m["id"] for m in data if m.get("id")}
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return {}


def ensure_model(settings: dict) -> bool:
    """Make sure a model is actually selected.

    The sidebar used to pick one on the fly, which meant the Archive and
    History pages -- which read the saved settings -- believed no model was
    configured and quietly disabled themselves. Now the first available
    model is chosen and saved the first time a provider answers.
    """
    ai = settings.get("ai", {})
    if not ai.get("enabled"):
        return False
    provider = ai.get("provider", "ollama")
    needs_key = cfg.PROVIDERS.get(provider, (None, None, None, False))[3]
    if needs_key and not cfg.api_key(settings, provider):
        return False
    available = list_models(provider, cfg.base_url(settings, provider),
                            cfg.api_key(settings, provider))
    if not available:
        return False
    if ai.get("model") in available:
        return False
    ai["model"] = next(iter(available))  # already ranked best-first
    return True


def provider_status(settings: dict) -> tuple[bool, str]:
    """(reachable, message) for the configured provider -- drives the sidebar badge."""
    provider = settings["ai"]["provider"]
    label = cfg.PROVIDERS.get(provider, (provider,))[0]
    needs_key = cfg.PROVIDERS.get(provider, (None, None, None, False))[3]
    key = cfg.api_key(settings, provider)
    if needs_key and not key:
        return False, f"{label}: no API key set"
    models = list_models(provider, cfg.base_url(settings, provider), key)
    if not models:
        return False, f"{label}: not reachable"
    return True, f"{label} · {len(models)} model(s)"


# --------------------------------------------------------------------------
# Completion
# --------------------------------------------------------------------------

# Tokens spent in the current run, reset by start_run(). A per-run cap is the
# guard that matters: a monthly cap can still be blown through in one go by a
# 200-company watchlist against a frontier model.
_RUN = {"tokens": 0}
LAST_BLOCK: str | None = None


def start_run() -> None:
    global LAST_BLOCK
    _RUN["tokens"] = 0
    LAST_BLOCK = None


def is_paid(settings: dict, provider: str) -> bool:
    """A provider costs money if it needs a key, or if a price was entered for
    it. A local Ollama or a self-hosted server is free and stays unmetered."""
    needs_key = cfg.PROVIDERS.get(provider, (None, None, None, False))[3]
    return bool(needs_key or any(cfg.prices(settings, provider)))


def budget_state(settings: dict) -> dict:
    """Where this month's spending stands against the caps."""
    budget = settings.get("budget", {})
    provider = settings["ai"]["provider"]
    month = archive.usage_since(cfg.month_start())
    token_cap = int(budget.get("monthly_tokens") or 0)
    cost_cap = float(budget.get("monthly_cost") or 0)
    return {
        "enabled": bool(budget.get("enabled", True)),
        "paid": is_paid(settings, provider),
        "tokens": month["tokens"], "cost": month["cost"], "calls": month["calls"],
        "token_cap": token_cap, "cost_cap": cost_cap,
        "token_pct": (month["tokens"] / token_cap * 100) if token_cap else 0.0,
        "cost_pct": (month["cost"] / cost_cap * 100) if cost_cap else 0.0,
        "run_tokens": _RUN["tokens"],
        "run_cap": int(budget.get("max_tokens_per_run") or 0),
    }


def _blocked_reason(settings: dict) -> str | None:
    """The reason this call must not be made, or None to go ahead."""
    budget = settings.get("budget", {})
    if not budget.get("enabled", True):
        return None
    provider = settings["ai"]["provider"]
    if not is_paid(settings, provider):
        return None  # free to run, so nothing to protect

    run_cap = int(budget.get("max_tokens_per_run") or 0)
    if run_cap and _RUN["tokens"] >= run_cap:
        return (f"This run has used {_RUN['tokens']:,} tokens, at the per-run limit of "
                f"{run_cap:,}. Raise it in Settings, or split the work into smaller runs.")

    month = archive.usage_since(cfg.month_start())
    token_cap = int(budget.get("monthly_tokens") or 0)
    if token_cap and month["tokens"] >= token_cap:
        return (f"This month's {token_cap:,}-token budget is spent ({month['tokens']:,} used). "
                "Raise it in Settings, or switch to a local model, which is free.")
    cost_cap = float(budget.get("monthly_cost") or 0)
    if cost_cap and month["cost"] >= cost_cap:
        return (f"This month's budget of {cfg.money(settings, cost_cap)} is spent "
                f"({cfg.money(settings, month['cost'])} used). Raise it in Settings, or switch to "
                "a local model, which is free.")
    return None


def complete(prompt: str, max_tokens: int, settings: dict, kind: str = "other") -> tuple[str | None, int]:
    """(text, tokens used) from the configured provider. Never raises.

    Every call passes through the budget first and is written to the usage
    ledger afterwards, so the spend figures in Settings are what actually
    happened rather than an estimate.
    """
    global LAST_BLOCK
    ai = settings["ai"]
    provider, model = ai["provider"], (ai.get("model") or "").strip()
    if not ai.get("enabled") or not model:
        return None, 0
    blocked = _blocked_reason(settings)
    if blocked:
        LAST_BLOCK = blocked
        return None, 0

    base, key = cfg.base_url(settings, provider), cfg.api_key(settings, provider)
    temperature, timeout = float(ai.get("temperature", 0.2)), int(ai.get("timeout", 60))
    tokens_in = tokens_out = 0

    def book(text, t_in, t_out):
        """Record what this call cost before handing the answer back."""
        nonlocal tokens_in, tokens_out
        tokens_in, tokens_out = t_in or 0, t_out or 0
        price_in, price_out = cfg.prices(settings, provider)
        cost = (tokens_in * price_in + tokens_out * price_out) / 1_000_000
        _RUN["tokens"] += tokens_in + tokens_out
        archive.record_usage(provider, model, kind, tokens_in, tokens_out, cost)
        return text, tokens_in + tokens_out

    try:
        if provider == "ollama":
            resp = requests.post(
                f"{base}/api/generate",
                # think=False: thinking models (e.g. gemma4) otherwise spend the
                # whole budget on hidden reasoning and return an empty response.
                json={"model": model, "prompt": prompt, "stream": False, "think": False,
                      "options": {"temperature": temperature, "num_predict": max_tokens,
                                  "num_ctx": int(ai.get("num_ctx", 2048))}},
                timeout=timeout)
            if resp.status_code != 200:
                return None, 0
            data = resp.json()
            return book(data.get("response", "").strip() or None,
                        data.get("prompt_eval_count", 0), data.get("eval_count", 0))

        if provider == "anthropic":
            resp = requests.post(
                f"{base}/v1/messages", headers=_auth_headers(provider, key),
                json={"model": model, "max_tokens": max_tokens, "temperature": temperature,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=timeout)
            if resp.status_code != 200:
                return None, 0
            data = resp.json()
            text = "".join(b.get("text", "") for b in data.get("content", []))
            usage = data.get("usage", {})
            return book(text.strip() or None, usage.get("input_tokens", 0), usage.get("output_tokens", 0))

        if provider == "google":
            resp = requests.post(
                f"{base}/models/{model}:generateContent", params={"key": key}, headers=_JSON,
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature}},
                timeout=timeout)
            if resp.status_code != 200:
                return None, 0
            data = resp.json()
            parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            usage = data.get("usageMetadata", {})
            return book("".join(p.get("text", "") for p in parts).strip() or None,
                        usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0))

        # OpenAI, OpenRouter and anything else speaking the OpenAI chat API.
        body = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]}
        resp = requests.post(f"{base}/chat/completions", headers=_auth_headers(provider, key),
                             json=body, timeout=timeout)
        if resp.status_code == 400 and "max_completion_tokens" in resp.text:
            # Newer OpenAI reasoning models renamed the field.
            body["max_completion_tokens"] = body.pop("max_tokens")
            resp = requests.post(f"{base}/chat/completions", headers=_auth_headers(provider, key),
                                 json=body, timeout=timeout)
        if resp.status_code != 200:
            return None, 0
        data = resp.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        usage = data.get("usage", {})
        return book(text.strip() or None, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return None, 0


# An answer that still contains <angle brackets> is the template being read
# back rather than filled in -- some models do this, and without this check a
# placeholder like "<3 sentences on financial position>" was shown to the user
# as if it were the analysis.
PLACEHOLDER_RE = re.compile(r"<[^<>\n]{3,}>")
FENCE_RE = re.compile(r"```.*?```", re.S)


def _clean_answer(text: str | None) -> str | None:
    """Prose only: no code blocks, no echoed data dump, no template."""
    if not text:
        return None
    text = FENCE_RE.sub(" ", text).replace("*", "")
    # Models that echo the input indent it; real answers are never indented.
    text = "\n".join(line for line in text.splitlines() if not line.startswith("    ")).strip()
    if not text or PLACEHOLDER_RE.search(text) or len(text) < 25:
        return None
    return text


def _drop_cut_off_sentence(text: str) -> str:
    """If the token cap cut the reply mid-sentence, keep complete sentences only."""
    text = (text or "").strip()
    if text.endswith((".", "!", "?")):
        return text
    end = max(text.rfind(". "), text.rfind("! "), text.rfind("? "))
    return text[: end + 1] if end > 0 else text


def _field(text: str, name: str, options: str) -> str | None:
    match = re.search(rf"{name}[:\s]+.*?\b({options})\b", text, re.I)
    return match.group(1).capitalize() if match else None


# --------------------------------------------------------------------------
# The app's prompts
# --------------------------------------------------------------------------

AI_QUARTERLY_ROWS = ("Sales", "Operating Profit", "OPM", "Net Profit", "EPS")


def analyze(ticker, company_name, metrics, quarterly_df, pros, cons, settings) -> dict:
    """Verdict + short analysis, built only from data already scraped.

    Input is the structured screener.in data -- ratios plus the last two
    quarters -- not the regex-extracted PDF figures. Those are right for the
    searchable figures table but too noisy for a prompt: a first-occurrence
    "Dividend 5000" was a TDS threshold and "Net Profit 216" a page number.
    """
    if not metrics and quarterly_df is None and not pros and not cons:
        return {"summary": None, "verdict": None, "tokens": 0}

    ratios = "; ".join(f"{k} {v}" for k, v in metrics.items() if k != "Face Value") or "none"
    quarters = ""
    if quarterly_df is not None and not quarterly_df.empty and len(quarterly_df.columns) >= 3:
        prev_q, last_q = quarterly_df.columns[-2], quarterly_df.columns[-1]
        lines = [f"{row['Metric']} {row[prev_q]} -> {row[last_q]}"
                 for _, row in quarterly_df.iterrows()
                 if str(row["Metric"]).startswith(AI_QUARTERLY_ROWS)]
        quarters = f"Quarterly, Rs Cr ({prev_q} -> {last_q}): " + "; ".join(lines) if lines else ""

    prompt = (
        f"Equity analyst review of {company_name} ({ticker}). Use ONLY this data; never invent numbers; no markdown.\n"
        f"Ratios: {ratios}\n{quarters}\n"
        f"Pros: {'; '.join(p[:120] for p in pros[:4]) or 'none'}\n"
        f"Cons: {'; '.join(c[:120] for c in cons[:4]) or 'none'}\n"
        "\nWrite exactly two lines in your own words. Do not repeat these instructions, "
        "do not use angle brackets, and do not list the data back.\n"
        "The first line starts with 'VERDICT: ' then one word: Positive, Neutral or Cautious.\n"
        "The second line starts with 'SUMMARY: ' then three sentences on the financial position "
        "and outlook, quoting the key numbers."
    )
    # Verdict first, so it survives even if the token cap cuts the summary short.
    raw, tokens = complete(prompt, settings["ai"]["tokens_analysis"], settings, "Company analysis")
    if not raw:
        return {"summary": None, "verdict": None, "tokens": tokens}
    clean = raw.replace("*", "")
    summary = re.search(r"SUMMARY:\s*(.*)", clean, re.S | re.I)
    body = _clean_answer(summary.group(1) if summary else clean)
    return {"summary": _drop_cut_off_sentence(body) if body else None,
            "verdict": _field(clean, "verdict", "Positive|Neutral|Cautious"), "tokens": tokens}


def summarize_news(company_name: str, headlines: list[dict], settings: dict) -> dict:
    """Headlines only (~15 tokens each). Fetching full articles would cost
    50-100x the tokens for a marginally better two-sentence digest."""
    if not headlines:
        return {"summary": None, "sentiment": None, "tokens": 0}
    lines = "\n".join(f"- {h['title']} ({h['source']})" for h in headlines)
    prompt = (
        f"Recent headlines about {company_name}:\n{lines}\n"
        "\nUsing ONLY these headlines, write exactly two lines in your own words. No markdown, "
        "no angle brackets, and do not repeat these instructions.\n"
        "The first line starts with 'SENTIMENT: ' then one word: Positive, Mixed or Negative.\n"
        "The second line starts with 'SUMMARY: ' then two sentences on what is happening."
    )
    raw, tokens = complete(prompt, settings["ai"]["tokens_news"], settings, "News digest")
    if not raw:
        return {"summary": None, "sentiment": None, "tokens": tokens}
    clean = raw.replace("*", "")
    summary = re.search(r"SUMMARY:\s*(.*)", clean, re.S | re.I)
    body = _clean_answer(summary.group(1) if summary else clean)
    return {"summary": _drop_cut_off_sentence(body) if body else None,
            "sentiment": _field(clean, "sentiment", "Positive|Mixed|Negative"), "tokens": tokens}


def explain_error(ticker: str, error: str, settings: dict) -> tuple[str | None, int]:
    """A raw error turned into one plain-English sentence a non-technical
    user can act on, so the app never just shows a stack trace."""
    if not error:
        return None, 0
    return complete(
        f"In ONE short plain-English sentence, tell a non-technical investor what likely went wrong "
        f"fetching stock data for '{ticker}' and a quick next step. Technical detail: {error[:300]}",
        settings["ai"]["tokens_error"], settings, "Error explanation")


GUIDANCE_LINE = re.compile(
    r"^\s*(?:[-*]\s*)?(?P<metric>[^|]{2,40})\|(?P<claim>[^|]{5,200})\|(?P<horizon>[^|]{1,40})\s*$", re.M)

# Sentences where management commits to something. Filtering in code first
# means the model only ever sees a page or two of a 60-page transcript.
FORWARD_RE = re.compile(
    r"\b(we (expect|aim|target|intend|plan|hope|believe|anticipate|guide|should|will)"
    r"|expects? to|targeting|guidance (of|for|is)|outlook|going forward|by fy\s?'?\d{2}"
    r"|next (quarter|year|few quarters)|over the next|in the coming)\b", re.I)


def extract_guidance(company_name: str, quarter: str, transcript_text: str, settings: dict) -> tuple[list[dict], int]:
    """Forward-looking commitments from a concall, as metric | claim | horizon.

    Extraction, not judgement -- which is what small local models are good
    at. Candidate sentences are found by regex first, so the prompt stays
    around 600 tokens no matter how long the transcript is.
    """
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", transcript_text or "")
                 if 40 < len(s.strip()) < 320 and FORWARD_RE.search(s)]
    if not sentences:
        return [], 0
    # De-duplicate (Q&A sessions repeat themselves) and cap the prompt.
    seen, picked = set(), []
    for s in sentences:
        key = s.lower()[:60]
        if key not in seen:
            seen.add(key)
            picked.append(s)
        if len(picked) >= 18:
            break
    prompt = (
        f"Statements from {company_name}'s {quarter} earnings call:\n"
        + "\n".join(f"- {s}" for s in picked)
        + "\n\nList only the statements where management commits to a future outcome. "
          "One per line, no markdown, exactly this format:\n"
          "metric | what management committed to | by when\n"
          "Use the words from the statement. Skip anything vague. Maximum 6 lines."
    )
    raw, tokens = complete(prompt, settings["ai"]["tokens_guidance"], settings, "Transcript reading")
    if not raw:
        return [], tokens
    claims = [{"metric": m.group("metric").strip(" -*")[:60],
               "claim": m.group("claim").strip()[:240],
               "horizon": m.group("horizon").strip()[:60]}
              for m in GUIDANCE_LINE.finditer(raw.replace("*", ""))]
    return claims[:6], tokens


def judge_guidance(company_name: str, claim: dict, actuals: str, settings: dict) -> tuple[dict, int]:
    """Did what management promised actually happen? Graded against the
    numbers the app scraped, never against the model's own knowledge."""
    prompt = (
        f"{company_name} management said: \"{claim['claim']}\" (metric: {claim['metric']}, by {claim['horizon']}).\n"
        f"What the reported results since then show:\n{actuals}\n\n"
        "\nUsing ONLY those reported numbers, write exactly two lines in your own words. "
        "No markdown, no angle brackets, and do not repeat these instructions.\n"
        "The first line starts with 'STATUS: ' then one word: Delivered, Missed or Unclear.\n"
        "The second line starts with 'WHY: ' then one sentence quoting a number."
    )
    raw, tokens = complete(prompt, 90, settings, "Guidance check")
    if not raw:
        return {"status": None, "why": None}, tokens
    clean = raw.replace("*", "")
    why = re.search(r"WHY:\s*(.*)", clean, re.S | re.I)
    body = _clean_answer(why.group(1) if why else clean)
    return {"status": _field(clean, "status", "Delivered|Missed|Unclear"),
            "why": _drop_cut_off_sentence(body) if body else None}, tokens


def expand_query(question: str, settings: dict) -> tuple[list[str], int]:
    """The words a filing would actually use for this question.

    Keyword search fails on vocabulary, not on logic: ask about "capex" and
    the transcript says "capital expenditure", "greenfield" or "expansion
    plan". The model supplies those synonyms, the search engine still does
    the retrieving, and nothing is invented because the terms are only ever
    used to look things up.
    """
    prompt = (
        f"An analyst is searching Indian company filings (earnings calls, investor presentations, "
        f"annual reports) for: \"{question}\"\n"
        "List the words and short phrases that would actually appear in those documents, including "
        "the formal term, common abbreviations and close synonyms. Indian financial vocabulary. "
        "No markdown, no explanation. One comma-separated line, at most 8 items."
    )
    raw, tokens = complete(prompt, 90, settings, "Archive search")
    if not raw:
        return [], tokens
    line = raw.replace("\n", ",").split(":")[-1]
    terms = [t.strip(" .-\"'") for t in line.split(",")]
    return [t for t in terms if 2 < len(t) < 40][:8], tokens


RANK_RE = re.compile(r"\d+")


def rerank_passages(question: str, snippets: list[dict], settings: dict,
                    keep: int = 8) -> tuple[list[int], int]:
    """Which retrieved passages actually answer the question.

    Search returns what matched the words; this drops the coincidences --
    the page that says "capital" about share capital when the question was
    about capital expenditure. Returns indexes into `snippets`, best first.
    """
    if len(snippets) <= keep:
        return list(range(len(snippets))), 0
    listing = "\n".join(
        f"[{i + 1}] {s['ticker']} {s['category']} p{s['page']}: "
        f"{' '.join((s.get('snippet') or s['text'])[:220].split())}"
        for i, s in enumerate(snippets[:30]))
    prompt = (
        f"Question: {question}\n\nNumbered passages from company filings:\n{listing}\n\n"
        f"Which passages genuinely help answer the question? Reply with their numbers only, "
        f"most useful first, comma-separated, at most {keep}. No other words. "
        "If none are relevant, reply NONE."
    )
    raw, tokens = complete(prompt, 60, settings, "Archive ranking")
    if not raw or "none" in raw.lower()[:8]:
        return list(range(min(keep, len(snippets)))), tokens
    order, seen = [], set()
    for match in RANK_RE.finditer(raw):
        index = int(match.group()) - 1
        if 0 <= index < len(snippets) and index not in seen:
            seen.add(index)
            order.append(index)
    return (order[:keep] or list(range(min(keep, len(snippets))))), tokens


def synthesize_search(question: str, snippets: list[dict], settings: dict) -> tuple[str | None, int]:
    """Answer a question across filings using only the retrieved passages,
    each tagged so the answer can cite which company and document it came from."""
    if not snippets:
        return None, 0
    body = "\n\n".join(
        f"[{i + 1}] {s['ticker']} · {s['category']} · page {s['page']}:\n{' '.join(s['text'][:700].split())}"
        for i, s in enumerate(snippets[:8]))
    prompt = (
        f"Passages from company filings:\n{body}\n\n"
        f"Question: {question}\n\n"
        "Answer as an equity analyst would, in 3 to 5 sentences, using ONLY these passages. "
        "No markdown. Quote the figures and cite the passage as [1], [2] after each claim. "
        "Where companies differ, say how. If the passages do not answer the question, say exactly "
        "what is missing rather than guessing."
    )
    raw, tokens = complete(prompt, settings["ai"]["tokens_synthesis"], settings, "Archive answer")
    body = _clean_answer(raw)
    return (_drop_cut_off_sentence(body) if body else None), tokens


def token_note(tokens: int, generated_at: float, run_started: float) -> str:
    return (f"{tokens:,} tokens" if generated_at >= run_started
            else f"reused from cache ({tokens:,} tokens when first generated)")


def now() -> float:
    return time.time()
