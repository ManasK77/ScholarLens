"""S1 full-text acquisition (D-FULLTEXT-01, 2026-10-12).

The 2026-10-12 audit found 10 of 20 papers metadata-only, six Unpaywall
"no OA copy", three "download failed" and one "DOI not found" -- with no way
to tell which paper had which outcome, and two legitimate routes never tried:
Semantic Scholar's own open-access link (dropped on the way into `papers`)
and the arXiv id a Semantic Scholar record carries. These tests pin the
provider order, the safety checks, PDF validation and the per-paper audit
trail. Nothing reaches the network: HTTP is faked and DNS is stubbed.
"""

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.ingestion import fulltext, unpaywall
from app.ingestion.collection_audit import FulltextAttempt
from app.ingestion.corpus import Base, Paper
from app.ingestion.pdf_store import DownloadResult
from app.ingestion.unpaywall import OaLookup

PDF = b"%PDF-1.7\nbody\n%%EOF"
PUBLIC = ["93.184.216.34"]


@pytest.fixture()
def dns(monkeypatch):
    """Host -> addresses; unknown hosts resolve to a public address."""
    table: dict[str, list[str]] = {}
    monkeypatch.setattr(fulltext, "resolve_host", lambda host: table.get(host, PUBLIC))
    return table


# ---------------------------------------------------------------------------
# URL safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("file:///etc/passwd", "scheme"),
        ("ftp://example.org/a.pdf", "scheme"),
        ("https://user:secret@example.org/a.pdf", "credentials_in_url"),
        ("https://example.org:8443/a.pdf", "port"),
        ("http://127.0.0.1/a.pdf", "non_public_address"),
        ("http://10.0.0.5/a.pdf", "non_public_address"),
        ("http://169.254.169.254/latest/meta-data", "non_public_address"),
        ("http://[::1]/a.pdf", "non_public_address"),
        ("http://192.168.1.10/a.pdf", "non_public_address"),
        ("https:///a.pdf", "no_host"),
    ],
)
def test_unsafe_urls_are_refused_before_any_request(dns, url, reason):
    assert fulltext.unsafe_url_reason(url) == reason


def test_a_name_resolving_to_a_private_address_is_refused(dns):
    dns["intranet.example"] = ["10.1.2.3"]
    dns["mixed.example"] = ["93.184.216.34", "127.0.0.1"]

    assert fulltext.unsafe_url_reason("https://intranet.example/a.pdf") == (
        "non_public_address"
    )
    assert fulltext.unsafe_url_reason("https://mixed.example/a.pdf") == (
        "non_public_address"
    )


def test_an_unresolvable_host_is_refused(monkeypatch):
    def fail(host):
        raise OSError("no such host")

    monkeypatch.setattr(fulltext, "resolve_host", fail)

    assert fulltext.unsafe_url_reason("https://nowhere.example/a.pdf") == (
        "unresolvable_host"
    )


def test_a_public_https_url_is_allowed(dns):
    assert fulltext.unsafe_url_reason("https://example.org/paper.pdf") is None
    assert fulltext.unsafe_url_reason("http://example.org:80/paper.pdf") is None


def test_the_recorded_location_drops_the_query_string():
    location = fulltext.safe_location("https://cdn.example.org/a/b.pdf?token=SECRET#x")

    assert location == "cdn.example.org/a/b.pdf"


# ---------------------------------------------------------------------------
# safe_download
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status=200, body=PDF, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_bytes(self):
        yield self._body


def _serve(monkeypatch, *answers):
    """httpx.stream answers, in order; an exception instance is raised."""
    calls = []

    def fake_stream(method, url, timeout=30.0, follow_redirects=True):
        assert follow_redirects is False  # redirects are followed by hand
        calls.append(url)
        answer = answers[len(calls) - 1]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(httpx, "stream", fake_stream)
    return calls


