"""Tests for transport selection and the HTTP bearer-auth guard (offline)."""
import asyncio

import server


def test_stdio_is_the_default(monkeypatch):
    # The module-level default must stay stdio so desktop clients are unaffected.
    assert server.ARK_TRANSPORT in ("stdio", "http", "sse")
    import importlib
    importlib.reload(server)
    assert server.ARK_TRANSPORT == "stdio"


def test_unknown_transport_is_rejected(monkeypatch):
    monkeypatch.setattr(server, "ARK_TRANSPORT", "carrier-pigeon")
    try:
        server.main()
    except SystemExit as exc:
        assert "unsupported ARK_TRANSPORT" in str(exc)
    else:
        raise AssertionError("expected SystemExit for an unknown transport")


def test_http_without_token_refuses_to_start(monkeypatch):
    monkeypatch.setattr(server, "ARK_TRANSPORT", "http")
    monkeypatch.setattr(server, "ARK_MCP_TOKEN", "")
    try:
        server.main()
    except SystemExit as exc:
        assert "ARK_MCP_TOKEN" in str(exc)
    else:
        raise AssertionError("expected SystemExit: an open endpoint must not start")


def _run_middleware(auth_header, query_string=b""):
    """Drive the middleware with a fake ASGI app; return (status, reached_app)."""
    seen = {}

    async def app(scope, receive, send):
        seen["reached"] = True

    async def send(message):
        if message["type"] == "http.response.start":
            seen["status"] = message["status"]

    headers = [(b"authorization", auth_header)] if auth_header is not None else []
    scope = {"type": "http", "headers": headers, "query_string": query_string}

    async def receive():
        return {"type": "http.request"}

    middleware = server._BearerAuthMiddleware(app, "good-token")
    asyncio.run(middleware(scope, receive, send))
    return seen


def test_middleware_rejects_missing_token():
    seen = _run_middleware(None)
    assert seen["status"] == 401
    assert "reached" not in seen


def test_middleware_rejects_wrong_token():
    seen = _run_middleware("Bearer nope")
    assert seen["status"] == 401
    assert "reached" not in seen


def test_middleware_allows_correct_token():
    seen = _run_middleware("Bearer good-token")
    assert seen.get("reached") is True


def test_middleware_allows_token_in_query_string():
    """Clients whose remote-MCP UI only offers a URL (Chatbox mobile) need this."""
    seen = _run_middleware(None, b"token=good-token")
    assert seen.get("reached") is True


def test_middleware_rejects_wrong_query_token():
    seen = _run_middleware(None, b"token=nope")
    assert seen["status"] == 401
    assert "reached" not in seen


def test_middleware_passes_through_non_http_scopes():
    """Lifespan events must not be blocked, or startup would hang."""
    seen = {}

    async def app(scope, receive, send):
        seen["reached"] = True

    middleware = server._BearerAuthMiddleware(app, "good-token")

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        pass

    asyncio.run(middleware({"type": "lifespan"}, receive, send))
    assert seen.get("reached") is True
