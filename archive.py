"""
The local archive: one SQLite file next to the downloaded filings.

It does four jobs, all of them offline:

1. Caches PDF extraction by file hash, so a filing is parsed once ever
   instead of on every run -- the single biggest speed-up in the app.
2. Keeps the text of every page so filings can be searched across companies.
3. Saves each analysis so you can reopen it later and see what changed
   since last time.
4. Remembers what management promised on a concall, to check later.

Nothing here talks to the network, and everything respects the retention
settings, so the archive can be told to forget on a schedule.
"""

import hashlib
import json
import re
import sqlite3
import io
import os
import shutil
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

import settings as cfg

DATA_ROOT = cfg.DATA_DIR / "downloads"
DB_PATH = DATA_ROOT / "archive.db"


def _rebase(db: Path, old: str, new: str) -> None:
    """Point stored file paths at a new data folder after a move or restore."""
    if old == new or not db.exists():
        return
    with sqlite3.connect(db) as conn:
        for table, col in (("documents", "path"), ("runs", "snapshot")):
            try:
                conn.execute(f"UPDATE {table} SET {col}=replace({col}, ?, ?)", (old, new))
            except sqlite3.OperationalError:
                pass  # table not created yet


# One-time move from the old in-app folder, which an update could overwrite.
_OLD_ROOT = Path(__file__).parent / "downloads"
if _OLD_ROOT.is_dir() and not DB_PATH.exists() and not os.environ.get("STOCK_HUB_DATA"):
    DATA_ROOT.parent.mkdir(parents=True, exist_ok=True)
    if DATA_ROOT.exists():
        DATA_ROOT.rmdir()  # empty; only created by a mkdir
    shutil.move(str(_OLD_ROOT), str(DATA_ROOT))
    _rebase(DB_PATH, str(_OLD_ROOT), str(DATA_ROOT))

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY, sha TEXT UNIQUE, ticker TEXT, category TEXT,
    path TEXT, pages INTEGER, indexed_at REAL);
CREATE TABLE IF NOT EXISTS figures (
    doc_id INTEGER, source TEXT, label TEXT, category TEXT, value REAL,
    currency TEXT, unit TEXT, page INTEGER, context TEXT);
CREATE INDEX IF NOT EXISTS figures_doc ON figures(doc_id);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, ticker TEXT, ts REAL, label TEXT, snapshot TEXT);
CREATE INDEX IF NOT EXISTS runs_ticker ON runs(ticker, ts);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY, ts REAL, provider TEXT, model TEXT, kind TEXT,
    tokens_in INTEGER, tokens_out INTEGER, cost REAL);
CREATE INDEX IF NOT EXISTS usage_ts ON usage(ts);
CREATE TABLE IF NOT EXISTS guidance (
    id INTEGER PRIMARY KEY, ticker TEXT, quarter TEXT, metric TEXT, claim TEXT,
    horizon TEXT, doc_id INTEGER, created REAL,
    status TEXT, why TEXT, checked_at REAL, checked_against TEXT,
    UNIQUE(ticker, quarter, claim));
"""

_FTS = ("CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5("
        "text, ticker UNINDEXED, category UNINDEXED, page UNINDEXED, doc_id UNINDEXED)")
_PLAIN = ("CREATE TABLE IF NOT EXISTS pages ("
          "text TEXT, ticker TEXT, category TEXT, page INTEGER, doc_id INTEGER)")

_has_fts: bool | None = None


@contextmanager
def connect():
    """A short-lived connection per call: safe from Streamlit's worker threads
    without any locking of our own."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> bool:
    """Create the schema if needed. Returns True when full-text search is
    available (it needs SQLite's FTS5 module; without it search falls back
    to a slower LIKE scan rather than disappearing)."""
    global _has_fts
    if _has_fts is not None:
        return _has_fts
    with connect() as conn:
        conn.executescript(SCHEMA)
        try:
            conn.execute(_FTS)
            _has_fts = True
        except sqlite3.OperationalError:
            conn.execute(_PLAIN)
            _has_fts = False
    return _has_fts


