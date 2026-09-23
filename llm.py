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

import json
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
# Why the last call failed. Without this a quota error, a bad key and a model
# that simply said nothing all looked identical: "returned no summary".
LAST_ERROR: str | None = None


# Providers rate-limit and go busy; both are temporary and both used to show
# up as "the model returned no summary".
TRANSIENT_STATUS = {429, 500, 502, 503, 504}


def _send(call):
    """Make a request, and give a busy provider one second chance."""
    resp = call()
    if getattr(resp, "status_code", 0) in TRANSIENT_STATUS:
        time.sleep(2)
        resp = call()
    return resp


def _fail(resp) -> tuple[None, int]:
    """Remember why a provider refused, in words the user can act on."""
    global LAST_ERROR
    try:
        detail = resp.json().get("error", {})
        message = detail.get("message") or detail.get("type") or resp.text[:200]
    except (ValueError, AttributeError):
        message = getattr(resp, "text", "")[:200]
    hint = {401: "the API key looks wrong or expired",
            403: "the key is not allowed to use this model",
            404: "that model name was not found for this provider",
            429: "the provider is rate-limiting you or the quota is spent"}.get(
                getattr(resp, "status_code", 0), "")
    LAST_ERROR = f"HTTP {getattr(resp, 'status_code', '?')}" + (f" — {hint}" if hint else "") + \
                 (f": {message}" if message else "")
    return None, 0


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


# --- Fallbacks: when the chosen model is overloaded, another answers ---------------
# Same key first, fastest first; then a local Ollama model. Shared by every AI
# feature, so one provider outage never blanks the verdicts, news or chat.
FALLBACKS = {"google": ["gemini-flash-lite-latest", "gemini-flash-latest", "gemma-4-26b-a4b-it"],
             "openai": ["gpt-4o-mini"], "anthropic": ["claude-haiku-4-5-20251001"]}
TRANSIENT_ERRORS = ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 529", "Timeout", "ConnectionError",
                    "timed out", "empty reply", "reply budget", "Read timed out")
BENCH_SECONDS = 300
_benched: dict[str, float] = {}  # model -> skipped until, after failing
LAST_MODEL: str | None = None    # which model actually wrote the last answer


def candidates(settings: dict) -> list[dict]:
    """The chosen model, then working alternatives, each as full settings. A
    model that just failed sits out five minutes so the next call skips it."""
    ai = settings["ai"]
    out = [settings]
    try:
        have = list_models(ai["provider"], cfg.base_url(settings, ai["provider"]), cfg.api_key(settings, ai["provider"]))
        out += [{**settings, "ai": {**ai, "model": m}} for m in FALLBACKS.get(ai["provider"], [])
                if m in have and m != ai["model"]]
        if ai["provider"] != "ollama":
            local = sorted(list_models("ollama", cfg.base_url(settings, "ollama"), ""),
                           key=lambda m: ("0.5b" in m or "1b" in m, m))  # skip tiny models if there is a choice
            if local:
                out.append({**settings, "ai": {**ai, "provider": "ollama", "model": local[0]}})
    except Exception:
        pass  # listing models is a nicety; the chosen model still gets its turn
    ready = [c for c in out if _benched.get(c["ai"]["model"], 0) < time.time()]
    return ready or out


def bench(model: str) -> None:
    _benched[model] = time.time() + BENCH_SECONDS


def is_transient(error: str | None) -> bool:
    return error is None or any(t in error for t in TRANSIENT_ERRORS)


def friendly(error: str | None) -> str:
    e = error or "no reply"
    if any(code in e for code in ("HTTP 503", "HTTP 529", "high demand", "overloaded")):
        return "the provider is overloaded right now"
    if "HTTP 429" in e:
        return "the provider's rate limit was hit — wait a minute"
    if "Timeout" in e or "timed out" in e:
        return "it took too long to start answering"
    if "HTTP 401" in e or "HTTP 403" in e:
        return "the API key was refused — check Settings → Models & keys"
    return e.split("{")[0].strip(" :") or e[:160]


def complete(prompt: str, max_tokens: int, settings: dict, kind: str = "other",
             system: str | None = None, schema: dict | None = None) -> tuple[str | None, int]:
    """(text, tokens) from the chosen model, or -- if it is busy, down or
    returns nothing -- from the next working one. Never raises. LAST_MODEL
    says which model answered; LAST_ERROR why nothing did."""
    global LAST_MODEL, LAST_ERROR
    LAST_MODEL, spent, tried = None, 0, []
    if not settings["ai"].get("enabled") or not (settings["ai"].get("model") or "").strip():
        return None, 0
    chain = candidates(settings)
    for attempt in chain:
        if attempt is not chain[-1]:  # others are waiting in line: do not let one stall for minutes
            attempt = {**attempt, "ai": {**attempt["ai"], "timeout": min(int(attempt["ai"].get("timeout", 60)), 45)}}
        text, tokens = _complete_once(prompt, max_tokens, attempt, kind, system, schema)
        spent += tokens
        if text:
            LAST_MODEL = attempt["ai"]["model"]
            return text, spent
        if LAST_BLOCK and _blocked_reason(settings):
            return None, spent  # the budget said no: another model would spend the same money
        tried.append(f"{attempt['ai']['model']}: {friendly(LAST_ERROR)}")
        if not is_transient(LAST_ERROR):
            break  # a bad key or request: another model will not fix it
        bench(attempt["ai"]["model"])
    LAST_ERROR = "; ".join(tried) or LAST_ERROR
    return None, spent


