"""The web API: routing, input checks and failures, with no network.
Run:  .venv/bin/python tests/test_web.py"""
import importlib.util
import io
import os
import tempfile
from pathlib import Path
from unittest import mock

os.environ["STOCK_HUB_DATA"] = tempfile.mkdtemp()
path = Path(__file__).resolve().parents[1] / "web" / "api" / "index.py"
spec = importlib.util.spec_from_file_location("web_api", path)
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

PAGE = """<h1>Acme Ltd</h1><ul id="top-ratios"><li><span class="name">P/E</span><span class="value">12</span></li></ul>
<div class="pros"><li>Debt free</li></div><div class="cons"><li>Slow growth</li></div>"""


def test_bad_input_is_rejected_before_any_request():
    with mock.patch.object(api.core, "fetch_company_page") as fetch:
        assert api.respond("/api/company", "t=../../etc")[0] == 400
        assert api.respond("/api/company", "")[0] == 400
        fetch.assert_not_called()
    assert api.respond("/api/search", "q=")[0] == 400
    assert api.respond("/api/search", "q=" + "a" * 61)[0] == 400
    assert api.respond("/api/prices", "id=1;drop")[0] == 400
    assert api.respond("/api/nope", "")[0] == 404


def test_company_is_parsed_and_cached():
    with mock.patch.object(api.core, "fetch_company_page", return_value=PAGE):
        status, body, ttl = api.respond("/api/company", "t=acme")
    assert status == 200 and ttl == 900
    assert body["ticker"] == "ACME" and body["name"] == "Acme Ltd"
    assert body["metrics"] == {"P/E": "12"}
    assert body["pros"] == ["Debt free"] and body["cons"] == ["Slow growth"]


def test_news_keeps_the_link():
    item = {"title": "Acme wins", "source": "Mint", "url": "https://example.com/a", "published": None}
    with mock.patch.object(api.core, "fetch_company_news", return_value=[item]):
        _, body, _ = api.respond("/api/news", "t=ACME&name=Acme")
    assert body == [{"title": "Acme wins", "link": "https://example.com/a", "source": "Mint"}]


def test_failures_are_never_cached_and_hide_details():
    with mock.patch.object(api.portfolio, "search", side_effect=RuntimeError("secret detail")):
        status, body, ttl = api.respond("/api/search", "q=acme")
    assert status == 502 and ttl == 0 and "secret" not in body["error"]
    with mock.patch.object(api.core, "fetch_company_page", return_value=None):
        status, _, ttl = api.respond("/api/company", "t=NOPE")
    assert status == 404 and ttl == 0


def test_paid_routes_need_a_verified_sign_in():
    with mock.patch.object(api.requests, "post") as post, mock.patch.object(api.core, "scrape_all") as run:
        assert api.respond_post("analyze", "", b'{"ticker":"ACME"}')[0] == 401
        post.return_value = mock.Mock(ok=False, json=lambda: {})
        assert api.respond_post("analyze", "Bearer bad", b'{"ticker":"ACME"}')[0] == 401
        run.assert_not_called()


def test_ai_server_cannot_be_a_private_address():
    for url in ("http://localhost:11434", "http://127.0.0.1", "http://169.254.169.254/latest", "http://10.0.0.1", "ftp://x.com"):
        try:
            api.safe_base_url(url)
        except api.BadRequest:
            continue
        raise AssertionError(url)
    with mock.patch.object(api.socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("8.8.8.8", 443))]):
        assert api.safe_base_url("https://my-ai.example.com/") == "https://my-ai.example.com"


def test_ai_key_is_used_only_for_that_request():
    with mock.patch.object(api.socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("8.8.8.8", 443))]):
        s = api.run_settings({"ai": {"provider": "openai", "model": "m", "api_key": "k"}})
    assert s["ai"]["enabled"] and s["providers"]["openai"]["api_key"] == "k"
    assert api.cfg.DEFAULTS["providers"]["openai"]["api_key"] == ""
    assert not api.run_settings({})["ai"]["enabled"]


def test_filings_parse_without_worker_processes():
    with mock.patch.object(api.core, "extract_all_numbers", return_value=[{"Label": "x"}]) as parse:
        figures, _ = api.core.parse_filing(Path("a.pdf"), "Annual Report")
    assert figures == [{"Label": "x"}] and parse.called


