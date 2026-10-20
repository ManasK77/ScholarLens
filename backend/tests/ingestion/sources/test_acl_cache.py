"""The ACL Anthology export cache (D-ACL-CACHE-01, 2026-10-12).

The 2026-10-12 audit found the export cached on 2026-09-21 with no refresh
policy: `download_anthology_export` was a no-op whenever the file existed. It
now refreshes a missing or stale export, validates a download before it can
replace the cache, keeps the last known-good file when a refresh fails, and
records when, from where and how many records. No test touches the network:
the HTTP client is faked.
"""

import gzip
import json
import os
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.ingestion.sources import acl_anthology


def _export(records: int) -> bytes:
    return "".join(
        f"@inproceedings{{paper-{i},\n  title = {{Paper {i}}},\n  year = {{2026}}\n}}\n"
        for i in range(records)
    ).encode("utf-8")


class _Answer:
    def __init__(self, content=b"", status=200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code != 200:
            raise httpx.HTTPStatusError(
                str(self.status_code),
                request=httpx.Request("GET", acl_anthology.EXPORT_URL),
                response=httpx.Response(self.status_code),
            )


def _serve(monkeypatch, answer):
    """Fake httpx.Client; `answer` is an `_Answer` or an exception to raise."""
    calls = []

    class FakeClient:
        def __init__(self, transport=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, timeout=60.0):
            calls.append(url)
            if isinstance(answer, BaseException):
                raise answer
            return answer

    monkeypatch.setattr(acl_anthology.httpx, "Client", FakeClient)
    return calls


def _cached(tmp_path, records=5, age_days=0.0, with_meta=True):
    path = tmp_path / "export.bib"
    path.write_bytes(_export(records))
    fetched = datetime.now(UTC) - timedelta(days=age_days)
    if with_meta:
        (tmp_path / "export.bib.meta.json").write_text(
            json.dumps(
                {
                    "fetched_at": fetched.isoformat(),
                    "record_count": records,
                    "source_url": acl_anthology.EXPORT_URL,
                }
            )
        )
    else:
        stamp = fetched.timestamp()
        os.utime(path, (stamp, stamp))
    return path


def test_a_fresh_cache_is_used_without_any_request(monkeypatch, tmp_path):
    path = _cached(tmp_path, age_days=1)
    calls = _serve(monkeypatch, AssertionError("must not download"))

    status = acl_anthology.download_anthology_export(str(path), max_age_days=7)

    assert calls == []
    assert status["refresh"] == "not_needed"
    assert status["stale"] is False
    assert status["record_count"] == 5
    assert status["fetched_at_source"] == "metadata"


def test_a_stale_cache_is_refreshed_and_its_metadata_recorded(monkeypatch, tmp_path):
    path = _cached(tmp_path, records=5, age_days=30)
    calls = _serve(monkeypatch, _Answer(gzip.compress(_export(6))))

    status = acl_anthology.download_anthology_export(
        str(path), max_age_days=7, min_records=5, url="https://example.test/export"
    )

    assert calls == ["https://example.test/export"]
    assert status["refresh"] == "refreshed"
    assert status["stale"] is False
    assert status["record_count"] == 6
    assert status["source_url"] == "https://example.test/export"
    assert len(status["sha256"]) == 64
    assert path.read_bytes() == _export(6)
    assert not (tmp_path / "export.bib.new").exists()


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (b"<html>not gzip</html>", "not a gzip archive"),
        (gzip.compress(b"<html>an error page</html>"), "not a BibTeX export"),
        (gzip.compress(_export(2)), "2 records, 4 required"),
        (gzip.compress(b"@inproceedings{x,\n title={\xff\xfe}}\n"), "not UTF-8"),
    ],
)
def test_an_invalid_download_never_replaces_the_cache(
    monkeypatch, tmp_path, body, error
):
    path = _cached(tmp_path, records=5, age_days=30)
    _serve(monkeypatch, _Answer(body))

    status = acl_anthology.download_anthology_export(
        str(path), max_age_days=7, min_records=1
    )

    assert status["refresh"] == "failed"
    assert status["refresh_error"] == error
    assert status["stale"] is True
    assert path.read_bytes() == _export(5)  # the last known-good file
    assert not (tmp_path / "export.bib.new").exists()


def test_a_much_smaller_export_is_rejected_against_the_cached_count(
    monkeypatch, tmp_path
):
    """A truncated export must not displace a good one, even above the floor."""
    path = _cached(tmp_path, records=100, age_days=30)
    _serve(monkeypatch, _Answer(gzip.compress(_export(80))))

    status = acl_anthology.download_anthology_export(
        str(path), max_age_days=7, min_records=10
    )

    assert status["refresh"] == "failed"
    assert status["refresh_error"] == "80 records, 90 required"
    assert path.read_bytes() == _export(100)


