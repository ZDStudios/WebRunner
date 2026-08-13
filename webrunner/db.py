"""SQLite storage layer.

One process owns the database, so a single connection guarded by a re-entrant
lock is both simplest and fastest. WAL mode keeps readers from blocking the
scheduler while a long run streams its output in.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL UNIQUE,
    description         TEXT NOT NULL DEFAULT '',
    enabled             INTEGER NOT NULL DEFAULT 1,
    tags                TEXT NOT NULL DEFAULT '',

    -- The workflow graph: {"nodes": [...], "start": "n1"}
    steps_json          TEXT NOT NULL DEFAULT '{"nodes":[]}',

    trigger_type        TEXT NOT NULL DEFAULT 'cron',   -- cron|interval|date|manual
    cron                TEXT NOT NULL DEFAULT '',
    interval_seconds    INTEGER NOT NULL DEFAULT 0,
    run_at              TEXT NOT NULL DEFAULT '',       -- ISO-8601 local time
    timezone            TEXT NOT NULL DEFAULT '',
    start_at            TEXT NOT NULL DEFAULT '',
    end_at              TEXT NOT NULL DEFAULT '',

    cwd                 TEXT NOT NULL DEFAULT '',
    env_json            TEXT NOT NULL DEFAULT '{}',
    run_as              TEXT NOT NULL DEFAULT '',
    timeout_seconds     INTEGER NOT NULL DEFAULT 0,
    max_retries         INTEGER NOT NULL DEFAULT 0,
    retry_delay_seconds INTEGER NOT NULL DEFAULT 30,
    concurrency         TEXT NOT NULL DEFAULT 'skip',   -- allow|skip|queue|replace
    catch_up            INTEGER NOT NULL DEFAULT 0,

    notify_on           TEXT NOT NULL DEFAULT 'failure',-- never|failure|always
    notify_url          TEXT NOT NULL DEFAULT '',

    next_run            REAL,
    last_run            REAL,
    last_status         TEXT NOT NULL DEFAULT '',
    last_run_id         INTEGER,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_next ON jobs(enabled, next_run);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       INTEGER,
    job_name     TEXT NOT NULL DEFAULT '',
    trigger      TEXT NOT NULL DEFAULT 'schedule',  -- schedule|manual|retry|catchup|api
    status       TEXT NOT NULL DEFAULT 'running',   -- running|success|failed|timeout|cancelled|skipped|error
    attempt      INTEGER NOT NULL DEFAULT 1,
    started_at   REAL NOT NULL,
    finished_at  REAL,
    duration     REAL,
    exit_code    INTEGER,
    output       TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT '',
    steps_json   TEXT NOT NULL DEFAULT '[]',        -- per-node results
    vars_json    TEXT NOT NULL DEFAULT '{}',
    truncated    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_runs_job    ON runs(job_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, id DESC);
CREATE INDEX IF NOT EXISTS idx_runs_start  ON runs(started_at DESC);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    REAL NOT NULL,
    last_login    REAL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    user_agent TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    prefix     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    last_used  REAL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Values are never returned by the API; blocks reference them as
-- {{ secrets.NAME }} so API keys stay out of exported job definitions.
CREATE TABLE IF NOT EXISTS secrets (
    name       TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "timezone": "",
    "notify_url": "",
    "notify_on": "failure",
    "global_env": "{}",
    "retention_runs": "5000",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "smtp_to": "",
    "smtp_tls": "1",
}


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self.migrate()

    # ---------------------------------------------------------------- core

    def migrate(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            for key, value in DEFAULT_SETTINGS.items():
                self._conn.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value)
                )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cursor

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------ settings

    def get_setting(self, key: str, default: str = "") -> str:
        value = self.scalar("SELECT value FROM settings WHERE key = ?", (key,))
        return default if value is None else str(value)

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def all_settings(self) -> dict[str, str]:
        return {row["key"]: row["value"] for row in self.query("SELECT key, value FROM settings")}

    # -------------------------------------------------------------- secrets

    def list_secret_names(self) -> list[dict]:
        return self.query("SELECT name, updated_at FROM secrets ORDER BY name COLLATE NOCASE")

    def load_secrets(self) -> dict[str, str]:
        return {row["name"]: row["value"] for row in self.query("SELECT name, value FROM secrets")}

    def set_secret(self, name: str, value: str) -> None:
        self.execute(
            "INSERT INTO secrets(name, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (name, value, time.time()),
        )

    def delete_secret(self, name: str) -> None:
        self.execute("DELETE FROM secrets WHERE name = ?", (name,))

    # ---------------------------------------------------------------- jobs

    def list_jobs(self, include_disabled: bool = True) -> list[dict]:
        sql = "SELECT * FROM jobs"
        if not include_disabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY name COLLATE NOCASE"
        return [self._hydrate_job(row) for row in self.query(sql)]

    def get_job(self, job_id: int) -> dict | None:
        row = self.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return self._hydrate_job(row) if row else None

    def get_job_by_name(self, name: str) -> dict | None:
        row = self.query_one("SELECT * FROM jobs WHERE name = ?", (name,))
        return self._hydrate_job(row) if row else None

    def due_jobs(self, now: float) -> list[dict]:
        rows = self.query(
            "SELECT * FROM jobs WHERE enabled = 1 AND next_run IS NOT NULL "
            "AND next_run <= ? ORDER BY next_run",
            (now,),
        )
        return [self._hydrate_job(row) for row in rows]

    @staticmethod
    def _hydrate_job(row: dict) -> dict:
        job = dict(row)
        job["steps"] = _loads(job.get("steps_json"), {"nodes": []})
        job["env"] = _loads(job.get("env_json"), {})
        job["enabled"] = bool(job.get("enabled"))
        job["catch_up"] = bool(job.get("catch_up"))
        return job

    JOB_COLUMNS = (
        "name", "description", "enabled", "tags", "steps_json", "trigger_type", "cron",
        "interval_seconds", "run_at", "timezone", "start_at", "end_at", "cwd", "env_json",
        "run_as", "timeout_seconds", "max_retries", "retry_delay_seconds", "concurrency",
        "catch_up", "notify_on", "notify_url",
    )

    def create_job(self, data: dict) -> int:
        now = time.time()
        payload = {key: data.get(key) for key in self.JOB_COLUMNS}
        columns = list(payload) + ["next_run", "created_at", "updated_at"]
        values = list(payload.values()) + [data.get("next_run"), now, now]
        placeholders = ", ".join("?" for _ in columns)
        cursor = self.execute(
            f"INSERT INTO jobs ({', '.join(columns)}) VALUES ({placeholders})", values
        )
        return int(cursor.lastrowid)

    def update_job(self, job_id: int, data: dict) -> None:
        fields = {key: data[key] for key in self.JOB_COLUMNS if key in data}
        if "next_run" in data:
            fields["next_run"] = data["next_run"]
        if not fields:
            return
        fields["updated_at"] = time.time()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        self.execute(
            f"UPDATE jobs SET {assignments} WHERE id = ?", list(fields.values()) + [job_id]
        )

    def set_next_run(self, job_id: int, next_run: float | None) -> None:
        self.execute("UPDATE jobs SET next_run = ? WHERE id = ?", (next_run, job_id))

    def delete_job(self, job_id: int) -> None:
        self.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        self.execute("UPDATE runs SET job_id = NULL WHERE job_id = ?", (job_id,))

    # ---------------------------------------------------------------- runs

    def create_run(self, job: dict, trigger: str, attempt: int = 1, status: str = "running") -> int:
        cursor = self.execute(
            "INSERT INTO runs (job_id, job_name, trigger, status, attempt, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job.get("id"), job.get("name", ""), trigger, status, attempt, time.time()),
        )
        return int(cursor.lastrowid)

    def get_run(self, run_id: int) -> dict | None:
        row = self.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not row:
            return None
        row["steps"] = _loads(row.get("steps_json"), [])
        row["vars"] = _loads(row.get("vars_json"), {})
        return row

    def list_runs(
        self,
        job_id: int | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        clauses, params = [], []
        if job_id is not None:
            clauses.append("job_id = ?")
            params.append(job_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        rows = self.query(
            "SELECT id, job_id, job_name, trigger, status, attempt, started_at, finished_at, "
            f"duration, exit_code, truncated FROM runs {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params,
        )
        return rows

    def count_runs(self, job_id: int | None = None, status: str | None = None) -> int:
        clauses, params = [], []
        if job_id is not None:
            clauses.append("job_id = ?")
            params.append(job_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return int(self.scalar(f"SELECT COUNT(*) FROM runs {where}", params) or 0)

    def finish_run(self, run_id: int, **fields: Any) -> None:
        if "steps" in fields:
            fields["steps_json"] = _dumps(fields.pop("steps"))
        if "vars" in fields:
            fields["vars_json"] = _dumps(fields.pop("vars"))
        assignments = ", ".join(f"{key} = ?" for key in fields)
        self.execute(
            f"UPDATE runs SET {assignments} WHERE id = ?", list(fields.values()) + [run_id]
        )

    def append_output(self, run_id: int, chunk: str) -> None:
        self.execute(
            "UPDATE runs SET output = output || ? WHERE id = ?", (chunk, run_id)
        )

    def mark_orphaned_runs(self) -> int:
        """Runs still marked 'running' at startup belong to a previous process."""
        cursor = self.execute(
            "UPDATE runs SET status = 'error', finished_at = ?, "
            "error = CASE WHEN error = '' THEN 'Interrupted: WebRunner restarted' ELSE error END "
            "WHERE status = 'running'",
            (time.time(),),
        )
        return cursor.rowcount

    def prune_runs(self, keep: int) -> int:
        if keep <= 0:
            return 0
        cursor = self.execute(
            "DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT ?)",
            (keep,),
        )
        return cursor.rowcount

    def clear_runs(self, job_id: int | None = None) -> int:
        if job_id is None:
            cursor = self.execute("DELETE FROM runs WHERE status != 'running'")
        else:
            cursor = self.execute(
                "DELETE FROM runs WHERE job_id = ? AND status != 'running'", (job_id,)
            )
        return cursor.rowcount

    # --------------------------------------------------------------- stats

    def stats(self, window_seconds: float = 86400) -> dict:
        since = time.time() - window_seconds
        total = int(self.scalar("SELECT COUNT(*) FROM jobs") or 0)
        enabled = int(self.scalar("SELECT COUNT(*) FROM jobs WHERE enabled = 1") or 0)
        running = int(self.scalar("SELECT COUNT(*) FROM runs WHERE status = 'running'") or 0)
        recent = self.query(
            "SELECT status, COUNT(*) AS n FROM runs WHERE started_at >= ? GROUP BY status",
            (since,),
        )
        by_status = {row["status"]: row["n"] for row in recent}
        succeeded = by_status.get("success", 0)
        failed = sum(v for k, v in by_status.items() if k in ("failed", "timeout", "error"))
        return {
            "jobs_total": total,
            "jobs_enabled": enabled,
            "running_now": running,
            "runs_24h": sum(by_status.values()),
            "success_24h": succeeded,
            "failed_24h": failed,
            "by_status": by_status,
        }


def _loads(raw: Any, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
