"""
Screens over every company listed on the NSE (main board and SME), and
comparisons over every company the app has analysed.

Queries read like screener.in's -- "Market Capitalization > 500 AND
Return on equity > 15" -- and are parsed, never eval'd. The universe is each
company's newest saved analysis, pulled with SQL so the big stored tables
are never loaded.
"""

import io
import json
import re

import pandas as pd
import streamlit as st

import core
import portfolio as pf

# Field -> the names people type for it (screener.in's wording first).
FIELDS = {
    "Market Cap": ["market capitalization", "market cap", "mcap"],
    "Current Price": ["current price", "price", "cmp"],
    "P/E": ["price to earning", "stock p/e", "p/e", "pe"],
    "Price to Book": ["price to book value", "price to book", "p/b", "pb"],
    "Book Value": ["book value"],
    "Dividend Yield": ["dividend yield", "yield"],
    "ROCE": ["return on capital employed", "roce"],
    "ROE": ["return on equity", "roe"],
    "Sales Growth": ["sales growth", "revenue growth", "yoy quarterly sales growth"],
    "Profit Growth": ["profit growth", "yoy quarterly profit growth"],
    "OPM": ["opm", "operating profit margin"],
    "Face Value": ["face value"],
    "EPS": ["earnings per share", "eps"],
    "1Y Return": ["return over 1 year", "1 year return", "1y return", "returns 1y"],
    "Sector": ["sector"],
    "Verdict": ["verdict", "ai verdict"],
    # Built from the app's own graded guidance, so it exists nowhere else:
    # how often this management did what it said it would.
    "Guidance Score": ["guidance score", "guidance hit rate", "promises kept", "delivery rate"],
    "Guidance Checked": ["guidance checked", "commitments graded", "promises graded"],
}
TEXT_FIELDS = {"Sector", "Verdict"}
UNITS = {"Market Cap": "₹ Cr", "Current Price": "₹", "Book Value": "₹", "Dividend Yield": "%", "ROCE": "%",
         "ROE": "%", "Sales Growth": "% YoY", "Profit Growth": "% YoY", "OPM": "%", "Face Value": "₹",
         "Guidance Score": "%", "Guidance Checked": "count", "EPS": "₹", "1Y Return": "%"}
# Known for every listed company (from the exchange and Yahoo); the rest only
# for companies the app has looked into.
MARKET_FIELDS = {"Market Cap", "Current Price", "P/E", "Price to Book", "Book Value", "Dividend Yield", "EPS",
                 "1Y Return"}
ALIASES = sorted(((alias, field) for field, names in FIELDS.items() for alias in names), key=lambda a: -len(a[0]))
PRESETS = {
    "Cheap and profitable": "P/E > 0 AND P/E < 15 AND Market Cap > 1000",
    "Big and cheap on book": "Price to Book < 1.5 AND Market Cap > 20000",
    "High dividend": "Dividend Yield > 3 AND Market Cap > 1000",
    "Strong past year": "1Y Return > 50 AND Market Cap > 5000",
    "Quality at a fair price": "ROCE > 20 AND ROE > 15 AND P/E < 30",
    "Growing fast": "Sales Growth > 20 AND Profit Growth > 20",
    "Dividend payers": "Dividend Yield > 2 AND ROE > 12",
    "Cheap on book": "Price to Book < 1.5 AND ROE > 10",
    "AI likes it": "Verdict = Positive",
    # Three graded promises is the floor: one kept promise is not a record.
    "Management keeps its word": "Guidance Score > 60 AND Guidance Checked >= 3",
}
_CMP = re.compile(r"^\s*(?P<field>.+?)\s*(?P<op>>=|<=|!=|=|>|<)\s*(?P<value>.+?)\s*$")


def _num(value) -> float | None:
    return core._to_number(str(value or "").replace("₹", "").replace("Cr.", "").replace("%", "").strip())


def _yoy(qdf_json, prefix: str) -> float | None:
    """Latest quarter against the same quarter a year earlier, in %."""
    qdf = core._df_from_json(qdf_json)
    if qdf is None or qdf.shape[1] < 6:
        return None
    row = qdf[qdf.iloc[:, 0].astype(str).str.strip().str.lower().str.startswith(prefix)]
    if row.empty:
        return None
    now, then = _num(row.iloc[0, -1]), _num(row.iloc[0, -5])
    return (now / then - 1) * 100 if now is not None and then not in (None, 0) and then > 0 else None


def guidance_scores() -> dict[str, tuple[int, float]]:
    """ticker -> (commitments graded, % of them delivered).

    Only graded claims count: an ungraded promise is not evidence either way,
    and counting it as a miss would punish a company for the app not having
    got round to checking it yet.
    """
    rows = pf._q("SELECT ticker, COUNT(*) AS graded, "
                 "SUM(CASE WHEN status = 'Delivered' THEN 1 ELSE 0 END) AS kept "
                 "FROM guidance WHERE status IS NOT NULL AND status != '' AND status != 'Unclear' "
                 "GROUP BY ticker")
    return {r["ticker"]: (r["graded"], (r["kept"] or 0) / r["graded"] * 100)
            for r in rows if r["graded"]}