def test_a_failed_refresh_falls_back_to_the_cache_and_says_so(monkeypatch, tmp_path):
    path = _cached(tmp_path, age_days=30)
    _serve(monkeypatch, httpx.ConnectError("no route"))

    status = acl_anthology.download_anthology_export(str(path), max_age_days=7)

    assert status["refresh"] == "failed"
    assert status["refresh_error"] == "network_error"
    assert status["stale"] is True
    assert status["last_refresh_result"] == "failed"
    assert path.exists()


def test_a_failed_download_with_no_cache_fails_the_source(monkeypatch, tmp_path):
    _serve(monkeypatch, _Answer(status=503))

    with pytest.raises(httpx.HTTPStatusError):
        acl_anthology.download_anthology_export(str(tmp_path / "export.bib"))


def test_an_invalid_first_download_fails_the_source(monkeypatch, tmp_path):
    _serve(monkeypatch, _Answer(b"garbage"))

    with pytest.raises(acl_anthology.ExportInvalid):
        acl_anthology.download_anthology_export(str(tmp_path / "export.bib"))


def test_without_a_max_age_an_existing_cache_is_never_refreshed(monkeypatch, tmp_path):
    path = _cached(tmp_path, age_days=400)
    calls = _serve(monkeypatch, AssertionError("must not download"))

    status = acl_anthology.download_anthology_export(str(path))

    assert calls == []
    assert status["refresh"] == "not_needed"
    assert status["stale"] is None


def test_force_refreshes_a_fresh_cache(monkeypatch, tmp_path):
    path = _cached(tmp_path, age_days=0)
    _serve(monkeypatch, _Answer(gzip.compress(_export(5))))

    status = acl_anthology.download_anthology_export(str(path), force=True)

    assert status["refresh"] == "refreshed"


def test_a_cache_written_before_metadata_existed_uses_its_file_time(tmp_path):
    path = _cached(tmp_path, age_days=21, with_meta=False)

    status = acl_anthology.cache_status(str(path), max_age_days=7)

    assert status["fetched_at_source"] == "file_mtime"
    assert status["record_count"] is None
    assert status["stale"] is True
    assert 20.9 < status["age_days"] < 21.1


def test_a_refresh_in_progress_elsewhere_is_not_raced(monkeypatch, tmp_path):
    path = _cached(tmp_path, age_days=30)
    (tmp_path / "export.bib.lock").write_text("held")
    calls = _serve(monkeypatch, AssertionError("must not download"))

    status = acl_anthology.download_anthology_export(str(path), max_age_days=7)

    assert calls == []
    assert status["refresh"] == "locked"
    assert path.read_bytes() == _export(5)


def test_a_lock_left_by_a_dead_refresh_is_taken_over(monkeypatch, tmp_path):
    path = _cached(tmp_path, age_days=30)
    lock = tmp_path / "export.bib.lock"
    lock.write_text("stale")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    _serve(monkeypatch, _Answer(gzip.compress(_export(5))))

    status = acl_anthology.download_anthology_export(str(path), max_age_days=7)

    assert status["refresh"] == "refreshed"
    assert not lock.exists()


def test_the_cli_reports_status_without_downloading(monkeypatch, tmp_path, capsys):
    path = _cached(tmp_path, age_days=2)
    _serve(monkeypatch, AssertionError("must not download"))

    class _Settings:
        acl_anthology_bib_path = str(path)
        acl_anthology_max_age_days = 7
        acl_anthology_min_records = 1
        acl_anthology_export_url = acl_anthology.EXPORT_URL

    monkeypatch.setattr("app.core.config.get_settings", lambda: _Settings())

    acl_anthology.main([])

    printed = json.loads(capsys.readouterr().out)
    assert printed["stale"] is False
    assert printed["record_count"] == 5


def test_the_cli_refresh_flag_refreshes_a_stale_cache(monkeypatch, tmp_path, capsys):
    path = _cached(tmp_path, age_days=30)
    _serve(monkeypatch, _Answer(gzip.compress(_export(5))))

    class _Settings:
        acl_anthology_bib_path = str(path)
        acl_anthology_max_age_days = 7
        acl_anthology_min_records = 1
        acl_anthology_export_url = acl_anthology.EXPORT_URL

    monkeypatch.setattr("app.core.config.get_settings", lambda: _Settings())

    acl_anthology.main(["--refresh"])

    assert json.loads(capsys.readouterr().out)["refresh"] == "refreshed"
