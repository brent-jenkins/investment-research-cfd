"""The palette, in both modes, and the CSS that applies it.

**Two palettes, not one.** The dark palette is taken from an existing site. The light
palette is not the same five colours on a white background — it cannot be:
``#66FCF1`` on white is roughly 1.3:1 contrast, which is unreadable. The
light mode keeps the *hue* and drops the luminance until the text passes,
which is why ``accent`` differs between the two sets while ``accent2`` does
not need to.

The active mode comes from Streamlit itself (:func:`active_mode`), so the
browser's light/dark toggle and the ``theme.base`` config option both work
without this module being told twice.

Pure: :func:`stylesheet` builds a string and :func:`apply` hands it to
Streamlit. Neither reads anything or touches disk.
"""

from __future__ import annotations

__all__ = [
    "PALETTES",
    "PALETTE",
    "SEMANTIC",
    "MODES",
    "active_mode",
    "palette_for",
    "stylesheet",
    "apply",
    "swatch",
    "colour_for",
]

MODES: tuple[str, ...] = ("light", "dark")

#: The two palettes. ``dark`` is that site's set verbatim.
PALETTES: dict[str, dict[str, str]] = {
    "dark": {
        "background": "#0B0C10",
        "panel": "#1F2833",
        "text": "#C5C6C7",
        "accent": "#66FCF1",
        "accent2": "#45A29E",
        "border": "rgba(102, 252, 241, 0.14)",
        "rule": "rgba(197, 198, 199, 0.15)",
        "subtle": "#8A9BA8",
    },
    "light": {
        "background": "#FFFFFF",
        "panel": "#F3F5F6",
        # The dark palette's panel colour, promoted to text: the same brand
        # navy, now carrying the contrast instead of the background.
        "text": "#1F2833",
        # #66FCF1 fails badly on white, so light mode keeps the hue and
        # drops the luminance until it passes.
        "accent": "#0E7C77",
        "accent2": "#45A29E",
        "border": "rgba(31, 40, 51, 0.12)",
        "rule": "rgba(31, 40, 51, 0.10)",
        "subtle": "#5B6B78",
    },
}

#: Backwards-compatible alias: the dark palette, which is the brand one.
PALETTE: dict[str, str] = PALETTES["dark"]

#: Status colours per mode, kept separate from the brand palette.
#:
#: Red and amber are deliberately *not* drawn from the five brand colours: a
#: warning that shares a hue with the accent is a warning nobody sees.
SEMANTIC_BY_MODE: dict[str, dict[str, str]] = {
    "dark": {
        "critical": "#FF4B4B",
        "warning": "#F5A623",
        "ok": "#45A29E",
        "muted": "#8A9BA8",
        "long": "#66FCF1",
        "short": "#F5A623",
        "flat": "#8A9BA8",
    },
    "light": {
        "critical": "#C62828",
        "warning": "#B26A00",
        "ok": "#0E7C77",
        "muted": "#5B6B78",
        "long": "#0E7C77",
        "short": "#B26A00",
        "flat": "#5B6B78",
    },
}

#: Backwards-compatible alias.
SEMANTIC: dict[str, str] = SEMANTIC_BY_MODE["dark"]


def active_mode(st=None) -> str:
    """Which mode Streamlit is rendering in.

    Asks the runtime first — that reflects the viewer's own light/dark
    toggle — then the ``theme.base`` config option, then falls back to
    ``"light"``. Outside a browser (tests, bare mode) both are ``None``, and
    a fallback is better than guessing dark and blinding somebody.
    """
    if st is None:
        return "light"

    try:
        reported = getattr(getattr(st, "context", None), "theme", None)
        kind = getattr(reported, "type", None)
        if kind in MODES:
            return kind
    except Exception:  # noqa: BLE001 - never fail to draw over a theme probe
        pass

    try:
        base = st.get_option("theme.base")
        if base in MODES:
            return base
    except Exception:  # noqa: BLE001
        pass

    return "light"


def palette_for(mode: str) -> dict[str, str]:
    """The palette for a mode, defaulting to light for anything unknown."""
    return PALETTES.get(mode, PALETTES["light"])


def semantic_for(mode: str) -> dict[str, str]:
    """The status colours for a mode."""
    return SEMANTIC_BY_MODE.get(mode, SEMANTIC_BY_MODE["light"])


def colour_for(state: str, mode: str = "dark") -> str:
    """The colour for a status word, falling back to muted.

    Pure. Unknown states render muted rather than raising: a page should not
    fail to draw because a new status string appeared upstream.
    """
    colours = semantic_for(mode)
    return colours.get(str(state).lower(), colours["muted"])


def swatch(colour: str, label: str, mode: str = "dark") -> str:
    """An inline coloured dot and label, as HTML.

    Pure. Used for direction and status cells, where a word alone is hard to
    scan down a column.
    """
    text = palette_for(mode)["text"]
    return (
        f'<span style="color:{colour};font-weight:700;">&#9679;</span> '
        f'<span style="color:{text};">{label}</span>'
    )


