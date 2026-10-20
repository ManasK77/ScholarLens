"""API-test fixtures.

`/collect` ends with the full-text step (D-FULLTEXT-01), which tries every
legitimate provider for every paper still without a PDF. In these tests the
network is disabled (tests/conftest.py); left alone, the arXiv provider would
then wait out its production retry backoff for real. `_offline_providers`
makes every provider answer at once, as an unreachable network would, so a
route test that is not about full text is unaffected and fast. A test about
full text patches the providers it needs on top of this.
"""

import pytest

from app.ingestion import fulltext, unpaywall
from app.ingestion.pdf_store import DownloadResult
from app.ingestion.sources import arxiv
from app.ingestion.unpaywall import OaLookup


@pytest.fixture(autouse=True)
def _offline_providers(monkeypatch):
    monkeypatch.setattr(
        arxiv,
        "download_pdf_result",
        lambda url, dest_path, **kw: DownloadResult("network_error", detail="offline"),
    )
    monkeypatch.setattr(
        fulltext,
        "safe_download",
        lambda url, dest_path, **kw: DownloadResult("network_error", detail="offline"),
    )
    monkeypatch.setattr(
        unpaywall, "lookup_oa_pdf", lambda doi, email: OaLookup("network_error")
    )


@pytest.fixture(autouse=True)
def _fresh_auth_throttle():
    """Failed-auth counts are process-wide; no test inherits another's."""
    from app.api.security import auth_throttle

    auth_throttle.reset()
    yield
    auth_throttle.reset()