@st.cache_data(ttl=600, max_entries=4, show_spinner=False)
def universe(version_key: int) -> pd.DataFrame:
    """One row per analysed company, from its newest saved analysis."""
    rows = pf._q("SELECT r.ticker, json_extract(r.snapshot, '$.company_name') AS name, "
                 "json_extract(r.snapshot, '$.metrics') AS metrics, json_extract(r.snapshot, '$.sector') AS sector, "
                 "json_extract(r.snapshot, '$.ai_verdict') AS verdict, json_extract(r.snapshot, '$.quarterly_df') AS q, "
                 "r.ts FROM runs r JOIN (SELECT ticker, MAX(ts) AS ts FROM runs GROUP BY ticker) m "
                 "ON r.ticker = m.ticker AND r.ts = m.ts")
    promises = guidance_scores()
    out = []
    for r in rows:
        m = json.loads(r["metrics"] or "{}")
        q = json.loads(r["q"]) if r["q"] else None
        price, book = _num(m.get("Current Price")), _num(m.get("Book Value"))
        opm = None
        qdf = core._df_from_json(q)
        if qdf is not None:
            hit = qdf[qdf.iloc[:, 0].astype(str).str.upper().str.startswith("OPM")]
            opm = _num(hit.iloc[0, -1]) if not hit.empty else None
        out.append({"Ticker": r["ticker"], "Company": r["name"] or r["ticker"],
                    "Market Cap": _num(m.get("Market Cap")), "Current Price": price,
                    "P/E": _num(m.get("Stock P/E")), "Price to Book": price / book if price and book else None,
                    "Book Value": book, "Dividend Yield": _num(m.get("Dividend Yield")),
                    "ROCE": _num(m.get("ROCE")), "ROE": _num(m.get("ROE")),
                    "Sales Growth": _yoy(q, "sales") or _yoy(q, "revenue"), "Profit Growth": _yoy(q, "net profit"),
                    "OPM": opm, "Face Value": _num(m.get("Face Value")),
                    "Sector": r["sector"] or "", "Verdict": r["verdict"] or "",
                    "Guidance Checked": promises.get(r["ticker"], (None, None))[0],
                    "Guidance Score": promises.get(r["ticker"], (None, None))[1],
                    "Analysed": pd.to_datetime(r["ts"], unit="s")})
    return pd.DataFrame(out)


NSE_LISTS = {False: "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
             True: "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv"}


@st.cache_data(ttl=86400, show_spinner=False)
def listed(day: str) -> pd.DataFrame:
    """Every company listed on the NSE: Ticker, Company, SME. Two small files a day.
    ponytail: NSE only -- BSE blocks automated downloads of its full list, so a
    BSE-only company is found through the live search fallback instead."""
    frames = []
    for sme, url in NSE_LISTS.items():
        try:
            resp = core.SESSION.get(url, headers=core._headers(), timeout=core.REQUEST_TIMEOUT)
            df = pd.read_csv(io.StringIO(resp.text)) if resp.ok else None
        except Exception:
            df = None
        if df is None or df.empty:
            continue
        df.columns = [c.strip().upper().replace(" ", "_") for c in df.columns]
        frames.append(pd.DataFrame({"Ticker": df["SYMBOL"].astype(str).str.strip(),
                                    "Company": df["NAME_OF_COMPANY"].astype(str).str.strip(), "SME": sme}))
    if not frames:
        return pd.DataFrame(columns=["Ticker", "Company", "SME"])
    return pd.concat(frames, ignore_index=True).drop_duplicates("Ticker")


@st.cache_data(ttl=86400, show_spinner=False)
def market(day: str) -> pd.DataFrame:
    """Price and valuation for every NSE listing Yahoo covers, 250 companies per request."""
    import yfinance as yf
    from yfinance import EquityQuery as Q
    query, rows, offset, total = Q("and", [Q("eq", ["region", "in"]), Q("eq", ["exchange", "NSI"])]), [], 0, 1
    while offset < min(total, 6000):
        try:
            page = yf.screen(query, size=250, offset=offset, sortField="intradaymarketcap", sortAsc=False)
        except Exception:
            break
        quotes, total = page.get("quotes") or [], page.get("total") or 0
        if not quotes:
            break
        # Yahoo writes an NSE SME symbol as NAME-SM.NS; the exchange list has plain NAME.
        rows += [{"Ticker": re.sub(r"-SM$", "", q["symbol"].rsplit(".", 1)[0]), "Market Cap": (q.get("marketCap") or 0) / 1e7 or None,
                  "Current Price": q.get("regularMarketPrice"), "P/E": q.get("trailingPE"),
                  "Price to Book": q.get("priceToBook"), "Book Value": q.get("bookValue"),
                  "Dividend Yield": q.get("dividendYield"), "EPS": q.get("epsTrailingTwelveMonths"),
                  "1Y Return": q.get("fiftyTwoWeekChangePercent")} for q in quotes]
        offset += 250
    return pd.DataFrame(rows).drop_duplicates("Ticker") if rows else pd.DataFrame(columns=["Ticker"])