def stylesheet(mode: str = "dark") -> str:
    """The full stylesheet for one mode, as a ``<style>`` block.

    Pure — the same string for the same mode, which is what makes it
    testable.

    Only the app's *own* furniture is styled here: banners, notes, badges,
    metric cards and table chrome. Streamlit's native theming handles the
    rest, so a viewer switching modes in the browser menu gets a coherent
    page rather than half of one.
    """
    p = palette_for(mode)
    s = semantic_for(mode)
    critical_wash = "rgba(255, 75, 75, 0.12)" if mode == "dark" else "rgba(198, 40, 40, 0.07)"
    ok_wash = "rgba(69, 162, 158, 0.10)" if mode == "dark" else "rgba(14, 124, 119, 0.07)"
    banner_text = "#FFD7D7" if mode == "dark" else "#7F1D1D"

    return f"""
<style>
  :root {{
    --cfd-bg: {p["background"]};
    --cfd-panel: {p["panel"]};
    --cfd-text: {p["text"]};
    --cfd-accent: {p["accent"]};
    --cfd-accent2: {p["accent2"]};
    --cfd-border: {p["border"]};
    --cfd-rule: {p["rule"]};
    --cfd-subtle: {p["subtle"]};
    --cfd-critical: {s["critical"]};
    --cfd-warning: {s["warning"]};
  }}

  /* Use the width. A dashboard read on a wide monitor should not render in
     a 46rem column with the numbers stacked. */
  .stMainBlockContainer {{
    max-width: 1600px;
    padding-top: 2.2rem;
    padding-bottom: 3rem;
  }}

  h1 {{ color: var(--cfd-accent); font-weight: 700; letter-spacing: -0.01em; }}
  h2, h3 {{ color: var(--cfd-accent); font-weight: 600; }}
  h2 {{ margin-top: 0.4rem; }}
  a, a:visited {{ color: var(--cfd-accent2); }}
  a:hover {{ color: var(--cfd-accent); }}
  hr {{ border-color: var(--cfd-rule); }}

  [data-testid="stSidebar"] {{ border-right: 1px solid var(--cfd-border); }}

  /* Metric cards */
  [data-testid="stMetric"] {{
    background-color: var(--cfd-panel);
    border: 1px solid var(--cfd-border);
    border-radius: 10px;
    padding: 12px 16px;
  }}
  [data-testid="stMetricLabel"] p {{
    color: var(--cfd-subtle) !important;
    font-size: 0.74rem;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    font-weight: 600;
  }}
  [data-testid="stMetricValue"] {{
    color: var(--cfd-accent) !important;
    font-variant-numeric: tabular-nums;
  }}

  /* Tables: tabular figures so columns of numbers line up on the decimal. */
  [data-testid="stDataFrame"] {{
    border: 1px solid var(--cfd-border);
    border-radius: 10px;
  }}
  [data-testid="stDataFrame"] * {{ font-variant-numeric: tabular-nums; }}

  [data-testid="stExpander"] {{
    background-color: var(--cfd-panel);
    border: 1px solid var(--cfd-border);
    border-radius: 10px;
  }}
  [data-testid="stExpander"] summary {{ color: var(--cfd-accent2); }}

  .stTabs [data-baseweb="tab-list"] {{ gap: 4px; }}
  .stTabs [aria-selected="true"] {{ color: var(--cfd-accent) !important; }}

  /* The critical banner. Deliberately loud, deliberately off-brand. */
  .cfd-banner {{
    background-color: {critical_wash};
    border: 1px solid var(--cfd-critical);
    border-left: 5px solid var(--cfd-critical);
    border-radius: 8px;
    padding: 14px 18px;
    margin-bottom: 16px;
    color: {banner_text};
    font-weight: 600;
  }}
  .cfd-banner ul {{ margin: 8px 0 0 18px; font-weight: 400; }}

  .cfd-ok {{
    background-color: {ok_wash};
    border-left: 4px solid var(--cfd-accent2);
    border-radius: 8px;
    padding: 9px 16px;
    margin-bottom: 16px;
    color: var(--cfd-text);
    font-size: 0.9rem;
  }}

  /* Read-only badge: this app never places an order. */
  .cfd-readonly {{
    display: inline-block;
    border: 1px solid var(--cfd-accent2);
    border-radius: 999px;
    padding: 2px 12px;
    font-size: 0.7rem;
    letter-spacing: 0.09em;
    text-transform: uppercase;
    font-weight: 600;
    color: var(--cfd-accent2);
  }}

  .cfd-note {{
    color: var(--cfd-subtle);
    font-size: 0.85rem;
    line-height: 1.55;
    border-left: 2px solid var(--cfd-border);
    padding-left: 12px;
    margin: 6px 0 14px 0;
  }}

  /* Compact key/value rows, for facts that do not deserve a metric card. */
  .cfd-kv {{
    display: flex;
    justify-content: space-between;
    gap: 16px;
    padding: 7px 0;
    border-bottom: 1px solid var(--cfd-rule);
    font-size: 0.9rem;
  }}
  .cfd-kv:last-child {{ border-bottom: none; }}
  .cfd-kv .k {{ color: var(--cfd-subtle); }}
  .cfd-kv .v {{
    color: var(--cfd-text);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    text-align: right;
  }}
</style>
"""


def apply(st) -> str:
    """Inject the stylesheet for the active mode. Returns the mode.

    Takes the ``streamlit`` module as an argument rather than importing it,
    so the palette and stylesheet stay importable — and testable — without a
    Streamlit runtime.
    """
    mode = active_mode(st)
    st.markdown(stylesheet(mode), unsafe_allow_html=True)
    return mode
