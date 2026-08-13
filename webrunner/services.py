"""Discovery and control of system services: systemd, Windows SCM and Docker."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
from typing import Any

IS_WINDOWS = sys.platform.startswith("win")

# Service/container names are interpolated into PowerShell commands, so keep
# them to a conservative character set rather than trusting the caller.
SAFE_NAME = re.compile(r"^[A-Za-z0-9._@:+\-/\\]{1,160}$")

SYSTEMD_ACTIONS = {"start", "stop", "restart", "reload", "status", "enable", "disable"}
DOCKER_ACTIONS = {"start", "stop", "restart", "pull", "logs", "kill", "pause", "unpause"}


class ServiceError(RuntimeError):
    pass


def available_backends() -> list[str]:
    backends: list[str] = []
    if not IS_WINDOWS and shutil.which("systemctl"):
        backends.append("systemd")
    if IS_WINDOWS:
        backends.append("windows")
    if shutil.which("docker"):
        backends.append("docker")
    return backends


def default_backend() -> str:
    backends = available_backends()
    for preferred in ("systemd", "windows", "docker"):
        if preferred in backends:
            return preferred
    return ""


def _powershell() -> str:
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell"


async def _run(argv: list[str], timeout: float = 45.0) -> tuple[int, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    except FileNotFoundError:
        raise ServiceError(f"{argv[0]} is not installed or not on PATH") from None
    except OSError as exc:
        raise ServiceError(str(exc)) from None
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        raise ServiceError(f"{argv[0]} timed out after {timeout:g}s") from None
    return process.returncode or 0, stdout.decode("utf-8", errors="replace")


def _check_name(name: str) -> str:
    name = (name or "").strip()
    if not SAFE_NAME.match(name):
        raise ServiceError(f"invalid service name {name!r}")
    return name


# ------------------------------------------------------------------- systemd

async def _list_systemd() -> list[dict[str, Any]]:
    code, output = await _run(
        ["systemctl", "list-units", "--type=service", "--all",
         "--no-legend", "--no-pager", "--plain"]
    )
    if code != 0 and not output.strip():
        raise ServiceError("systemctl returned no output (is systemd running?)")

    services: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("●"):
            line = line.lstrip("● ").strip()
        if not line:
            continue
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        unit, load, active, sub = parts[:4]
        description = parts[4] if len(parts) > 4 else ""
        if not unit.endswith(".service"):
            continue
        services.append({
            "backend": "systemd",
            "name": unit.removesuffix(".service"),
            "unit": unit,
            "description": description,
            "state": sub,
            "active": active == "active",
            "loaded": load == "loaded",
            "enabled": None,
        })

    try:
        code, output = await _run(
            ["systemctl", "list-unit-files", "--type=service", "--no-legend",
             "--no-pager", "--plain"]
        )
        enabled_map = {}
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].endswith(".service"):
                enabled_map[parts[0]] = parts[1]
        for service in services:
            state = enabled_map.get(service["unit"])
            if state is not None:
                service["enabled"] = state == "enabled"
                service["startup"] = state
    except ServiceError:
        pass

    services.sort(key=lambda s: (not s["active"], s["name"].lower()))
    return services


async def _control_systemd(name: str, action: str) -> tuple[int, str]:
    if action not in SYSTEMD_ACTIONS:
        raise ServiceError(f"unsupported systemd action {action!r}")
    unit = _check_name(name)
    if not unit.endswith(".service") and "." not in unit:
        unit += ".service"
    if action == "status":
        return await _run(["systemctl", "status", unit, "--no-pager", "--lines=40"])
    return await _run(["systemctl", action, unit])


# ------------------------------------------------------------------- Windows

async def _list_windows() -> list[dict[str, Any]]:
    script = (
        "Get-Service | Select-Object Name,DisplayName,Status,StartType | "
        "ConvertTo-Json -Compress -Depth 3"
    )
    code, output = await _run([_powershell(), "-NoProfile", "-NonInteractive", "-Command", script])
    output = output.strip()
    if not output:
        raise ServiceError("Get-Service returned no output")
    try:
        payload = json.loads(output)
    except ValueError:
        raise ServiceError(f"could not parse Get-Service output: {output[:200]}") from None
    if isinstance(payload, dict):
        payload = [payload]

    status_names = {1: "Stopped", 2: "StartPending", 3: "StopPending", 4: "Running",
                    5: "ContinuePending", 6: "PausePending", 7: "Paused"}
    startup_names = {0: "Boot", 1: "System", 2: "Automatic", 3: "Manual", 4: "Disabled"}

    services = []
    for entry in payload:
        status = entry.get("Status")
        startup = entry.get("StartType")
        status_text = status_names.get(status, str(status)) if isinstance(status, int) else str(status)
        startup_text = (
            startup_names.get(startup, str(startup)) if isinstance(startup, int) else str(startup)
        )
        services.append({
            "backend": "windows",
            "name": entry.get("Name", ""),
            "unit": entry.get("Name", ""),
            "description": entry.get("DisplayName", ""),
            "state": status_text.lower(),
            "active": status_text.lower() == "running",
            "loaded": True,
            "enabled": startup_text in ("Automatic", "Boot", "System"),
            "startup": startup_text,
        })
    services.sort(key=lambda s: (not s["active"], s["name"].lower()))
    return services


async def _control_windows(name: str, action: str) -> tuple[int, str]:
    service = _check_name(name)
    commands = {
        "start": f"Start-Service -Name '{service}' -ErrorAction Stop; 'started'",
        "stop": f"Stop-Service -Name '{service}' -Force -ErrorAction Stop; 'stopped'",
        "restart": f"Restart-Service -Name '{service}' -Force -ErrorAction Stop; 'restarted'",
        "reload": f"Restart-Service -Name '{service}' -Force -ErrorAction Stop; 'restarted'",
        "enable": f"Set-Service -Name '{service}' -StartupType Automatic -ErrorAction Stop; 'enabled'",
        "disable": f"Set-Service -Name '{service}' -StartupType Disabled -ErrorAction Stop; 'disabled'",
        "status": f"Get-Service -Name '{service}' | Format-List Name,DisplayName,Status,StartType",
    }
    if action not in commands:
        raise ServiceError(f"unsupported Windows service action {action!r}")
    return await _run([_powershell(), "-NoProfile", "-NonInteractive", "-Command", commands[action]])


# -------------------------------------------------------------------- Docker

async def _list_docker() -> list[dict[str, Any]]:
    code, output = await _run(["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"])
    if code != 0:
        raise ServiceError(output.strip() or "docker ps failed")
    containers = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        state = (entry.get("State") or "").lower()
        containers.append({
            "backend": "docker",
            "name": entry.get("Names", ""),
            "unit": entry.get("ID", "")[:12],
            "description": f"{entry.get('Image', '')} · {entry.get('Status', '')}",
            "state": state,
            "active": state == "running",
            "loaded": True,
            "enabled": None,
            "startup": entry.get("RunningFor", ""),
        })
    containers.sort(key=lambda c: (not c["active"], c["name"].lower()))
    return containers


async def _control_docker(name: str, action: str) -> tuple[int, str]:
    if action not in DOCKER_ACTIONS:
        raise ServiceError(f"unsupported docker action {action!r}")
    target = _check_name(name)
    if action == "logs":
        return await _run(["docker", "logs", "--tail", "200", target])
    return await _run(["docker", action, target])


# --------------------------------------------------------------------- entry

async def list_services(backend: str) -> list[dict[str, Any]]:
    if backend == "systemd":
        return await _list_systemd()
    if backend == "windows":
        return await _list_windows()
    if backend == "docker":
        return await _list_docker()
    raise ServiceError(f"unknown service backend {backend!r}")


async def control(backend: str, name: str, action: str) -> tuple[int, str]:
    if backend == "auto":
        backend = default_backend()
    if backend == "systemd":
        return await _control_systemd(name, action)
    if backend == "windows":
        return await _control_windows(name, action)
    if backend == "docker":
        return await _control_docker(name, action)
    raise ServiceError(f"unknown service backend {backend!r}")


def build_service_argv(backend: str, name: str, action: str) -> list[str]:
    """argv for a service action, for the workflow block to run and stream."""
    if backend == "auto":
        backend = default_backend()
        if not backend:
            raise ServiceError("no service manager detected on this host")
    if backend == "systemd":
        if action not in SYSTEMD_ACTIONS:
            raise ServiceError(f"unsupported systemd action {action!r}")
        unit = _check_name(name)
        if not unit.endswith(".service") and "." not in unit:
            unit += ".service"
        if action == "status":
            return ["systemctl", "status", unit, "--no-pager", "--lines=40"]
        return ["systemctl", action, unit]
    if backend == "windows":
        service = _check_name(name)
        verbs = {
            "start": f"Start-Service -Name '{service}' -ErrorAction Stop",
            "stop": f"Stop-Service -Name '{service}' -Force -ErrorAction Stop",
            "restart": f"Restart-Service -Name '{service}' -Force -ErrorAction Stop",
            "reload": f"Restart-Service -Name '{service}' -Force -ErrorAction Stop",
            "enable": f"Set-Service -Name '{service}' -StartupType Automatic -ErrorAction Stop",
            "disable": f"Set-Service -Name '{service}' -StartupType Disabled -ErrorAction Stop",
            "status": f"Get-Service -Name '{service}' | Format-List Name,DisplayName,Status,StartType",
        }
        if action not in verbs:
            raise ServiceError(f"unsupported Windows service action {action!r}")
        return [_powershell(), "-NoProfile", "-NonInteractive", "-Command", verbs[action]]
    raise ServiceError(f"unknown service backend {backend!r}")


def build_docker_argv(action: str, target: str, command: str, compose_file: str) -> list[str]:
    """argv for the Docker workflow block."""
    if action.startswith("compose-"):
        verb = action.removeprefix("compose-")
        argv = ["docker", "compose"]
        if compose_file.strip():
            argv += ["-f", compose_file.strip()]
        if verb == "up":
            return argv + ["up", "-d"]
        if verb == "down":
            return argv + ["down"]
        if verb == "restart":
            return argv + ["restart"]
        raise ServiceError(f"unsupported compose action {action!r}")
    if action == "prune":
        return ["docker", "system", "prune", "-f"]

    name = _check_name(target)
    if action == "exec":
        from .process import split_args

        pieces = split_args(command)
        if not pieces:
            raise ServiceError("docker exec needs a command")
        return ["docker", "exec", name, *pieces]
    if action == "logs":
        return ["docker", "logs", "--tail", "200", name]
    if action in ("start", "stop", "restart", "pull", "kill"):
        return ["docker", action, name]
    raise ServiceError(f"unsupported docker action {action!r}")