def test_trades_use_oldest_shares_first_and_tax_year():
    t = [{"ticker": "A", "side": "buy", "qty": 10, "price": 100, "date": "2023-01-01", "account": "x"},
         {"ticker": "A", "side": "buy", "qty": 10, "price": 200, "date": "2025-01-01", "account": "x"},
         {"ticker": "A", "side": "sell", "qty": 10, "price": 300, "date": "2025-02-01", "account": "x"}]
    r = api.trades_summary({"trades": t, "value": 3000})
    assert r["gains"][0]["gain"] == 2000 and r["gains"][0]["term"] == "Long" and "2024" in r["gains"][0]["fy"]
    assert r["positions"] == [{"account": "x", "ticker": "A", "qty": 10, "avg_price": 200}] and r["unmatched"] == 0


def test_own_checks_fill_screener_gaps_only():
    market = api.pd.DataFrame({"Ticker": ["A"], "Company": ["Acme"], "SME": [False], "Current Price": [10.0], "ROE": [None], "Verdict": [None]})
    with mock.patch.object(api, "_market", return_value=market):
        r = api.screen_own({"q": "ROE > 15", "own": [{"Ticker": "A", "Company": "Acme", "ROE": 20, "Current Price": 99}]})
    assert r["count"] == 1 and r["rows"][0][r["columns"].index("Current Price")] == 10


def test_upload_rejects_other_files_and_reads_csv():
    b64 = lambda b: api.base64.b64encode(b).decode()
    try:
        api.upload({"name": "x.exe", "file": b64(b"x")})
        raise AssertionError
    except api.BadRequest:
        pass
    with mock.patch.object(api.portfolio, "resolve", side_effect=[{"ticker": "TCS", "name": "TCS", "screener_id": 1}, None]):
        r = api.upload({"name": "p.csv", "file": b64(b"ticker,quantity,avg_price\ntcs,5,100\nzzz,,\n")})
    assert r["rows"][0]["qty"] == 5 and r["rows"][0]["avg_price"] == 100 and "ZZZ" in r["problems"][0]


def test_daily_job_needs_its_secret():
    api.os.environ.pop("CRON_SECRET", None)
    assert api.cron("Bearer ")[0] == 401  # no secret set: nothing runs
    with mock.patch.dict(api.os.environ, {"CRON_SECRET": "s"}):
        with mock.patch.object(api, "run_alert_job") as job:
            assert api.cron("Bearer wrong")[0] == 401 and api.cron("")[0] == 401
            job.assert_not_called()
            api.os.environ.pop("FIREBASE_SERVICE_ACCOUNT", None)
            assert api.cron("Bearer s")[0] == 503
            api.os.environ["FIREBASE_SERVICE_ACCOUNT"] = "{}"
            job.return_value = {"fired": 0, "failed": 0}
            assert api.cron("Bearer s") == (200, {"fired": 0, "failed": 0})
            del api.os.environ["FIREBASE_SERVICE_ACCOUNT"]


def test_daily_job_fires_only_reached_alerts_once():
    doc = lambda i, op, level, fired=False: {"document": {"name": f"projects/p/databases/(default)/documents/users/u{i}/alerts/a{i}", "fields": {
        "ticker": {"stringValue": "TCS"}, "op": {"stringValue": op}, "level": {"integerValue": str(level)}, "fired": {"booleanValue": fired}, "screener_id": {"integerValue": "1"}}}}
    rows = [doc(1, "above", 100), doc(2, "above", 500), doc(3, "below", 300), doc(4, "above", 1, True), {"readTime": "x"}]
    with mock.patch.object(api, "_firestore_token", return_value=("t", "https://f/documents")), \
            mock.patch.object(api, "_latest_close", return_value=200.0), mock.patch.object(api, "_email_user") as mail, \
            mock.patch.object(api.requests, "post") as post, mock.patch.object(api.requests, "patch") as patch:
        post.return_value = mock.Mock(json=lambda: rows)
        assert api.run_alert_job() == {"fired": 2, "failed": 0}
    assert patch.call_count == 2 and mail.call_count == 2  # above 100 and below 300, not above 500 or already fired
    assert [c.args[0] for c in post.call_args_list if "/events" in c.args[0]] == ["https://f/documents/users/u1/events", "https://f/documents/users/u3/events"]


