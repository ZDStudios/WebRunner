#!/usr/bin/env bash
# WebRunner installer for Linux (systemd).
#
#   curl -fsSL .../install.sh | sudo bash
#   sudo ./install.sh --port 9000 --user webrunner
#
# Creates an isolated virtualenv under /opt/webrunner, installs WebRunner into
# it, registers a systemd unit and starts it. Re-running upgrades in place and
# leaves your database alone.

set -euo pipefail

PREFIX="${PREFIX:-/opt/webrunner}"
DATA_DIR="${DATA_DIR:-/var/lib/webrunner}"
SERVICE_NAME="webrunner"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
HOST="0.0.0.0"
PORT="8770"
RUN_USER="webrunner"
CREATE_USER=1
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  \033[36m•\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<EOF
WebRunner installer

  --port PORT       port to listen on (default ${PORT})
  --host ADDR       bind address (default ${HOST}; use 127.0.0.1 behind a proxy)
  --user NAME       system user to run as (default ${RUN_USER}; use root to run as root)
  --prefix PATH     install location (default ${PREFIX})
  --data-dir PATH   database location (default ${DATA_DIR})
  --no-create-user  do not create the system user
  -h, --help        this message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --user) RUN_USER="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --data-dir) DATA_DIR="$2"; shift 2 ;;
    --no-create-user) CREATE_USER=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option $1 (try --help)" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run this with sudo"
command -v systemctl >/dev/null 2>&1 || die "systemd not found — use Docker instead (see README)"

bold "Installing WebRunner"

# ---------------------------------------------------------------- python

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 0.0)"
    if [[ "$(printf '%s\n3.10\n' "$version" | sort -V | head -1)" == "3.10" ]]; then
      PYTHON="$candidate"
      break
    fi
  fi
done
[[ -n "$PYTHON" ]] || die "Python 3.10 or newer is required (apt install python3 python3-venv)"
info "using $($PYTHON -V) at $(command -v "$PYTHON")"

if ! "$PYTHON" -c 'import venv' >/dev/null 2>&1; then
  die "the venv module is missing — install python3-venv (Debian/Ubuntu) or python3-libs (RHEL)"
fi

# ------------------------------------------------------------------ user

if [[ "$RUN_USER" != "root" && $CREATE_USER -eq 1 ]]; then
  if id -u "$RUN_USER" >/dev/null 2>&1; then
    info "user '$RUN_USER' already exists"
  else
    useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin \
            --comment "WebRunner service account" "$RUN_USER"
    info "created system user '$RUN_USER'"
  fi
fi

# --------------------------------------------------------------- install

mkdir -p "$PREFIX" "$DATA_DIR"

if [[ ! -x "$PREFIX/venv/bin/python" ]]; then
  "$PYTHON" -m venv "$PREFIX/venv"
  info "created virtualenv at $PREFIX/venv"
fi

"$PREFIX/venv/bin/pip" install --quiet --upgrade pip wheel

if [[ -f "$SOURCE_DIR/pyproject.toml" ]]; then
  info "installing from $SOURCE_DIR"
  "$PREFIX/venv/bin/pip" install --quiet --upgrade "$SOURCE_DIR"
else
  info "installing from PyPI"
  "$PREFIX/venv/bin/pip" install --quiet --upgrade webrunner
fi

VERSION="$("$PREFIX/venv/bin/python" -c 'import webrunner; print(webrunner.__version__)')"
info "WebRunner $VERSION installed"

chown -R "$RUN_USER":"$RUN_USER" "$DATA_DIR" 2>/dev/null || true
chmod 750 "$DATA_DIR"

ln -sf "$PREFIX/venv/bin/webrunner" /usr/local/bin/webrunner
info "linked /usr/local/bin/webrunner"

# ---------------------------------------------------------------- systemd

USER_LINES=""
if [[ "$RUN_USER" != "root" ]]; then
  USER_LINES="User=${RUN_USER}
Group=${RUN_USER}
"
fi

cat > "$UNIT_PATH" <<EOF
[Unit]
Description=WebRunner scheduled job runner
Documentation=https://github.com/ZDStudios/WebRunner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
${USER_LINES}ExecStart=${PREFIX}/venv/bin/webrunner serve --host ${HOST} --port ${PORT}
Environment=WEBRUNNER_DATA_DIR=${DATA_DIR}
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=${DATA_DIR}
Restart=always
RestartSec=5
TimeoutStopSec=30
KillMode=mixed

StandardOutput=journal
StandardError=journal
SyslogIdentifier=webrunner

[Install]
WantedBy=multi-user.target
EOF

info "wrote $UNIT_PATH"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
systemctl restart "$SERVICE_NAME"
sleep 2

if ! systemctl is-active --quiet "$SERVICE_NAME"; then
  warn "the service did not start — recent log:"
  journalctl -u "$SERVICE_NAME" -n 25 --no-pager >&2 || true
  exit 1
fi

# ------------------------------------------------------------------ done

ADDRESS="$HOST"
[[ "$ADDRESS" == "0.0.0.0" || "$ADDRESS" == "::" ]] && ADDRESS="$(hostname -I 2>/dev/null | awk '{print $1}')"
[[ -z "$ADDRESS" ]] && ADDRESS="localhost"

echo
bold "WebRunner is running and will start on boot."
echo
echo "  dashboard   http://${ADDRESS}:${PORT}"
echo "  status      systemctl status ${SERVICE_NAME}"
echo "  logs        journalctl -u ${SERVICE_NAME} -f"
echo "  restart     systemctl restart ${SERVICE_NAME}"
echo "  data        ${DATA_DIR}"
echo

# The password file only appears on a genuinely first run — WebRunner
# generates one when the user table is empty.
for _ in 1 2 3 4 5 6; do
  [[ -f "$DATA_DIR/initial-password.txt" ]] && break
  sleep 0.5
done

if [[ -f "$DATA_DIR/initial-password.txt" ]]; then
  bold "First-run login"
  sed 's/^/    /' "$DATA_DIR/initial-password.txt"
  echo
  echo "  Change it in Settings, then delete $DATA_DIR/initial-password.txt"
  echo
else
  bold "No first-run password file at $DATA_DIR/initial-password.txt"
  echo
  echo "  Expected when an account already exists here, or when"
  echo "  WEBRUNNER_ADMIN_PASSWORD was set. Passwords are stored hashed and"
  echo "  cannot be recovered — set a new one instead:"
  echo
  echo "      sudo -u ${RUN_USER} ${PREFIX}/venv/bin/webrunner passwd admin"
  echo
fi

if [[ "$HOST" == "0.0.0.0" ]]; then
  warn "WebRunner runs commands on this machine. It is listening on all interfaces —"
  warn "keep it behind a firewall, VPN or authenticated reverse proxy."
fi