def test_a_pdf_is_stored(dns, monkeypatch, tmp_path):
    _serve(monkeypatch, _Resp(headers={"content-type": "application/pdf"}))
    dest = tmp_path / "1.pdf"

    result = fulltext.safe_download("https://example.org/p.pdf", str(dest))

    assert result.status == "pdf_stored"
    assert dest.read_bytes() == PDF


def test_an_html_landing_page_is_not_a_pdf(dns, monkeypatch, tmp_path):
    _serve(
        monkeypatch,
        _Resp(body=b"<html>landing</html>", headers={"content-type": "text/html"}),
    )
    dest = tmp_path / "1.pdf"

    result = fulltext.safe_download("https://doi.org/10.1/x", str(dest))

    assert result.status == "not_pdf"
    assert not dest.exists()


@pytest.mark.parametrize(
    ("status", "outcome", "retryable"),
    [
        (404, "http_error", False),
        (503, "http_error", True),
        (429, "rate_limited", True),
    ],
)
def test_http_failures_are_classified(
    dns, monkeypatch, tmp_path, status, outcome, retryable
):
    _serve(monkeypatch, _Resp(status=status))

    result = fulltext.safe_download(
        "https://example.org/p.pdf", str(tmp_path / "1.pdf")
    )

    assert (result.status, result.http_status, result.retryable) == (
        outcome,
        status,
        retryable,
    )


def test_a_timeout_and_a_network_error_are_distinguished(dns, monkeypatch, tmp_path):
    _serve(monkeypatch, httpx.ReadTimeout("slow"), httpx.ConnectError("refused"))
    dest = str(tmp_path / "1.pdf")

    assert fulltext.safe_download("https://example.org/a.pdf", dest).status == "timeout"
    assert fulltext.safe_download("https://example.org/a.pdf", dest).status == (
        "network_error"
    )


def test_retryable_failures_are_retried_a_bounded_number_of_times(
    dns, monkeypatch, tmp_path
):
    calls = _serve(monkeypatch, _Resp(status=503), _Resp(status=503), _Resp())
    sleeps = []

    result = fulltext.safe_download(
        "https://example.org/a.pdf",
        str(tmp_path / "1.pdf"),
        max_retries=1,
        sleep=sleeps.append,
    )

    assert result.status == "http_error"
    assert len(calls) == 2
    assert sleeps == [2.0]


def test_a_permanent_failure_is_not_retried(dns, monkeypatch, tmp_path):
    calls = _serve(monkeypatch, _Resp(status=404), _Resp())

    fulltext.safe_download(
        "https://example.org/a.pdf", str(tmp_path / "1.pdf"), max_retries=2
    )

    assert len(calls) == 1


def test_redirects_are_followed_and_rechecked(dns, monkeypatch, tmp_path):
    calls = _serve(
        monkeypatch,
        _Resp(status=302, headers={"location": "/files/p.pdf"}),
        _Resp(),
    )

    result = fulltext.safe_download("https://example.org/p", str(tmp_path / "1.pdf"))

    assert result.ok
    assert calls == ["https://example.org/p", "https://example.org/files/p.pdf"]


def test_a_redirect_into_a_private_network_is_refused(dns, monkeypatch, tmp_path):
    calls = _serve(
        monkeypatch,
        _Resp(status=302, headers={"location": "http://169.254.169.254/x"}),
    )

    result = fulltext.safe_download("https://example.org/p", str(tmp_path / "1.pdf"))

    assert result.status == "blocked_url"
    assert result.detail == "non_public_address"
    assert len(calls) == 1


def test_a_redirect_loop_ends(dns, monkeypatch, tmp_path):
    loop = [_Resp(status=302, headers={"location": "/again"}) for _ in range(10)]
    _serve(monkeypatch, *loop)

    result = fulltext.safe_download("https://example.org/p", str(tmp_path / "1.pdf"))

    assert result.status == "blocked_url"
    assert result.detail == "too_many_redirects"


