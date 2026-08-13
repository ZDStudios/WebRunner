"""Cross-platform subprocess execution with streaming output and hard timeouts."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

IS_WINDOWS = sys.platform.startswith("win")

# POSIX gets a real SIGTERM, so it is worth waiting for a clean shutdown.
# On Windows `taskkill` without /F only posts WM_CLOSE, which console
# programs ignore outright — waiting the same 5s there just adds latency.
KILL_GRACE_SECONDS = 1.5 if IS_WINDOWS else 5.0
READ_CHUNK = 8192

OutputCallback = Callable[[str], None] | Callable[[str], Awaitable[None]]


@dataclass
class ProcessResult:
    exit_code: int | None = None
    stdout: str = ""
    timed_out: bool = False
    cancelled: bool = False
    error: str = ""
    pid: int | None = None
    truncated: bool = False


@dataclass
class OutputBuffer:
    """Accumulates output up to a byte budget, then keeps only a tail marker."""

    limit: int = 512 * 1024
    chunks: list[str] = field(default_factory=list)
    size: int = 0
    truncated: bool = False

    def append(self, text: str) -> str:
        """Store ``text`` and return the portion that was actually kept."""
        if self.truncated:
            return ""
        remaining = self.limit - self.size
        if remaining <= 0:
            self.truncated = True
            note = "\n… output truncated (limit reached) …\n"
            self.chunks.append(note)
            return note
        if len(text) > remaining:
            text = text[:remaining]
            self.truncated = True
            text += "\n… output truncated (limit reached) …\n"
        self.chunks.append(text)
        self.size += len(text)
        return text

    @property
    def text(self) -> str:
        return "".join(self.chunks)


def build_shell_invocation(shell: str, command: str) -> tuple[list[str], str | None]:
    """Work out how to hand ``command`` to ``shell``.

    Returns ``(argv, shell_string)``. When ``shell_string`` is set the caller
    must launch it through the platform shell rather than exec'ing argv.

    Quoting on Windows is the awkward part. ``subprocess`` builds a command
    line with ``list2cmdline``, which escapes an embedded quote as ``\\"`` --
    correct for normal programs, but cmd.exe does not understand that escape
    and passes the backslash through literally, so ``echo "hi"`` would print
    ``\\"hi\\"``. Two ways out, one per shell:

      * cmd.exe    -- let subprocess use its own shell path, which writes the
                      command line verbatim after ``cmd /c``.
      * PowerShell -- ``-EncodedCommand`` takes base64 of UTF-16LE, so no
                      quoting rules apply at all.
    """
    name = Path(shell).name.lower().removesuffix(".exe")

    if name in ("powershell", "pwsh"):
        # Silencing the progress stream stops PowerShell emitting a CLIXML
        # blob on stderr for otherwise perfectly successful commands. It stays
        # on the *same line* as the user's first statement so that line numbers
        # in error messages still match what they typed.
        preamble = "$ProgressPreference='SilentlyContinue'; "
        encoded = base64.b64encode((preamble + command).encode("utf-16-le")).decode("ascii")
        return [shell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], None

    if name == "cmd":
        if IS_WINDOWS:
            return [], command
        return [shell, "/d", "/s", "/c", command], None

    if name == "wsl":
        return [shell, "--", "bash", "-lc", command], None

    # sh, bash, zsh, dash, ... including Git Bash on Windows, which uses
    # POSIX quoting and so survives list2cmdline unchanged.
    return [shell, "-c", command], None


def build_shell_argv(shell: str, command: str) -> list[str]:
    """argv form of :func:`build_shell_invocation`, for callers that cannot
    fall back to the platform shell."""
    argv, shell_string = build_shell_invocation(shell, command)
    if shell_string is not None:
        return [shell, "/d", "/s", "/c", shell_string]
    return argv


class ClixmlFilter:
    """Turns PowerShell's CLIXML error stream back into readable text.

    When stderr is redirected, powershell.exe serialises its non-output
    streams as XML preceded by a ``#< CLIXML`` marker, so a failing command
    fills the run log with markup instead of the error message. Filtering is
    line-based, which also keeps a blob split across two pipe reads intact.
    """

    _ESCAPES = {"_x000D_": "\r", "_x000A_": "\n", "_x0009_": "\t", "_x0020_": " "}
    _ENTITIES = {"&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'", "&amp;": "&"}

    def __init__(self) -> None:
        self._tail = ""

    def feed(self, chunk: str) -> str:
        text = self._tail + chunk
        lines = text.split("\n")
        self._tail = lines.pop()          # keep the incomplete final line
        return "".join(self._line(line) for line in lines)

    def flush(self) -> str:
        remaining, self._tail = self._tail, ""
        return self._line(remaining, newline=False) if remaining else ""

    def _line(self, line: str, newline: bool = True) -> str:
        stripped = line.strip()
        if stripped == "#< CLIXML":
            return ""
        if stripped.startswith("<Objs ") and stripped.endswith("</Objs>"):
            decoded = self._decode(stripped)
            return (decoded + "\n") if decoded else ""
        return line + ("\n" if newline else "")

    @classmethod
    def _decode(cls, blob: str) -> str:
        pieces = re.findall(r"<S(?:\s[^>]*)?>(.*?)</S>", blob, re.DOTALL)
        if not pieces:
            return ""
        text = "".join(pieces)
        for token, replacement in cls._ESCAPES.items():
            text = text.replace(token, replacement)
        text = re.sub(r"_x([0-9A-Fa-f]{4})_", lambda m: chr(int(m.group(1), 16)), text)
        for entity, replacement in cls._ENTITIES.items():
            text = text.replace(entity, replacement)
        return text.rstrip("\r\n")


def uses_clixml(shell: str) -> bool:
    return Path(shell).name.lower().removesuffix(".exe") in ("powershell", "pwsh")


def _posix_preexec(run_as: str):  # pragma: no cover - POSIX only
    """Drop privileges to ``run_as`` in the child before exec."""
    import grp
    import pwd

    entry = pwd.getpwnam(run_as)

    def apply() -> None:
        os.setgid(entry.pw_gid)
        try:
            groups = [g.gr_gid for g in grp.getgrall() if entry.pw_name in g.gr_mem]
            os.setgroups(sorted(set(groups + [entry.pw_gid])))
        except (OSError, KeyError):
            pass
        os.setuid(entry.pw_uid)
        os.environ["HOME"] = entry.pw_dir
        os.environ["USER"] = entry.pw_name

    return apply


def terminate_tree(pid: int, force: bool = False) -> None:
    """Kill a process and everything it spawned."""
    if IS_WINDOWS:
        flags = ["/T", "/PID", str(pid)]
        if force:
            flags.insert(0, "/F")
        try:
            subprocess.run(
                ["taskkill", *flags],
                capture_output=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            pass
        return
    try:
        group = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        group = pid
    try:
        os.killpg(group, signal.SIGKILL if force else signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        except OSError:
            pass


async def run_command(
    argv: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    on_output: OutputCallback | None = None,
    buffer: OutputBuffer | None = None,
    run_as: str = "",
    stdin_text: str | None = None,
    on_start: Callable[[asyncio.subprocess.Process], None] | None = None,
    shell_string: str | None = None,
) -> ProcessResult:
    """Run ``argv``, streaming merged stdout/stderr to ``on_output``.

    If ``shell_string`` is given it is executed through the platform shell
    instead of ``argv`` -- see :func:`build_shell_invocation`.
    """
    buffer = buffer or OutputBuffer()
    result = ProcessResult()
    # Only PowerShell needs line-based filtering; everything else streams raw
    # so partial lines (progress bars, prompts) appear immediately.
    clixml = ClixmlFilter() if argv and uses_clixml(argv[0]) else None

    popen_kwargs: dict = {}
    if IS_WINDOWS:
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        popen_kwargs["start_new_session"] = True
        if run_as:
            try:
                popen_kwargs["preexec_fn"] = _posix_preexec(run_as)
            except KeyError:
                result.error = f"user {run_as!r} does not exist"
                result.exit_code = -1
                return result
            except PermissionError:
                result.error = f"not permitted to run as {run_as!r}"
                result.exit_code = -1
                return result

    if cwd:
        expanded = os.path.expanduser(cwd)
        if not os.path.isdir(expanded):
            result.error = f"working directory not found: {cwd}"
            result.exit_code = -1
            return result
        popen_kwargs["cwd"] = expanded

    stream_kwargs = dict(
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    label = shell_string if shell_string is not None else (argv[0] if argv else "")

    try:
        if shell_string is not None:
            process = await asyncio.create_subprocess_shell(
                shell_string, **stream_kwargs, **popen_kwargs
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *argv, **stream_kwargs, **popen_kwargs
            )
    except FileNotFoundError:
        result.error = f"command not found: {label}"
        result.exit_code = 127
        return result
    except PermissionError:
        result.error = f"permission denied: {label}"
        result.exit_code = 126
        return result
    except OSError as exc:
        result.error = f"failed to start process: {exc}"
        result.exit_code = -1
        return result

    result.pid = process.pid
    if on_start:
        on_start(process)

    if stdin_text is not None and process.stdin is not None:
        try:
            process.stdin.write(stdin_text.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    async def emit(text: str) -> None:
        if not text:
            return
        kept = buffer.append(text)
        if kept and on_output is not None:
            outcome = on_output(kept)
            if asyncio.iscoroutine(outcome):
                await outcome

    async def pump() -> None:
        assert process.stdout is not None
        try:
            while True:
                chunk = await process.stdout.read(READ_CHUNK)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                await emit(clixml.feed(text) if clixml else text)
        finally:
            if clixml:
                await emit(clixml.flush())

    pump_task = asyncio.create_task(pump())

    async def drain_and_wait() -> int:
        # Read to EOF first so no output is lost, then reap the exit status.
        await pump_task
        return await process.wait()

    waiter = asyncio.ensure_future(drain_and_wait())
    try:
        if timeout and timeout > 0:
            # shield() keeps our own cleanup in charge of stopping the child.
            result.exit_code = await asyncio.wait_for(asyncio.shield(waiter), timeout=timeout)
        else:
            result.exit_code = await waiter
    except asyncio.TimeoutError:
        result.timed_out = True
        result.error = f"timed out after {timeout:g}s"
        await _stop(process, pump_task)
        result.exit_code = process.returncode
    except asyncio.CancelledError:
        result.cancelled = True
        result.error = "cancelled"
        await _stop(process, pump_task)
        result.exit_code = process.returncode
        raise
    finally:
        for task in (pump_task, waiter):
            if not task.done():
                task.cancel()
        result.stdout = buffer.text
        result.truncated = buffer.truncated

    return result


async def _stop(process: asyncio.subprocess.Process, pump_task: asyncio.Task) -> None:
    """Terminate politely, then forcefully, then reap."""
    if process.returncode is None:
        terminate_tree(process.pid, force=False)
        try:
            await asyncio.wait_for(process.wait(), timeout=KILL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            terminate_tree(process.pid, force=True)
            try:
                await asyncio.wait_for(process.wait(), timeout=KILL_GRACE_SECONDS)
            except asyncio.TimeoutError:
                pass
    pump_task.cancel()
    try:
        await pump_task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 - draining only
        pass


def split_args(text: str) -> list[str]:
    """Split an argument string the way the local shell would."""
    if not text.strip():
        return []
    try:
        return shlex.split(text, posix=not IS_WINDOWS)
    except ValueError:
        return text.split()
