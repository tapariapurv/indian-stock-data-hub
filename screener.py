"""
Screens and comparisons over every company the app has analysed.

Queries read like screener.in's -- "Market Capitalization > 500 AND
Return on equity > 15" -- and are parsed, never eval'd. The universe is each
company's newest saved analysis, pulled with SQL so the big stored tables
are never loaded.
"""

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
    "Sector": ["sector"],
    "Verdict": ["verdict", "ai verdict"],
}
TEXT_FIELDS = {"Sector", "Verdict"}
UNITS = {"Market Cap": "₹ Cr", "Current Price": "₹", "Book Value": "₹", "Dividend Yield": "%", "ROCE": "%",
         "ROE": "%", "Sales Growth": "% YoY", "Profit Growth": "% YoY", "OPM": "%", "Face Value": "₹"}
ALIASES = sorted(((alias, field) for field, names in FIELDS.items() for alias in names), key=lambda a: -len(a[0]))
PRESETS = {
    "Quality at a fair price": "ROCE > 20 AND ROE > 15 AND P/E < 30",
    "Growing fast": "Sales Growth > 20 AND Profit Growth > 20",
    "Dividend payers": "Dividend Yield > 2 AND ROE > 12",
    "Cheap on book": "Price to Book < 1.5 AND ROE > 10",
    "AI likes it": "Verdict = Positive",
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


@st.cache_data(ttl=600, max_entries=4, show_spinner=False)
def universe(_version: int) -> pd.DataFrame:
    """One row per analysed company, from its newest saved analysis."""
    rows = pf._q("SELECT r.ticker, json_extract(r.snapshot, '$.company_name') AS name, "
                 "json_extract(r.snapshot, '$.metrics') AS metrics, json_extract(r.snapshot, '$.sector') AS sector, "
                 "json_extract(r.snapshot, '$.ai_verdict') AS verdict, json_extract(r.snapshot, '$.quarterly_df') AS q, "
                 "r.ts FROM runs r JOIN (SELECT ticker, MAX(ts) AS ts FROM runs GROUP BY ticker) m "
                 "ON r.ticker = m.ticker AND r.ts = m.ts")
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
                    "Sector": r["sector"] or "", "Verdict": r["verdict"] or "", "Analysed": pd.to_datetime(r["ts"], unit="s")})
    return pd.DataFrame(out)


def version() -> int:
    return pf._q("SELECT COALESCE(MAX(id), 0) AS v FROM runs")[0]["v"]


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
            col = df[field]
            if field in TEXT_FIELDS:
                hit = col.str.lower().str.contains(value.lower(), regex=False)
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
