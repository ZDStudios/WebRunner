"""The workflow engine: walks a graph of blocks and runs each one."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import http_client, notify, services, template
from .blocks import BLOCKS_BY_TYPE
from .bus import bus
from .config import IS_WINDOWS, Config
from .db import Database
from .process import OutputBuffer, build_shell_invocation, run_command, split_args

log = logging.getLogger("webrunner.engine")

MAX_STEPS = 500
FLUSH_INTERVAL = 0.75
SAFE_ENV_KEY = re.compile(r"[^A-Za-z0-9_]")

SCRIPT_INTERPRETERS = {
    ".py": [sys.executable, "-u"],
    ".sh": ["bash"],
    ".bash": ["bash"],
    ".zsh": ["zsh"],
    ".ps1": ["powershell", "-NoProfile", "-NonInteractive", "-File"],
    ".js": ["node"],
    ".mjs": ["node"],
    ".cjs": ["node"],
    ".ts": ["node"],
    ".rb": ["ruby"],
    ".pl": ["perl"],
    ".php": ["php"],
    ".lua": ["lua"],
    ".r": ["Rscript"],
}

NAMED_INTERPRETERS = {
    "python": [sys.executable, "-u"],
    "bash": ["bash"],
    "sh": ["sh"],
    "powershell": ["powershell", "-NoProfile", "-NonInteractive", "-File"],
    "node": ["node"],
}


@dataclass
class StepResult:
    id: str
    type: str
    name: str
    status: str = "success"          # success | failed | warning | skipped | error
    started_at: float = 0.0
    duration: float = 0.0
    exit_code: int | None = None
    error: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("success", "warning", "skipped")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "duration": round(self.duration, 3),
            "exit_code": self.exit_code,
            "error": self.error,
        }


@dataclass
class RunOutcome:
    status: str = "success"          # success | failed | timeout | cancelled | error
    exit_code: int | None = 0
    error: str = ""
    output: str = ""
    truncated: bool = False
    steps: list[dict[str, Any]] = field(default_factory=list)
    variables: dict[str, Any] = field(default_factory=dict)


class _OutputSink:
    """Buffers run output, batching database writes and browser pushes."""

    def __init__(self, db: Database, run_id: int, limit: int):
        self.db = db
        self.run_id = run_id
        self.buffer = OutputBuffer(limit=limit)
        self.topic = f"run:{run_id}"
        self._pending: list[str] = []
        self._flush_task: asyncio.Task | None = None

    def write(self, text: str) -> None:
        kept = self.buffer.append(text)
        if not kept:
            return
        self._pending.append(kept)
        bus.publish(self.topic, {"type": "output", "run_id": self.run_id, "text": kept})
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_later())

    def line(self, text: str) -> None:
        self.write(text if text.endswith("\n") else text + "\n")

    async def _flush_later(self) -> None:
        try:
            await asyncio.sleep(FLUSH_INTERVAL)
        except asyncio.CancelledError:
            pass
        self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        chunk = "".join(self._pending)
        self._pending.clear()
        try:
            self.db.append_output(self.run_id, chunk)
        except Exception:  # noqa: BLE001 - never let logging kill a run
            log.exception("failed to persist output for run %s", self.run_id)

    async def close(self) -> None:
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        self.flush()


class WorkflowEngine:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config

    # ------------------------------------------------------------- helpers

    def _global_env(self) -> dict[str, str]:
        raw = self.db.get_setting("global_env", "{}")
        try:
            parsed = json.loads(raw) if raw.strip().startswith("{") else _parse_env_lines(raw)
        except ValueError:
            parsed = _parse_env_lines(raw)
        return {str(k): str(v) for k, v in (parsed or {}).items()}

    def _build_env(
        self, job: dict, node_env: dict, context: dict, run_id: int, step_id: str
    ) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items()}
        for source in (self._global_env(), job.get("env") or {}, node_env or {}):
            for key, value in (source or {}).items():
                key = str(key).strip()
                if key:
                    env[key] = template.render(str(value), context)
        env["WEBRUNNER_JOB_ID"] = str(job.get("id") or "")
        env["WEBRUNNER_JOB_NAME"] = str(job.get("name") or "")
        env["WEBRUNNER_RUN_ID"] = str(run_id)
        env["WEBRUNNER_STEP_ID"] = str(step_id)
        for key, value in (context.get("vars") or {}).items():
            safe = SAFE_ENV_KEY.sub("_", str(key)).upper()
            if safe:
                env[f"WR_{safe}"] = template.stringify(value)
        return env

    @staticmethod
    def _node_timeout(node_config: dict, job: dict) -> float:
        try:
            node_timeout = float(node_config.get("timeout_seconds") or 0)
        except (TypeError, ValueError):
            node_timeout = 0.0
        if node_timeout > 0:
            return node_timeout
        try:
            return float(job.get("timeout_seconds") or 0)
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------ execution

    async def execute(
        self,
        job: dict,
        run_id: int,
        trigger: str = "schedule",
        on_process=None,
    ) -> RunOutcome:
        graph = job.get("steps") or {"nodes": []}
        nodes = {node["id"]: node for node in graph.get("nodes", []) if node.get("id")}
        outcome = RunOutcome()
        sink = _OutputSink(self.db, run_id, self.config.max_output_bytes)

        if not nodes:
            outcome.status = "error"
            outcome.error = "this job has no blocks"
            outcome.exit_code = -1
            return outcome

        context = template.base_context(job, run_id)
        context["secrets"] = self.db.load_secrets()
        context["env"] = dict(os.environ)
        context["vars"] = {}

        start_id = graph.get("start") or graph.get("nodes", [{}])[0].get("id")
        if start_id not in nodes:
            start_id = next(iter(nodes))

        results: list[StepResult] = []
        current: str | None = start_id
        visited = 0
        started_wall = time.time()

        try:
            while current:
                node = nodes.get(current)
                if node is None:
                    outcome.status = "error"
                    outcome.error = f"block {current!r} does not exist"
                    outcome.exit_code = -1
                    break

                visited += 1
                if visited > MAX_STEPS:
                    outcome.status = "error"
                    outcome.error = (
                        f"stopped after {MAX_STEPS} blocks — the workflow is probably looping"
                    )
                    outcome.exit_code = -1
                    break

                block = BLOCKS_BY_TYPE.get(node.get("type", ""))
                label = node.get("name") or (block or {}).get("label") or node.get("type", "block")
                sink.line(f"\n\x1b[36m── [{visited}] {label} ({node.get('type')}) ──\x1b[0m")

                bus.publish(f"run:{run_id}", {
                    "type": "step", "run_id": run_id, "node_id": node["id"],
                    "status": "running", "index": visited,
                })

                result = await self._run_node(node, job, context, sink, run_id, on_process)
                results.append(result)
                context["steps"][node["id"]] = {**result.data, "status": result.status,
                                                "exit_code": result.exit_code}
                context["last"] = context["steps"][node["id"]]

                bus.publish(f"run:{run_id}", {
                    "type": "step", "run_id": run_id, "node_id": node["id"],
                    "status": result.status, "index": visited,
                    "duration": round(result.duration, 3),
                })

                if result.status == "cancelled":
                    outcome.status = "cancelled"
                    outcome.error = result.error or "cancelled"
                    outcome.exit_code = result.exit_code
                    break
                if result.status == "timeout":
                    outcome.status = "timeout"
                    outcome.error = result.error
                    outcome.exit_code = result.exit_code
                    break

                # An explicit Stop block ends the run with its own verdict.
                if node.get("type") == "stop":
                    outcome.status = result.data.get("result", "success")
                    outcome.error = result.data.get("message", "")
                    outcome.exit_code = 0 if outcome.status == "success" else 1
                    current = None
                    break

                edges = node.get("edges") or {}
                handle = self._pick_handle(node, block, result)
                next_id = edges.get(handle) or ""

                # A failed block only sinks the run when nothing catches it.
                # Wiring the 'failure' handle somewhere means the author has
                # handled it, so the run carries on from there.
                if not result.ok and not next_id:
                    outcome.status = "failed"
                    outcome.error = result.error or f"{label} failed"
                    outcome.exit_code = result.exit_code if result.exit_code is not None else 1
                    break

                current = next_id or None

            else:
                outcome.status = outcome.status or "success"

        except asyncio.CancelledError:
            outcome.status = "cancelled"
            outcome.error = "cancelled"
            outcome.exit_code = None
            sink.line("\n\x1b[33m✖ run cancelled\x1b[0m")
            await sink.close()
            outcome.output = sink.buffer.text
            outcome.truncated = sink.buffer.truncated
            outcome.steps = [r.as_dict() for r in results]
            outcome.variables = context.get("vars", {})
            raise RunCancelled(outcome) from None
        except Exception as exc:  # noqa: BLE001 - surfaced to the user as run output
            log.exception("workflow crashed for job %s", job.get("name"))
            outcome.status = "error"
            outcome.error = f"{type(exc).__name__}: {exc}"
            outcome.exit_code = -1
            sink.line(f"\n\x1b[31m✖ engine error: {outcome.error}\x1b[0m")

        if outcome.status == "success" and any(r.status == "failed" for r in results):
            # A failure that was routed away from, or explicitly ignored.
            failed = [r for r in results if r.status == "failed"]
            if all(nodes[r.id].get("ignore_failure") for r in failed if r.id in nodes):
                outcome.error = outcome.error or f"{len(failed)} step(s) failed but were ignored"

        duration = time.time() - started_wall
        icon = "✔" if outcome.status == "success" else "✖"
        colour = "32" if outcome.status == "success" else "31"
        sink.line(
            f"\n\x1b[{colour}m{icon} run {outcome.status} in {duration:.2f}s "
            f"({len(results)} block{'s' if len(results) != 1 else ''})\x1b[0m"
        )

        await sink.close()
        outcome.output = sink.buffer.text
        outcome.truncated = sink.buffer.truncated
        outcome.steps = [result.as_dict() for result in results]
        outcome.variables = context.get("vars", {})
        return outcome

    @staticmethod
    def _pick_handle(node: dict, block: dict | None, result: StepResult) -> str:
        outputs = (block or {}).get("outputs") or ["next"]
        if node.get("type") == "condition":
            return "true" if result.data.get("matched") else "false"
        if "success" in outputs:
            if result.ok:
                return "success"
            if node.get("ignore_failure") and not (node.get("edges") or {}).get("failure"):
                return "success"
            return "failure"
        return outputs[0] if outputs else "next"

    async def _run_node(
        self, node: dict, job: dict, context: dict, sink: _OutputSink,
        run_id: int, on_process,
    ) -> StepResult:
        block = BLOCKS_BY_TYPE.get(node.get("type", ""))
        result = StepResult(
            id=node.get("id", "?"),
            type=node.get("type", "?"),
            name=node.get("name") or (block or {}).get("label") or node.get("type", "block"),
            started_at=time.time(),
        )
        if block is None:
            result.status = "error"
            result.error = f"unknown block type {node.get('type')!r}"
            sink.line(f"\x1b[31m{result.error}\x1b[0m")
            return result

        config = template.render_value(node.get("config") or {}, context)
        started = time.monotonic()
        try:
            handler = getattr(self, f"_block_{node['type'].replace('-', '_')}")
            await handler(node, config, job, context, sink, result, run_id, on_process)
        except asyncio.CancelledError:
            result.status = "cancelled"
            result.error = "cancelled"
            raise
        except services.ServiceError as exc:
            result.status = "failed"
            result.error = str(exc)
            sink.line(f"\x1b[31m{exc}\x1b[0m")
        except Exception as exc:  # noqa: BLE001
            log.exception("block %s failed", node.get("id"))
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}"
            sink.line(f"\x1b[31m{result.error}\x1b[0m")
        finally:
            result.duration = time.monotonic() - started

        if result.status == "failed" and node.get("ignore_failure"):
            result.status = "warning"
            sink.line("\x1b[33m(failure ignored — continuing)\x1b[0m")
        return result

    # -------------------------------------------------------------- blocks

    async def _execute_argv(
        self, argv: list[str], node: dict, config: dict, job: dict, context: dict,
        sink: _OutputSink, result: StepResult, run_id: int, on_process,
        stdin_text: str | None = None, cwd_override: str | None = None,
        shell_string: str | None = None,
    ) -> None:
        env = self._build_env(job, config.get("env") or {}, context, run_id, result.id)
        cwd = cwd_override if cwd_override is not None else (config.get("cwd") or job.get("cwd") or "")
        timeout = self._node_timeout(config, job)

        # Each step captures its own output so `steps.<id>.stdout` refers to
        # that step alone; `sink` separately accumulates the whole run log.
        step_buffer = OutputBuffer(limit=self.config.max_output_bytes)
        process_result = await run_command(
            argv,
            cwd=cwd or None,
            env=env,
            timeout=timeout,
            on_output=sink.write,
            buffer=step_buffer,
            run_as=job.get("run_as") or "",
            stdin_text=stdin_text,
            on_start=on_process,
            shell_string=shell_string,
        )

        result.exit_code = process_result.exit_code
        result.data = {
            "exit_code": process_result.exit_code,
            "stdout": _tail(process_result.stdout, 64 * 1024),
            "pid": process_result.pid,
        }
        if process_result.timed_out:
            result.status = "timeout"
            result.error = process_result.error
            sink.line(f"\x1b[31m{process_result.error}\x1b[0m")
            return
        if process_result.cancelled:
            result.status = "cancelled"
            result.error = "cancelled"
            return
        if process_result.error:
            result.status = "failed"
            result.error = process_result.error
            sink.line(f"\x1b[31m{process_result.error}\x1b[0m")
            return

        expected = str(config.get("expect_exit") or "0").strip().lower()
        code = process_result.exit_code or 0
        if expected in ("any", "*"):
            accepted = True
        else:
            wanted = {int(p) for p in re.findall(r"-?\d+", expected)} or {0}
            accepted = code in wanted
        if not accepted:
            result.status = "failed"
            result.error = f"exited with code {code}"

    async def _block_shell(self, node, config, job, context, sink, result, run_id, on_process):
        command = (config.get("command") or "").strip()
        if not command:
            result.status = "failed"
            result.error = "no command configured"
            return
        shell = (config.get("shell") or self.config.default_shell).strip()
        argv, shell_string = build_shell_invocation(shell, command)
        await self._execute_argv(
            argv, node, config, job, context, sink, result, run_id, on_process,
            shell_string=shell_string,
        )

    async def _block_script(self, node, config, job, context, sink, result, run_id, on_process):
        path_text = (config.get("path") or "").strip()
        if not path_text:
            result.status = "failed"
            result.error = "no script path configured"
            return
        script = Path(os.path.expanduser(path_text))
        if not script.exists():
            result.status = "failed"
            result.error = f"script not found: {script}"
            sink.line(f"\x1b[31m{result.error}\x1b[0m")
            return

        choice = (config.get("interpreter") or "auto").strip().lower()
        # prefix of None means "hand the whole line to the shell" — the right
        # answer for .bat/.cmd and for extensions we have no mapping for.
        prefix: list[str] | None
        if choice == "auto":
            prefix = SCRIPT_INTERPRETERS.get(script.suffix.lower())
            if prefix is None and not IS_WINDOWS and os.access(script, os.X_OK):
                prefix = []                       # has a shebang and the +x bit
        elif choice == "direct":
            prefix = []
        elif choice == "custom":
            custom = (config.get("interpreter_path") or "").strip()
            if not custom:
                result.status = "failed"
                result.error = "custom interpreter not set"
                return
            prefix = split_args(custom)
        else:
            prefix = NAMED_INTERPRETERS.get(choice, [choice])

        args = (config.get("args") or "").strip()
        shell_string = None
        if prefix is None:
            path_text = str(script)
            quoted = f'"{path_text}"' if " " in path_text else path_text
            argv, shell_string = build_shell_invocation(
                self.config.default_shell, f"{quoted} {args}".strip()
            )
        else:
            argv = [*prefix, str(script), *split_args(args)]

        cwd = config.get("cwd") or job.get("cwd") or str(script.parent)
        await self._execute_argv(
            argv, node, config, job, context, sink, result, run_id, on_process,
            cwd_override=cwd, shell_string=shell_string,
        )

    async def _block_python(self, node, config, job, context, sink, result, run_id, on_process):
        code = config.get("code") or ""
        if not code.strip():
            result.status = "failed"
            result.error = "no Python code configured"
            return
        handle, temp_path = tempfile.mkstemp(
            suffix=".py", prefix="webrunner_", dir=str(self.config.data_dir)
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(code)
            argv = [sys.executable, "-u", temp_path]
            await self._execute_argv(
                argv, node, config, job, context, sink, result, run_id, on_process
            )
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    async def _block_http(self, node, config, job, context, sink, result, run_id, on_process):
        url = (config.get("url") or "").strip()
        if not url:
            result.status = "failed"
            result.error = "no URL configured"
            return

        method = (config.get("method") or "GET").upper()
        headers = {str(k): str(v) for k, v in (config.get("headers") or {}).items() if k}
        params = {str(k): str(v) for k, v in (config.get("query") or {}).items() if k}

        auth_type = (config.get("auth_type") or "none").lower()
        token = str(config.get("auth_token") or "")
        if auth_type == "bearer" and token:
            headers["Authorization"] = f"Bearer {token}"
        elif auth_type == "basic":
            headers["Authorization"] = http_client.basic_auth_header(
                str(config.get("auth_user") or ""), str(config.get("auth_password") or "")
            )
        elif auth_type == "header" and token:
            headers[str(config.get("auth_name") or "X-API-Key")] = token
        elif auth_type == "query" and token:
            params[str(config.get("auth_name") or "api_key")] = token

        body: bytes | None = None
        body_type = (config.get("body_type") or "none").lower()
        if body_type == "json":
            raw = (config.get("body") or "").strip()
            if raw:
                try:
                    json.loads(raw)          # fail fast on malformed JSON
                except ValueError as exc:
                    result.status = "failed"
                    result.error = f"request body is not valid JSON: {exc}"
                    sink.line(f"\x1b[31m{result.error}\x1b[0m")
                    return
                body = raw.encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif body_type == "form":
            import urllib.parse

            fields = {str(k): str(v) for k, v in (config.get("form") or {}).items() if k}
            body = urllib.parse.urlencode(fields).encode("utf-8")
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif body_type == "raw":
            body = (config.get("body") or "").encode("utf-8")

        try:
            timeout = float(config.get("timeout_seconds") or 30)
        except (TypeError, ValueError):
            timeout = 30.0

        sink.line(f"\x1b[90m→ {method} {url}\x1b[0m")
        response = await http_client.request(
            method, url,
            headers=headers,
            params=params,
            body=body,
            timeout=timeout,
            follow_redirects=bool(config.get("follow_redirects", True)),
            verify_tls=bool(config.get("verify_tls", True)),
            expect_status=str(config.get("expect_status") or "2xx"),
        )

        result.exit_code = response.status_code or (0 if response.ok else 1)
        result.data = {
            "status_code": response.status_code,
            "ok": response.ok,
            "headers": response.headers,
            "body": _tail(response.body, 64 * 1024),
            "json": response.json,
            "elapsed": round(response.elapsed, 3),
            "url": response.url,
        }

        if response.error and not response.status_code:
            result.status = "failed"
            result.error = response.error
            sink.line(f"\x1b[31m✖ {response.error}\x1b[0m")
            return

        colour = "32" if response.ok else "31"
        sink.line(
            f"\x1b[{colour}m← {response.status_code} {response.reason}\x1b[0m "
            f"\x1b[90m({response.elapsed * 1000:.0f} ms, {len(response.body)} bytes)\x1b[0m"
        )
        preview = response.body.strip()
        if preview:
            sink.write(_tail(preview, 8000) + "\n")

        # Extraction paths are relative to the decoded JSON body, with the
        # whole response available as a fallback for things like
        # `headers.Location` or `status_code`.
        envelope = {"json": response.json, "body": response.body,
                    "headers": response.headers, "status_code": response.status_code}
        for name, path in (config.get("extract") or {}).items():
            if not name:
                continue
            value = None
            if isinstance(response.json, (dict, list)):
                value = template.resolve(str(path), response.json)
            if value is None:
                value = template.resolve(str(path), envelope)
            context["vars"][str(name)] = value
            shown = template.stringify(value)[:200] if value is not None else "(not found)"
            sink.line(f"\x1b[90m  set {name} = {shown}\x1b[0m")

        if not response.ok:
            result.status = "failed"
            result.error = response.error or f"unexpected status {response.status_code}"

    async def _block_service(self, node, config, job, context, sink, result, run_id, on_process):
        argv = services.build_service_argv(
            (config.get("backend") or "auto").lower(),
            str(config.get("name") or ""),
            (config.get("action") or "restart").lower(),
        )
        sink.line(f"\x1b[90m$ {' '.join(argv[:3])} …\x1b[0m")
        await self._execute_argv(argv, node, config, job, context, sink, result, run_id, on_process)

    async def _block_docker(self, node, config, job, context, sink, result, run_id, on_process):
        argv = services.build_docker_argv(
            (config.get("action") or "restart").lower(),
            str(config.get("target") or ""),
            str(config.get("command") or ""),
            str(config.get("compose_file") or ""),
        )
        sink.line(f"\x1b[90m$ {' '.join(argv)}\x1b[0m")
        await self._execute_argv(argv, node, config, job, context, sink, result, run_id, on_process)

    async def _block_set_vars(self, node, config, job, context, sink, result, run_id, on_process):
        assigned = {}
        for key, value in (config.get("vars") or {}).items():
            if not key:
                continue
            context["vars"][str(key)] = value
            assigned[str(key)] = value
            sink.line(f"\x1b[90m  {key} = {template.stringify(value)[:200]}\x1b[0m")
        result.data = {"vars": assigned}
        result.exit_code = 0

    async def _block_sleep(self, node, config, job, context, sink, result, run_id, on_process):
        try:
            seconds = max(0.0, float(config.get("seconds") or 0))
        except (TypeError, ValueError):
            seconds = 0.0
        sink.line(f"\x1b[90m  waiting {seconds:g}s…\x1b[0m")
        await asyncio.sleep(seconds)
        result.data = {"seconds": seconds}
        result.exit_code = 0

    async def _block_condition(self, node, config, job, context, sink, result, run_id, on_process):
        left = str(config.get("left") or "")
        right = str(config.get("right") or "")
        operator = config.get("operator") or "=="
        matched = _compare(left, operator, right)
        result.data = {"matched": matched, "left": left, "right": right, "operator": operator}
        result.exit_code = 0
        arrow = "true" if matched else "false"
        sink.line(f"\x1b[90m  {left!r} {operator} {right!r} → \x1b[0m\x1b[1m{arrow}\x1b[0m")

    async def _block_notify(self, node, config, job, context, sink, result, run_id, on_process):
        channel = (config.get("channel") or "webhook").lower()
        message = str(config.get("message") or "")
        if channel == "email":
            settings = self.db.all_settings()
            await notify.send_email(
                settings, str(config.get("subject") or job.get("name") or "WebRunner"), message
            )
            sink.line("\x1b[90m  e-mail sent\x1b[0m")
        else:
            url = str(config.get("url") or "").strip() or self.db.get_setting("notify_url")
            if not url:
                result.status = "failed"
                result.error = "no webhook URL configured"
                return
            payload = {
                "event": "workflow.notify",
                "job": job.get("name"),
                "run_id": run_id,
                "message": message,
                "vars": context.get("vars", {}),
            }
            sent = await notify.send_webhook(url, payload)
            if not sent:
                result.status = "failed"
                result.error = "webhook failed"
                return
            sink.line(f"\x1b[90m  webhook posted to {url}\x1b[0m")
        result.data = {"message": message, "channel": channel}
        result.exit_code = 0

    async def _block_stop(self, node, config, job, context, sink, result, run_id, on_process):
        verdict = (config.get("result") or "success").lower()
        message = str(config.get("message") or "")
        result.data = {"result": "success" if verdict == "success" else "failed",
                       "message": message}
        result.status = "success" if verdict == "success" else "failed"
        result.exit_code = 0 if verdict == "success" else 1
        if message:
            sink.line(f"\x1b[90m  {message}\x1b[0m")


class RunCancelled(Exception):
    """Raised out of the engine when a run is cancelled, carrying its outcome."""

    def __init__(self, outcome: RunOutcome):
        super().__init__("run cancelled")
        self.outcome = outcome


def _compare(left: str, operator: str, right: str) -> bool:
    if operator == "empty":
        return not left.strip()
    if operator == "not_empty":
        return bool(left.strip())
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    if operator == "contains":
        return right in left
    if operator == "not_contains":
        return right not in left
    if operator == "matches":
        try:
            return re.search(right, left) is not None
        except re.error:
            return False
    try:
        left_number, right_number = float(left), float(right)
    except (TypeError, ValueError):
        return False
    if operator == ">":
        return left_number > right_number
    if operator == ">=":
        return left_number >= right_number
    if operator == "<":
        return left_number < right_number
    if operator == "<=":
        return left_number <= right_number
    return False


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


def _parse_env_lines(raw: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines, ignoring blanks and ``#`` comments."""
    result: dict[str, str] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result
