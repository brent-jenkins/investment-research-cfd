"""Read-only Streamlit dashboard for the cfdinvest system.

**This package never writes, trades or mutates anything.** Every read goes
through :mod:`cfdinvest.ui.sources`, which has no write path; the pages
render what it returns. Nothing here calls a broker, a vendor or any other
external service.

Run it with::

    streamlit run cfdinvest/ui/app.py

See ``cfdinvest/ui/README.md`` for local development and for deploying
behind Caddy.
"""

from __future__ import annotations

__all__ = ["configview", "health", "sources", "theme"]
