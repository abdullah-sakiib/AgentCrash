"""ASGI tests for production frontend serving (M14 Phase 1).

All fixtures live in pytest tmp directories. The real frontend build is
never required: tests plant a minimal dist tree instead.
"""

from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from agentcrash.api import create_app


@pytest.fixture
def dist_dir(tmp_path: Path, monkeypatch) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><body><div id=\"root\"></div></body></html>",
        encoding="utf-8",
    )
    (dist / "assets" / "app.js").write_text(
        "console.log('hi');", encoding="utf-8"
    )
    (dist / "assets" / "style.css").write_text(
        "body { color: red; }", encoding="utf-8"
    )
    monkeypatch.setenv("AGENTCRASH_FRONTEND_DIST", str(dist))
    monkeypatch.delenv("AGENTCRASH_NO_UI", raising=False)
    return dist


@pytest.fixture
def app(tmp_path: Path, dist_dir: Path) -> FastAPI:
    return create_app(f"sqlite:///{tmp_path / 'test.db'}")


@pytest_asyncio.fixture
async def client(app: FastAPI):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_root_serves_index_html(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert '<div id="root">' in response.text


async def test_hashed_assets_served_with_content_types(client):
    js = await client.get("/assets/app.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert "console.log" in js.text
    css = await client.get("/assets/style.css")
    assert css.status_code == 200
    assert "css" in css.headers["content-type"]


@pytest.mark.parametrize(
    "path",
    ["/experiments/run.json", "/scenarios", "/scorecards/run.json", "/compare"],
)
async def test_spa_deep_links_return_frontend_html(client, path: str):
    response = await client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert '<div id="root">' in response.text


async def test_api_routes_take_precedence_over_static(client):
    health = await client.get("/api/health")
    assert health.status_code == 200
    assert "text/html" not in health.headers["content-type"]
    assert health.json()["status"] == "ok"


async def test_unknown_api_paths_are_json_404(client):
    response = await client.get("/api/nonexistent")
    assert response.status_code == 404
    assert "text/html" not in response.headers["content-type"]
    assert response.json() == {"detail": "Not Found"}


async def test_simulator_docs_and_openapi_intact(client):
    inventory = await client.post(
        "/inventory", json={"item": "widget", "quantity_on_hand": 1}
    )
    assert inventory.status_code == 201
    docs = await client.get("/docs")
    assert docs.status_code == 200
    assert "text/html" in docs.headers["content-type"]
    schema = await client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/api/health" in schema.json()["paths"]


def test_static_mount_is_registered_last(app):
    """Regression test: the static mount must follow every API route."""
    from starlette.routing import Mount

    # The ui include_router materializes as _IncludedRouter; the static
    # mount is the trailing Mount (Starlette stores a "/" mount path as "").
    static_at = [
        i
        for i, route in enumerate(app.routes)
        if isinstance(route, Mount) and getattr(route, "path", "") in ("", "/")
    ]
    assert len(static_at) == 1
    for i, route in enumerate(app.routes):
        kind = type(route).__name__
        if kind == "APIRoute" or kind == "_IncludedRouter":
            assert i < static_at[0], getattr(
                route, "path_format", getattr(route, "path", "?")
            )


async def test_redoc_never_falls_through_to_spa_shell(client):
    """Regression: /redoc is excluded from SPA fallback by rule, not luck."""
    from fastapi import HTTPException as FastAPIHTTPException

    from agentcrash.static_ui import SPAStaticFiles

    response = await client.get("/redoc")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "redoc" in response.text.lower()

    # The guard itself rejects /redoc even if the route were unregistered.
    dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    service = SPAStaticFiles(directory=str(dist), html=True)
    scope = {"type": "http", "method": "GET", "path": "/redoc", "headers": []}
    with pytest.raises(FastAPIHTTPException) as excinfo:
        await service.get_response("redoc", scope)
    assert excinfo.value.status_code == 404


async def test_traversal_cannot_escape_build_dir(
    client, tmp_path: Path, dist_dir: Path
):
    secret = tmp_path / "secret.txt"
    secret.write_text("top-secret", encoding="utf-8")
    # Encoded dot segments are normalized by routing before the app sees
    # them, and static lookup is contained to the build directory, so these
    # can never escape: the only acceptable outcomes are a JSON 404 or the
    # fixed SPA shell. The escaped file's contents must never appear.
    for path in [
        "/assets/%2e%2e/secret.txt",
        "/%2e%2e/secret.txt",
    ]:
        response = await client.get(path)
        assert "top-secret" not in response.text, path
        assert response.status_code in (404, 200), path
        if response.status_code == 200:
            assert '<div id="root">' in response.text, path
    # Plain dot segments are normalized away by HTTP clients before sending,
    # so the server sees a plain unknown path: it must serve the SPA shell
    # (or 404), but never the escaped file's contents.
    response = await client.get("/assets/../secret.txt")
    assert "top-secret" not in response.text
    # Repository files are not reachable through the mount either: unknown
    # non-API paths get the fixed SPA shell (or 404), never file contents.
    response = await client.get("/api.py")
    assert "create_app" not in response.text
    assert response.status_code in (404, 200)


async def test_missing_dist_keeps_api_working_with_hint(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("AGENTCRASH_FRONTEND_DIST", str(tmp_path / "absent"))
    monkeypatch.delenv("AGENTCRASH_NO_UI", raising=False)
    app = create_app(f"sqlite:///{tmp_path / 'test.db'}")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        health = await client.get("/api/health")
        assert health.status_code == 200
        root = await client.get("/")
        assert root.status_code == 404
        assert "npm run build" in root.json()["detail"]


async def test_no_ui_flag_disables_mounting(tmp_path: Path, monkeypatch, dist_dir: Path):
    monkeypatch.setenv("AGENTCRASH_NO_UI", "1")
    app = create_app(f"sqlite:///{tmp_path / 'test.db'}")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        health = await client.get("/api/health")
        assert health.status_code == 200
        root = await client.get("/")
        assert root.status_code == 404
        assert "text/html" not in root.headers.get("content-type", "")
