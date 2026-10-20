import httpx
import pytest
import respx

from app.parsing.grobid_client import fulltext, parse_fulltext, parse_header

FULLTEXT_URL = "http://grobid:8070/api/processFulltextDocument"


@respx.mock
def test_fulltext_outcome_ok_carries_the_tei() -> None:
    respx.post(FULLTEXT_URL).mock(return_value=httpx.Response(200, text="<TEI/>"))
    outcome = fulltext(b"pdf", "http://grobid:8070")
    assert (outcome.tei, outcome.status) == ("<TEI/>", "ok")


@respx.mock
def test_http_500_means_grobid_rejected_this_document() -> None:
    # GROBID answers 500 when *this PDF* could not be processed: retrying the
    # same bytes later gives the same answer, so the fallback parse is final.
    respx.post(FULLTEXT_URL).mock(return_value=httpx.Response(500))
    outcome = fulltext(b"pdf", "http://grobid:8070")
    assert (outcome.tei, outcome.status) == (None, "rejected")


@pytest.mark.parametrize("status_code", [502, 503, 504])
@respx.mock
def test_gateway_and_busy_statuses_mean_the_service_is_unavailable(status_code):
    respx.post(FULLTEXT_URL).mock(return_value=httpx.Response(status_code))
    outcome = fulltext(b"pdf", "http://grobid:8070")
    assert (outcome.tei, outcome.status) == (None, "unavailable")


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("offline"), httpx.ReadTimeout("timed out")]
)
@respx.mock
def test_transport_errors_mean_the_service_is_unavailable(error):
    respx.post(FULLTEXT_URL).mock(side_effect=error)
    outcome = fulltext(b"pdf", "http://grobid:8070")
    assert (outcome.tei, outcome.status) == (None, "unavailable")


@respx.mock
def test_parse_fulltext_returns_tei_and_requests_sentence_segmentation() -> None:
    route = respx.post("http://grobid:8070/api/processFulltextDocument").mock(
        return_value=httpx.Response(200, text="<TEI>ok</TEI>")
    )

    assert parse_fulltext(b"%PDF-test", "http://grobid:8070") == "<TEI>ok</TEI>"
    assert route.called
    assert b"segmentSentences" in route.calls.last.request.content
    assert b"1" in route.calls.last.request.content


@respx.mock
def test_parse_fulltext_returns_none_on_non_200() -> None:
    respx.post("http://grobid:8070/api/processFulltextDocument").mock(
        return_value=httpx.Response(500)
    )
    assert parse_fulltext(b"pdf", "http://grobid:8070") is None


@respx.mock
def test_parse_fulltext_returns_none_on_http_error() -> None:
    respx.post("http://grobid:8070/api/processFulltextDocument").mock(
        side_effect=httpx.ConnectError("offline")
    )
    assert parse_fulltext(b"pdf", "http://grobid:8070") is None


@respx.mock
def test_non_200_is_logged_with_endpoint_and_status(caplog) -> None:
    respx.post("http://grobid:8070/api/processFulltextDocument").mock(
        return_value=httpx.Response(503)
    )
    with caplog.at_level("WARNING", logger="app.parsing.grobid_client"):
        assert parse_fulltext(b"pdf", "http://grobid:8070") is None
    assert "processFulltextDocument" in caplog.text
    assert "503" in caplog.text


@respx.mock
def test_transport_error_is_logged_without_the_service_url(caplog) -> None:
    respx.post("http://grobid:8070/api/processFulltextDocument").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )
    with caplog.at_level("WARNING", logger="app.parsing.grobid_client"):
        assert parse_fulltext(b"pdf", "http://grobid:8070") is None
    assert "processFulltextDocument" in caplog.text
    assert "ReadTimeout" in caplog.text
    assert "grobid:8070" not in caplog.text


@respx.mock
def test_parse_header_returns_text_on_success_and_none_on_failure() -> None:
    route = respx.post("http://grobid:8070/api/processHeaderDocument").mock(
        return_value=httpx.Response(200, text="<TEI>header</TEI>")
    )
    assert parse_header(b"pdf", "http://grobid:8070") == "<TEI>header</TEI>"
    route.mock(return_value=httpx.Response(503))
    assert parse_header(b"pdf", "http://grobid:8070") is None
