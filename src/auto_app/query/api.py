"""Optional HTTP adapter for the same read-only queries used by the agent script."""

from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from auto_app.query.service import QueryError, QueryService


def create_app(db_path: Path, preferences_path: Path | None = None, *, port: int = 8766,
               allowed_hosts: set[str] | None = None) -> FastAPI:
    db_path = Path(db_path)
    app = FastAPI(title="auto-app read API", description="Local/private read-only job queries; no write routes.")
    hosts = allowed_hosts if allowed_hosts is not None else {
        f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    origins = {f"{scheme}://{host}" for host in hosts for scheme in ("http", "https")}

    @app.middleware("http")
    async def only_allowed_hosts(request: Request, call_next):
        if request.headers.get("host") not in hosts:
            return JSONResponse({"detail": "wrong host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin is not None and origin not in origins:
            return JSONResponse({"detail": "wrong origin"}, status_code=403)
        return await call_next(request)

    def execute(operation):
        # Database opening/schema failures are service availability errors.
        try:
            service = QueryService(db_path, preferences_path=preferences_path)
        except QueryError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        with service:
            try:
                return operation(service)
            except QueryError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/context")
    def context():
        return execute(lambda service: service.context())

    @app.get("/api/jobs")
    def jobs(query: str | None = None, state: str | None = None, company: str | None = None,
             source: str | None = None, archive: Literal["active", "archived", "all"] = "active",
             limit: int = Query(default=50, ge=1, le=200), cursor: str | None = None,
             include_excluded: bool = False):
        return execute(lambda service: service.jobs(query=query, state=state, company=company,
                       source=source, archived={"active": False, "archived": True, "all": None}[archive],
                       limit=limit, cursor=cursor, include_excluded=include_excluded))

    @app.get("/api/jobs/{job_id}")
    def job(job_id: int):
        result = execute(lambda service: service.job(job_id))
        if result is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found.")
        return result

    return app
