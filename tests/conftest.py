import httpx2
import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: httpx2.HTTPTransport, request: httpx2.Request) -> httpx2.Response:
        raise RuntimeError(f"Test tried to reach the network: {request.method} {request.url}")

    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", refuse)
