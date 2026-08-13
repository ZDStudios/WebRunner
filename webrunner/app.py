"""The FastAPI application: dashboard pages, REST API and live WebSockets."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect

from . import __version__, blocks, cronparse, security, services
from .bus import bus, publish_event
from .config import Config, get_config
from .db import Database
from .scheduler import Scheduler, describe_schedule

log = logging.getLogger("webrunner.app")

BASE_DIR = Path(__file__).parent
SESSION_COOKIE = "wr_session"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# --------------------------------------------------------------------- models


class NodeIn(BaseModel):
    id: str
    type: str
    name: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    edges: dict[str, str] = Field(default_factory=dict)
    position: dict[str, float] = Field(default_factory=dict)
    ignore_failure: bool = False


class GraphIn(BaseModel):
    nodes: list[NodeIn] = Field(default_factory=list)
    start: str = ""


class JobIn(BaseModel):
    name: str
    description: str = ""
    enabled: bool = True
    tags: str = ""
    steps: GraphIn = Field(default_factory=GraphIn)
    trigger_type: str = "cron"
    cron: str = ""
    interval_seconds: int = 0
    run_at: str = ""
    timezone: str = ""
    start_at: str = ""
    end_at: str = ""
    cwd: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    run_as: str = ""
    timeout_seconds: int = 0
    max_retries: int = 0
    retry_delay_seconds: int = 30
    concurrency: str = "skip"
    catch_up: bool = False
    notify_on: str = "failure"
    notify_url: str = ""


class LoginIn(BaseModel):
    username: str
    password: str


class PasswordIn(BaseModel):
    current_password: str = ""
    new_password: str


class SecretIn(BaseModel):
    name: str
    value: str


class CronPreviewIn(BaseModel):
    trigger_type: str = "cron"
    cron: str = ""
    interval_seconds: int = 0
    run_at: str = ""
    timezone: str = ""
    start_at: str = ""
    end_at: str = ""
    count: int = 5


# ----------------------------------------------------------------- app setup


def create_app(config: Config | None = None) -> FastAPI:
    config = config or get_config()
    database = Database(config.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ensure_admin(database, config)
        scheduler = Scheduler(database, config)
        app.state.scheduler = scheduler
        await scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()
            database.close()

    app = FastAPI(
        title="WebRunner",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.db = database
    app.state.config = config

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    templates.env.filters["datetime"] = _format_datetime
    templates.env.filters["relative"] = _relative_time
    templates.env.filters["duration"] = _format_duration
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    register_routes(app)
    return app


def ensure_admin(db: Database, config: Config) -> None:
    """Create the first account, and surface the generated password once."""
    if db.scalar("SELECT COUNT(*) FROM users"):
        return
    username = config.admin_user or "admin"
    password = config.admin_password
    generated = False
    if not password:
        import secrets as _secrets

        password = _secrets.token_urlsafe(12)
        generated = True
    db.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
        (username, security.hash_password(password), time.time()),
    )
    if generated:
        # ASCII only: this banner is the single most important thing WebRunner
        # ever prints, and it has to survive journald, Windows Task Scheduler
        # logs and legacy code pages intact. Box-drawing characters either
        # arrive as ═ escapes or raise UnicodeEncodeError outright.
        rule = "=" * 66
        banner = (
            f"\n{rule}\n"
            f"  WebRunner first-run login\n\n"
            f"    username: {username}\n"
            f"    password: {password}\n\n"
            f"  This password is shown once. Change it in Settings after logging in.\n"
            f"{rule}\n"
        )
        print(banner, file=sys.stderr, flush=True)
        try:
            path = config.data_dir / "initial-password.txt"
            path.write_text(f"{username}\n{password}\n", encoding="utf-8")
            path.chmod(0o600)
            print(f"  (also written to {path})\n", file=sys.stderr, flush=True)
        except OSError:
            pass
    else:
        log.info("created initial user %r from the environment", username)


# ------------------------------------------------------------------- helpers


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_scheduler(request: Request) -> Scheduler:
    return request.app.state.scheduler


def _session_user(request: Request) -> str | None:
    config: Config = request.app.state.config
    if config.auth_disabled:
        return "anonymous"

    db: Database = request.app.state.db
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        row = db.query_one(
            "SELECT id, name FROM api_tokens WHERE token_hash = ?", (security.hash_token(token),)
        )
        if row:
            db.execute("UPDATE api_tokens SET last_used = ? WHERE id = ?", (time.time(), row["id"]))
            request.state.via_api_token = True
            return f"token:{row['name']}"

    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    row = db.query_one("SELECT * FROM sessions WHERE token = ?", (cookie,))
    if not row:
        return None
    if row["expires_at"] < time.time():
        db.execute("DELETE FROM sessions WHERE token = ?", (cookie,))
        return None
    return str(row["username"])


def require_user(request: Request) -> str:
    user = _session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    _enforce_csrf(request)
    return user


def _enforce_csrf(request: Request) -> None:
    """Cookie-authenticated writes must carry the session-bound CSRF token."""
    if request.method not in UNSAFE_METHODS:
        return
    if getattr(request.state, "via_api_token", False):
        return
    config: Config = request.app.state.config
    if config.auth_disabled:
        return
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return
    presented = request.headers.get("x-csrf-token", "")
    if not security.verify_csrf(config.secret_key, cookie, presented):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")


def _format_datetime(value: Any, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    if not value:
        return "—"
    try:
        return datetime.fromtimestamp(float(value)).strftime(fmt)
    except (TypeError, ValueError, OSError):
        return str(value)


def _relative_time(value: Any) -> str:
    if not value:
        return "—"
    try:
        delta = time.time() - float(value)
    except (TypeError, ValueError):
        return str(value)
    future = delta < 0
    delta = abs(delta)
    for size, label in ((86400, "d"), (3600, "h"), (60, "m"), (1, "s")):
        if delta >= size:
            amount = int(delta // size)
            return f"in {amount}{label}" if future else f"{amount}{label} ago"
    return "just now"


def _format_duration(value: Any) -> str:
    try:
        seconds = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _job_to_payload(job: dict, scheduler: Scheduler | None = None) -> dict:
    payload = {key: job[key] for key in job if key not in ("steps_json", "env_json")}
    payload["steps"] = job.get("steps") or {"nodes": []}
    payload["env"] = job.get("env") or {}
    payload["schedule_text"] = describe_schedule(job)
    if job.get("trigger_type") == "cron" and job.get("cron"):
        payload["schedule_human"] = cronparse.describe(job["cron"])
    payload["running"] = bool(
        scheduler and any(a.job_id == job["id"] for a in scheduler.active.values())
    )
    return payload


def _graph_from_payload(payload: JobIn) -> dict:
    nodes = []
    for node in payload.steps.nodes:
        nodes.append({
            "id": node.id,
            "type": node.type,
            "name": node.name,
            "config": node.config,
            "edges": {handle: target for handle, target in node.edges.items() if target},
            "position": node.position or {"x": 0, "y": 0},
            "ignore_failure": node.ignore_failure,
        })
    start = payload.steps.start or (nodes[0]["id"] if nodes else "")
    return {"nodes": nodes, "start": start}


def _job_columns(payload: JobIn) -> dict:
    return {
        "name": payload.name.strip(),
        "description": payload.description,
        "enabled": 1 if payload.enabled else 0,
        "tags": payload.tags.strip(),
        "steps_json": json.dumps(_graph_from_payload(payload), ensure_ascii=False),
        "trigger_type": payload.trigger_type,
        "cron": payload.cron.strip(),
        "interval_seconds": max(0, int(payload.interval_seconds or 0)),
        "run_at": payload.run_at.strip(),
        "timezone": payload.timezone.strip(),
        "start_at": payload.start_at.strip(),
        "end_at": payload.end_at.strip(),
        "cwd": payload.cwd.strip(),
        "env_json": json.dumps(payload.env, ensure_ascii=False),
        "run_as": payload.run_as.strip(),
        "timeout_seconds": max(0, int(payload.timeout_seconds or 0)),
        "max_retries": max(0, int(payload.max_retries or 0)),
        "retry_delay_seconds": max(0, int(payload.retry_delay_seconds or 0)),
        "concurrency": payload.concurrency,
        "catch_up": 1 if payload.catch_up else 0,
        "notify_on": payload.notify_on,
        "notify_url": payload.notify_url.strip(),
    }


def _validate_job(payload: JobIn) -> list[str]:
    problems: list[str] = []
    if not payload.name.strip():
        problems.append("the job needs a name")
    problems.extend(blocks.validate_graph(_graph_from_payload(payload)))

    trigger = payload.trigger_type
    if trigger == "cron":
        if not payload.cron.strip():
            problems.append("a cron expression is required")
        else:
            try:
                cronparse.CronExpr(payload.cron)
            except cronparse.CronError as exc:
                problems.append(f"cron: {exc}")
    elif trigger == "interval" and payload.interval_seconds <= 0:
        problems.append("the interval must be greater than zero")
    elif trigger == "date":
        if not payload.run_at.strip():
            problems.append("a date and time is required")
        else:
            try:
                datetime.fromisoformat(payload.run_at.replace("Z", "+00:00"))
            except ValueError:
                problems.append("the run date is not a valid date/time")
    elif trigger not in ("cron", "interval", "date", "manual"):
        problems.append(f"unknown trigger type {trigger!r}")

    if payload.concurrency not in ("allow", "skip", "queue", "replace"):
        problems.append(f"unknown concurrency policy {payload.concurrency!r}")
    if payload.timezone:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(payload.timezone)
        except Exception:  # noqa: BLE001
            problems.append(f"unknown timezone {payload.timezone!r}")
    return problems


# -------------------------------------------------------------------- routes


def register_routes(app: FastAPI) -> None:
    templates: Jinja2Templates = app.state.templates

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        config: Config = request.app.state.config
        cookie = request.cookies.get(SESSION_COOKIE, "")
        context.setdefault("user", _session_user(request))
        context.setdefault("version", __version__)
        context.setdefault("csrf_token", security.csrf_token(config.secret_key, cookie) if cookie else "")
        context.setdefault("auth_disabled", config.auth_disabled)
        context.setdefault("nav", name)
        return templates.TemplateResponse(request, f"{name}.html", context)

    def guard(request: Request) -> RedirectResponse | None:
        if _session_user(request):
            return None
        target = request.url.path
        return RedirectResponse(f"/login?next={target}", status_code=303)

    # ------------------------------------------------------------- pages

    @app.get("/", response_class=HTMLResponse)
    async def dashboard_page(request: Request):
        return guard(request) or page(request, "dashboard")

    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs_page(request: Request):
        return guard(request) or page(request, "jobs")

    @app.get("/jobs/new", response_class=HTMLResponse)
    async def new_job_page(request: Request):
        return guard(request) or page(request, "editor", job_id=0, nav="jobs")

    @app.get("/jobs/{job_id}/edit", response_class=HTMLResponse)
    async def edit_job_page(request: Request, job_id: int):
        return guard(request) or page(request, "editor", job_id=job_id, nav="jobs")

    @app.get("/runs", response_class=HTMLResponse)
    async def runs_page(request: Request):
        return guard(request) or page(request, "runs")

    @app.get("/services", response_class=HTMLResponse)
    async def services_page(request: Request):
        return guard(request) or page(request, "services")

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        return guard(request) or page(request, "settings")

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "/"):
        if _session_user(request):
            return RedirectResponse(next or "/", status_code=303)
        return page(request, "login", next=next, nav="")

    # -------------------------------------------------------------- auth

    @app.post("/api/login")
    async def login(request: Request, payload: LoginIn, response: Response):
        db = get_db(request)
        row = db.query_one("SELECT * FROM users WHERE username = ?", (payload.username.strip(),))
        # Always spend the hashing time so a missing user is not detectably faster.
        stored = row["password_hash"] if row else security.hash_password("placeholder")
        if not row or not security.verify_password(payload.password, stored):
            await asyncio.sleep(0.4)
            raise HTTPException(status_code=401, detail="Incorrect username or password")

        config: Config = request.app.state.config
        token = security.new_session_token()
        db.execute(
            "INSERT INTO sessions (token, username, created_at, expires_at, user_agent) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, row["username"], time.time(), security.session_expiry(config.session_days),
             request.headers.get("user-agent", "")[:300]),
        )
        db.execute("UPDATE users SET last_login = ? WHERE id = ?", (time.time(), row["id"]))
        db.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))

        response = JSONResponse({
            "ok": True,
            "username": row["username"],
            "csrf_token": security.csrf_token(config.secret_key, token),
        })
        response.set_cookie(
            SESSION_COOKIE, token,
            max_age=config.session_days * 86400,
            httponly=True,
            samesite="strict",
            secure=config.secure_cookie,
            path="/",
        )
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie:
            get_db(request).execute("DELETE FROM sessions WHERE token = ?", (cookie,))
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.post("/api/password")
    async def change_password(request: Request, payload: PasswordIn, user: str = Depends(require_user)):
        db = get_db(request)
        row = db.query_one("SELECT * FROM users WHERE username = ?", (user,))
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        if not security.verify_password(payload.current_password, row["password_hash"]):
            raise HTTPException(status_code=403, detail="Current password is incorrect")
        if len(payload.new_password) < 8:
            raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
        db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (security.hash_password(payload.new_password), row["id"]),
        )
        cookie = request.cookies.get(SESSION_COOKIE, "")
        db.execute("DELETE FROM sessions WHERE username = ? AND token != ?", (user, cookie))
        return {"ok": True, "warning": security.password_strength_warning(payload.new_password)}

    # ------------------------------------------------------------- system

    @app.get("/healthz")
    async def healthz(request: Request):
        scheduler: Scheduler = request.app.state.scheduler
        return {
            "status": "ok",
            "version": __version__,
            "uptime": round(time.time() - scheduler.started_at, 1),
            "jobs": get_db(request).scalar("SELECT COUNT(*) FROM jobs"),
            "active_runs": len(scheduler.active),
        }

    @app.get("/api/blocks")
    async def block_registry(user: str = Depends(require_user)):
        return {
            "blocks": blocks.BLOCKS,
            "context_hints": sorted([
                "job.name", "job.id", "run.id", "now.iso", "now.date", "now.time", "now.epoch",
                "vars.<name>", "secrets.<NAME>", "env.<NAME>", "last.stdout", "last.exit_code",
                "steps.<block>.stdout", "steps.<block>.exit_code", "steps.<block>.status_code",
                "steps.<block>.json.<path>", "steps.<block>.body",
            ]),
            "filters": ["json", "upper", "lower", "trim", "urlencode", "b64", "int", "len",
                        "first", "last", "lines", "replace:a:b", "slice:n", "default:x"],
        }

    @app.get("/api/stats")
    async def stats(request: Request, user: str = Depends(require_user)):
        db = get_db(request)
        scheduler: Scheduler = request.app.state.scheduler
        payload = db.stats()
        payload["upcoming"] = scheduler.next_runs(6)
        payload["recent"] = db.list_runs(limit=8)
        payload["active"] = scheduler.status()["active_runs"]
        payload["host"] = {
            "platform": platform.system(),
            "release": platform.release(),
            "python": platform.python_version(),
            "hostname": platform.node(),
            "in_docker": os.path.exists("/.dockerenv"),
            "uptime": round(time.time() - scheduler.started_at, 1),
            "version": __version__,
        }
        history = db.query(
            "SELECT status, started_at FROM runs WHERE started_at >= ? ORDER BY started_at",
            (time.time() - 86400,),
        )
        buckets: list[dict[str, int]] = [{"success": 0, "failed": 0} for _ in range(24)]
        now = time.time()
        for row in history:
            index = int((now - row["started_at"]) // 3600)
            if 0 <= index < 24:
                key = "success" if row["status"] == "success" else "failed"
                buckets[23 - index][key] += 1
        payload["timeline"] = buckets
        return payload

    # --------------------------------------------------------------- jobs

    @app.get("/api/jobs")
    async def list_jobs(request: Request, user: str = Depends(require_user)):
        scheduler: Scheduler = request.app.state.scheduler
        return [_job_to_payload(job, scheduler) for job in get_db(request).list_jobs()]

    @app.get("/api/jobs/{job_id}")
    async def get_job(request: Request, job_id: int, user: str = Depends(require_user)):
        job = get_db(request).get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        scheduler: Scheduler = request.app.state.scheduler
        payload = _job_to_payload(job, scheduler)
        payload["preview"] = scheduler.preview_runs(job, 5)
        return payload

    @app.post("/api/jobs", status_code=201)
    async def create_job(request: Request, payload: JobIn, user: str = Depends(require_user)):
        db = get_db(request)
        problems = _validate_job(payload)
        if problems:
            raise HTTPException(status_code=400, detail="; ".join(problems))
        if db.get_job_by_name(payload.name.strip()):
            raise HTTPException(status_code=409, detail=f"a job named {payload.name!r} already exists")

        scheduler: Scheduler = request.app.state.scheduler
        columns = _job_columns(payload)
        job_id = db.create_job(columns)
        job = db.get_job(job_id)
        scheduler.reschedule(job)
        publish_event("job.created", job_id=job_id, job_name=payload.name)
        log.info("%s created job %r", user, payload.name)
        return _job_to_payload(db.get_job(job_id), scheduler)

    @app.put("/api/jobs/{job_id}")
    async def update_job(request: Request, job_id: int, payload: JobIn,
                         user: str = Depends(require_user)):
        db = get_db(request)
        existing = db.get_job(job_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Job not found")
        problems = _validate_job(payload)
        if problems:
            raise HTTPException(status_code=400, detail="; ".join(problems))
        clash = db.get_job_by_name(payload.name.strip())
        if clash and clash["id"] != job_id:
            raise HTTPException(status_code=409, detail=f"a job named {payload.name!r} already exists")

        db.update_job(job_id, _job_columns(payload))
        scheduler: Scheduler = request.app.state.scheduler
        job = db.get_job(job_id)
        scheduler.reschedule(job)
        publish_event("job.updated", job_id=job_id, job_name=payload.name)
        return _job_to_payload(db.get_job(job_id), scheduler)

    @app.delete("/api/jobs/{job_id}")
    async def delete_job(request: Request, job_id: int, user: str = Depends(require_user)):
        db = get_db(request)
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        scheduler: Scheduler = request.app.state.scheduler
        for active in [a for a in scheduler.active.values() if a.job_id == job_id]:
            await scheduler.cancel_run(active.run_id, reason="job deleted")
        db.delete_job(job_id)
        publish_event("job.deleted", job_id=job_id, job_name=job["name"])
        log.info("%s deleted job %r", user, job["name"])
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/run")
    async def run_job(request: Request, job_id: int, user: str = Depends(require_user)):
        db = get_db(request)
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        scheduler: Scheduler = request.app.state.scheduler
        run_id = await scheduler.trigger(job, trigger="manual")
        log.info("%s manually triggered job %r (run #%s)", user, job["name"], run_id)
        return {"ok": True, "run_id": run_id}

    @app.post("/api/jobs/{job_id}/toggle")
    async def toggle_job(request: Request, job_id: int, user: str = Depends(require_user)):
        db = get_db(request)
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        enabled = 0 if job["enabled"] else 1
        db.execute("UPDATE jobs SET enabled = ? WHERE id = ?", (enabled, job_id))
        scheduler: Scheduler = request.app.state.scheduler
        updated = db.get_job(job_id)
        scheduler.reschedule(updated)
        publish_event("job.updated", job_id=job_id, job_name=job["name"])
        return _job_to_payload(db.get_job(job_id), scheduler)

    @app.post("/api/jobs/{job_id}/duplicate")
    async def duplicate_job(request: Request, job_id: int, user: str = Depends(require_user)):
        db = get_db(request)
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        base = f"{job['name']} (copy)"
        name, index = base, 2
        while db.get_job_by_name(name):
            name = f"{base} {index}"
            index += 1
        columns = {key: job[key] for key in Database.JOB_COLUMNS}
        columns["name"] = name
        columns["enabled"] = 0
        new_id = db.create_job(columns)
        scheduler: Scheduler = request.app.state.scheduler
        return _job_to_payload(db.get_job(new_id), scheduler)

    @app.post("/api/jobs/preview")
    async def preview_schedule(request: Request, payload: CronPreviewIn,
                               user: str = Depends(require_user)):
        scheduler: Scheduler = request.app.state.scheduler
        fake_job = {
            "id": 0, "enabled": True,
            "trigger_type": payload.trigger_type,
            "cron": payload.cron,
            "interval_seconds": payload.interval_seconds,
            "run_at": payload.run_at,
            "timezone": payload.timezone,
            "start_at": payload.start_at,
            "end_at": payload.end_at,
        }
        result: dict[str, Any] = {"runs": [], "error": "", "human": ""}
        if payload.trigger_type == "cron" and payload.cron.strip():
            try:
                cronparse.CronExpr(payload.cron)
                result["human"] = cronparse.describe(payload.cron)
            except cronparse.CronError as exc:
                result["error"] = str(exc)
                return result
        try:
            result["runs"] = scheduler.preview_runs(fake_job, max(1, min(10, payload.count)))
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)
        if not result["runs"] and not result["error"]:
            result["error"] = "this schedule will never fire"
        return result

    @app.get("/api/jobs/{job_id}/runs")
    async def job_runs(request: Request, job_id: int, limit: int = 25,
                       user: str = Depends(require_user)):
        return get_db(request).list_runs(job_id=job_id, limit=min(200, max(1, limit)))

    # --------------------------------------------------------------- runs

    @app.get("/api/runs")
    async def list_runs(
        request: Request,
        job_id: int | None = None,
        status: str | None = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        user: str = Depends(require_user),
    ):
        db = get_db(request)
        return {
            "runs": db.list_runs(job_id=job_id, status=status, limit=limit, offset=offset),
            "total": db.count_runs(job_id=job_id, status=status),
        }

    @app.get("/api/runs/{run_id}")
    async def get_run(request: Request, run_id: int, user: str = Depends(require_user)):
        run = get_db(request).get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return run

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(request: Request, run_id: int, user: str = Depends(require_user)):
        scheduler: Scheduler = request.app.state.scheduler
        cancelled = await scheduler.cancel_run(run_id, reason=f"cancelled by {user}")
        if not cancelled:
            raise HTTPException(status_code=404, detail="That run is not currently active")
        return {"ok": True}

    @app.delete("/api/runs")
    async def clear_runs(request: Request, job_id: int | None = None,
                         user: str = Depends(require_user)):
        removed = get_db(request).clear_runs(job_id)
        return {"ok": True, "removed": removed}

    # ----------------------------------------------------------- services

    @app.get("/api/services")
    async def list_services(request: Request, backend: str = "", user: str = Depends(require_user)):
        available = services.available_backends()
        if not available:
            return {"backends": [], "services": [], "backend": "",
                    "error": "no service manager was detected on this host"}
        chosen = backend or services.default_backend()
        if chosen not in available:
            raise HTTPException(status_code=400, detail=f"backend {chosen!r} is not available here")
        try:
            found = await services.list_services(chosen)
            return {"backends": available, "backend": chosen, "services": found, "error": ""}
        except services.ServiceError as exc:
            return {"backends": available, "backend": chosen, "services": [], "error": str(exc)}

    @app.post("/api/services/{backend}/{action}")
    async def control_service(request: Request, backend: str, action: str,
                              name: str = Query(...), user: str = Depends(require_user)):
        try:
            code, output = await services.control(backend, name, action)
        except services.ServiceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        log.info("%s ran %s %s on %s", user, action, name, backend)
        return {"ok": code == 0, "exit_code": code, "output": output[-8000:]}

    # ----------------------------------------------------------- settings

    @app.get("/api/settings")
    async def get_settings(request: Request, user: str = Depends(require_user)):
        db = get_db(request)
        config: Config = request.app.state.config
        values = db.all_settings()
        values.pop("smtp_password", None)
        values["smtp_password_set"] = "1" if db.get_setting("smtp_password") else ""
        return {
            "settings": values,
            "secrets": db.list_secret_names(),
            "tokens": db.query("SELECT id, name, prefix, created_at, last_used FROM api_tokens ORDER BY id"),
            "runtime": {
                "data_dir": str(config.data_dir),
                "shell": config.default_shell,
                "host": f"{config.host}:{config.port}",
                "auth_disabled": config.auth_disabled,
                "platform": platform.platform(),
                "python": sys.version.split()[0],
                "version": __version__,
                "backends": services.available_backends(),
                "timezone": str(request.app.state.scheduler.timezone_for()),
            },
        }

    @app.put("/api/settings")
    async def update_settings(request: Request, payload: dict[str, Any],
                              user: str = Depends(require_user)):
        db = get_db(request)
        allowed = {
            "timezone", "notify_url", "notify_on", "global_env", "retention_runs",
            "smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from",
            "smtp_to", "smtp_tls",
        }
        for key, value in payload.items():
            if key not in allowed:
                continue
            if key == "smtp_password" and value == "":
                continue          # blank means "leave the stored password alone"
            db.set_setting(key, "" if value is None else str(value))
        if "timezone" in payload:
            request.app.state.scheduler.reschedule_all()
        return {"ok": True}

    @app.get("/api/secrets")
    async def list_secrets(request: Request, user: str = Depends(require_user)):
        return get_db(request).list_secret_names()

    @app.post("/api/secrets")
    async def set_secret(request: Request, payload: SecretIn, user: str = Depends(require_user)):
        name = payload.name.strip()
        if not name or not name.replace("_", "").isalnum():
            raise HTTPException(status_code=400, detail="Use letters, digits and underscores only")
        get_db(request).set_secret(name, payload.value)
        return {"ok": True, "name": name}

    @app.delete("/api/secrets/{name}")
    async def delete_secret(request: Request, name: str, user: str = Depends(require_user)):
        get_db(request).delete_secret(name)
        return {"ok": True}

    @app.get("/api/tokens")
    async def list_tokens(request: Request, user: str = Depends(require_user)):
        return get_db(request).query(
            "SELECT id, name, prefix, created_at, last_used FROM api_tokens ORDER BY id"
        )

    @app.post("/api/tokens")
    async def create_token(request: Request, payload: dict[str, str],
                           user: str = Depends(require_user)):
        name = (payload.get("name") or "").strip() or "api token"
        token = security.new_api_token()
        get_db(request).execute(
            "INSERT INTO api_tokens (name, token_hash, prefix, created_at) VALUES (?, ?, ?, ?)",
            (name, security.hash_token(token), security.token_prefix(token), time.time()),
        )
        return {"ok": True, "token": token, "name": name}

    @app.delete("/api/tokens/{token_id}")
    async def delete_token(request: Request, token_id: int, user: str = Depends(require_user)):
        get_db(request).execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
        return {"ok": True}

    # ------------------------------------------------------ export/import

    @app.get("/api/export")
    async def export_jobs(request: Request, user: str = Depends(require_user)):
        db = get_db(request)
        jobs = []
        for job in db.list_jobs():
            entry = {key: job[key] for key in Database.JOB_COLUMNS}
            entry["steps"] = job["steps"]
            entry["env"] = job["env"]
            entry.pop("steps_json", None)
            entry.pop("env_json", None)
            jobs.append(entry)
        return {"version": __version__, "exported_at": time.time(), "jobs": jobs}

    @app.post("/api/import")
    async def import_jobs(request: Request, payload: dict[str, Any],
                          user: str = Depends(require_user)):
        db = get_db(request)
        scheduler: Scheduler = request.app.state.scheduler
        created, skipped, errors = 0, 0, []
        for entry in payload.get("jobs", []):
            try:
                job = JobIn(**{
                    **entry,
                    "steps": entry.get("steps") or {"nodes": []},
                    "env": entry.get("env") or {},
                    "enabled": bool(entry.get("enabled", True)),
                    "catch_up": bool(entry.get("catch_up", False)),
                })
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{entry.get('name', '?')}: {exc}")
                continue
            if db.get_job_by_name(job.name.strip()):
                skipped += 1
                continue
            problems = _validate_job(job)
            if problems:
                errors.append(f"{job.name}: {'; '.join(problems)}")
                continue
            job_id = db.create_job(_job_columns(job))
            scheduler.reschedule(db.get_job(job_id))
            created += 1
        return {"ok": True, "created": created, "skipped": skipped, "errors": errors}

    # --------------------------------------------------------- websockets

    @app.websocket("/ws/events")
    async def events_socket(websocket: WebSocket):
        if not await _accept_socket(websocket):
            return
        queue = bus.subscribe("events")
        try:
            while True:
                message = await queue.get()
                await websocket.send_json(message)
        except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
            pass
        finally:
            bus.unsubscribe("events", queue)

    @app.websocket("/ws/runs/{run_id}")
    async def run_socket(websocket: WebSocket, run_id: int):
        if not await _accept_socket(websocket):
            return
        db: Database = websocket.app.state.db
        topic = f"run:{run_id}"
        queue = bus.subscribe(topic)
        try:
            run = db.get_run(run_id)
            if run:
                await websocket.send_json({
                    "type": "snapshot", "run_id": run_id, "status": run["status"],
                    "text": run.get("output") or "",
                    "steps": run.get("steps") or [],
                })
                if run["status"] != "running":
                    await websocket.send_json({
                        "type": "finished", "run_id": run_id, "status": run["status"],
                        "duration": run.get("duration"), "exit_code": run.get("exit_code"),
                    })
            while True:
                message = await queue.get()
                await websocket.send_json(message)
        except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
            pass
        finally:
            bus.unsubscribe(topic, queue)


async def _accept_socket(websocket: WebSocket) -> bool:
    """Authenticate a WebSocket using the same session cookie as the pages."""
    config: Config = websocket.app.state.config
    if not config.auth_disabled:
        cookie = websocket.cookies.get(SESSION_COOKIE)
        authorised = False
        if cookie:
            row = websocket.app.state.db.query_one(
                "SELECT expires_at FROM sessions WHERE token = ?", (cookie,)
            )
            authorised = bool(row and row["expires_at"] > time.time())
        if not authorised:
            await websocket.close(code=4401)
            return False
    await websocket.accept()
    return True