def test_an_oversized_body_is_not_stored(dns, monkeypatch, tmp_path):
    _serve(monkeypatch, _Resp(body=PDF * 10))
    dest = tmp_path / "1.pdf"

    result = fulltext.safe_download(
        "https://example.org/p.pdf", str(dest), max_bytes=20
    )

    assert result.status == "too_large"
    assert list(tmp_path.iterdir()) == []


def test_a_failed_download_keeps_an_existing_good_file(dns, monkeypatch, tmp_path):
    dest = tmp_path / "1.pdf"
    dest.write_bytes(b"%PDF-1.7 the good copy")
    _serve(monkeypatch, _Resp(body=b"<html></html>"))

    fulltext.safe_download("https://example.org/p.pdf", str(dest))

    assert dest.read_bytes() == b"%PDF-1.7 the good copy"


def test_a_blocked_url_sends_no_request(monkeypatch, tmp_path):
    calls = _serve(monkeypatch)

    result = fulltext.safe_download("http://localhost/p.pdf", str(tmp_path / "1.pdf"))

    assert result.status == "blocked_url"
    assert calls == []


# ---------------------------------------------------------------------------
# The enrichment step: provider order, fallbacks and the audit trail
# ---------------------------------------------------------------------------


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _paper(db, **fields):
    row = Paper(
        **{"source": "semantic_scholar", "title": "T", "has_full_text": False, **fields}
    )
    db.add(row)
    db.flush()
    return row


class _Downloads:
    """Scripted downloaders: url -> DownloadResult (default: not found)."""

    def __init__(self, results=None):
        self.results = results or {}
        self.arxiv_calls: list[str] = []
        self.other_calls: list[str] = []

    def _answer(self, url, dest):
        result = self.results.get(url, DownloadResult("http_error", 404))
        if result.ok:
            with open(dest, "wb") as handle:
                handle.write(PDF)
        return result

    def arxiv(self, url, dest):
        self.arxiv_calls.append(url)
        return self._answer(url, dest)

    def other(self, url, dest):
        self.other_calls.append(url)
        return self._answer(url, dest)


def _acquire(
    db, tmp_path, downloads, *, email="ops@example.org", budget=600.0, clock=None
):
    log = fulltext.AttemptLog(db, run_id=None)
    kwargs = {"clock": clock} if clock else {}
    summary = fulltext.acquire_missing_full_text(
        db,
        log,
        pdf_dir=str(tmp_path),
        unpaywall_email=email,
        download_arxiv=downloads.arxiv,
        download_other=downloads.other,
        budget_seconds=budget,
        **kwargs,
    )
    return summary


def _attempts(db, paper_id):
    return [
        (a.provider, a.outcome)
        for a in db.scalars(
            select(FulltextAttempt)
            .where(FulltextAttempt.paper_id == paper_id)
            .order_by(FulltextAttempt.id)
        )
    ]


@pytest.fixture()
def no_unpaywall(monkeypatch):
    lookups = []

    def lookup(doi, email):
        lookups.append(doi)
        return OaLookup("no_oa_copy", http_status=200)

    monkeypatch.setattr(unpaywall, "lookup_oa_pdf", lookup)
    return lookups


def test_the_semantic_scholar_link_is_tried_first_and_stops_the_chain(
    db, tmp_path, no_unpaywall
):
    paper = _paper(
        db,
        openaccess_pdf_url="https://repo.example.org/p.pdf",
        arxiv_id="2401.00001",
        doi="10.1/a",
    )
    downloads = _Downloads(
        {"https://repo.example.org/p.pdf": DownloadResult("pdf_stored", 200)}
    )

    summary = _acquire(db, tmp_path, downloads)

    assert _attempts(db, paper.id) == [("semantic_scholar_oa", "pdf_stored")]
    assert downloads.arxiv_calls == []
    assert no_unpaywall == []
    assert paper.has_full_text is True
    assert paper.pdf_path == f"{tmp_path}/{paper.id}.pdf"
    assert summary["pdfs_stored"] == 1


