"""The SEC contact address comes from the environment, never the repository.

Mutations checked:

  M2160 a hard-coded fallback address used when the variable is unset
  M2161 the contact dropped from the header when it is set
"""
from __future__ import annotations

import pytest

from cfdinvest.ingestion import filings


def test_no_contact_refuses_rather_than_sending_anonymously(monkeypatch):
    """M2160."""
    monkeypatch.delenv(filings.CONTACT_ENV, raising=False)
    with pytest.raises(RuntimeError, match=filings.CONTACT_ENV):
        filings.user_agent()


def test_the_configured_contact_is_sent(monkeypatch):
    """M2161."""
    monkeypatch.setenv(filings.CONTACT_ENV, "ops@example.org")
    assert filings.user_agent().endswith("ops@example.org")