def _complete_once(prompt: str, max_tokens: int, settings: dict, kind: str = "other",
                   system: str | None = None, schema: dict | None = None) -> tuple[str | None, int]:
    """(text, tokens used) from exactly the configured model. Never raises.

    Every call passes through the budget first and is written to the usage
    ledger afterwards, so the spend figures in Settings are what actually
    happened rather than an estimate.
    """
    global LAST_BLOCK, LAST_ERROR
    LAST_ERROR = None
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
            resp = _send(lambda: requests.post(
                f"{base}/api/generate",
                # think=False: thinking models (e.g. gemma4) otherwise spend the
                # whole budget on hidden reasoning and return an empty response.
                json={"model": model, "prompt": prompt, "stream": False, "think": False,
                      **({"system": system} if system else {}),
                      **({"format": "json"} if schema else {}),
                      "options": {"temperature": temperature, "num_predict": max_tokens,
                                  "num_ctx": int(ai.get("num_ctx", 2048))}},
                timeout=timeout))
            if resp.status_code != 200:
                return _fail(resp)
            data = resp.json()
            return book(data.get("response", "").strip() or None,
                        data.get("prompt_eval_count", 0), data.get("eval_count", 0))

        if provider == "anthropic":
            resp = _send(lambda: requests.post(
                f"{base}/v1/messages", headers=_auth_headers(provider, key),
                json={"model": model, "max_tokens": max_tokens, "temperature": temperature,
                      **({"system": system} if system else {}),
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=timeout))
            if resp.status_code != 200:
                return _fail(resp)
            data = resp.json()
            text = "".join(b.get("text", "") for b in data.get("content", []))
            usage = data.get("usage", {})
            return book(text.strip() or None, usage.get("input_tokens", 0), usage.get("output_tokens", 0))

        if provider == "google":
            def _gemini(body):
                return _send(lambda: requests.post(f"{base}/models/{model}:generateContent",
                                                   params={"key": key}, headers=_JSON, json=body,
                                                   timeout=timeout))

            # Thinking models (Gemma 4, Gemini 2.5+) spend output tokens reasoning
            # before they answer; a tight cap left them nothing to answer with.
            room = max(max_tokens * 4, 2048)
            gen = {"maxOutputTokens": room, "temperature": temperature}
            if schema:
                # Forced structured output: the model cannot narrate its way
                # out of a schema. Not every model on this API supports it,
                # hence the retry below.
                gen = {**gen, "responseMimeType": "application/json", "responseSchema": schema}
            body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": gen}
            if system:
                body["systemInstruction"] = {"parts": [{"text": system}]}
            resp = _gemini(body)
            if resp.status_code != 200 and schema:  # model without schema support
                gen = {"maxOutputTokens": room, "temperature": temperature}
                body["generationConfig"] = gen
                resp = _gemini(body)
            if resp.status_code != 200 and system:
                # Gemma models are served by the Gemini API but do not accept a
                # system instruction ("Developer instruction is not enabled"),
                # so fold it into the prompt and try once more.
                resp = _gemini({"contents": [{"role": "user",
                                              "parts": [{"text": f"{system}\n\n{prompt}"}]}],
                                "generationConfig": gen})
            if resp.status_code != 200:
                return _fail(resp)
            data = resp.json()
            parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            usage = data.get("usageMetadata", {})
            prompt_tok = usage.get("promptTokenCount", 0)
            out_tok = usage.get("candidatesTokenCount", 0)
            total = usage.get("totalTokenCount", 0)
            if total > prompt_tok + out_tok:  # Gemma reports only the total
                out_tok = total - prompt_tok
            # Thinking models (Gemma 4, Gemini 2.5) return their reasoning as parts
            # flagged "thought"; only the rest is the answer.
            return book("".join(p.get("text", "") for p in parts if not p.get("thought")).strip() or None,
                        prompt_tok, out_tok)

        # OpenAI, OpenRouter and anything else speaking the OpenAI chat API.
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]
        body = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
                "messages": messages}
        if schema:
            body["response_format"] = {"type": "json_object"}
        resp = _send(lambda: requests.post(f"{base}/chat/completions",
                                           headers=_auth_headers(provider, key), json=body,
                                           timeout=timeout))
        if resp.status_code == 400 and schema and "response_format" in resp.text:
            body.pop("response_format")          # provider without a JSON mode
            resp = requests.post(f"{base}/chat/completions", headers=_auth_headers(provider, key),
                                 json=body, timeout=timeout)
        if resp.status_code == 400 and "max_completion_tokens" in resp.text:
            # Newer OpenAI reasoning models renamed the field.
            body["max_completion_tokens"] = body.pop("max_tokens")
            resp = requests.post(f"{base}/chat/completions", headers=_auth_headers(provider, key),
                                 json=body, timeout=timeout)
        if resp.status_code != 200:
            return _fail(resp)
        data = resp.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        usage = data.get("usage", {})
        return book(text.strip() or None, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
        LAST_ERROR = f"{type(exc).__name__}: {exc}"[:200]
        return None, 0


# An answer that still contains <angle brackets> is the template being read
# back rather than filled in -- some models do this, and without this check a
# placeholder like "<3 sentences on financial position>" was shown to the user
# as if it were the analysis.
PLACEHOLDER_RE = re.compile(r"<[^<>\n]{3,}>")
FENCE_RE = re.compile(r"```.*?```", re.S)


JSON_RE = re.compile(r"\{.*?\}", re.S)
# Some models narrate their way to an answer. Asking them to fence the answer
# off is the one thing that reliably separates it from the narration.
MARKED_RE = re.compile(r"\[ANSWER\](.*?)\[END\]", re.S)
ANSWER_RULE = ("Write your final answer between [ANSWER] and [END], and write nothing after [END]. "
               "Think first if you need to, but keep any thinking before [ANSWER].")


def _marked(text: str | None) -> str | None:
    """The last fenced answer block, which is the real one if a model
    repeated the instructions before getting to it."""
    if not text:
        return None
    blocks = [b.strip() for b in MARKED_RE.findall(text) if b.strip()]
    return blocks[-1] if blocks else None

# Lines a model writes while reasoning out loud before it answers -- bullets
# restating the task ("* Constraint 1: ...", "Task: ...", "Input: ...").
SCRATCHPAD_RE = re.compile(
    r"^\s*(?:[*\-\u2022]+\s*)?(?:role|task|input|output|constraint\s*\d*|step\s*\d*|goal|"
    r"analysis|reasoning|plan|note|topic|question|answer\s+format)\s*[:\-]", re.I)

# A line that is just a label and a number is the input being read back,
# not a sentence: "Market Cap: Rs 16,59,630Cr." Prose after the colon stays.
DATA_ECHO_RE = re.compile(r"^[A-Za-z][\w &/()'.-]{0,34}:\s*[\u20b9$]?[\d,.\s/%()-]*$")


_PAIR_RE = re.compile(r'"(\w+)"\s*:\s*"(.*?)(?:"\s*[,}]|$)', re.S)


def _json_salvage(text: str | None) -> dict | None:
    """Fields out of JSON that was cut off before its closing brace.

    The token cap lands mid-string often enough to matter: the model had
    answered correctly, json.loads failed on the truncation, and the whole
    raw blob was shown to the user as the summary with no verdict at all.
    Reading the pairs directly rescues everything written before the cut.
    """
    if not text or "{" not in text:
        return None
    found = {k.lower(): v.strip() for k, v in _PAIR_RE.findall(text[text.index("{"):])}
    return found or None


def _json_reply(text: str | None, *want: str) -> dict | None:
    """The JSON object in a reply, whatever wrapping came with it.

    Asking for JSON is the one instruction every provider's chat model
    follows reliably; a two-line text template is not -- one hosted model
    read the template back verbatim instead of filling it in.

    Parsed with raw_decode rather than a `{.*?}` regex, which cannot see
    nesting: asked for a verdict, one model also returned a "key_ratios"
    object, and the regex handed back the innermost `{"Sales": 3510}` --
    a perfectly valid dict with none of the fields that were asked for.
    `want` names those fields, so a stray inner object is skipped.
    """
    if not text:
        return None
    body = text.replace("```json", " ").replace("```", " ")
    decoder = json.JSONDecoder()
    for index, char in enumerate(body):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(body[index:])
        except ValueError:
            continue  # truncated or not JSON: try the next opening brace
        if not isinstance(parsed, dict) or not any(parsed.values()):
            continue
        # Models are inconsistent about key case ("SUMMARY" vs "summary").
        flat = {str(k).lower(): v for k, v in parsed.items()}
        if want and not any(w in flat for w in want):
            continue
        return flat
    return None


def _clean_answer(text: str | None) -> str | None:
    """Turn a model's reply into the prose part of it, or None if there isn't any.

    Models differ wildly in what they wrap an answer in. Some think out loud
    first ("* Topic: ...", "* Constraint: exactly three sentences"), some
    write the answer itself as an indented bullet list, and some restate the
    input data back. Keeping only the sentences, and refusing to show
    anything else, is what makes the same app work across providers.
    """
    if not text:
        return None
    kept = []
    for line in FENCE_RE.sub(" ", text).splitlines():
        line = re.sub(r"^\s*[*\-\u2022]+\s*", "", line).strip()
        if not line or SCRATCHPAD_RE.match(line) or DATA_ECHO_RE.match(line):
            continue
        kept.append(line)
    answer = " ".join(kept).replace("*", "").strip()
    if not answer or PLACEHOLDER_RE.search(answer) or len(answer) < 25:
        return None
    return answer


def is_usable(text: str | None) -> bool:
    """Is this fit to show a user as an answer?

    Applied when rendering, not just when parsing: an answer stored before
    this check existed must not reappear just because it is on disk.
    """
    return _clean_answer(text) is not None


def _drop_cut_off_sentence(text: str) -> str:
    """If the token cap cut the reply mid-sentence, keep complete sentences only."""
    text = (text or "").strip()
    if text.endswith((".", "!", "?")):
        return text
    end = max(text.rfind(". "), text.rfind("! "), text.rfind("? "))
    return text[: end + 1] if end > 0 else text


def _field(text: str, name: str, options: str) -> str | None:
    """The one allowed word that follows a label, e.g. verdict -> Positive.

    The separator has to tolerate JSON punctuation: on `"verdict": "Positive"`
    the old `[:\\s]+` failed at the closing quote, so a correct answer inside
    valid JSON was read as no answer at all.
    """
    match = re.search(rf"{name}\W{{0,6}}\b({options})\b", text, re.I)
    return match.group(1).capitalize() if match else None


def _strip_markers(text: str | None) -> str | None:
    """Drop [ANSWER] / [END] fences wherever they landed.

    A model that half-follows the fencing rule leaves one marker behind, and
    a summary that opens with "[END]" was shipped to users before this."""
    return re.sub(r"\[\s*/?\s*(?:ANSWER|END)\s*\]", " ", text).strip() if text else text


def _two_fields(raw: str | None, label: str, options: str, prose: str) -> tuple[str | None, str | None]:
    """(one-word label, prose) out of a reply, whatever wrapper it arrived in.

    Every judgement call in the app has this shape -- verdict + summary,
    sentiment + summary, status + why -- so they all parse through here."""
    if not raw:
        return None, None
    reply = _json_reply(raw, label, prose) or _json_salvage(raw)
    value = body = None
    if reply:
        value = _field(f"{label}: {reply.get(label, '')}", label, options)
        body = _clean_answer(str(reply.get(prose) or ""))
    if value and body:
        return value, body
    clean = _strip_markers(raw).replace("*", "")
    value = value or _field(clean, label, options)
    if not body:
        match = re.search(rf"{prose}\s*[:\-]\s*(.*)", clean, re.S | re.I)
        body = _clean_answer(match.group(1) if match else clean)
    return value, body


# --------------------------------------------------------------------------
# The app's prompts
#
# One contract everywhere: a single JSON object, asked for once. Earlier
# versions asked for JSON *and* a "VERDICT:" line *and* an [ANSWER] fence in
# the same call; small models answered a different one each time, which is
# how "[END]" ended up inside a summary and how a verdict came back empty.
#
# Three rules do most of the work, and they are short on purpose -- every
# token spent on instructions is a token not spent on the company:
#   1. Only the figures given, copied exactly. (Stops invented numbers.)
#   2. Decision rules with real thresholds, not adjectives. (A 0.5B model
#      has no idea whether 7.6% ROE is good; told the cut-off, it does.)
#   3. Never a bare phrase that could be mistaken for an answer -- "three to
#      five sentences" came back once as the answer itself.
# --------------------------------------------------------------------------

AI_QUARTERLY_ROWS = ("Sales", "Operating Profit", "OPM", "Net Profit", "EPS")

# The shared preamble. Kept to two sentences: it is prepended to every
# analytic call, so its cost is paid on every company, every run.
ANALYST = ("You are a careful equity analyst covering Indian listed companies. "
           "Use only the figures given to you: never add outside knowledge, and copy every "
           "number exactly as it appears in the input. Reply with one JSON object and nothing else.")


# The citation example, kept here so the guard below can recognise it coming
# back. Deliberately free of figures: see synthesize_search.
EXAMPLE_ANSWER = "The new plant will add capacity [2], funded from internal accruals [1]."


def _echoes(answer: str | None, example: str) -> bool:
    """Is this the example handed back instead of an answer?

    A model too small for the task copies the illustration. Showing that to
    someone as an answer about their company is worse than showing nothing,
    so it is caught here rather than trusted to the prompt.
    """
    if not answer:
        return False
    import difflib
    trim = lambda t: re.sub(r"[\W\d_]+", " ", t.lower()).strip()
    return difflib.SequenceMatcher(None, trim(answer), trim(example)).ratio() > 0.6


def _json_only(shape: str, *rules: str) -> str:
    """A system prompt: who you are, the exact shape wanted, then the rules."""
    return "\n".join([ANALYST, f"Reply in this form: {shape}", *rules])


def _schema(*fields: str) -> dict:
    """A flat object of string fields, in the shape Google's API expects and
    that every other provider's JSON mode is happy to ignore."""
    return {"type": "OBJECT", "properties": {f: {"type": "STRING"} for f in fields},
            "required": list(fields)}


def _metric_num(metrics: dict, *names: str) -> float | None:
    """The first of these ratios that is present, as a number ("7.60%" -> 7.6)."""
    for name in names:
        for key, value in (metrics or {}).items():
            if key.strip().lower() == name.lower():
                match = re.search(r"-?\d[\d,]*\.?\d*", str(value))
                if match:
                    return float(match.group().replace(",", ""))
    return None


def _profit_direction(quarterly_df) -> int | None:
    """+1 if net profit rose between the last two quarters, -1 if it fell."""
    if quarterly_df is None or getattr(quarterly_df, "empty", True) or len(quarterly_df.columns) < 3:
        return None
    rows = quarterly_df[quarterly_df.iloc[:, 0].astype(str).str.strip().str.lower()
                        .str.startswith("net profit")]
    if rows.empty:
        return None
    try:
        before = float(str(rows.iloc[0, -2]).replace(",", ""))
        after = float(str(rows.iloc[0, -1]).replace(",", ""))
    except (ValueError, TypeError):
        return None
    return 0 if before == after else (1 if after > before else -1)


def verdict_from_metrics(metrics: dict, quarterly_df) -> str | None:
    """The verdict, worked out in code rather than asked of the model.

    These are the same thresholds the prompt used to describe, and moving
    them here is what made the verdict trustworthy: a small local model
    cannot reliably decide that 7.6% is below 12%, and it called a company
    with a 7.6% ROE "Positive" every time. Arithmetic the app can do itself
    should never be delegated to a language model.

    Returns None when the ratios needed are missing, and the model is asked
    for a verdict instead.

    ponytail: three plain thresholds, deliberately. They are documented in
    the guide so the badge can be argued with; tune them there, not here.
    """
    roe = _metric_num(metrics, "ROE", "Return on equity")
    debt = _metric_num(metrics, "Debt to equity", "Debt to Equity Ratio")
    direction = _profit_direction(quarterly_df)
    if roe is None and debt is None and direction is None:
        return None
    if (roe is not None and roe < 12) or (debt is not None and debt > 1.5) or direction == -1:
        return "Cautious"
    if roe is not None and roe > 18 and direction == 1:
        return "Positive"
    return "Neutral"


def analyze(ticker, company_name, metrics, quarterly_df, pros, cons, settings) -> dict:
    """Verdict + short analysis, built only from data already scraped.

    Input is the structured screener.in data -- ratios plus the last two
    quarters -- not the regex-extracted PDF figures. Those are right for the
    searchable figures table but too noisy for a prompt: a first-occurrence
    "Dividend 5000" was a TDS threshold and "Net Profit 216" a page number.
    """
    if not metrics and quarterly_df is None and not pros and not cons:
        return {"summary": None, "verdict": None, "tokens": 0}

    # One metric per line, each named. Run together on one line, a small model
    # reads "ROE 7.60%" straight after a strength about dividends and reports
    # a "dividend payout of 7.60%" -- the right number under the wrong label.
    ratios = "\n".join(f"- {k}: {v}" for k, v in metrics.items() if k != "Face Value") or "- none given"
    quarters = ""
    if quarterly_df is not None and not quarterly_df.empty and len(quarterly_df.columns) >= 3:
        prev_q, last_q = quarterly_df.columns[-2], quarterly_df.columns[-1]
        lines = [f"- {row['Metric']}: {row[prev_q]} -> {row[last_q]}"
                 for _, row in quarterly_df.iterrows()
                 if str(row["Metric"]).startswith(AI_QUARTERLY_ROWS)]
        quarters = f"\nQuarterly results in Rs Crore, {prev_q} then {last_q}:\n" + "\n".join(lines) if lines else ""

    facts = (f"Company: {company_name} ({ticker})\n\nKey ratios:\n{ratios}\n{quarters}\n"
             f"\nStrengths flagged by the data provider:\n"
             + ("\n".join(f"- {p[:120]}" for p in pros[:4]) or "- none") +
             f"\n\nRisks flagged by the data provider:\n"
             + ("\n".join(f"- {c[:120]}" for c in cons[:4]) or "- none"))
    # The verdict is decided here, in code, from the ratios. The model is
    # told what it is and writes only the prose -- which also stops it
    # praising a company the rules called Cautious.
    ruled = verdict_from_metrics(metrics, quarterly_df)
    if ruled:
        system = _json_only(
            '{"summary": "..."}',
            f'This company has already been assessed as "{ruled}". Explain that assessment in at '
            'most 70 words of prose, using the figures below.',
            'Quote the figures that support it and name the metric each one belongs to. Do not '
            'contradict the assessment and do not restate these instructions.',
            'No headings and no bullet points.')
        fields = ("summary",)
    else:
        system = _json_only(
            '{"verdict": "Positive", "summary": "..."}',
            'Set "verdict" by the first rule that matches:',
            '- "Cautious" if ROE is below 12%, or Debt to equity is above 1.5, or net profit fell '
            'between the two quarters shown.',
            '- "Positive" if ROE is above 18% and net profit rose between the two quarters shown.',
            '- "Neutral" in every other case.',
            '"summary" is prose of at most 70 words on the financial position and outlook. Quote '
            'the figures that decided the verdict and name the metric each one belongs to. No '
            'headings, no bullet points, no repetition of these instructions.')
        fields = ("verdict", "summary")
    # A verbose model spends its first hundred tokens restating the task, so
    # the floor here is what stops a tight user setting from starving it.
    budget = max(int(settings["ai"]["tokens_analysis"]), 320)
    raw, tokens = complete(facts, budget, settings, "Company analysis",
                           system=system, schema=_schema(*fields))

    verdict, summary = _two_fields(raw, "verdict", "Positive|Neutral|Cautious", "summary")
    verdict = ruled or verdict

    if not summary:
        # One plain retry with room to spare: a model that narrates needs the
        # budget for its narration before it reaches the answer. No mention of
        # a sentence count -- asked for "three to five sentences", a small
        # model has answered with the phrase "three to five" itself.
        retry, retry_tokens = complete(
            facts, max(budget * 3, 600), settings, "Company analysis (retry)",
            system=(ANALYST.replace("Reply with one JSON object and nothing else.", "")
                    + " Write at most 70 words of plain prose on this company's financial position "
                      "and outlook, quoting the key figures above and naming the metric each one "
                      "belongs to. Do not repeat these instructions."))
        tokens += retry_tokens
        summary = _clean_answer(_strip_markers(_marked(retry) or retry))

    return {"summary": _drop_cut_off_sentence(summary) if summary else None,
            "verdict": verdict, "tokens": tokens}


def summarize_news(company_name: str, headlines: list[dict], settings: dict) -> dict:
    """Headlines only (~15 tokens each). Fetching full articles would cost
    50-100x the tokens for a marginally better two-sentence digest."""
    if not headlines:
        return {"summary": None, "sentiment": None, "tokens": 0}
    lines = "\n".join(f"- {h['title']} ({h['source']})" for h in headlines)
    # "Mixed" is the middle option, and a model with no rule picks the middle
    # every time -- a fraud probe and a big order win both came back Mixed.
    # Naming the events that force a side is what breaks the tie.
    system = _json_only(
        '{"sentiment": "Negative", "summary": "..."}',
        'Set "sentiment" by the first rule that matches:',
        '- "Negative" if any headline reports a downgrade, a guidance cut, an investigation or '
        'regulatory action, fraud, a resignation, a loss, or a fall in profit.',
        '- "Positive" if the headlines are mainly new orders, profit growth, upgrades, expansion '
        'or approvals.',
        '- "Mixed" only when clearly good and clearly bad news both appear.',
        '"summary" is at most 45 words on what is happening, drawn only from these headlines.')
    raw, tokens = complete(f"Recent headlines about {company_name}:\n{lines}",
                           max(int(settings["ai"]["tokens_news"]), 260), settings, "News digest",
                           system=system,
                           schema=_schema("sentiment", "summary"))

    sentiment, summary = _two_fields(raw, "sentiment", "Positive|Mixed|Negative", "summary")
    if not summary:
        retry, retry_tokens = complete(
            f"Recent headlines about {company_name}:\n{lines}\n\n"
            "Write at most 45 words on what is happening with this company, using only these "
            "headlines. Plain prose only: no headings, no labels, and do not repeat this instruction.",
            max(settings["ai"]["tokens_news"] * 3, 500), settings, "News digest (retry)")
        tokens += retry_tokens
        summary = _clean_answer(retry)
    return {"summary": _drop_cut_off_sentence(summary) if summary else None,
            "sentiment": sentiment, "tokens": tokens}


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
    # The one call that is not JSON: a flat "a | b | c" line per claim parses
    # just as strictly, costs fewer tokens than the braces and quoting, and
    # every model tested gets it right. The instructions live in the system
    # prompt like everywhere else, so the user turn is only the transcript.
    # The instructions stay in the user turn here, unlike every other call.
    # Moving them to the system prompt was measured and lost every claim on
    # a 0.5B model -- small models follow a format shown next to the data far
    # better than one described somewhere above it.
    prompt = (
        f"Statements from {company_name}'s {quarter} earnings call:\n"
        + "\n".join(f"- {s}" for s in picked)
        + "\n\nList only the statements where management commits to a specific future outcome. "
          "One per line, no markdown, exactly this format:\n"
          "metric | what management committed to | by when\n"
          "Use the words and numbers from the statement itself. Skip anything vague: being "
          "pleased with a quarter or remaining optimistic is not a commitment. Maximum 6 lines."
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
    # Grading a promise is a comparison, so the rules are a comparison. Left
    # to its own judgement a small model reads the confident wording of the
    # promise and marks it Delivered -- it graded "margin to 28%" as delivered
    # against a reported 22%.
    system = _json_only(
        '{"status": "Missed", "why": "..."}',
        'Compare the number management promised with the number actually reported, then set '
        '"status" by the first rule that matches:',
        '- "Unclear" if the reported results do not contain a number for that metric.',
        '- "Delivered" if the reported number reaches or beats the promised number.',
        '- "Missed" if the reported number falls short of the promised number, including when it '
        'moved the wrong way.',
        '"why" is one sentence of at most 30 words that states the promised number and the '
        'reported number side by side.')
    prompt = (
        f"{company_name} management promised: \"{claim['claim']}\"\n"
        f"Metric: {claim['metric']}. By when: {claim['horizon']}.\n\n"
        f"Reported results since then:\n{actuals}"
    )
    raw, tokens = complete(prompt, 160, settings, "Guidance check", system=system,
                           schema=_schema("status", "why"))
    status, why = _two_fields(raw, "status", "Delivered|Missed|Unclear", "why")
    return {"status": status, "why": _drop_cut_off_sentence(why) if why else None}, tokens


def expand_query(question: str, settings: dict) -> tuple[list[str], int]:
    """The words a filing would actually use for this question.

    Keyword search fails on vocabulary, not on logic: ask about "capex" and
    the transcript says "capital expenditure", "greenfield" or "expansion
    plan". The model supplies those synonyms, the search engine still does
    the retrieving, and nothing is invented because the terms are only ever
    used to look things up.
    """
    # The old prompt named the document types it was searching ("earnings
    # calls, investor presentations, annual reports"); a small model handed
    # exactly those three phrases back as the search terms. Nothing that is
    # not the topic goes in the prompt any more.
    prompt = f'Search topic: "{question}"'
    # The shape carries a worked example rather than describing one: shown
    # "nim" expanding to "net interest margin" in the shape itself, a model
    # generalises to capex -> capital expenditure; told the same thing as a
    # sentence, it repeats the question back instead.
    system = _json_only(
        '{"terms": "net interest margin, nim, interest spread, margin on advances"}',
        'List the words and phrases an Indian company filing would actually use for this topic: '
        'the full formal term, common abbreviations, and close synonyms.',
        'Always expand an abbreviation into its full form as well (for example "NIM" would give '
        '"net interest margin").',
        'At most 8 items, comma separated, lower case. List only search terms for the topic above: '
        'never document types, never the example terms, and never words taken from these '
        'instructions.')
    raw, tokens = complete(prompt, max(160, settings["ai"]["tokens_news"]), settings,
                           "Archive search", system=system, schema=_schema("terms"))
    if not raw:
        return [], tokens
    reply = _json_reply(raw) or _json_salvage(raw)
    line = str(reply.get("terms", "")) if reply else raw.replace("\n", ",").split(":")[-1]
    terms = [term.strip(" .-\"'*[]") for term in line.split(",")]
    # Deduplicate in code rather than asking for it: a small model given the
    # same topic eight times over happily returns "capex plan" eight times,
    # and each repeat costs a search pass for nothing.
    seen = dict.fromkeys(t.lower() for t in terms if 2 < len(t) < 40)
    return list(seen)[:8], tokens


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
        f"{' '.join((s.get('snippet') or s['text'])[:160].split())}"
        for i, s in enumerate(snippets[:15]))
    system = _json_only(
        '{"passages": "3, 1, 7"}',
        f'List the numbers of the passages that actually answer the question, best first, at most '
        f'{keep} of them.',
        'A passage counts only if it discusses the subject of the question. A passage that merely '
        'repeats a word from the question in another sense does not count -- "share capital" is '
        'not "capital expenditure", "human capital" is not an investment plan.',
        'Use {"passages": ""} if none of them are relevant. Numbers only, no explanation.')
    raw, tokens = complete(f"Question: {question}\n\nNumbered passages from company filings:\n{listing}",
                           200, settings, "Archive ranking", system=system, schema=_schema("passages"))
    if not raw:
        return list(range(min(keep, len(snippets)))), tokens
    reply = _json_reply(raw)
    picked = str(reply.get("passages", "")) if reply else raw
    order, seen = [], set()
    for match in RANK_RE.finditer(picked):
        index = int(match.group()) - 1
        if 0 <= index < len(snippets) and index not in seen:
            seen.add(index)
            order.append(index)
    return (order[:keep] or list(range(min(keep, len(snippets))))), tokens


def synthesize_search(question: str, snippets: list[dict], settings: dict,
                      history: list[dict] | None = None, coverage: dict | None = None,
                      matched_pages: int = 0) -> tuple[str | None, int]:
    """Answer a question using only the retrieved passages, each cited.

    Structured output, for the same reason as everywhere else: asked in
    prose, a model that narrates will spend the whole budget restating the
    question before it answers.
    """
    if not snippets:
        return None, 0
    body = "\n\n".join(
        f"[{i + 1}] {s['ticker']} · {s['category']} · page {s['page']}:\n"
        f"{s.get('focus') or ' '.join(s['text'][:450].split())}"
        for i, s in enumerate(snippets[:8]))
    # The passages are a sample; this tally is the whole archive. Without it
    # the model answers "which companies..." from the sample and is wrong.
    tally = ""
    if coverage:
        listed = ", ".join(f"{ticker} ({count})" for ticker, count in list(coverage.items())[:30])
        tally = (f"\n\nComplete tally from the whole archive (not just the passages above): "
                 f"{matched_pages} page(s) match, across {len(coverage)} compan(y/ies) — {listed}. "
                 f"If the question asks which, how many, or whether a company is involved, answer "
                 f"from this tally and name the companies; the passages are only for detail and "
                 f"quotes.")
    # Never a bare count in the instructions: asked for "three to five
    # sentences", a small model replied with the answer "three to five years".
    # A word limit cannot be mistaken for the content.
    # The example shows where the bracket goes and nothing else. An earlier
    # version illustrated it with realistic figures ("Rs 1,500 crore [2]")
    # and a 0.5B model copied them into its answer as though they were real
    # -- inventing a capex number for a company it had just been given the
    # true one for. An example in a finance prompt must contain no figure a
    # model could pass off as data.
    system = _json_only(
        '{"answer": "%s"}' % EXAMPLE_ANSWER,
        'Answer the question in at most 120 words, using only the passages given.',
        'Quote the figures from the passages, and put the number of the passage each figure came '
        'from in square brackets straight after it, as the example shows. Never copy the wording '
        'or any value out of the example itself. Where companies differ, say how.',
        'If the passages do not answer the question, say plainly which part is missing rather '
        'than filling the gap.')
    thread = ""
    if history:
        thread = "Earlier in this conversation:\n" + "\n".join(
            f"{turn['role']}: {' '.join(str(turn['content']).split())[:200]}"
            for turn in history[-4:]) + "\n\n"
    raw, tokens = complete(f"{thread}Passages from company filings:\n{body}{tally}\n\n"
                           f"Question: {question}",
                           settings["ai"]["tokens_synthesis"], settings, "Archive answer",
                           system=system, schema=_schema("answer"))
    reply = _json_reply(raw, "answer")
    answer = _clean_answer(str(reply.get("answer") or "")) if reply else None
    if not answer:
        answer = _clean_answer(_strip_markers(_marked(raw) or raw))
    if _echoes(answer, EXAMPLE_ANSWER):
        return None, tokens  # the example read back, not an answer
    return (_drop_cut_off_sentence(answer) if answer else None), tokens


def token_note(tokens: int, generated_at: float, run_started: float) -> str:
    return (f"{tokens:,} tokens" if generated_at >= run_started
            else f"reused from cache ({tokens:,} tokens when first generated)")


def now() -> float:
    return time.time()


def stream(messages: list[dict], system: str, max_tokens: int, settings: dict, kind: str = "Chat",
           patient: bool = True, first_by: float | None = None):
    """Yield the reply as it is written (see _stream); if nothing at all comes
    back, LAST_ERROR says why instead of the chat going silently blank."""
    global LAST_ERROR
    wrote = False
    for chunk in _stream(messages, system, max_tokens, settings, kind, patient, first_by):
        if chunk:
            wrote = True
            yield chunk
    if not wrote and not LAST_ERROR:
        LAST_ERROR = ("the model used its whole reply budget thinking and wrote no answer — raise "
                      "“Chat replies” in Settings → Generation limits" if _FINISH.get("reason") == "MAX_TOKENS"
                      else f"the model returned an empty reply ({_FINISH.get('reason') or 'no reason given'})")


_FINISH: dict = {}


def _stream(messages: list[dict], system: str, max_tokens: int, settings: dict, kind: str, patient: bool = True,
            first_by: float | None = None):
    """Yield the reply as it is written, for a chat that feels alive. Same
    budget check and usage ledger as complete(); never raises -- on failure it
    stops and leaves the reason in LAST_ERROR."""
    global LAST_BLOCK, LAST_ERROR
    LAST_ERROR = None
    ai = settings["ai"]
    provider, model = ai["provider"], (ai.get("model") or "").strip()
    if not ai.get("enabled") or not model:
        LAST_ERROR = "no model is set up"
        return
    blocked = _blocked_reason(settings)
    if blocked:
        LAST_BLOCK = LAST_ERROR = blocked
        return
    base, key = cfg.base_url(settings, provider), cfg.api_key(settings, provider)
    temperature, timeout = float(ai.get("temperature", 0.2)), max(int(ai.get("timeout", 60)), 90)
    usage = {"in": 0, "out": 0}
    _FINISH.clear()
    t0, wrote = time.time(), [False]

    def stalled() -> bool:
        """True once a model has thought silently past `first_by` seconds -- the
        caller would rather hand the question to the next model than keep waiting."""
        global LAST_ERROR
        if first_by and not wrote[0] and time.time() - t0 > first_by:
            LAST_ERROR = f"Timeout: no answer after {first_by:.0f}s of thinking"
            return True
        return False

    def post(*args, **kwargs):
        """A streamed POST that waits out a busy provider (429/5xx) twice before giving up --
        unless the caller has a fallback of its own and would rather move on at once."""
        for wait in (2, 6, 0) if patient else (0,):
            resp = requests.post(*args, stream=True, timeout=timeout, **kwargs)
            resp.encoding = "utf-8"  # SSE replies often omit a charset; the default mangles ₹
            if resp.status_code not in (429, 500, 502, 503, 529) or not wait:
                return resp
            resp.close()
            time.sleep(wait)

    def sse(resp):
        for line in resp.iter_lines(decode_unicode=True):
            if line and line.startswith("data:") and line[5:].strip() not in ("", "[DONE]"):
                yield json.loads(line[5:])

    try:
        if provider == "ollama":
            body = {"model": model, "stream": True, "think": False,
                    "messages": [{"role": "system", "content": system}, *messages],
                    "options": {"temperature": temperature, "num_predict": max_tokens,
                                "num_ctx": max(8192, int(ai.get("num_ctx", 2048)))}}
            with post(f"{base}/api/chat", json=body) as resp:
                if resp.status_code != 200:
                    LAST_ERROR = f"HTTP {resp.status_code}: {resp.text[:150]}"
                    return
                for line in resp.iter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("message", {}).get("content"):
                        wrote[0] = True
                        yield data["message"]["content"]
                    elif stalled():
                        return
                    if data.get("done"):
                        usage.update({"in": data.get("prompt_eval_count", 0), "out": data.get("eval_count", 0)})
        elif provider == "anthropic":
            body = {"model": model, "max_tokens": max_tokens, "temperature": temperature, "system": system,
                    "messages": messages, "stream": True}
            with post(f"{base}/v1/messages", headers=_auth_headers(provider, key), json=body) as resp:
                if resp.status_code != 200:
                    LAST_ERROR = f"HTTP {resp.status_code}: {resp.text[:150]}"
                    return
                for data in sse(resp):
                    if data.get("type") == "content_block_delta":
                        yield data["delta"].get("text", "")
                    elif data.get("type") == "message_start":
                        usage["in"] = data["message"].get("usage", {}).get("input_tokens", 0)
                    elif data.get("type") == "message_delta":
                        usage["out"] = data.get("usage", {}).get("output_tokens", 0)
        elif provider == "google":
            # Folded into the first turn: Gemma on this API rejects a system instruction.
            turns = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                     for m in messages]
            turns[0]["parts"][0]["text"] = f"{system}\n\n{turns[0]['parts'][0]['text']}"
            with post(f"{base}/models/{model}:streamGenerateContent", params={"key": key, "alt": "sse"},
                      headers=_JSON,
                               json={"contents": turns, "generationConfig": {"maxOutputTokens": max(max_tokens * 4, 4096),
                                                                             "temperature": temperature}}) as resp:
                if resp.status_code != 200:
                    LAST_ERROR = f"HTTP {resp.status_code}: {resp.text[:150]}"
                    return
                for data in sse(resp):
                    _FINISH["reason"] = (data.get("candidates") or [{}])[0].get("finishReason") or _FINISH.get("reason")
                    for part in (data.get("candidates") or [{}])[0].get("content", {}).get("parts", []):
                        if not part.get("thought") and part.get("text"):  # skip the model's hidden reasoning
                            wrote[0] = True
                            yield part["text"]
                    if stalled():
                        return
                    meta = data.get("usageMetadata") or {}
                    usage.update({"in": meta.get("promptTokenCount", usage["in"]),
                                  "out": meta.get("totalTokenCount", 0) - meta.get("promptTokenCount", 0)})
        else:  # OpenAI and anything speaking its chat API
            body = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "stream": True,
                    "stream_options": {"include_usage": True},
                    "messages": [{"role": "system", "content": system}, *messages]}
            with post(f"{base}/chat/completions", headers=_auth_headers(provider, key), json=body) as resp:
                if resp.status_code != 200:
                    LAST_ERROR = f"HTTP {resp.status_code}: {resp.text[:150]}"
                    return
                for data in sse(resp):
                    for choice in data.get("choices") or []:
                        yield (choice.get("delta") or {}).get("content") or ""
                    if data.get("usage"):
                        usage.update({"in": data["usage"].get("prompt_tokens", 0),
                                      "out": data["usage"].get("completion_tokens", 0)})
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
        LAST_ERROR = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        if usage["in"] or usage["out"]:
            price_in, price_out = cfg.prices(settings, provider)
            _RUN["tokens"] += usage["in"] + usage["out"]
            archive.record_usage(provider, model, kind, usage["in"], usage["out"],
                                 (usage["in"] * price_in + usage["out"] * price_out) / 1_000_000)
