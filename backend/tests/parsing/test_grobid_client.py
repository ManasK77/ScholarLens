import httpx
import respx

from app.parsing.grobid_client import parse_fulltext, parse_header


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
def test_parse_header_returns_text_on_success_and_none_on_failure() -> None:
    route = respx.post("http://grobid:8070/api/processHeaderDocument").mock(
        return_value=httpx.Response(200, text="<TEI>header</TEI>")
    )
    assert parse_header(b"pdf", "http://grobid:8070") == "<TEI>header</TEI>"
    route.mock(return_value=httpx.Response(503))
    assert parse_header(b"pdf", "http://grobid:8070") is None