def file_sha(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# Extraction cache + page index
# --------------------------------------------------------------------------

def cached_figures(sha: str, column: str = "sha") -> list[dict] | None:
    """Figures for an already-parsed file, or None if it has never been seen.
    column="path" finds it by where it is saved instead of by its hash."""
    init()
    with connect() as conn:
        row = conn.execute(f"SELECT id FROM documents WHERE {'path' if column == 'path' else 'sha'}=?",
                           (sha,)).fetchone()
        if row is None:
            return None
        rows = conn.execute(
            "SELECT source AS Source, label AS Label, category AS Category, value AS Value, "
            "currency AS Currency, unit AS Unit, page AS Page, context AS Context "
            "FROM figures WHERE doc_id=? ORDER BY rowid", (row["id"],)).fetchall()
    return [dict(r) for r in rows]


def store_document(sha: str, ticker: str, category: str, path: str,
                   figures: list[dict], pages: list[tuple[int, str]]) -> None:
    init()
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR REPLACE INTO documents (sha, ticker, category, path, pages, indexed_at) "
            "VALUES (?,?,?,?,?,?)", (sha, ticker, category, path, len(pages), time.time()))
        doc_id = cur.lastrowid
        conn.execute("DELETE FROM figures WHERE doc_id=?", (doc_id,))
        conn.execute("DELETE FROM pages WHERE doc_id=?", (doc_id,))
        conn.executemany(
            "INSERT INTO figures (doc_id, source, label, category, value, currency, unit, page, context) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [(doc_id, f.get("Source"), f.get("Label"), f.get("Category"), f.get("Value"),
              f.get("Currency"), f.get("Unit"), f.get("Page"), f.get("Context")) for f in figures])
        conn.executemany(
            "INSERT INTO pages (text, ticker, category, page, doc_id) VALUES (?,?,?,?,?)",
            [(text, ticker, category, page, doc_id) for page, text in pages if text.strip()])


def document_text(ticker: str, category: str) -> str:
    """All text of the most recently indexed document of a category."""
    init()
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM documents WHERE ticker=? AND category=? ORDER BY indexed_at DESC LIMIT 1",
            (ticker, category)).fetchone()
        if row is None:
            return ""
        rows = conn.execute("SELECT text FROM pages WHERE doc_id=? ORDER BY page", (row["id"],)).fetchall()
    return "\n".join(r["text"] for r in rows)


# Words that carry no meaning in a filing search. Without this, a natural
# question ("what did management say about capex?") drags in "what", "did"
# and "about", which match almost every page ever written.
STOPWORDS = {
    "the", "and", "for", "are", "was", "were", "has", "have", "had", "did", "does", "with",
    "that", "this", "they", "them", "there", "their", "what", "when", "where", "which", "who",
    "why", "how", "any", "all", "about", "from", "into", "over", "under", "been", "being",
    "say", "said", "says", "tell", "told", "give", "show", "find", "you", "your", "our", "its",
    "can", "will", "would", "could", "should", "may", "might", "much", "many", "more", "most",
    "some", "such", "than", "then", "also", "just", "only", "very", "out", "not", "but",
}


def _fts_query(question: str, extra_terms=()) -> str:
    """A question (plus any terms the model suggested) -> an FTS5 query.

    Terms are OR-ed, not AND-ed: a filing rarely contains every word of a
    question, and bm25 ranking already floats the passages that match the
    most and rarest terms to the top. Each term is quoted so punctuation in
    a question is never read as FTS syntax.
    """
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9'&.-]*", question.lower())
             if len(w) > 2 and w not in STOPWORDS]
    terms = [f'"{w}"' for w in words]
    for term in extra_terms or ():
        term = re.sub(r'[^a-z0-9 \'&.-]', " ", str(term).lower()).strip()
        if len(term) <= 2:
            continue
        terms.append(f'"{term}"')  # a term with spaces becomes a phrase match
        # A phrase matches only those exact consecutive words. Asked about
        # capex, a model suggests "capital expenditure plan", which misses a
        # transcript saying "capital expenditure of Rs 1,200 crore" -- and
        # the question is then answered "no mention of that" from a filing
        # that says it plainly. The phrase's own words are OR-ed in too, so
        # the passage is found; bm25 still floats the exact phrase above it.
        if " " in term:
            terms += [f'"{w}"' for w in term.split() if len(w) > 2 and w not in STOPWORDS]
    return " OR ".join(dict.fromkeys(terms))


