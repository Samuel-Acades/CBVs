from __future__ import annotations

from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from . import config

PUBLIC_PATHS = {"/login", "/static", "/health"}

SESSION_KEY = "cbv_user"


def install(app) -> None:
    # NOTE: register the auth middleware FIRST so SessionMiddleware (added after,
    # inserted at position 0) ends up outermost and populates scope["session"].
    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if (
            path == "/login"
            or path == "/static"
            or path.startswith("/static/")
            or path == "/assets"
            or path.startswith("/assets/")
            or path == "/health"
        ):
            return await call_next(request)
        sess = request.scope.get("session") or {}
        if sess.get(SESSION_KEY) == config.AUTH_USERNAME:
            return await call_next(request)
        if path.startswith("/api/"):
            from starlette.responses import JSONResponse

            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse(f"/login?next={path}", status_code=303)

    app.add_middleware(
        SessionMiddleware, secret_key=config.get_secret(), session_cookie="cbv_session", same_site="lax"
    )


def check(username: str, password: str) -> bool:
    return username.strip() == config.AUTH_USERNAME and password == config.AUTH_PASSWORD