def test_an_arxiv_id_on_a_metadata_record_is_used(db, tmp_path, no_unpaywall):
    """Run 2026-10-12, paper 20: stored with arXiv id 2512.10104, never fetched."""
    paper = _paper(db, arxiv_id="2512.10104", doi="10.1/b")
    downloads = _Downloads(
        {"https://arxiv.org/pdf/2512.10104": DownloadResult("pdf_stored", 200)}
    )

    _acquire(db, tmp_path, downloads)

    assert _attempts(db, paper.id) == [("arxiv", "pdf_stored")]
    assert downloads.arxiv_calls == ["https://arxiv.org/pdf/2512.10104"]
    assert paper.has_full_text is True


def test_providers_fall_back_in_order_and_each_attempt_is_recorded(
    db, tmp_path, monkeypatch
):
    paper = _paper(
        db,
        openaccess_pdf_url="https://doi.org/10.1/c",
        arxiv_id="2401.00002",
        acl_id="2024.acl-long.9",
        doi="10.1/c",
    )
    monkeypatch.setattr(
        unpaywall,
        "lookup_oa_pdf",
        lambda doi, email: OaLookup(
            "oa_location_found", "https://oa.example.org/c.pdf", 200
        ),
    )
    downloads = _Downloads(
        {
            "https://doi.org/10.1/c": DownloadResult("not_pdf", 200),
            "https://arxiv.org/pdf/2401.00002": DownloadResult("http_error", 404),
            "https://aclanthology.org/2024.acl-long.9.pdf": DownloadResult("timeout"),
            "https://oa.example.org/c.pdf": DownloadResult("pdf_stored", 200),
        }
    )

    _acquire(db, tmp_path, downloads)

    assert _attempts(db, paper.id) == [
        ("semantic_scholar_oa", "not_pdf"),
        ("arxiv", "http_error"),
        ("acl_anthology", "timeout"),
        ("unpaywall", "pdf_stored"),
    ]
    rows = list(db.scalars(select(FulltextAttempt).order_by(FulltextAttempt.id)))
    assert [r.retryable for r in rows] == [False, False, True, False]
    assert rows[-1].location_found is True
    assert rows[-1].location == "oa.example.org/c.pdf"
    assert paper.has_full_text is True


@pytest.mark.parametrize(
    ("lookup", "outcome", "retryable"),
    [
        (OaLookup("no_oa_copy", http_status=200), "no_oa_location", False),
        (OaLookup("doi_not_found", http_status=404), "doi_not_found", False),
        (OaLookup("timeout"), "lookup_timeout", True),
        (OaLookup("network_error"), "lookup_network_error", True),
        (OaLookup("http_error", http_status=500), "lookup_http_error", True),
        (
            OaLookup("invalid_response", http_status=200),
            "lookup_invalid_response",
            False,
        ),
    ],
)
def test_every_unpaywall_lookup_outcome_is_recorded_per_paper(
    db, tmp_path, monkeypatch, lookup, outcome, retryable
):
    paper = _paper(db, doi="10.1/d")
    monkeypatch.setattr(unpaywall, "lookup_oa_pdf", lambda doi, email: lookup)

    _acquire(db, tmp_path, _Downloads())

    [row] = db.scalars(select(FulltextAttempt))
    assert (row.paper_id, row.provider, row.outcome, row.retryable) == (
        paper.id,
        "unpaywall",
        outcome,
        retryable,
    )
    assert paper.has_full_text is False


def test_a_paper_without_doi_or_identifiers_says_so(db, tmp_path, no_unpaywall):
    paper = _paper(db)

    _acquire(db, tmp_path, _Downloads())

    assert _attempts(db, paper.id) == [("unpaywall", "no_doi")]
    assert no_unpaywall == []


def test_unpaywall_without_a_contact_email_is_not_configured(
    db, tmp_path, no_unpaywall
):
    paper = _paper(db, doi="10.1/e")

    _acquire(db, tmp_path, _Downloads(), email="")

    assert _attempts(db, paper.id) == [("unpaywall", "not_configured")]
    assert no_unpaywall == []