def everything(version_key: int, day: str, include_sme: bool) -> pd.DataFrame:
    """Every listed company: the exchange's list, Yahoo's market figures, and --
    where the app has looked into a company -- everything its own analysis knows."""
    base = listed(day)
    if not include_sme:
        base = base[~base["SME"]]
    df = base.merge(market(day), on="Ticker", how="left")
    mine = universe(version_key)
    if not mine.empty:
        df = df.merge(mine.drop(columns=["Company"]), on="Ticker", how="outer", suffixes=("", "_own"))
        for col in [c for c in df.columns if c.endswith("_own")]:  # today's market figure first, the analysis's as a fallback
            df[col[:-4]] = df[col[:-4]].fillna(df[col])
            df = df.drop(columns=col)
        df["Company"] = df["Company"].fillna(df["Ticker"].map(dict(zip(mine["Ticker"], mine["Company"]))))
        df["SME"] = df["SME"].fillna(False)
    for col in [*FIELDS, "Analysed"]:  # a field no company has yet is still a field you can search on
        if col not in df:
            df[col] = None
    for col in set(FIELDS) - TEXT_FIELDS:  # real numbers, so a missing one shows as blank, not "None"
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df[list(TEXT_FIELDS)] = df[list(TEXT_FIELDS)].fillna("")
    return df


def version() -> int:
    """Changes whenever anything the screener reads changes -- a new analysis
    or a newly graded promise, which does not create a run of its own."""
    return pf._q("SELECT (SELECT COALESCE(MAX(id), 0) FROM runs) + "
                 "(SELECT COUNT(*) FROM guidance WHERE status IS NOT NULL) AS v")[0]["v"]


def _field(text: str) -> str | None:
    t = text.strip().lower()
    return next((field for alias, field in ALIASES if t == alias), None)


def parse(query: str):
    """'A > 1 AND B < 2 OR C = x' -> [[(field, op, value), ...], ...]: OR of
    AND-groups, like screener.in. Raises ValueError with a helpful message."""
    query = " ".join(query.replace("\n", " ").split())
    if not query:
        raise ValueError("Write a query, e.g. ROE > 15 AND P/E < 25")
    groups = []
    for part in re.split(r"\s+OR\s+", query, flags=re.I):
        group = []
        for cond in re.split(r"\s+AND\s+", part, flags=re.I):
            m = _CMP.match(cond)
            if not m:
                raise ValueError(f"Could not read “{cond}”. Use a field, a comparison (> < >= <= = !=) and a value.")
            field = _field(m["field"])
            if not field:
                raise ValueError(f"Unknown field “{m['field']}”. See the list of fields.")
            raw = m["value"].strip().strip("\"'")
            if field in TEXT_FIELDS:
                if m["op"] not in ("=", "!="):
                    raise ValueError(f"{field} can only be compared with = or !=.")
                group.append((field, m["op"], raw))
            else:
                value = _num(raw)
                if value is None:
                    raise ValueError(f"“{raw}” is not a number (for {field}).")
                group.append((field, m["op"], value))
        groups.append(group)
    return groups


def run(df: pd.DataFrame, groups) -> pd.DataFrame:
    ops = {">": pd.Series.gt, "<": pd.Series.lt, ">=": pd.Series.ge, "<=": pd.Series.le}
    keep = pd.Series(False, index=df.index)
    for group in groups:
        mask = pd.Series(True, index=df.index)
        for field, op, value in group:
            col = df[field] if field in TEXT_FIELDS else pd.to_numeric(df[field], errors="coerce")
            if field in TEXT_FIELDS:
                hit = col.fillna("").astype(str).str.lower().str.contains(value.lower(), regex=False)
                mask &= hit if op == "=" else ~hit
            elif op in ops:
                mask &= ops[op](col, value).fillna(False)
            else:
                mask &= (col == value) if op == "=" else (col != value)
        keep |= mask
    return df[keep]


def fields_used(groups) -> list[str]:
    return list(dict.fromkeys(f for g in groups for f, _, _ in g))


def saved() -> dict[str, str]:
    return {r["name"]: r["query"] for r in pf._q("SELECT * FROM screens ORDER BY name")}


def save(name: str, query: str) -> None:
    pf._q("INSERT INTO screens (name, query) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET query=excluded.query",
          (name.strip(), query))


def delete(name: str) -> None:
    pf._q("DELETE FROM screens WHERE name=?", (name,))


if __name__ == "__main__":
    df = pd.DataFrame({"Ticker": ["A", "B", "C"], "ROE": [20, 10, None], "P/E": [15, 40, 12],
                       "Sector": ["IT", "Banks", "IT"], "Verdict": ["Positive", "", "Neutral"]})
    g = parse("Return on equity > 15 AND price to earning < 25 OR sector = banks")
    assert g == [[("ROE", ">", 15.0), ("P/E", "<", 25.0)], [("Sector", "=", "banks")]], g
    assert list(run(df, g)["Ticker"]) == ["A", "B"]
    for bad in ("ROE >", "Foo > 1", "ROE > abc", "Sector > 3"):
        try:
            parse(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    print("ok")
