"""Self-check for the model providers and the spending budget.

Every provider is exercised against a mock server that speaks the real API
shapes, so this proves the requests, the authentication and the token
accounting are right without needing an API key or spending a penny.

Run: python tests/test_providers.py
"""
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import archive  # noqa: E402

tmp = tempfile.TemporaryDirectory()
archive.DB_PATH = Path(tmp.name) / "usage.db"
archive._has_fts = None
archive.init()

import llm  # noqa: E402
import settings as cfg  # noqa: E402

SEEN = []  # every request the mock server received


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/tags"):                      # Ollama
            return self._send({"models": [{"name": "local-model", "size": 4_000_000_000,
                                           "details": {"parameter_size": "4B"}}]})
        if "/models" in self.path and "key=" in self.path:          # Google
            return self._send({"models": [{"name": "models/gemini-test",
                                           "supportedGenerationMethods": ["generateContent"]}]})
        if self.path.startswith("/v1/models"):                      # Anthropic
            return self._send({"data": [{"id": "claude-mock"}]})
        if "/models" in self.path:                                  # OpenAI-compatible
            return self._send({"data": [{"id": "mock-model"}]})
        return self._send({})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        SEEN.append({"path": self.path, "headers": dict(self.headers), "body": body})

        if self.path.startswith("/api/generate"):                  # Ollama
            return self._send({"response": "local answer", "prompt_eval_count": 30, "eval_count": 12})
        if self.path.startswith("/v1/messages"):                   # Anthropic
            return self._send({"content": [{"type": "text", "text": "claude answer"}],
                               "usage": {"input_tokens": 100, "output_tokens": 40}})
        if ":generateContent" in self.path:                        # Google
            return self._send({"candidates": [{"content": {"parts": [{"text": "gemini answer"}]}}],
                               "usageMetadata": {"promptTokenCount": 70, "candidatesTokenCount": 30}})
        if self.path.startswith("/chat/completions"):              # OpenAI / OpenRouter / custom
            return self._send({"choices": [{"message": {"content": "openai answer"}}],
                               "usage": {"prompt_tokens": 200, "completion_tokens": 50}})
        return self._send({})


server = HTTPServer(("127.0.0.1", 0), Mock)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_address[1]}"


def configure(provider, model, price_in=0.0, price_out=0.0, base=BASE):
    s = cfg.load()
    s["ai"].update(enabled=True, provider=provider, model=model, timeout=10)
    s["providers"][provider] = {"base_url": base, "api_key": "test-key-123",
                                "price_in": price_in, "price_out": price_out}
    s["budget"].update(enabled=True, monthly_tokens=0, monthly_cost=0, max_tokens_per_run=0)
    return s


# --- every provider answers, and its tokens are counted correctly ------------
cases = [
    ("ollama", "local-model", "local answer", 42, "/api/generate"),
    ("anthropic", "claude-mock", "claude answer", 140, "/v1/messages"),
    ("openai", "mock-model", "openai answer", 250, "/chat/completions"),
    ("openrouter", "mock-model", "openai answer", 250, "/chat/completions"),
    ("custom", "mock-model", "openai answer", 250, "/chat/completions"),
    ("google", "gemini-test", "gemini answer", 100, "/v1beta/models/gemini-test:generateContent"),
]
for provider, model, expected_text, expected_tokens, expected_path in cases:
    base = BASE + "/v1beta" if provider == "google" else BASE
    llm.start_run()
    text, tokens = llm.complete("hello", 50, configure(provider, model, base=base), kind="test")
    assert text == expected_text, (provider, text)
    assert tokens == expected_tokens, (provider, tokens, expected_tokens)
    sent = SEEN[-1]
    assert expected_path in sent["path"], (provider, sent["path"])
    if provider == "google":
        # Gemini names the model in the URL and takes the key as a query parameter.
        assert f"models/{model}:generateContent" in sent["path"], sent["path"]
        assert "key=test-key-123" in sent["path"], "the key must be sent"
    else:
        assert sent["body"].get("model") == model, (provider, sent["body"])
    if provider == "anthropic":
        assert sent["headers"].get("x-api-key") == "test-key-123", "Anthropic uses x-api-key"
        assert sent["headers"].get("anthropic-version"), "Anthropic needs a version header"
    elif provider in ("openai", "openrouter", "custom"):
        assert sent["headers"].get("Authorization") == "Bearer test-key-123", provider
    print(f"  ok {provider:<11} {tokens:>4} tokens via {expected_path}")