def _distinctive(conn, terms: list[str], total_pages: int) -> list[str]:
    """Keep the words that carry the question, drop the ones that don't.

    "Which companies mentioned China?" OR-matches "companies" and
    "mentioned" as well, and those sit on 23% and 8% of the archive against
    China's 2% -- so the results drown in pages that merely say "companies".
    Rather than guess a threshold, this keeps the rarest term and anything
    within three times its frequency, which adapts to whatever is in the
    archive.
    """
    if not terms or total_pages < 20:
        return terms
    counted = []
    for term in terms:
        try:
            hits = conn.execute("SELECT COUNT(*) FROM pages WHERE pages MATCH ?",
                                (term,)).fetchone()[0]
        except sqlite3.OperationalError:
            continue
        if hits:
            counted.append((hits, term))
    if not counted:
        return terms
    rarest = min(counted)[0]
    return [term for hits, term in counted
            if hits <= max(rarest * 3, 1) and hits <= total_pages * 0.25] or [min(counted)[1]]


def coverage(question: str, tickers: list[str] | None = None,
             categories: list[str] | None = None, extra_terms=()) -> tuple[int, dict]:
    """(pages matched, {company: pages}) across the WHOLE archive.

    Counted in SQL rather than by listing results, so a question like "which
    companies mentioned China?" is answered from all 121 matching pages
    instead of the handful the model has time to read.
    """
    has_fts = init()
    query = _fts_query(question, extra_terms)
    if not query or not has_fts:
        hits = search(question, tickers, categories, limit=2000, extra_terms=extra_terms)
        tally: dict[str, int] = {}
        for hit in hits:
            tally[hit["ticker"]] = tally.get(hit["ticker"], 0) + 1
        return len(hits), dict(sorted(tally.items(), key=lambda kv: -kv[1]))

    where, params = ["pages MATCH ?"], []
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        query = " OR ".join(_distinctive(conn, query.split(" OR "), total))
        params.append(query)
        if tickers:
            where.append(f"ticker IN ({','.join('?' * len(tickers))})")
            params += tickers
        if categories:
            where.append(f"category IN ({','.join('?' * len(categories))})")
            params += categories
        rows = conn.execute(
            f"SELECT ticker, COUNT(*) AS pages FROM pages WHERE {' AND '.join(where)} "
            "GROUP BY ticker ORDER BY pages DESC", params).fetchall()
    tally = {r["ticker"]: r["pages"] for r in rows}
    return sum(tally.values()), tally