def test_a_location_already_tried_is_not_fetched_again(db, tmp_path, monkeypatch):
    """Semantic Scholar's link and the arXiv id often name the same PDF."""
    paper = _paper(
        db, openaccess_pdf_url="http://arxiv.org/pdf/2409.13331", arxiv_id="2409.13331"
    )
    monkeypatch.setattr(
        unpaywall, "lookup_oa_pdf", lambda doi, email: pytest.fail("no DOI")
    )
    downloads = _Downloads()

    _acquire(db, tmp_path, downloads)

    assert downloads.arxiv_calls == ["http://arxiv.org/pdf/2409.13331"]
    assert _attempts(db, paper.id) == [
        ("semantic_scholar_oa", "http_error"),
        ("unpaywall", "no_doi"),
    ]


def test_an_arxiv_hosted_link_goes_through_the_polite_downloader(
    db, tmp_path, no_unpaywall
):
    _paper(db, openaccess_pdf_url="https://arxiv.org/pdf/2606.29602")
    downloads = _Downloads()

    _acquire(db, tmp_path, downloads)

    assert downloads.arxiv_calls == ["https://arxiv.org/pdf/2606.29602"]
    assert downloads.other_calls == []


def test_papers_with_full_text_are_not_touched(db, tmp_path, no_unpaywall):
    _paper(db, doi="10.1/f", has_full_text=True, pdf_path="/x.pdf")

    summary = _acquire(db, tmp_path, _Downloads())

    assert summary["papers_considered"] == 0
    assert db.scalars(select(FulltextAttempt)).all() == []


def test_a_failure_keeps_the_paper_metadata_only(db, tmp_path, no_unpaywall):
    paper = _paper(
        db, openaccess_pdf_url="https://repo.example.org/p.pdf", doi="10.1/g"
    )

    _acquire(db, tmp_path, _Downloads())

    assert paper.has_full_text is False
    assert paper.pdf_path is None
    assert _attempts(db, paper.id) == [
        ("semantic_scholar_oa", "http_error"),
        ("unpaywall", "no_oa_location"),
    ]


def test_the_time_budget_records_unreached_papers_as_not_attempted(
    db, tmp_path, no_unpaywall
):
    first = _paper(db, doi="10.1/h")
    second = _paper(db, doi="10.1/i")
    ticks = iter([0.0, 0.0, 100.0, 100.0])

    summary = _acquire(
        db, tmp_path, _Downloads(), budget=50.0, clock=lambda: next(ticks)
    )

    assert _attempts(db, first.id) == [("unpaywall", "no_oa_location")]
    assert _attempts(db, second.id) == [("all", "not_attempted")]
    assert summary["budget_exhausted"] is True


def test_a_later_failure_never_erases_an_earlier_success(db, tmp_path, no_unpaywall):
    """Attempts are appended: a paper stored by one run is simply not
    considered by the next, and its success row stays."""
    paper = _paper(db, openaccess_pdf_url="https://repo.example.org/p.pdf")
    _acquire(
        db,
        tmp_path,
        _Downloads(
            {"https://repo.example.org/p.pdf": DownloadResult("pdf_stored", 200)}
        ),
    )

    _acquire(db, tmp_path, _Downloads())

    assert _attempts(db, paper.id) == [("semantic_scholar_oa", "pdf_stored")]
    assert paper.has_full_text is True


def test_the_summary_counts_outcomes_by_provider(db, tmp_path, no_unpaywall):
    _paper(db, doi="10.1/j")
    _paper(db, openaccess_pdf_url="https://repo.example.org/k.pdf", doi="10.1/k")

    summary = _acquire(
        db,
        tmp_path,
        _Downloads(
            {"https://repo.example.org/k.pdf": DownloadResult("pdf_stored", 200)}
        ),
    )

    assert summary["by_provider"] == {
        "semantic_scholar_oa": {"pdf_stored": 1},
        "unpaywall": {"no_oa_location": 1},
    }
    assert summary["attempts"] == 2