# --- model discovery ---------------------------------------------------------
for provider, base, expect in [("ollama", BASE, "local-model"), ("anthropic", BASE, "claude-mock"),
                               ("openai", BASE, "mock-model"),
                               ("google", BASE + "/v1beta", "gemini-test")]:
    llm.list_models.clear()
    found = llm.list_models(provider, base, "test-key-123")
    assert expect in found, (provider, found)
print("  ok model lists come back from all four API shapes")

# --- what it cost ------------------------------------------------------------
archive.clear_usage()
llm.start_run()
# $3 per million in, $15 per million out: 200 in + 50 out = $0.00135
s = configure("openai", "mock-model", price_in=3.0, price_out=15.0)
llm.complete("hello", 50, s, kind="Company analysis")
month = archive.usage_since(0)
assert month["calls"] == 1 and month["tokens"] == 250, month
assert abs(month["cost"] - 0.00135) < 1e-9, month["cost"]
breakdown = archive.usage_breakdown(0)
assert breakdown[0]["kind"] == "Company analysis", breakdown
print(f"  ok spend recorded: 250 tokens = {cfg.money(s, month['cost'])}")

# A local model is free, and recorded as free.
archive.clear_usage()
llm.complete("hello", 50, configure("ollama", "local-model"), kind="test")
assert archive.usage_since(0)["cost"] == 0.0, "a local model must never be billed"

# --- the budget actually stops a call ----------------------------------------
archive.clear_usage()
llm.start_run()
s = configure("openai", "mock-model", price_in=3.0, price_out=15.0)
s["budget"].update(max_tokens_per_run=100)
assert llm.complete("hello", 50, s, kind="test")[0], "the first call is under the cap"
text, tokens = llm.complete("hello", 50, s, kind="test")   # now 250 > 100
assert text is None and tokens == 0, "the per-run cap must stop the next call"
assert "per-run limit" in (llm.LAST_BLOCK or ""), llm.LAST_BLOCK
assert archive.usage_since(0)["calls"] == 1, "a blocked call must not be billed"

archive.clear_usage()
llm.start_run()
s = configure("openai", "mock-model", price_in=3.0, price_out=15.0)
s["budget"].update(monthly_cost=0.001)       # one call costs $0.00135
assert llm.complete("hello", 50, s, kind="test")[0]
assert llm.complete("hello", 50, s, kind="test")[0] is None, "the monthly cost cap must bite"
assert "budget of" in (llm.LAST_BLOCK or ""), llm.LAST_BLOCK

# A free provider is never blocked, whatever the caps say.
llm.start_run()
s = configure("ollama", "local-model")
s["budget"].update(max_tokens_per_run=1, monthly_cost=0.0001)
assert llm.complete("hello", 50, s, kind="test")[0] == "local answer", \
    "a local model must never be blocked by a spending limit"

# Turning the budget off removes every limit.
llm.start_run()
s = configure("openai", "mock-model", price_in=3.0, price_out=15.0)
s["budget"].update(enabled=False, max_tokens_per_run=1)
assert llm.complete("hello", 50, s, kind="test")[0], "a disabled budget imposes nothing"

# --- a model that echoes the template instead of answering -------------------
# Seen in the wild from a hosted gemma-4-31b: it replied with the literal
# placeholder plus a dump of the input, which was shown to the user as though
# it were the analysis. A placeholder must never reach the page.
ECHOED = ("VERDICT: Positive\n"
          "SUMMARY: <3 sentences on financial position and outlook, citing key numbers>\n\n"
          "    Market Cap: 16,59,630Cr.\n    Current Price: 1,226.\n    P/E: 42.3.")
REAL = ("VERDICT: Cautious\nSUMMARY: Reliance trades at a P/E of 42.3 against a return on equity "
        "of just 7.71%. Sales rose 15.4% in the latest quarter. The valuation leaves little room "
        "for disappointment.")

_real_complete = llm.complete
for canned, expect_summary in ((ECHOED, False), (REAL, True)):
    llm.complete = lambda *a, canned=canned, **k: (canned, 289)
    out = llm.analyze("RELIANCE", "Reliance Industries Ltd", {"ROE": "7.71%"}, None, [], [], cfg.load())
    assert bool(out["summary"]) is expect_summary, (canned[:40], out["summary"])
    if not expect_summary:
        assert out["summary"] is None, f"a template echo reached the page: {out['summary']!r}"

    news = llm.summarize_news("Reliance", [{"title": "x", "source": "ET"}], cfg.load())
    assert bool(news["summary"]) is expect_summary, news["summary"]
llm.complete = _real_complete
assert llm._clean_answer("```\ndata dump\n```") is None, "a code block is not an answer"
print("ok: an echoed template is rejected, a real answer is kept")

print("ok: six providers answer correctly, spend is booked, and the caps stop paid calls")
server.shutdown()
tmp.cleanup()