def search(question: str, tickers: list[str] | None = None,
           categories: list[str] | None = None, limit: int = 40,
           extra_terms=()) -> list[dict]:
    """Passages from the archive matching a question, best first."""
    has_fts = init()
    query = _fts_query(question, extra_terms)
    if not query:
        return []
    where, params = [], []
    if tickers:
        where.append(f"ticker IN ({','.join('?' * len(tickers))})")
        params += tickers
    if categories:
        where.append(f"category IN ({','.join('?' * len(categories))})")
        params += categories

    with connect() as conn:
        if has_fts:
            total = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            kept = _distinctive(conn, query.split(" OR "), total)
            query = " OR ".join(kept)
            if not query:
                return []
            sql = ("SELECT ticker, category, page, doc_id, "
                   "snippet(pages, 0, '**', '**', ' … ', 24) AS snippet, text "
                   "FROM pages WHERE pages MATCH ?")
            args = [query] + params
            if where:
                sql += " AND " + " AND ".join(where)
            sql += " ORDER BY bm25(pages) LIMIT ?"
            args.append(limit)
        else:  # no FTS5 in this SQLite build: match any meaningful term
            words = [w.strip('"') for w in query.split(" OR ")] or [question]
            likes = " OR ".join("text LIKE ?" for _ in words)
            sql = (f"SELECT ticker, category, page, doc_id, substr(text,1,400) AS snippet, text "
                   f"FROM pages WHERE ({likes})")
            args = [f"%{w}%" for w in words] + params
            if where:
                sql += " AND " + " AND ".join(where)
            sql += " LIMIT ?"
            args.append(limit)
        rows = conn.execute(sql, args).fetchall()

    out = []
    for r in rows:
        with connect() as conn:
            doc = conn.execute("SELECT path FROM documents WHERE id=?", (r["doc_id"],)).fetchone()
        out.append({"ticker": r["ticker"], "category": r["category"], "page": r["page"],
                    "snippet": r["snippet"], "text": r["text"], "path": doc["path"] if doc else ""})
    return out


def indexed_tickers() -> list[str]:
    init()
    with connect() as conn:
        return [r["ticker"] for r in conn.execute(
            "SELECT DISTINCT ticker FROM documents WHERE ticker IS NOT NULL ORDER BY ticker")]


# --------------------------------------------------------------------------
# Saved analyses (history) + what-changed
# --------------------------------------------------------------------------

def save_run(ticker: str, label: str, snapshot: dict) -> int:
    init()
    with connect() as conn:
        cur = conn.execute("INSERT INTO runs (ticker, ts, label, snapshot) VALUES (?,?,?,?)",
                           (ticker, time.time(), label, json.dumps(snapshot, default=str)))
        return cur.lastrowid


def list_runs(ticker: str | None = None, limit: int = 100) -> list[dict]:
    init()
    sql = "SELECT id, ticker, ts, label FROM runs"
    params: list = []
    if ticker:
        sql += " WHERE ticker=?"
        params.append(ticker)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def get_run(run_id: int) -> dict | None:
    init()
    with connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "ticker": row["ticker"], "ts": row["ts"], "label": row["label"],
            "snapshot": json.loads(row["snapshot"])}


def previous_run(ticker: str, before_ts: float) -> dict | None:
    init()
    with connect() as conn:
        row = conn.execute("SELECT id FROM runs WHERE ticker=? AND ts<? ORDER BY ts DESC LIMIT 1",
                           (ticker, before_ts)).fetchone()
    return get_run(row["id"]) if row else None


def run_history(ticker: str, limit: int = 40) -> list[dict]:
    """Saved snapshots oldest-first -- used to chart a ratio over time."""
    init()
    with connect() as conn:
        rows = conn.execute("SELECT id FROM runs WHERE ticker=? ORDER BY ts DESC LIMIT ?",
                            (ticker, limit)).fetchall()
    return [run for run in (get_run(r["id"]) for r in reversed(rows)) if run]


