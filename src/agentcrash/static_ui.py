"""Serve the built Vite frontend through FastAPI (M14).

Single-process demo mode: the React build in ``frontend/dist`` is served at
the domain root alongside the existing simulator and read-only UI APIs.
No new dependencies; plain Starlette static serving.

Mount order matters and is enforced by registration order in
``create_app``: simulator routes, then the ``/api`` UI router, then docs
(FastAPI registers those itself), and only then the static mount plus a
final SPA fallback. The fallback explicitly excludes API, docs, and
OpenAPI paths so they always receive JSON 404s instead of ``index.html``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

BUILD_HINT = (
    "Frontend is not built. Run: cd frontend; npm install; npm run build. "
    "Or start the UI with: .\\start-ui.ps1 -Build"
)

# Prefixes (and exact names) that must never fall through to the SPA shell.
# "redoc" is covered by FastAPI's own route today; it is listed here too so
# a future routing change can never silently serve the SPA shell instead.
_EXCLUDED_FIRST_SEGMENTS = ("api", "docs", "openapi.json", "redoc")


def frontend_dist_dir() -> Path:
    """Resolve the frontend build directory (env override is for tests)."""
    override = os.environ.get("AGENTCRASH_FRONTEND_DIST")
    if override:
        return Path(override).resolve()
    return (
        Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
    ).resolve()


class SPAStaticFiles(StaticFiles):
    """Static files with an SPA fallback that respects API boundaries.

    Unknown non-API paths serve ``index.html`` so BrowserRouter deep links
    work on refresh. API, docs, and OpenAPI paths always raise JSON 404s.
    Path containment inside the build directory is enforced by
    ``StaticFiles.lookup_path``; the fallback only ever serves the fixed
    ``index.html`` file.
    """

    async def get_response(self, path: str, scope: dict[str, Any]):  # type: ignore[override]
        # Router-registered API, docs, and OpenAPI routes already matched
        # before the mount; this guard keeps their unknown sub-paths (and
        # the docs endpoints themselves, if ever unregistered) on JSON 404s.
        # Separators are normalized first: on Windows, Starlette passes
        # native backslash separators here. Parent-directory segments are
        # rejected outright rather than served the SPA shell.
        check = path.replace("\\", "/").lstrip("/")
        segments = check.split("/")
        if segments[0].lower() in _EXCLUDED_FIRST_SEGMENTS or ".." in segments:
            raise HTTPException(status_code=404)
        try:
            return await super().get_response(path, scope)
        except (HTTPException, StarletteHTTPException) as exc:
            if getattr(exc, "status_code", None) != 404:
                raise
            return await super().get_response("index.html", scope)


def mount_frontend(app: FastAPI) -> bool:
    """Mount the built frontend at the domain root. Returns True if mounted.

    Never raises for a missing build or an opt-out: the API keeps working
    and ``/`` explains how to build the UI.
    """
    if os.environ.get("AGENTCRASH_NO_UI") == "1":
        logger.info("Frontend serving disabled by AGENTCRASH_NO_UI=1.")
        return False
    dist = frontend_dist_dir()
    try:
        has_build = (dist / "index.html").is_file()
    except OSError:
        has_build = False
    if not has_build:
        logger.warning(
            "Frontend build not found at %s. API only; %s", dist, BUILD_HINT
        )

        @app.get("/", include_in_schema=False)
        def _missing_build() -> JSONResponse:
            return JSONResponse(status_code=404, content={"detail": BUILD_HINT})

        return False
    app.mount("/", SPAStaticFiles(directory=str(dist), html=True), name="frontend")
    logger.info("Serving frontend from %s.", dist)
    return True
