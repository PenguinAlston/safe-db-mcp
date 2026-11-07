from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from safe_db_mcp.credentials import InMemoryCredentialManager


logger = logging.getLogger("safedb.worker_ui")
_STATIC = Path(__file__).with_name("static")
_SESSION_COOKIE = "safedb_worker_session"
_SESSION_TTL_SECONDS = 8 * 60 * 60
_MAX_BODY_BYTES = 16 * 1024
_ALLOWED_HOSTS = {
    "127.0.0.1:8090",
    "localhost:8090",
    "127.0.0.1",
    "localhost",
}
_ALLOWED_ORIGINS = {
    "http://127.0.0.1:8090",
    "http://localhost:8090",
}


def create_worker_ui_app(manager: InMemoryCredentialManager) -> FastAPI:
    app = FastAPI(
        title="SafeDB Worker Console",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    sessions: dict[str, dict[str, Any]] = {}
    login_failures: deque[float] = deque()
    lock = threading.RLock()
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.middleware("http")
    async def local_only_security(request: Request, call_next):
        host = request.headers.get("host", "").lower()
        if host not in _ALLOWED_HOSTS:
            return JSONResponse({"detail": "Local Worker Console only"}, status_code=400)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin", "").rstrip("/")
            if origin not in _ALLOWED_ORIGINS or origin.split("://", 1)[-1] != host:
                return JSONResponse({"detail": "Request origin rejected"}, status_code=403)
            length = request.headers.get("content-length")
            if length:
                try:
                    if int(length) > _MAX_BODY_BYTES:
                        return JSONResponse({"detail": "Request too large"}, status_code=413)
                except ValueError:
                    return JSONResponse({"detail": "Invalid request"}, status_code=400)

        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
        return response

    async def read_json(request: Request) -> dict[str, Any]:
        try:
            body = await request.body()
            if not body or len(body) > _MAX_BODY_BYTES:
                raise ValueError
            value = await request.json()
            if not isinstance(value, dict):
                raise ValueError
            return value
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid request") from None

    def session_for(request: Request, require_csrf: bool = False) -> dict[str, Any]:
        token = request.cookies.get(_SESSION_COOKIE, "")
        if not token:
            raise HTTPException(status_code=401, detail="Login required")
        key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.time()
        with lock:
            session = sessions.get(key)
            if session is None or session["expires_at"] <= now:
                sessions.pop(key, None)
                raise HTTPException(status_code=401, detail="Session expired")
            if require_csrf:
                supplied = request.headers.get("x-safedb-csrf", "")
                if not supplied or not hmac.compare_digest(supplied, session["csrf"]):
                    raise HTTPException(status_code=403, detail="Request verification failed")
            return session

    @app.get("/")
    async def index():
        return FileResponse(_STATIC / "worker.html", media_type="text/html")

    @app.get("/api/session")
    async def get_session(request: Request):
        session = session_for(request)
        return {"csrf_token": session["csrf"], "expires_in": max(0, int(session["expires_at"] - time.time()))}

    @app.post("/api/login")
    async def login(request: Request, response: Response):
        body = await read_json(request)
        password = body.get("password")
        if not isinstance(password, str) or len(password) > 1024:
            raise HTTPException(status_code=401, detail="Invalid password")
        now = time.monotonic()
        with lock:
            while login_failures and login_failures[0] < now - 60:
                login_failures.popleft()
            if len(login_failures) >= 5:
                raise HTTPException(status_code=429, detail="Too many login attempts")
        if not manager.ui_password_configured():
            raise HTTPException(
                status_code=503,
                detail="Set a dashboard password with the Worker CLI first",
            )
        if not manager.verify_ui_password(password):
            with lock:
                login_failures.append(now)
            raise HTTPException(status_code=401, detail="Invalid password")

        with lock:
            login_failures.clear()
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        session_key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with lock:
            sessions[session_key] = {
                "csrf": csrf,
                "expires_at": time.time() + _SESSION_TTL_SECONDS,
            }
        response.set_cookie(
            _SESSION_COOKIE,
            token,
            max_age=_SESSION_TTL_SECONDS,
            httponly=True,
            secure=False,
            samesite="strict",
            path="/",
        )
        return {"ok": True, "csrf_token": csrf}

    @app.post("/api/logout")
    async def logout(request: Request, response: Response):
        session_for(request, require_csrf=True)
        token = request.cookies.get(_SESSION_COOKIE, "")
        key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with lock:
            sessions.pop(key, None)
        response.delete_cookie(_SESSION_COOKIE, path="/", httponly=True, samesite="strict")
        return {"ok": True}

    @app.get("/api/status")
    async def status(request: Request):
        session_for(request)
        return {"datasources": manager.list_status()}

    @app.post("/api/credentials/{datasource_id}")
    async def set_credential(datasource_id: str, request: Request):
        session_for(request, require_csrf=True)
        body = await read_json(request)
        database_url = body.get("database_url")
        expires_at = body.get("expires_at")
        if not isinstance(database_url, str) or not isinstance(expires_at, str):
            raise HTTPException(status_code=400, detail="Datasource credential request is invalid")
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            status_value = await run_in_threadpool(
                manager.set_credential, datasource_id, database_url, expiry
            )
        except Exception:
            # Never echo exception text or request data; they may contain a DSN.
            logger.warning("worker_ui.credential_rejected datasource=%s", datasource_id)
            raise HTTPException(
                status_code=400,
                detail="Credential rejected. Check its expiry, dialect and database connectivity.",
            ) from None
        logger.info(
            "worker_ui.credential_installed datasource=%s version=%s expires_at=%s",
            status_value["datasource_id"], status_value["version"], status_value["expires_at"],
        )
        return {"status": status_value}

    @app.delete("/api/credentials/{datasource_id}")
    async def clear_credential(datasource_id: str, request: Request):
        session_for(request, require_csrf=True)
        try:
            status_value = manager.clear_credential(datasource_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Credential could not be cleared") from None
        logger.warning(
            "worker_ui.credential_cleared datasource=%s version=%s",
            status_value["datasource_id"], status_value["version"],
        )
        return {"status": status_value}

    return app


class WorkerUiServer:
    """Loopback-only browser console for local Worker credential management."""

    def __init__(
        self,
        manager: InMemoryCredentialManager,
        host: str = "127.0.0.1",
        port: int = 8090,
    ):
        if host not in {"127.0.0.1", "0.0.0.0"}:
            raise ValueError("Worker UI may bind only to loopback or a loopback-published container")
        if not 1 <= port <= 65535:
            raise ValueError("Worker UI port is invalid")
        self.host = host
        self.port = port
        self.app = create_worker_ui_app(manager)
        self._server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                host=host,
                port=port,
                log_level="warning",
                access_log=False,
                lifespan="off",
            )
        )
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.run,
            name="safedb-worker-ui",
            daemon=True,
        )
        self._thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self._server.started:
                logger.info("Worker Console listening on %s:%s", self.host, self.port)
                return
            if not self._thread.is_alive():
                raise RuntimeError("Worker Console failed to start")
            time.sleep(0.05)
        self.close()
        raise RuntimeError("Worker Console startup timed out")

    def close(self) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=3)