def diff_snapshots(old: dict, new: dict) -> list[dict]:
    """What changed between two saved analyses of the same company.

    Deliberately plain comparison, no model involved: a "what changed" list
    that could hallucinate would be worse than none.
    """
    changes: list[dict] = []
    old_m, new_m = old.get("metrics") or {}, new.get("metrics") or {}
    for key, new_value in new_m.items():
        old_value = old_m.get(key)
        if old_value is None:
            changes.append({"kind": "Ratio", "item": key, "from": "—", "to": new_value})
        elif str(old_value) != str(new_value):
            changes.append({"kind": "Ratio", "item": key, "from": old_value, "to": new_value})

    for field, kind in (("pros", "Strength"), ("cons", "Risk")):
        before, after = set(old.get(field) or []), set(new.get(field) or [])
        changes += [{"kind": kind, "item": t, "from": "—", "to": "Added"} for t in sorted(after - before)]
        changes += [{"kind": kind, "item": t, "from": "Listed", "to": "Gone"} for t in sorted(before - after)]

    if old.get("ai_verdict") != new.get("ai_verdict") and new.get("ai_verdict"):
        changes.append({"kind": "AI verdict", "item": "Verdict",
                        "from": old.get("ai_verdict") or "—", "to": new.get("ai_verdict")})

    before_docs = {d["url"] for d in (old.get("documents") or []) if d.get("url")}
    for doc in (new.get("documents") or []):
        if doc.get("url") and doc["url"] not in before_docs:
            changes.append({"kind": "New filing", "item": f"{doc.get('category')} · {doc.get('text')}",
                            "from": "—", "to": doc["url"]})

    before_news = {h["url"] for h in ((old.get("news") or {}).get("headlines") or [])}
    for h in ((new.get("news") or {}).get("headlines") or []):
        if h.get("url") and h["url"] not in before_news:
            changes.append({"kind": "News", "item": h["title"], "from": "—", "to": h.get("source") or ""})
    return changes


# --------------------------------------------------------------------------
# Guidance: what management promised, and whether it happened
# --------------------------------------------------------------------------

def save_claims(ticker: str, quarter: str, claims: list[dict]) -> int:
    init()
    saved = 0
    with connect() as conn:
        for c in claims:
            cur = conn.execute(
                "INSERT OR IGNORE INTO guidance (ticker, quarter, metric, claim, horizon, created) "
                "VALUES (?,?,?,?,?,?)",
                (ticker, quarter, c.get("metric"), c.get("claim"), c.get("horizon"), time.time()))
            saved += cur.rowcount
    return saved


def claims_for(ticker: str | None = None) -> list[dict]:
    init()
    sql = "SELECT * FROM guidance"
    params: list = []
    if ticker:
        sql += " WHERE ticker=?"
        params.append(ticker)
    sql += " ORDER BY created DESC"
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def set_claim_status(claim_id: int, status: str | None, why: str | None, against: str) -> None:
    init()
    with connect() as conn:
        conn.execute("UPDATE guidance SET status=?, why=?, checked_at=?, checked_against=? WHERE id=?",
                     (status, why, time.time(), against, claim_id))


def delete_claims(ticker: str) -> None:
    init()
    with connect() as conn:
        conn.execute("DELETE FROM guidance WHERE ticker=?", (ticker,))


# --------------------------------------------------------------------------
# Spending: every model call is logged, so the budget is based on fact
# --------------------------------------------------------------------------

def record_usage(provider: str, model: str, kind: str,
                 tokens_in: int, tokens_out: int, cost: float) -> None:
    init()
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO usage (ts, provider, model, kind, tokens_in, tokens_out, cost) "
                "VALUES (?,?,?,?,?,?,?)",
                (time.time(), provider, model, kind, int(tokens_in), int(tokens_out), float(cost)))
    except sqlite3.Error:
        pass  # never let bookkeeping break a call that already succeeded


def usage_since(since_ts: float, provider: str | None = None) -> dict:
    """Totals since a moment in time -- what the budget is measured against."""
    init()
    sql = ("SELECT COALESCE(SUM(tokens_in + tokens_out), 0) AS tokens, "
           "COALESCE(SUM(cost), 0) AS cost, COUNT(*) AS calls FROM usage WHERE ts >= ?")
    params: list = [since_ts]
    if provider:
        sql += " AND provider = ?"
        params.append(provider)
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return {"tokens": row["tokens"] or 0, "cost": row["cost"] or 0.0, "calls": row["calls"] or 0}


