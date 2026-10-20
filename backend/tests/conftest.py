"""Shared pytest fixtures for the ScholarLens backend test suite.

**No test reaches the network** (D-23). Request-level tests serve answers from
fake adapters/transports; anything that slips past them would otherwise make
a real request to arXiv, Semantic Scholar, Unpaywall or a PDF host -- as a
refactor of the arXiv pager briefly did. `_no_network` makes every name
lookup that is not for the local machine fail like an offline host, so such a
call fails fast and visibly instead. Tests marked ``integration`` (excluded
from the default run) may use the network.
"""

import socket

import pytest

_LOCAL = {"localhost", "127.0.0.1", "::1", "testserver", "test", ""}


class NetworkDisabled(OSError):
    """A test tried to resolve a remote host."""


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    if request.node.get_closest_marker("integration"):
        return
    real_getaddrinfo = socket.getaddrinfo

    def guarded(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else (host or "")
        if name in _LOCAL:
            return real_getaddrinfo(host, *args, **kwargs)
        raise NetworkDisabled(f"network access is disabled in tests ({name})")

    monkeypatch.setattr(socket, "getaddrinfo", guarded)
