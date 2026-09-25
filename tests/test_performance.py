"""Self-check that analysis cannot make the app sluggish, and stays inside the
CPU and memory limits set on the Settings page.

Parses the bundled user guide, so no network or model is needed.
Run: python tests/test_performance.py
"""
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

_tmp = tempfile.TemporaryDirectory()
os.environ["STOCK_HUB_DATA"] = _tmp.name

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.getLogger("pdfminer").setLevel(logging.ERROR)  # font warnings on this PDF are noise

import core  # noqa: E402
import llm  # noqa: E402
import runtime  # noqa: E402
import settings as cfg  # noqa: E402

GUIDE = ROOT / "static" / "user_guide.pdf"
CORES = os.cpu_count() or 1

def main():
    # --- The limits, as numbers -------------------------------------------------
    s = cfg._merge(cfg.DEFAULTS, {})
    s["data"]["doc_workers"] = 64
    s["performance"]["cpu_limit_pct"] = 100
    assert core.parse_limits(s) == {"workers": CORES, "duty": 1.0, "ram_mb": 2048.0}
    s["performance"]["cpu_limit_pct"] = 5
    tiny = core.parse_limits(s)
    assert tiny == {"workers": 1, "duty": min(CORES * 5 / 100, 1.0), "ram_mb": 2048.0}, tiny
    s["data"]["doc_workers"] = 1
    s["performance"]["cpu_limit_pct"] = 100
    assert core.parse_limits(s)["workers"] == 1, "never more parse workers than download workers"
    assert llm._cpu_threads(s) == {}, "at 100% Ollama picks its own threads"
    s["performance"]["cpu_limit_pct"] = 50
    assert llm._cpu_threads(s) == {"num_thread": max(1, round(CORES / 2))}
    print(f"ok: limits on {CORES} cores -> workers, duty and Ollama threads")

    # --- Memory is read live, not as a peak ---------------------------------------
    live = runtime.live_memory_mb()
    assert 10 < live < 100_000, live
    print(f"ok: live memory {live:,.0f} MB")

    # --- Parsed off the server process, with the same result --------------------
    core.PARSE_LIMITS.update(workers=2, duty=1.0, ram_mb=0.0)
    here_pages: list = []
    here = core.extract_all_numbers(GUIDE, "Annual Report", pages_out=here_pages)
    there, there_pages = core.parse_filing(GUIDE, "Annual Report")
    assert there == here and there_pages == here_pages, "a worker must parse exactly as the server would"
    assert core._POOL_KEY is not None, "the worker pool was not used"
    print(f"ok: a worker process returns the same {len(there)} figures and {len(there_pages)} pages")


    # --- Clicks stay quick while filings parse --------------------------------------
    def page_work() -> float:
        """Stand-in for a page rerun: pure-Python work that needs the GIL."""
        started = time.perf_counter()
        sum(i * i for i in range(2_000_000))
        return time.perf_counter() - started


    idle = min(page_work() for _ in range(3))
    stop = threading.Event()


    def parse_forever():
        while not stop.is_set():
            core.parse_filing(GUIDE, "Annual Report")


    threads = [threading.Thread(target=parse_forever, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(3)  # workers started and busy
    busy = max(page_work() for _ in range(3))
    stop.set()
    for t in threads:
        t.join()
    # Parsed in-process this was ~100x slower; off-process only CPU sharing is left.
    assert busy < idle * 3, f"page work took {busy:.2f}s while parsing vs {idle:.2f}s idle"
    print(f"ok: page work {idle:.2f}s idle, {busy:.2f}s while two filings parse")

    # --- The CPU limit slows a parse down -----------------------------------------
    started = time.perf_counter()
    core.extract_all_numbers(GUIDE, "Annual Report")
    full = time.perf_counter() - started
    core._DUTY = 0.5
    started = time.perf_counter()
    core.extract_all_numbers(GUIDE, "Annual Report")
    half = time.perf_counter() - started
    core._DUTY = 1.0
    assert half > full * 1.6, f"at a 50% share a parse took {half:.2f}s vs {full:.2f}s unlimited"
    print(f"ok: a half-share parse takes {half:.2f}s vs {full:.2f}s")

    # --- The memory limit holds new parses back -----------------------------------
    core.PARSE_LIMITS["ram_mb"] = 1.0  # always over
    real_sleep, waits = time.sleep, []
    time.sleep = lambda s: (waits.append(s), real_sleep(0))[1]
    real_time = time.time
    clock = [real_time()]
    time.time = lambda: clock.__setitem__(0, clock[0] + 30) or clock[0]  # 30 s per look
    core._wait_for_memory()
    time.sleep, time.time = real_sleep, real_time
    assert 2 <= len(waits) <= 5, f"waited {len(waits)} times; should hold, then give up after 2 min"
    core.PARSE_LIMITS["ram_mb"] = 0.0
    print(f"ok: over the memory limit a parse waits ({len(waits)} checks), then goes ahead")

    core._POOL.shutdown()
    _tmp.cleanup()
    print("\nok: performance limits")


if __name__ == "__main__":  # parse workers re-import this file; only the parent runs it
    main()
