"""How a discovery source retrieved its candidates (D-S1-RETRIEVAL-01).

A source's `search_topic` returns `Candidates`: the candidate dicts it always
returned (it *is* that list), plus a `retrieval` report saying how they were
obtained -- what the source said exists, how many pages were requested, how
many records came back, and whether the result set was read to the end. A
run that stopped at its ceiling, or lost a page to an error, is reported as
incomplete instead of being presented as exhaustive.

Units, all per source per run:

* ``total_reported`` -- results the source says match the query (None when it
  does not say; the ACL export is searched locally, so it is the match count);
* ``pages_requested`` / ``pages_succeeded`` -- search requests sent / answered;
* ``records_fetched`` -- records the answered pages contained;
* ``records_dropped`` -- of those, records the source itself discarded, by
  reason (``duplicate_across_pages``, ``non_research``, ``no_identifier``);
* ``candidates`` -- records returned to /collect (the gate judges these).

The query is recorded as sent (it carries no credentials) with a short
SHA-256 fingerprint of the query and the retrieval parameters, so two runs
can be compared without diffing strings.
"""

import hashlib
import json


class Candidates(list):
    """A source's candidate dicts, with the `retrieval` report attached."""

    def __init__(self, items=(), retrieval: dict | None = None) -> None:
        super().__init__(items)
        self.retrieval: dict = retrieval or {}


def fingerprint(query: str, params: dict) -> str:
    payload = json.dumps({"query": query, **params}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def report(
    *,
    query: str,
    params: dict,
    total_reported: int | None,
    pages_requested: int,
    pages_succeeded: int,
    records_fetched: int,
    records_dropped: dict[str, int],
    candidates: int,
    stop_reason: str,
    error: str | None = None,
) -> dict:
    """The `retrieval` report. `complete` is derived, never asserted.

    Complete means every result the source reported was read: the stop reason
    is ``exhausted`` and no page failed. ``ceiling`` (the configured retrieval
    limit was reached first), ``page_budget``, ``page_error`` and
    ``empty_page`` (the source returned nothing before its own total) are all
    incomplete.
    """
    complete = stop_reason == "exhausted" and pages_succeeded == pages_requested
    return {
        "query": query,
        "query_fingerprint": fingerprint(query, params),
        "params": params,
        "total_reported": total_reported,
        "pages_requested": pages_requested,
        "pages_succeeded": pages_succeeded,
        "records_fetched": records_fetched,
        "records_dropped": {k: v for k, v in records_dropped.items() if v},
        "candidates": candidates,
        "stop_reason": stop_reason,
        "complete": complete,
        "error": error,
    }


def error_category(exc: BaseException) -> str:
    """A safe, short category for a retrieval failure -- never its message,
    which can carry a request URL."""
    name = type(exc).__name__
    if "Timeout" in name:
        return "timeout"
    status = getattr(exc, "status", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if isinstance(status, int):
        return f"http_{status}"
    if name in ("ConnectError", "ConnectionError", "URLError"):
        return "network_error"
    return name