def test_daily_job_fails_loudly_when_the_database_refuses():
    refused = mock.Mock(json=lambda: [{"error": {"code": 403}}])
    refused.raise_for_status.side_effect = api.requests.HTTPError("403")
    with mock.patch.object(api, "_firestore_token", return_value=("t", "https://f/documents")), \
            mock.patch.object(api.requests, "post", return_value=refused):
        try:
            api.run_alert_job()
        except api.requests.HTTPError:
            return
    raise AssertionError("a refused query looked like no alerts")


def test_screener_puts_the_biggest_first():
    market = api.pd.DataFrame({"Ticker": ["A", "B", "C"], "Company": ["a", "b", "c"], "SME": [False] * 3, "Current Price": [1.0] * 3, "ROE": [20, 40, None], "Verdict": [None] * 3})
    with mock.patch.object(api, "_market", return_value=market):
        r = api.screen_own({"q": "ROE > 15"})
    assert [row[0] for row in r["rows"]] == ["B", "A"]


def test_screener_words_reach_the_page():
    _, body, _ = api.respond("/api/meta", "")
    assert body["fields"]["P/E"] and "P/E" in body["market"] and body["units"]["Market Cap"] == "₹ Cr"
    assert all(len(names) <= 2 for names in body["fields"].values())


def test_build_copies_every_shared_module():
    import json, re
    root = path.parents[2]
    listed = re.search(r"for m in (.+?);", json.loads((root / "web/vercel.json").read_text())["buildCommand"]).group(1).split()
    local = {p.stem for p in root.glob("*.py")}
    for source in [path, *(root / f"{m}.py" for m in listed)]:
        used = set(re.findall(r"^\s*(?:import|from) (\w+)", source.read_text(), re.M)) & local
        assert used <= set(listed), (source.name, used - set(listed))


def test_page_furniture_is_served_from_this_site():
    import re
    public = path.parents[1] / "public"
    html = (public / "index.html").read_text()
    for local in re.findall(r'(?:href|src)="/([^"]+)"', html):
        assert (public / local).is_file(), local
    assert 'rel="icon"' in html and "jsdelivr" not in html


def test_data_sources_get_more_tries():
    retry = api.core.SESSION.get_adapter("https://www.screener.in").max_retries
    assert retry.total == 2 and 503 in retry.status_forcelist and retry.allowed_methods == ("GET",)


def test_failures_name_the_error_in_the_logs_and_nothing_else():
    import contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out), mock.patch.object(api.portfolio, "search", side_effect=RuntimeError("secret detail")):
        assert api.respond("/api/search", "q=acme")[0] == 502
    assert "RuntimeError" in out.getvalue() and "secret" not in out.getvalue()
    out = io.StringIO()
    with contextlib.redirect_stdout(out), mock.patch.object(api, "verify_user", side_effect=RuntimeError("secret key")):
        assert api.respond_post("analyze", "x", b"{}")[0] == 502
    assert "RuntimeError" in out.getvalue() and "secret" not in out.getvalue()


def test_the_deployed_bundle_works_without_the_repo_or_streamlit():
    """Exactly what Vercel ships. This is what would have caught runtime.py going missing."""
    import json, re, shutil, subprocess, sys
    root = path.parents[2]
    listed = re.search(r"for m in (.+?);", json.loads((root / "web/vercel.json").read_text())["buildCommand"]).group(1).split()
    with tempfile.TemporaryDirectory() as tmp:
        site = Path(tmp).resolve() / "site"
        (site / "api").mkdir(parents=True)
        shutil.copy(path, site / "api/index.py")
        for m in listed:
            shutil.copy(root / f"{m}.py", site)
        code = ("import sys, importlib.util as u; sys.modules['streamlit'] = None\n"
                f"s = u.spec_from_file_location('web_api', {str(site / 'api/index.py')!r}); a = u.module_from_spec(s); s.loader.exec_module(a)\n"
                f"assert a.core.__file__.startswith({str(site)!r}), a.core.__file__\n"
                "a.core._wait_for_memory()\nprint('bundle ok')")
        run = subprocess.run([sys.executable, "-c", code], cwd=tmp, capture_output=True, text=True, timeout=120, env={**os.environ, "STOCK_HUB_DATA": tmp})
        assert "bundle ok" in run.stdout, run.stderr[-600:]


def test_nan_becomes_null():
    assert api.clean({"a": [float("nan"), 1.0], "b": float("inf")}) == {"a": [None, 1.0], "b": None}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
