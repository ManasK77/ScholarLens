"""An overall wall-clock deadline for one Mode B query.

`run_query` is synchronous: LangGraph runs retrieval, reranking and up to four
Groq calls (generate, grounding check, one regeneration, its check) on the
calling thread, and each Groq call has its own 120 s timeout and retry budget.
Nothing bounded the whole. Two mechanisms do so here, without touching the
workflow's logic:

* **The caller stops waiting.** `call_with_deadline` runs the query on a
  dedicated daemon thread and waits at most the remaining time; on expiry the
  route answers 504 at once.
* **The abandoned work stops at its next step.** `bound_services` wraps every
  service the workflow calls; each wrapper checks the deadline *before*
  calling through, so after expiry no further retrieval, rerank or model call
  starts and the worker thread ends with `QueryDeadlineExceeded`.

* **A paced call never starts late.** A model call that would have to wait
  for its pacing slot (`GROQ_MIN_INTERVAL_SECONDS`) past the deadline is
  refused at once instead of being sent after the caller got its 504 (D-B-04).

What this cannot do: Python cannot interrupt a thread, so a call already in
flight when the deadline passes (an HTTP request to Groq, a reranker forward
pass) runs to its own timeout. At most one such call outlives the deadline;
its result is discarded, never served.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import replace
from typing import TypeVar

from app.ingestion.rate_limit import WaitBudgetExceeded
from app.interactive.workflow import InteractiveServices

T = TypeVar("T")


class QueryDeadlineExceeded(RuntimeError):
    """The query's overall deadline passed; no answer may be served."""


class Deadline:
    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic):
        self.seconds = seconds
        self._clock = clock
        self._expires_at = clock() + seconds

    def remaining(self) -> float:
        return max(0.0, self._expires_at - self._clock())

    def check(self, step: str) -> None:
        if self.remaining() <= 0.0:
            raise QueryDeadlineExceeded(step)


class _Guarded:
    """Delegates everything; checks the deadline before the named methods."""

    def __init__(self, inner, deadline: Deadline, guarded: tuple[str, ...]) -> None:
        self._inner = inner
        self._deadline = deadline
        self._guarded = guarded

    def __getattr__(self, name: str):
        value = getattr(self._inner, name)
        if name not in self._guarded:
            return value

        def checked(*args, **kwargs):
            self._deadline.check(name)
            try:
                return value(*args, **kwargs)
            except WaitBudgetExceeded as exc:
                # Its request slot lies past the deadline: never sent (D-B-04).
                raise QueryDeadlineExceeded(f"{name}:pacing") from exc

        return checked


def _guard_callable(fn, deadline: Deadline, step: str):
    if fn is None:
        return None

    def checked(*args, **kwargs):
        deadline.check(step)
        return fn(*args, **kwargs)

    return checked


def bound_services(
    services: InteractiveServices, deadline: Deadline
) -> InteractiveServices:
    """The same services, each refusing to start once `deadline` has passed.

    A model client that paces its requests (`wait_budget`, the Groq client)
    is also told how long it may still wait for a slot, so a call queued
    behind the pacing interval is refused before the deadline rather than
    sent after it. The client is per query (`default_services`), so this
    never reaches Mode A's.
    """
    if hasattr(services.llm, "wait_budget"):
        services.llm.wait_budget = deadline.remaining
    return replace(
        services,
        retriever=_Guarded(services.retriever, deadline, ("retrieve",)),
        graph_expand=_guard_callable(services.graph_expand, deadline, "graph_expand"),
        llm=_Guarded(services.llm, deadline, ("generate_json",)),
        reranker=_guard_callable(services.reranker, deadline, "rerank"),
        title_lookup=_guard_callable(services.title_lookup, deadline, "title_lookup"),
    )


def call_with_deadline(fn: Callable[[], T], deadline: Deadline) -> T:
    """`fn()`, or `QueryDeadlineExceeded` once `deadline` passes.

    `fn` runs on its own daemon thread (not a shared pool, which abandoned
    work could exhaust); its exception, if any, is re-raised here unchanged.
    """
    done = threading.Event()
    outcome: dict = {}

    def target() -> None:
        try:
            outcome["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 -- re-raised in the waiting caller
            outcome["error"] = exc
        finally:
            done.set()

    threading.Thread(target=target, name="mode-b-query", daemon=True).start()
    if not done.wait(timeout=deadline.remaining()):
        raise QueryDeadlineExceeded("response")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]