def usage_breakdown(since_ts: float) -> list[dict]:
    """Spend grouped by provider, model and what it was spent on."""
    init()
    with connect() as conn:
        rows = conn.execute(
            "SELECT provider, model, kind, COUNT(*) AS calls, "
            "SUM(tokens_in) AS tokens_in, SUM(tokens_out) AS tokens_out, SUM(cost) AS cost "
            "FROM usage WHERE ts >= ? GROUP BY provider, model, kind ORDER BY cost DESC, calls DESC",
            (since_ts,)).fetchall()
    return [dict(r) for r in rows]


def usage_daily(since_ts: float) -> list[dict]:
    init()
    with connect() as conn:
        rows = conn.execute(
            "SELECT date(ts, 'unixepoch', 'localtime') AS day, "
            "SUM(tokens_in + tokens_out) AS tokens, SUM(cost) AS cost "
            "FROM usage WHERE ts >= ? GROUP BY day ORDER BY day", (since_ts,)).fetchall()
    return [dict(r) for r in rows]


def clear_usage() -> None:
    with connect() as conn:
        try:
            conn.execute("DELETE FROM usage")
        except sqlite3.OperationalError:
            pass


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------

def stats() -> dict:
    init()
    with connect() as conn:
        one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        out = {
            "documents": one("SELECT COUNT(*) FROM documents"),
            "pages": one("SELECT COUNT(*) FROM pages"),
            "figures": one("SELECT COUNT(*) FROM figures"),
            "runs": one("SELECT COUNT(*) FROM runs"),
            "claims": one("SELECT COUNT(*) FROM guidance"),
            "oldest_run": conn.execute("SELECT MIN(ts) FROM runs").fetchone()[0],
        }
    out["db_mb"] = DB_PATH.stat().st_size / 1e6 if DB_PATH.exists() else 0.0
    pdfs = list(DB_PATH.parent.rglob("*.pdf"))
    out["pdf_count"] = len(pdfs)
    out["pdf_mb"] = sum(p.stat().st_size for p in pdfs) / 1e6
    return out


def purge(storage: dict) -> dict:
    """Apply the retention settings. 0 days means keep forever."""
    init()
    removed = {"runs": 0, "pdfs": 0, "documents": 0}
    now = time.time()
    keep_runs = int(storage.get("keep_runs_days") or 0)
    keep_text = int(storage.get("keep_text_days") or 0)
    keep_docs = int(storage.get("keep_documents_days") or 0)
    max_per_ticker = int(storage.get("max_runs_per_ticker") or 0)

    with connect() as conn:
        if keep_runs > 0:
            cur = conn.execute("DELETE FROM runs WHERE ts < ?", (now - keep_runs * 86400,))
            removed["runs"] += cur.rowcount
        if max_per_ticker > 0:
            for (ticker,) in conn.execute("SELECT DISTINCT ticker FROM runs").fetchall():
                cur = conn.execute(
                    "DELETE FROM runs WHERE ticker=? AND id NOT IN "
                    "(SELECT id FROM runs WHERE ticker=? ORDER BY ts DESC LIMIT ?)",
                    (ticker, ticker, max_per_ticker))
                removed["runs"] += cur.rowcount
        if keep_text > 0:
            cutoff = now - keep_text * 86400
            stale = [r["id"] for r in conn.execute("SELECT id FROM documents WHERE indexed_at < ?", (cutoff,))]
            for doc_id in stale:
                conn.execute("DELETE FROM pages WHERE doc_id=?", (doc_id,))
                conn.execute("DELETE FROM figures WHERE doc_id=?", (doc_id,))
                conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            removed["documents"] += len(stale)

    if keep_docs > 0:
        cutoff = now - keep_docs * 86400
        for pdf in DB_PATH.parent.rglob("*.pdf"):
            try:
                if pdf.stat().st_mtime < cutoff:
                    pdf.unlink()
                    removed["pdfs"] += 1
            except OSError:
                pass
    return removed


