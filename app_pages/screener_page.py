"""Screener: filter every analysed company with a screener.in-style query."""

import streamlit as st

import screener as sc

st.markdown("### :material/filter_alt: Screener")
df = sc.universe(sc.version())
if df.empty:
    st.info("The screener works on companies the app has analysed. Add stocks to a portfolio or run the "
            "Research page first.", icon=":material/info:")
    st.stop()

ss = st.session_state
ss.setdefault("screen_query", sc.PRESETS["Quality at a fair price"])
saved = sc.saved()

left, right = st.columns([3, 1.2], gap="large")
with right:
    st.markdown("**Ready-made screens**")
    preset = st.pills("Presets", list(sc.PRESETS) + list(saved), label_visibility="collapsed", key="screen_preset")
    if preset and ss.get("_last_preset") != preset:
        ss.screen_query = sc.PRESETS.get(preset) or saved.get(preset, "")
        ss._last_preset = preset
    with st.expander("Fields you can use", icon=":material/list:"):
        st.markdown("\n".join(f"- **{f}**" + (f" :gray[({sc.UNITS[f]})]" if f in sc.UNITS else "")
                              + f"  \n  :gray[also: {', '.join(n for n in names[:2])}]" for f, names in sc.FIELDS.items()))
with left:
    query = st.text_area("Query", key="screen_query", height=110, label_visibility="collapsed",
                         placeholder="Market Capitalization > 5000 AND Return on equity > 15")
    st.caption("Write conditions like **ROE > 15**, join them with **AND** / **OR**. Numbers are in the units "
               "shown in the field list; Sector and Verdict take text, e.g. **Sector = Banks**.")
    with st.container(horizontal=True):
        st.button("Run this query", type="primary", icon=":material/play_arrow:")
        with st.popover("Save screen", icon=":material/bookmark_add:"):
            name = st.text_input("Name", placeholder="e.g. Cheap compounders")
            if st.button("Save", disabled=not name.strip()):
                sc.save(name, query)
                st.rerun()
        if preset in saved and st.button("Delete saved", icon=":material/delete:", type="tertiary"):
            sc.delete(preset)
            st.rerun()

try:
    groups = sc.parse(query)
except ValueError as exc:
    st.error(str(exc), icon=":material/error:")
    st.stop()

found = sc.run(df, groups)
shown = ["Ticker", "Company", *[f for f in sc.fields_used(groups) if f not in ("Ticker", "Company")]]
for extra in ("Current Price", "Market Cap", "P/E", "ROCE", "ROE", "Verdict"):
    if extra not in shown and len(shown) < 9:
        shown.append(extra)
st.markdown(f"**{len(found)} result{'s' if len(found) != 1 else ''}** :gray[· out of {len(df)} analysed companies]")
fmt = {f: st.column_config.NumberColumn(f, format="%.1f" + ("%%" if "%" in sc.UNITS.get(f, "") else ""))
       for f in shown if f not in ("Ticker", "Company", "Sector", "Verdict")}
fmt["Market Cap"] = st.column_config.NumberColumn("Market Cap (₹ Cr)", format="%,.0f")
fmt["Current Price"] = st.column_config.NumberColumn("Price (₹)", format="%,.2f")
st.dataframe(found[shown].sort_values(shown[2] if len(shown) > 2 else "Ticker", ascending=False),
             hide_index=True, column_config=fmt, height=min(38 * (len(found) + 1) + 4, 600))
st.caption("Every figure is from each company's latest saved analysis (screener.in ratios; growth compares the "
           "latest quarter with a year earlier). Companies are added as you analyse them.")
