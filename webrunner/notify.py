"""Outbound notifications: JSON webhooks and SMTP e-mail."""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
from email.message import EmailMessage
from typing import Any

from . import http_client

log = logging.getLogger("webrunner.notify")


async def send_webhook(url: str, payload: dict[str, Any], timeout: float = 15.0) -> bool:
    if not url.strip():
        return False
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    result = await http_client.request(
        "POST",
        url.strip(),
        headers={"Content-Type": "application/json"},
        body=body,
        timeout=timeout,
        expect_status="2xx",
    )
    if not result.ok:
        log.warning("webhook to %s failed: %s", url, result.error or result.status_code)
    return result.ok


def _send_email_sync(settings: dict[str, str], subject: str, body: str, to: str = "") -> bool:
    host = (settings.get("smtp_host") or "").strip()
    if not host:
        raise RuntimeError("SMTP is not configured (Settings → Notifications)")

    recipients = [
        address.strip()
        for address in (to or settings.get("smtp_to", "")).replace(";", ",").split(",")
        if address.strip()
    ]
    if not recipients:
        raise RuntimeError("no recipient address configured")

    message = EmailMessage()
    message["Subject"] = subject or "WebRunner notification"
    message["From"] = settings.get("smtp_from") or settings.get("smtp_user") or "webrunner@localhost"
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    port = int(settings.get("smtp_port") or 587)
    use_tls = (settings.get("smtp_tls") or "1") in ("1", "true", "yes", "on")

    if port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
    try:
        server.ehlo()
        if use_tls and port != 465:
            try:
                server.starttls()
                server.ehlo()
            except smtplib.SMTPNotSupportedError:
                pass
        user = settings.get("smtp_user") or ""
        password = settings.get("smtp_password") or ""
        if user:
            server.login(user, password)
        server.send_message(message)
    finally:
        try:
            server.quit()
        except smtplib.SMTPException:
            pass
    return True


async def send_email(settings: dict[str, str], subject: str, body: str, to: str = "") -> bool:
    return await asyncio.to_thread(_send_email_sync, settings, subject, body, to)


def run_payload(job: dict, run: dict, steps: list[dict] | None = None) -> dict[str, Any]:
    """The JSON body posted to webhooks when a run finishes."""
    return {
        "event": "run.finished",
        "job": {
            "id": job.get("id"),
            "name": job.get("name"),
            "tags": job.get("tags", ""),
        },
        "run": {
            "id": run.get("id"),
            "status": run.get("status"),
            "trigger": run.get("trigger"),
            "attempt": run.get("attempt", 1),
            "exit_code": run.get("exit_code"),
            "duration": round(float(run.get("duration") or 0), 3),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "error": (run.get("error") or "")[:2000],
            "output_tail": (run.get("output") or "")[-4000:],
        },
        "steps": [
            {
                "id": step.get("id"),
                "type": step.get("type"),
                "name": step.get("name"),
                "status": step.get("status"),
                "exit_code": step.get("exit_code"),
                "duration": step.get("duration"),
            }
            for step in (steps or [])
        ],
    }


def summary_text(job: dict, run: dict) -> str:
    lines = [
        f"Job:      {job.get('name')}",
        f"Status:   {run.get('status')}",
        f"Trigger:  {run.get('trigger')}",
        f"Exit code:{run.get('exit_code')}",
        f"Duration: {round(float(run.get('duration') or 0), 2)}s",
        f"Run ID:   {run.get('id')}",
    ]
    if run.get("error"):
        lines += ["", "Error:", str(run["error"])[:2000]]
    output = (run.get("output") or "").strip()
    if output:
        lines += ["", "Last output:", output[-3000:]]
    return "\n".join(lines)
