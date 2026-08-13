#!/usr/bin/env bash
# Remove the WebRunner service. Your jobs and history are kept unless you
# pass --purge.

set -euo pipefail

PREFIX="${PREFIX:-/opt/webrunner}"
DATA_DIR="${DATA_DIR:-/var/lib/webrunner}"
SERVICE_NAME="webrunner"
PURGE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge) PURGE=1; shift ;;
    -h|--help) echo "usage: sudo ./uninstall.sh [--purge]"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "run this with sudo" >&2; exit 1; }

systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
echo "  • stopped and removed the systemd unit"

rm -f /usr/local/bin/webrunner
rm -rf "$PREFIX"
echo "  • removed $PREFIX"

if [[ $PURGE -eq 1 ]]; then
  rm -rf "$DATA_DIR"
  userdel webrunner 2>/dev/null || true
  echo "  • purged $DATA_DIR and the service account"
else
  echo "  • kept your data in $DATA_DIR (use --purge to delete it)"
fi

echo "WebRunner has been uninstalled."