def drop_unusable(is_usable) -> int:
    """Strip summaries saved before the app learned to reject them.

    Only the summary text is cleared; the ratios, filings and news of that
    saved analysis are untouched, so history stays intact.
    """
    init()
    fixed = 0
    with connect() as conn:
        rows = conn.execute("SELECT id, snapshot FROM runs").fetchall()
        for row in rows:
            try:
                snap = json.loads(row["snapshot"])
            except ValueError:
                continue
            changed = False
            if snap.get("ai_summary") and not is_usable(snap["ai_summary"]):
                snap["ai_summary"], changed = None, True
            news = snap.get("news") or {}
            if news.get("summary") and not is_usable(news["summary"]):
                news["summary"], changed = None, True
            if changed:
                conn.execute("UPDATE runs SET snapshot=? WHERE id=?",
                             (json.dumps(snap, default=str), row["id"]))
                fixed += 1
    return fixed


# --------------------------------------------------------------------------
# Backup: everything in one zip, restorable on this or another computer
# --------------------------------------------------------------------------

def export_bundle(include_keys: bool = False, include_pdfs: bool = False) -> bytes:
    """A zip of settings, portfolios, saved analyses, the search index and
    (optionally) the filing PDFs. API keys are left out unless asked for."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps({"app": "stock-data-hub", "version": 1,
                                                "data_root": str(DATA_ROOT), "created": time.time()}))
        s = cfg.load()
        if not include_keys:
            for p in s.get("providers", {}).values():
                p["api_key"] = ""
        z.writestr("settings.json", json.dumps(s, indent=2))
        if DB_PATH.exists():
            snap = DATA_ROOT / "export.tmp.db"
            with sqlite3.connect(DB_PATH) as src, sqlite3.connect(snap) as dst:
                src.backup(dst)  # consistent copy even while the app is writing
            z.write(snap, "archive.db")
            snap.unlink()
        if include_pdfs:
            for pdf in DATA_ROOT.rglob("*.pdf"):
                z.write(pdf, f"files/{pdf.relative_to(DATA_ROOT).as_posix()}")
    return buf.getvalue()


def import_bundle(uploaded) -> str:
    """Restore a zip from export_bundle(), replacing the current data.
    Returns a message; raises ValueError for a file that is not a backup."""
    try:
        z = zipfile.ZipFile(uploaded)
        manifest = json.loads(z.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, ValueError):
        raise ValueError("That is not a backup made by this app.")
    if manifest.get("app") != "stock-data-hub":
        raise ValueError("That is not a backup made by this app.")
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    pdfs = 0
    for name in z.namelist():
        if name.startswith("files/") and name.endswith(".pdf"):
            dest = (DATA_ROOT / name[len("files/"):]).resolve()
            if DATA_ROOT.resolve() not in dest.parents:
                continue  # never write outside the data folder
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(name))
            pdfs += 1
    if "archive.db" in z.namelist():
        tmp = DATA_ROOT / "import.tmp.db"
        tmp.write_bytes(z.read("archive.db"))
        _rebase(tmp, manifest.get("data_root", ""), str(DATA_ROOT))
        for suffix in ("-wal", "-shm"):
            Path(f"{DB_PATH}{suffix}").unlink(missing_ok=True)
        os.replace(tmp, DB_PATH)
    if "settings.json" in z.namelist():
        restored = json.loads(z.read("settings.json"))
        current = cfg.load()
        for pid, p in restored.get("providers", {}).items():
            if not p.get("api_key"):  # a backup without keys keeps the keys you have
                p["api_key"] = current.get("providers", {}).get(pid, {}).get("api_key", "")
        cfg.save(cfg._merge(cfg.DEFAULTS, restored))
    return f"Restored settings, portfolios and saved analyses" + (f", plus {pdfs} filing(s)." if pdfs else ".")


def clear_all() -> None:
    """Forget everything: the index, saved analyses and guidance."""
    with connect() as conn:
        for table in ("pages", "figures", "documents", "runs", "guidance", "usage"):
            try:
                conn.execute(f"DELETE FROM {table}")
            except sqlite3.OperationalError:
                pass
