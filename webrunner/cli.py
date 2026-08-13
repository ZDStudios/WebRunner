"""Command line interface: ``webrunner serve``, ``install``, ``passwd`` and friends."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, security
from .config import IS_WINDOWS, Config, get_config, set_config

SERVICE_NAME = "webrunner"
SYSTEMD_UNIT_PATH = Path("/etc/systemd/system/webrunner.service")
WINDOWS_TASK_NAME = "WebRunner"


# ----------------------------------------------------------------- unit files


def systemd_unit(python: str, data_dir: Path, host: str, port: int, user: str = "") -> str:
    user_lines = f"User={user}\nGroup={user}\n" if user else ""
    return f"""[Unit]
Description=WebRunner scheduled job runner
Documentation=https://github.com/yourname/webrunner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
{user_lines}ExecStart={python} -m webrunner serve --host {host} --port {port}
Environment=WEBRUNNER_DATA_DIR={data_dir}
Environment=PYTHONUNBUFFERED=1
WorkingDirectory={data_dir}
Restart=always
RestartSec=5
TimeoutStopSec=30
KillMode=mixed

# Journald keeps the logs; `journalctl -u webrunner -f` to follow them.
StandardOutput=journal
StandardError=journal
SyslogIdentifier=webrunner

[Install]
WantedBy=multi-user.target
"""


# --------------------------------------------------------------------- serve


def cmd_serve(args: argparse.Namespace) -> int:
    import logging

    import uvicorn

    config = get_config()
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port
    if args.data_dir:
        config.data_dir = Path(args.data_dir)
        config.data_dir.mkdir(parents=True, exist_ok=True)
    set_config(config)

    logging.basicConfig(
        level=getattr(logging, (args.log_level or config.log_level).upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(name)-20s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    from .app import create_app

    app = create_app(config)
    url = f"http://{'localhost' if config.host in ('0.0.0.0', '::') else config.host}:{config.port}"
    print(f"\n  WebRunner {__version__}", file=sys.stderr)
    print(f"  dashboard : {url}", file=sys.stderr)
    print(f"  data dir  : {config.data_dir}", file=sys.stderr)
    if config.auth_disabled:
        print("  WARNING   : authentication is DISABLED (WEBRUNNER_AUTH_DISABLED)", file=sys.stderr)
    print("", file=sys.stderr)

    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level=(args.log_level or config.log_level).lower(),
        access_log=args.access_log,
        timeout_graceful_shutdown=20,
    )
    return 0


# ------------------------------------------------------------------- install


def cmd_install(args: argparse.Namespace) -> int:
    config = get_config()
    python = sys.executable
    host = args.host or config.host
    port = args.port or config.port

    if IS_WINDOWS:
        return _install_windows(python, config.data_dir, host, port, args)
    return _install_systemd(python, config.data_dir, host, port, args)


def _install_systemd(python: str, data_dir: Path, host: str, port: int,
                     args: argparse.Namespace) -> int:
    if os.geteuid() != 0:
        print("This needs root. Try: sudo -E webrunner install", file=sys.stderr)
        return 1

    unit = systemd_unit(python, data_dir, host, port, args.user or "")
    SYSTEMD_UNIT_PATH.write_text(unit, encoding="utf-8")
    print(f"wrote {SYSTEMD_UNIT_PATH}")

    for command in (
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", SERVICE_NAME],
        ["systemctl", "restart", SERVICE_NAME],
    ):
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  {' '.join(command)} failed: {result.stderr.strip()}", file=sys.stderr)
            return result.returncode
        print(f"  {' '.join(command)}")

    print(f"\nWebRunner is installed and starts on boot.")
    print(f"  status : systemctl status {SERVICE_NAME}")
    print(f"  logs   : journalctl -u {SERVICE_NAME} -f")
    print(f"  open   : http://localhost:{port}")
    return 0


def _install_windows(python: str, data_dir: Path, host: str, port: int,
                     args: argparse.Namespace) -> int:
    command = f'"{python}" -m webrunner serve --host {host} --port {port}'
    run_level = "HIGHEST" if args.elevated else "LIMITED"
    account = "SYSTEM" if args.system else os.environ.get("USERNAME", "")

    powershell = [
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        f"""
$action  = New-ScheduledTaskAction -Execute '{python}' `
    -Argument '-m webrunner serve --host {host} --port {port}'
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 999 -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
$principal = New-ScheduledTaskPrincipal -UserId '{account}' -LogonType ServiceAccount `
    -RunLevel {run_level}
