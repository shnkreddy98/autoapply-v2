"""FastAPI factory. Run: uv run uvicorn api.app:app (in api/).

Audit middleware: every successful mutation (POST/PUT/PATCH/DELETE) by a
signed-in user gets an audit_log row. Endpoints that audit richer detail
themselves (e.g. login) are skipped here.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request

from api import auth, db, routes
from common import repo

log = logging.getLogger("api")

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def create_app() -> FastAPI:
    app = FastAPI(title="autoapply API", version="0.1.0")
    app.include_router(auth.router)
    app.include_router(routes.router)

    @app.middleware("http")
    async def audit_mutations(request: Request, call_next):
        response = await call_next(request)
        user_id = getattr(request.state, "user_id", None)
        if (
            request.method in _MUTATING
            and response.status_code < 400
            and user_id
            and not request.url.path.startswith("/auth/")
        ):
            try:
                with db.admin_conn() as conn:
                    repo.audit(
                        conn, user_id,
                        f"{request.method} {request.url.path}",
                        detail={"query": str(request.url.query) or None},
                    )
            except Exception:  # auditing must not take down the request
                log.exception("audit write failed")
        return response

    return app


app = create_app()
