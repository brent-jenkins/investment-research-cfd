"""The dark palette, and the CSS that applies it.

Streamlit's own theming covers the chrome but not the widgets this app
leans on, so the palette is defined once here as data and injected as CSS.
Every colour the app uses comes from :data:`PALETTE`; nothing hard-codes a
hex value, so restyling is a one-place edit.

Pure: :func:`stylesheet` builds a string and :func:`apply` hands it to
Streamlit. Neither reads anything or touches disk.
"""

from __future__ import annotations

__all__ = [
    "PALETTE",
    "SEMANTIC",
    "stylesheet",
    "apply",
    "swatch",
    "colour_for",
]

#: The example.com palette.
PALETTE: dict[str, str] = {
    "background": "#0B0C10",
    "panel": "#1F2833",
    "text": "#C5C6C7",
    "accent": "#66FCF1",
    "accent2": "#45A29E",
}

#: Status colours, kept separate from the brand palette.
#:
#: Red and amber are deliberately *not* drawn from the five brand colours: a
#: warning that shares a hue with the accent is a warning nobody sees. These
#: are the only colours in the app that are allowed to shout.
SEMANTIC: dict[str, str] = {
    "critical": "#FF4B4B",
    "warning": "#F5A623",
    "ok": "#45A29E",
    "muted": "#6B7280",
    "long": "#66FCF1",
    "short": "#F5A623",
    "flat": "#6B7280",
}


def colour_for(state: str) -> str:
    """The colour for a status word, falling back to muted.

    Pure. Unknown states render muted rather than raising: a page should
    not fail to draw because a new status string appeared upstream.
    """
    return SEMANTIC.get(str(state).lower(), SEMANTIC["muted"])


def swatch(colour: str, label: str) -> str:
    """An inline coloured dot and label, as HTML.

    Pure. Used for direction and status cells, where a word alone is hard
    to scan down a column.
    """
    return (
        f'<span style="color:{colour};font-weight:600;">&#9679;</span> '
        f'<span style="color:{PALETTE["text"]};">{label}</span>'
    )


def stylesheet() -> str:
    """The full stylesheet, as a ``<style>`` block.

    Pure — same string every call, which is what makes it testable.
    """
    p = PALETTE
    return f"""
<style>
  :root {{
    --cfd-bg: {p["background"]};
    --cfd-panel: {p["panel"]};
    --cfd-text: {p["text"]};
    --cfd-accent: {p["accent"]};
    --cfd-accent2: {p["accent2"]};
    --cfd-critical: {SEMANTIC["critical"]};
    --cfd-warning: {SEMANTIC["warning"]};
    --cfd-muted: {SEMANTIC["muted"]};
  }}

  .stApp, [data-testid="stAppViewContainer"] {{
    background-color: var(--cfd-bg);
    color: var(--cfd-text);
  }}
  [data-testid="stHeader"] {{ background: transparent; }}
  [data-testid="stSidebar"] {{
    background-color: var(--cfd-panel);
    border-right: 1px solid rgba(102, 252, 241, 0.15);
  }}
  [data-testid="stSidebar"] * {{ color: var(--cfd-text); }}

  h1, h2, h3, h4 {{
    color: var(--cfd-accent);
    font-weight: 600;
    letter-spacing: 0.01em;
  }}
  a, a:visited {{ color: var(--cfd-accent2); }}
  a:hover {{ color: var(--cfd-accent); }}
  code, pre, .stCode {{
    background-color: var(--cfd-panel) !important;
    color: var(--cfd-text) !important;
  }}
  hr {{ border-color: rgba(197, 198, 199, 0.15); }}

  /* Metric cards */
  [data-testid="stMetric"] {{
    background-color: var(--cfd-panel);
    border: 1px solid rgba(102, 252, 241, 0.12);
    border-radius: 8px;
    padding: 12px 16px;
  }}
  [data-testid="stMetricLabel"] p {{
    color: var(--cfd-muted) !important;
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }}
  [data-testid="stMetricValue"] {{ color: var(--cfd-accent) !important; }}

  /* Tables and frames */
  [data-testid="stDataFrame"], [data-testid="stTable"] {{
    background-color: var(--cfd-panel);
    border-radius: 8px;
  }}
  [data-testid="stExpander"] {{
    background-color: var(--cfd-panel);
    border: 1px solid rgba(102, 252, 241, 0.12);
    border-radius: 8px;
  }}
  [data-testid="stExpander"] summary {{ color: var(--cfd-accent2); }}

  .stTabs [data-baseweb="tab-list"] {{ gap: 4px; }}
  .stTabs [data-baseweb="tab"] {{
    background-color: var(--cfd-panel);
    border-radius: 6px 6px 0 0;
    color: var(--cfd-text);
  }}
  .stTabs [aria-selected="true"] {{
    color: var(--cfd-bg) !important;
    background-color: var(--cfd-accent2);
  }}

  /* The critical banner. Deliberately loud, deliberately not brand-coloured. */
  .cfd-banner {{
    background-color: rgba(255, 75, 75, 0.12);
    border: 1px solid var(--cfd-critical);
    border-left: 5px solid var(--cfd-critical);
    border-radius: 6px;
    padding: 14px 18px;
    margin-bottom: 18px;
    color: #FFD7D7;
    font-weight: 600;
  }}
  .cfd-banner ul {{ margin: 8px 0 0 18px; font-weight: 400; }}

  .cfd-ok {{
    background-color: rgba(69, 162, 158, 0.10);
    border-left: 4px solid var(--cfd-accent2);
    border-radius: 6px;
    padding: 10px 16px;
    margin-bottom: 18px;
    color: var(--cfd-text);
  }}

  /* Read-only badge: this app never places an order. */
  .cfd-readonly {{
    display: inline-block;
    border: 1px solid var(--cfd-accent2);
    border-radius: 999px;
    padding: 2px 12px;
    font-size: 0.72rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--cfd-accent2);
  }}

  .cfd-note {{
    color: var(--cfd-muted);
    font-size: 0.86rem;
    border-left: 2px solid rgba(197, 198, 199, 0.2);
    padding-left: 12px;
    margin: 8px 0 16px 0;
  }}
</style>
"""


def apply(st) -> None:
    """Inject the stylesheet into a Streamlit page.

    Takes the ``streamlit`` module as an argument rather than importing it,
    so the palette and stylesheet stay importable — and testable — without
    a Streamlit runtime.
    """
    st.markdown(stylesheet(), unsafe_allow_html=True)