Register-ScheduledTask -TaskName '{WINDOWS_TASK_NAME}' -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName '{WINDOWS_TASK_NAME}'
'installed'
""",
    ]
    env_command = [
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        f"[Environment]::SetEnvironmentVariable('WEBRUNNER_DATA_DIR', '{data_dir}', 'Machine')",
    ]
    subprocess.run(env_command, capture_output=True, text=True)
    result = subprocess.run(powershell, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        print("\nCould not register the scheduled task. Run this from an "
              "Administrator PowerShell prompt.", file=sys.stderr)
        return result.returncode

    print(f"Registered the '{WINDOWS_TASK_NAME}' scheduled task (starts at boot).")
    print(f"  status : Get-ScheduledTask -TaskName {WINDOWS_TASK_NAME}")
    print(f"  stop   : Stop-ScheduledTask -TaskName {WINDOWS_TASK_NAME}")
    print(f"  open   : http://localhost:{port}")
    print(f"  command: {command}")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    if IS_WINDOWS:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Unregister-ScheduledTask -TaskName '{WINDOWS_TASK_NAME}' -Confirm:$false"],
            capture_output=True, text=True,
        )
        print("removed the scheduled task" if result.returncode == 0 else result.stderr)
        return result.returncode

    if os.geteuid() != 0:
        print("This needs root. Try: sudo webrunner uninstall", file=sys.stderr)
        return 1
    subprocess.run(["systemctl", "disable", "--now", SERVICE_NAME], capture_output=True)
    if SYSTEMD_UNIT_PATH.exists():
        SYSTEMD_UNIT_PATH.unlink()
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
    print("removed the systemd unit (your data directory was left alone)")
    return 0


# --------------------------------------------------------------- maintenance


def cmd_passwd(args: argparse.Namespace) -> int:
    from .db import Database

    config = get_config()
    db = Database(config.db_path)
    username = args.username
    if not username:
        row = db.query_one("SELECT username FROM users ORDER BY id LIMIT 1")
        if not row:
            print("no users exist yet — start the server once to create the first account",
                  file=sys.stderr)
            return 1
        username = row["username"]

    row = db.query_one("SELECT id FROM users WHERE username = ?", (username,))
    password = args.password or getpass.getpass(f"New password for {username}: ")
    if not args.password:
        if password != getpass.getpass("Repeat: "):
            print("passwords do not match", file=sys.stderr)
            return 1
    if len(password) < 8:
        print("password must be at least 8 characters", file=sys.stderr)
        return 1

    hashed = security.hash_password(password)
    if row:
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hashed, row["id"]))
        db.execute("DELETE FROM sessions WHERE username = ?", (username,))
        print(f"password updated for {username} (existing sessions were signed out)")
    else:
        db.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, hashed, time.time()),
        )
        print(f"created user {username}")
    warning = security.password_strength_warning(password)
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    from .db import Database
    from .scheduler import describe_schedule

    db = Database(get_config().db_path)
    jobs = db.list_jobs()
    if not jobs:
        print("no jobs yet")
        return 0
    width = max(len(job["name"]) for job in jobs)
    print(f"{'NAME'.ljust(width)}  {'STATE':8} {'SCHEDULE':22} {'LAST':10} NEXT")
    for job in jobs:
        next_run = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(job["next_run"]))
            if job["next_run"] else "—"
        )
        print(
            f"{job['name'].ljust(width)}  "
            f"{'enabled' if job['enabled'] else 'disabled':8} "
            f"{describe_schedule(job)[:22]:22} "
            f"{(job['last_status'] or '—')[:10]:10} {next_run}"
        )
    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    """Run a job's workflow once, in this process, printing its output."""
    import logging

    from .db import Database
    from .engine import WorkflowEngine

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = get_config()
    db = Database(config.db_path)
    job = db.get_job_by_name(args.job)
    if not job and args.job.isdigit():
        job = db.get_job(int(args.job))
    if not job:
        print(f"no job named {args.job!r}", file=sys.stderr)
        return 1

    run_id = db.create_run(job, "cli")
    engine = WorkflowEngine(db, config)

    async def main() -> int:
        outcome = await engine.execute(job, run_id, "cli")
        finished = time.time()
        db.finish_run(
            run_id, status=outcome.status, finished_at=finished,
            duration=finished - db.get_run(run_id)["started_at"],
            exit_code=outcome.exit_code, error=outcome.error,
            steps=outcome.steps, vars=outcome.variables,
        )
        sys.stdout.write(outcome.output)
        sys.stdout.flush()
        return 0 if outcome.status == "success" else 1

    return asyncio.run(main())


