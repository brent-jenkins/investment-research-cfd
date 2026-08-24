"""A FRED stub whose behaviour mirrors the real API's vintage semantics.

The payloads below are transcribed from live responses (see the engineering
note), so the offline suite exercises the same shapes production hits:

* ``latest``          -- newest revision, ``realtime_start`` stamped *today*.
* ``vintage``         -- value as it stood on a given real-time date.
* ``initial_release`` -- first print, ``realtime_start`` = true publish date.

GDPC1 2019Q4 is the canonical example: **19219.767** when first published on
2020-01-30, **20985.448** after later rebasing. A backtest that reads the
latter in 2020 is using a number that did not exist yet.
"""

from __future__ import annotations

from typing import Any

# The date the "latest" payloads pretend to have been fetched on.
INGEST_DATE = "2026-08-24"
FRED_OPEN_ENDED = "9999-12-31"

#: Real GDPC1 first prints (output_type=4). date -> (value, publication date).
GDPC1_INITIAL_RELEASE: dict[str, tuple[str, str]] = {
    "2019-10-01": ("19219.767", "2020-01-30"),
    "2020-01-01": ("18987.877", "2020-04-29"),
    "2020-04-01": ("17205.822", "2020-07-30"),
}

#: The same observations after years of revision (latest mode).
GDPC1_LATEST: dict[str, str] = {
    "2019-10-01": "20985.448",
    "2020-01-01": "20674.376",
    "2020-04-01": "19023.169",
}

#: GDPC1 as ALFRED reported it on 2020-04-01 (a real vintage snapshot).
GDPC1_VINTAGE_2020_04_01: dict[str, str] = {"2019-10-01": "19221.97"}

#: UNRATE first prints, showing a ~5 week publication lag.
UNRATE_INITIAL_RELEASE: dict[str, tuple[str, str]] = {
    "2020-01-01": ("3.6", "2020-02-07"),
    "2020-02-01": ("3.5", "2020-03-06"),
    "2020-03-01": ("4.4", "2020-04-03"),
    "2020-04-01": ("14.7", "2020-05-08"),
}

#: DGS10 latest, including FRED's "." missing-value sentinel on a holiday.
DGS10_LATEST: dict[str, str] = {
    "2020-01-01": ".",      # New Year's Day: market shut, no print
    "2020-01-02": "1.88",
    "2020-01-03": "1.80",
    "2020-01-06": "1.81",
}

SERIES_METADATA: dict[str, dict[str, str]] = {
    "GDPC1": {
        "id": "GDPC1",
        "title": "Real Gross Domestic Product",
        "units": "Billions of Chained 2017 Dollars",
        "frequency": "Quarterly",
        "last_updated": "2026-07-30 07:51:02-05",
    },
    "UNRATE": {
        "id": "UNRATE",
        "title": "Unemployment Rate",
        "units": "Percent",
        "frequency": "Monthly",
        "last_updated": "2026-08-01 07:45:01-05",
    },
    "DGS10": {
        "id": "DGS10",
        "title": "Market Yield on U.S. Treasury Securities at 10-Year "
                 "Constant Maturity",
        "units": "Percent",
        "frequency": "Daily",
        "last_updated": "2026-08-21 15:16:35-05",
    },
}


class StubResponse:
    """Minimal stand-in for :class:`requests.Response`."""

    def __init__(self, payload: Any, status_code: int = 200, reason: str = "OK"):
        self._payload = payload
        self.status_code = status_code
        self.reason = reason
        self.text = str(payload)

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FredStub:
    """Vintage-aware fake FRED session.

    Dispatches exactly as the real API does -- on ``output_type`` and the
    presence of ``realtime_start`` -- so a test that passes here is testing
    the same branch production takes.
    """

    def __init__(self, *, status_code: int = 200, fail_metadata: bool = False):
        self.status_code = status_code
        self.fail_metadata = fail_metadata
        self.calls: list[dict[str, Any]] = []

    # -- introspection helpers used by tests ------------------------------ #
    @property
    def observation_calls(self) -> list[dict[str, Any]]:
        """Params of every ``/series/observations`` request made."""
        return [c["params"] for c in self.calls if c["url"].endswith("/observations")]

    @property
    def metadata_calls(self) -> list[dict[str, Any]]:
        """Params of every ``/series`` metadata request made."""
        return [c["params"] for c in self.calls if c["url"].endswith("/series")]

    # -- session interface ------------------------------------------------- #
    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {})})

        if self.status_code != 200:
            return StubResponse(
                {"error_message": "stubbed failure"}, self.status_code, "Error"
            )

        if url.endswith("/series"):
            if self.fail_metadata:
                return StubResponse({"error_message": "no"}, 500, "Server Error")
            series_id = (params or {}).get("series_id", "")
            entry = SERIES_METADATA.get(series_id)
            return StubResponse({"seriess": [entry] if entry else []})

        return StubResponse({"observations": self._observations(params or {})})

    # -- payload selection -------------------------------------------------- #
    def _observations(self, params: dict[str, Any]) -> list[dict[str, str]]:
        series_id = params.get("series_id", "")
        output_type = str(params.get("output_type", "1"))
        realtime_start = params.get("realtime_start")

        if output_type == "4":
            return self._initial_release(series_id)
        if realtime_start and realtime_start != "1776-07-04":
            return self._vintage(series_id, realtime_start)
        return self._latest(series_id)

    @staticmethod
    def _initial_release(series_id: str) -> list[dict[str, str]]:
        table = {
            "GDPC1": GDPC1_INITIAL_RELEASE,
            "UNRATE": UNRATE_INITIAL_RELEASE,
        }.get(series_id, {})
        return [
            {
                "realtime_start": published,
                "realtime_end": FRED_OPEN_ENDED,
                "date": date,
                "value": value,
            }
            for date, (value, published) in sorted(table.items())
        ]

    @staticmethod
    def _vintage(series_id: str, as_of: str) -> list[dict[str, str]]:
        if series_id != "GDPC1":
            return []
        return [
            {
                "realtime_start": as_of,
                "realtime_end": as_of,
                "date": date,
                "value": value,
            }
            for date, value in sorted(GDPC1_VINTAGE_2020_04_01.items())
        ]

    @staticmethod
    def _latest(series_id: str) -> list[dict[str, str]]:
        table = {
            "GDPC1": GDPC1_LATEST,
            "DGS10": DGS10_LATEST,
            "UNRATE": {d: v for d, (v, _) in UNRATE_INITIAL_RELEASE.items()},
        }.get(series_id, {})
        # Latest mode stamps every row with the fetch date -- the signature of
        # a revised file, and what makes the look-ahead bias detectable.
        return [
            {
                "realtime_start": INGEST_DATE,
                "realtime_end": INGEST_DATE,
                "date": date,
                "value": value,
            }
            for date, value in sorted(table.items())
        ]

    def close(self) -> None:
        pass
