"""The site origin: serves published sites, and nothing else (§1.26, v2.26).

Its own process on its own origin, reading the database and the object store
and writing only the single-use ticket record. GET and HEAD only.
"""

import html
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse

from tiny_hermes.session_workspace.application.files import (
    WorkspaceFileNotFound,
    WorkspaceFilesUnavailable,
)
from tiny_hermes.session_workspace.infrastructure.minio_store import MinioObjectStore
from tiny_hermes.shared.config import Settings
from tiny_hermes.shared.database import build_session_factory
from tiny_hermes.sites.application.publisher import (
    ActiveSite,
    SiteFileNotFound,
    SitePublisher,
    SiteSettings,
)
from tiny_hermes.sites.domain.site import security_headers, sign, verify

PASS_COOKIE = "site_pass"  # noqa: S105 - a cookie name, not a secret
PASS_HOURS = 12


def create_site_app(settings: Settings) -> FastAPI:
    if not settings.site_public_url or not settings.site_signing_secret:
        raise ValueError("the site origin needs site_public_url and site_signing_secret")
    secret = settings.site_signing_secret
    secure = settings.site_public_url.startswith("https://")
    console = settings.console_base_url.rstrip("/")
    sites = SitePublisher(
        sessions=build_session_factory(settings),
        objects=MinioObjectStore(
            endpoint=settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            bucket=settings.s3_bucket,
        ),
        settings=SiteSettings(
            public_url=settings.site_public_url,
            signing_secret=secret,
            ttl_days=settings.site_publication_ttl_days,
        ),
    )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    @app.api_route("/s/{token}", methods=["GET", "HEAD"])
    async def bare(token: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        return RedirectResponse(f"/s/{token}/", status_code=308)

    @app.api_route("/s/{token}/__auth", methods=["GET", "HEAD"])
    async def enter(token: str, ticket: str = "") -> Response:  # pyright: ignore[reportUnusedFunction]
        site = await sites.resolve(token)
        if site is None:
            return _gone()
        now = datetime.now(UTC)
        claims = verify(secret, "ticket", ticket, now)
        if claims is None or claims.get("p") != str(site.id):
            return _refused()
        if not await sites.use_ticket_once(claims.get("n", "")):
            return _refused()
        base = f"/s/{token}/"
        response = RedirectResponse(base, status_code=302)
        response.set_cookie(
            PASS_COOKIE,
            sign(secret, "pass", {"p": str(site.id), "u": claims.get("u", "")},
                 now + timedelta(hours=PASS_HOURS)),
            max_age=PASS_HOURS * 3600,
            path=base,
            httponly=True,
            samesite="lax",
            secure=secure,
        )
        return response

    @app.api_route("/s/{token}/{path:path}", methods=["GET", "HEAD"])
    async def serve(token: str, path: str, request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        site = await sites.resolve(token)
        if site is None:
            return _gone()
        if site.visibility != "link" and not _admitted(site, request, secret):
            return _door(console, token)
        try:
            media, size, stream = await sites.open_file(site, path)
        except (SiteFileNotFound, WorkspaceFileNotFound):
            return _missing()
        except WorkspaceFilesUnavailable:
            return Response("unavailable", status_code=503, headers=security_headers())
        headers = {**security_headers(), "Content-Length": str(size)}
        if request.method == "HEAD":
            return Response(media_type=media, headers=headers)
        return StreamingResponse(stream, media_type=media, headers=headers)

    return app


def _admitted(site: ActiveSite, request: Request, secret: str) -> bool:
    value = request.cookies.get(PASS_COOKIE, "")
    claims = verify(secret, "pass", value, datetime.now(UTC)) if value else None
    return claims is not None and claims.get("p") == str(site.id)


def _page(status: int, title: str, body: str) -> HTMLResponse:
    document = (
        f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
        f"<body style='font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
        f"<h1>{html.escape(title)}</h1>{body}</body>"
    )
    return HTMLResponse(document, status_code=status, headers=security_headers())


def _door(console: str, token: str) -> HTMLResponse:
    link = html.escape(f"{console}/api/v1/sites/open?t={token}", quote=True)
    return _page(
        401,
        "Sign in to open this site",
        f"<p>This site is shared with members of a workspace.</p>"
        f"<p><a href=\"{link}\">Sign in and open it</a></p>",
    )


def _refused() -> HTMLResponse:
    return _page(403, "This link has already been used or has expired",
                 "<p>Open the site again from its link.</p>")


def _gone() -> HTMLResponse:
    return _page(
        404, "Site not found", "<p>This site does not exist, was revoked or has expired.</p>"
    )


def _missing() -> HTMLResponse:
    return _page(404, "Page not found", "<p>This site has no such page.</p>")