def cmd_export(args: argparse.Namespace) -> int:
    from .db import Database

    db = Database(get_config().db_path)
    jobs = []
    for job in db.list_jobs():
        entry = {key: job[key] for key in Database.JOB_COLUMNS if key not in ("steps_json", "env_json")}
        entry["steps"] = job["steps"]
        entry["env"] = job["env"]
        jobs.append(entry)
    payload = json.dumps({"version": __version__, "jobs": jobs}, indent=2, ensure_ascii=False)
    if args.output and args.output != "-":
        Path(args.output).write_text(payload, encoding="utf-8")
        print(f"exported {len(jobs)} job(s) to {args.output}")
    else:
        print(payload)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    from .db import Database

    config = get_config()
    db = Database(config.db_path)
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    created = skipped = 0
    for entry in data.get("jobs", []):
        name = (entry.get("name") or "").strip()
        if not name or db.get_job_by_name(name):
            skipped += 1
            continue
        columns = {key: entry.get(key, "") for key in Database.JOB_COLUMNS}
        columns["name"] = name
        columns["enabled"] = 1 if entry.get("enabled", True) else 0
        columns["catch_up"] = 1 if entry.get("catch_up") else 0
        columns["steps_json"] = json.dumps(entry.get("steps") or {"nodes": []})
        columns["env_json"] = json.dumps(entry.get("env") or {})
        db.create_job(columns)
        created += 1
    print(f"imported {created} job(s), skipped {skipped}")
    print("restart WebRunner (or wait a tick) for the new schedules to take effect")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    config = get_config()
    print(f"WebRunner {__version__}")
    print(f"python    {sys.version.split()[0]} ({sys.executable})")
    print(f"platform  {sys.platform}")
    print(f"data dir  {config.data_dir}")
    print(f"database  {config.db_path} ({'exists' if config.db_path.exists() else 'not created yet'})")
    return 0


# --------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="webrunner",
        description="WebRunner — schedule and run commands, scripts and services from a web dashboard.",
    )
    parser.add_argument("--version", action="version", version=f"WebRunner {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the scheduler and web dashboard")
    serve.add_argument("--host", default="", help="bind address (default 0.0.0.0)")
    serve.add_argument("--port", type=int, default=0, help="port (default 8770)")
    serve.add_argument("--data-dir", default="", help="where the database lives")
    serve.add_argument("--log-level", default="", choices=["debug", "info", "warning", "error"])
    serve.add_argument("--access-log", action="store_true", help="log every HTTP request")
    serve.set_defaults(func=cmd_serve)

    install = sub.add_parser("install", help="start WebRunner automatically on boot")
    install.add_argument("--host", default="", help="bind address for the service")
    install.add_argument("--port", type=int, default=0)
    install.add_argument("--user", default="", help="Linux: run the service as this user")
    install.add_argument("--system", action="store_true",
                         help="Windows: run as SYSTEM instead of the current user")
    install.add_argument("--elevated", action="store_true",
                         help="Windows: run the task with highest privileges")
    install.set_defaults(func=cmd_install)

    uninstall = sub.add_parser("uninstall", help="remove the boot service")
    uninstall.set_defaults(func=cmd_uninstall)

    passwd = sub.add_parser("passwd", help="set a user's password")
    passwd.add_argument("username", nargs="?", default="")
    passwd.add_argument("--password", default="", help="non-interactive (avoid in shell history)")
    passwd.set_defaults(func=cmd_passwd)

    listing = sub.add_parser("list", help="list configured jobs")
    listing.set_defaults(func=cmd_list)

    execute = sub.add_parser("exec", help="run one job now, in the foreground")
    execute.add_argument("job", help="job name or id")
    execute.set_defaults(func=cmd_exec)

    export = sub.add_parser("export", help="write all jobs to JSON")
    export.add_argument("output", nargs="?", default="-")
    export.set_defaults(func=cmd_export)

    importer = sub.add_parser("import", help="load jobs from a JSON export")
    importer.add_argument("input")
    importer.set_defaults(func=cmd_import)

    version = sub.add_parser("version", help="show version and paths")
    version.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # Bare `webrunner` is the common case for a container entrypoint.
        args = parser.parse_args(["serve", *(argv or [])])
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
