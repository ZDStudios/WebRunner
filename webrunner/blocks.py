"""Block (node) type registry.

Every block type is described once, here. The workflow engine reads these
definitions to validate node configuration, and the dashboard's node editor
reads the very same definitions (over ``GET /api/blocks``) to render its
palette and inspector forms. Adding a block type is a one-file change.

Field ``type`` values understood by the front-end:
    text, textarea, code, password, number, select, checkbox, keyvalue, note

``show_if`` hides a field unless another field holds one of the listed values.
"""

from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------- blocks

BLOCKS: list[dict[str, Any]] = [
    {
        "type": "shell",
        "label": "Shell Command",
        "icon": "terminal",
        "color": "#5b8def",
        "category": "Run",
        "description": "Run a command through the system shell.",
        "outputs": ["success", "failure"],
        "summary": "{command}",
        "fields": [
            {"key": "command", "label": "Command", "type": "code", "language": "shell",
             "required": True, "rows": 6,
             "placeholder": "echo \"hello from {{ job.name }}\"",
             "help": "Supports {{ vars.x }}, {{ secrets.NAME }} and {{ steps.<id>.stdout }}."},
            {"key": "shell", "label": "Shell override", "type": "text",
             "placeholder": "/bin/bash  ·  powershell  ·  cmd.exe",
             "help": "Leave blank to use the server default."},
            {"key": "cwd", "label": "Working directory", "type": "text",
             "placeholder": "/srv/app"},
            {"key": "env", "label": "Environment", "type": "keyvalue"},
            {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number", "min": 0,
             "placeholder": "0 = inherit job timeout"},
            {"key": "expect_exit", "label": "Success exit codes", "type": "text",
             "default": "0", "placeholder": "0   ·   0,1   ·   any"},
        ],
    },
    {
        "type": "script",
        "label": "Script File",
        "icon": "file-code",
        "color": "#7c5cff",
        "category": "Run",
        "description": "Run an existing script file on disk (.py, .sh, .ps1, .js …).",
        "outputs": ["success", "failure"],
        "summary": "{path}",
        "fields": [
            {"key": "path", "label": "Script path", "type": "text", "required": True,
             "placeholder": "/opt/scripts/sync.py"},
            {"key": "interpreter", "label": "Interpreter", "type": "select", "default": "auto",
             "options": [
                 {"value": "auto", "label": "Auto-detect from extension"},
                 {"value": "python", "label": "Python (this interpreter)"},
                 {"value": "bash", "label": "bash"},
                 {"value": "sh", "label": "sh"},
                 {"value": "powershell", "label": "PowerShell"},
                 {"value": "node", "label": "Node.js"},
                 {"value": "direct", "label": "Execute directly (shebang)"},
                 {"value": "custom", "label": "Custom…"},
             ]},
            {"key": "interpreter_path", "label": "Custom interpreter", "type": "text",
             "placeholder": "/usr/bin/php", "show_if": {"interpreter": ["custom"]}},
            {"key": "args", "label": "Arguments", "type": "text",
             "placeholder": "--verbose --date {{ now.date }}"},
            {"key": "cwd", "label": "Working directory", "type": "text",
             "placeholder": "Defaults to the script's own folder"},
            {"key": "env", "label": "Environment", "type": "keyvalue"},
            {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number", "min": 0},
        ],
    },
    {
        "type": "python",
        "label": "Python Code",
        "icon": "python",
        "color": "#3f9c6d",
        "category": "Run",
        "description": "Run an inline Python snippet.",
        "outputs": ["success", "failure"],
        "summary": "{code}",
        "fields": [
            {"key": "code", "label": "Python", "type": "code", "language": "python",
             "required": True, "rows": 12,
             "placeholder": "import json, urllib.request\nprint('hello')"},
            {"key": "cwd", "label": "Working directory", "type": "text"},
            {"key": "env", "label": "Environment", "type": "keyvalue"},
            {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number", "min": 0},
            {"key": "note", "type": "note",
             "text": "The snippet runs with the same Python that runs WebRunner. "
                     "Anything printed to stdout becomes this step's output; a non-zero "
                     "exit (or an uncaught exception) marks the step failed."},
        ],
    },
    {
        "type": "http",
        "label": "HTTP Request",
        "icon": "globe",
        "color": "#e0913a",
        "category": "Integrate",
        "description": "Call an API endpoint with headers, auth and a body.",
        "outputs": ["success", "failure"],
        "summary": "{method} {url}",
        "fields": [
            {"key": "method", "label": "Method", "type": "select", "default": "GET",
             "options": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]},
            {"key": "url", "label": "URL", "type": "text", "required": True,
             "placeholder": "https://api.example.com/v1/items"},
            {"key": "auth_type", "label": "Authentication", "type": "select", "default": "none",
             "options": [
                 {"value": "none", "label": "None"},
                 {"value": "bearer", "label": "Bearer token"},
                 {"value": "basic", "label": "Basic (user + password)"},
                 {"value": "header", "label": "Custom header"},
                 {"value": "query", "label": "Query parameter"},
             ]},
            {"key": "auth_token", "label": "Token", "type": "password",
             "placeholder": "{{ secrets.API_KEY }}",
             "show_if": {"auth_type": ["bearer", "header", "query"]},
             "help": "Reference a stored secret so it stays out of exports."},
            {"key": "auth_name", "label": "Header / parameter name", "type": "text",
             "placeholder": "X-API-Key", "show_if": {"auth_type": ["header", "query"]}},
            {"key": "auth_user", "label": "Username", "type": "text",
             "show_if": {"auth_type": ["basic"]}},
            {"key": "auth_password", "label": "Password", "type": "password",
             "show_if": {"auth_type": ["basic"]}},
            {"key": "headers", "label": "Headers", "type": "keyvalue",
             "placeholder_key": "Content-Type", "placeholder_value": "application/json"},
            {"key": "query", "label": "Query parameters", "type": "keyvalue"},
            {"key": "body_type", "label": "Body", "type": "select", "default": "none",
             "options": [
                 {"value": "none", "label": "No body"},
                 {"value": "json", "label": "JSON"},
                 {"value": "form", "label": "Form (urlencoded)"},
                 {"value": "raw", "label": "Raw"},
             ]},
            {"key": "body", "label": "Body content", "type": "code", "language": "json",
             "rows": 8, "show_if": {"body_type": ["json", "raw"]},
             "placeholder": "{\n  \"status\": \"ok\",\n  \"at\": \"{{ now.iso }}\"\n}"},
            {"key": "form", "label": "Form fields", "type": "keyvalue",
             "show_if": {"body_type": ["form"]}},
            {"key": "expect_status", "label": "Success statuses", "type": "text",
             "default": "2xx", "placeholder": "2xx   ·   200,201   ·   any"},
            {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number",
             "default": 30, "min": 1},
            {"key": "follow_redirects", "label": "Follow redirects", "type": "checkbox",
             "default": True},
            {"key": "verify_tls", "label": "Verify TLS certificate", "type": "checkbox",
             "default": True},
            {"key": "extract", "label": "Extract into variables", "type": "keyvalue",
             "placeholder_key": "token", "placeholder_value": "data.access_token",
             "help": "Variable name → dotted JSON path of the response, e.g. items.0.id"},
        ],
    },
    {
        "type": "service",
        "label": "Service Control",
        "icon": "server",
        "color": "#d1596b",
        "category": "Integrate",
        "description": "Start, stop or restart a system service.",
        "outputs": ["success", "failure"],
        "summary": "{action} {name}",
        "fields": [
            {"key": "backend", "label": "Manager", "type": "select", "default": "auto",
             "options": [
                 {"value": "auto", "label": "Auto (systemd on Linux, SCM on Windows)"},
                 {"value": "systemd", "label": "systemd"},
                 {"value": "windows", "label": "Windows Services"},
             ]},
            {"key": "name", "label": "Service name", "type": "text", "required": True,
             "placeholder": "nginx"},
            {"key": "action", "label": "Action", "type": "select", "default": "restart",
             "options": ["start", "stop", "restart", "reload", "status", "enable", "disable"]},
            {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number", "default": 60},
        ],
    },
    {
        "type": "docker",
        "label": "Docker",
        "icon": "container",
        "color": "#2f9bd4",
        "category": "Integrate",
        "description": "Control containers or run a one-off container command.",
        "outputs": ["success", "failure"],
        "summary": "docker {action} {target}",
        "fields": [
            {"key": "action", "label": "Action", "type": "select", "default": "restart",
             "options": ["start", "stop", "restart", "pull", "exec", "logs", "compose-up",
                         "compose-down", "compose-restart", "prune"]},
            {"key": "target", "label": "Container / image", "type": "text",
             "placeholder": "my-container",
             "show_if": {"action": ["start", "stop", "restart", "pull", "exec", "logs"]}},
            {"key": "command", "label": "Command", "type": "text",
             "placeholder": "sh -c 'php artisan migrate --force'",
             "show_if": {"action": ["exec"]}},
            {"key": "compose_file", "label": "Compose file", "type": "text",
             "placeholder": "/srv/stack/docker-compose.yml",
             "show_if": {"action": ["compose-up", "compose-down", "compose-restart"]}},
            {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number", "default": 300},
            {"key": "note", "type": "note",
             "text": "Inside the official image this needs the Docker socket mounted: "
                     "-v /var/run/docker.sock:/var/run/docker.sock"},
        ],
    },
    {
        "type": "set_vars",
        "label": "Set Variables",
        "icon": "variable",
        "color": "#8a8f98",
        "category": "Flow",
        "description": "Define or rewrite workflow variables.",
        "outputs": ["next"],
        "summary": "{vars}",
        "fields": [
            {"key": "vars", "label": "Variables", "type": "keyvalue",
             "placeholder_key": "target", "placeholder_value": "{{ steps.fetch.json.id }}",
             "help": "Values are templated, so you can build them from earlier steps."},
        ],
    },
    {
        "type": "condition",
        "label": "Condition",
        "icon": "branch",
        "color": "#c58af9",
        "category": "Flow",
        "description": "Branch on a comparison between two values.",
        "outputs": ["true", "false"],
        "summary": "{left} {operator} {right}",
        "fields": [
            {"key": "left", "label": "Left value", "type": "text", "required": True,
             "placeholder": "{{ steps.fetch.status_code }}"},
            {"key": "operator", "label": "Comparison", "type": "select", "default": "==",
             "options": [
                 {"value": "==", "label": "equals"},
                 {"value": "!=", "label": "does not equal"},
                 {"value": "contains", "label": "contains"},
                 {"value": "not_contains", "label": "does not contain"},
                 {"value": "matches", "label": "matches regex"},
                 {"value": ">", "label": "greater than"},
                 {"value": ">=", "label": "greater or equal"},
                 {"value": "<", "label": "less than"},
                 {"value": "<=", "label": "less or equal"},
                 {"value": "empty", "label": "is empty"},
                 {"value": "not_empty", "label": "is not empty"},
             ]},
            {"key": "right", "label": "Right value", "type": "text",
             "placeholder": "200",
             "show_if": {"operator": ["==", "!=", "contains", "not_contains", "matches",
                                      ">", ">=", "<", "<="]}},
        ],
    },
    {
        "type": "sleep",
        "label": "Wait",
        "icon": "clock",
        "color": "#8a8f98",
        "category": "Flow",
        "description": "Pause before continuing.",
        "outputs": ["next"],
        "summary": "wait {seconds}s",
        "fields": [
            {"key": "seconds", "label": "Seconds", "type": "number", "default": 5,
             "min": 0, "max": 86400},
        ],
    },
    {
        "type": "notify",
        "label": "Notify",
        "icon": "bell",
        "color": "#4fb286",
        "category": "Flow",
        "description": "Send a webhook or e-mail from inside the workflow.",
        "outputs": ["next"],
        "summary": "notify via {channel}",
        "fields": [
            {"key": "channel", "label": "Channel", "type": "select", "default": "webhook",
             "options": [
                 {"value": "webhook", "label": "Webhook (JSON POST)"},
                 {"value": "email", "label": "E-mail (SMTP settings)"},
             ]},
            {"key": "url", "label": "Webhook URL", "type": "text",
             "placeholder": "Leave blank to use the global webhook",
             "show_if": {"channel": ["webhook"]}},
            {"key": "subject", "label": "Subject", "type": "text",
             "default": "{{ job.name }}", "show_if": {"channel": ["email"]}},
            {"key": "message", "label": "Message", "type": "textarea", "rows": 4,
             "default": "{{ job.name }} reached this step at {{ now.iso }}"},
        ],
    },
    {
        "type": "stop",
        "label": "Stop",
        "icon": "stop",
        "color": "#8a8f98",
        "category": "Flow",
        "description": "End the run here with an explicit result.",
        "outputs": [],
        "summary": "stop: {result}",
        "fields": [
            {"key": "result", "label": "Mark run as", "type": "select", "default": "success",
             "options": [
                 {"value": "success", "label": "Success"},
                 {"value": "failed", "label": "Failed"},
             ]},
            {"key": "message", "label": "Message", "type": "text",
             "placeholder": "Nothing to do today"},
        ],
    },
]

BLOCKS_BY_TYPE: dict[str, dict[str, Any]] = {block["type"]: block for block in BLOCKS}


def defaults_for(block_type: str) -> dict[str, Any]:
    """Config dict pre-filled with each field's declared default."""
    block = BLOCKS_BY_TYPE.get(block_type)
    if not block:
        return {}
    config: dict[str, Any] = {}
    for field in block["fields"]:
        if field.get("type") == "note":
            continue
        if "default" in field:
            config[field["key"]] = field["default"]
        elif field.get("type") == "keyvalue":
            config[field["key"]] = {}
        elif field.get("type") == "checkbox":
            config[field["key"]] = False
        else:
            config[field["key"]] = ""
    return config


def validate_node(node: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems with a single node."""
    problems: list[str] = []
    block_type = node.get("type")
    block = BLOCKS_BY_TYPE.get(block_type or "")
    if not block:
        return [f"unknown block type {block_type!r}"]

    config = node.get("config") or {}
    label = node.get("name") or block["label"]
    for field in block["fields"]:
        if field.get("type") == "note" or not field.get("required"):
            continue
        # A required field only matters when its show_if condition is met.
        condition = field.get("show_if") or {}
        if any(str(config.get(key, "")) not in values for key, values in condition.items()):
            continue
        value = config.get(field["key"])
        if value in (None, "", [], {}):
            problems.append(f"{label}: {field['label']} is required")
    return problems


def validate_graph(graph: dict[str, Any]) -> list[str]:
    """Validate a whole workflow graph, returning all problems found."""
    problems: list[str] = []
    nodes = graph.get("nodes") or []
    if not isinstance(nodes, list) or not nodes:
        return ["the workflow needs at least one block"]

    ids: set[str] = set()
    for node in nodes:
        node_id = node.get("id")
        if not node_id:
            problems.append("a block is missing its id")
            continue
        if node_id in ids:
            problems.append(f"duplicate block id {node_id!r}")
        ids.add(node_id)
        problems.extend(validate_node(node))

    for node in nodes:
        for handle, target in (node.get("edges") or {}).items():
            if target and target not in ids:
                name = node.get("name") or node.get("id")
                problems.append(f"{name}: '{handle}' points at a block that no longer exists")

    start = graph.get("start")
    if start and start not in ids:
        problems.append("the start block no longer exists")
    return problems
