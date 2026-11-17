#!/usr/bin/env bash
# Install or upgrade BlackHawk NetAgent as a systemd service on Debian or Ubuntu.
# Works on bare metal, in a VM, and in a Proxmox LXC container. Run as root from a copy of the repository:
#
#   ./scripts/install-service.sh [options]
#
# Re-running it upgrades the code and dependencies in place; settings and data are kept.
#
# Options:
#   --port N            Port to listen on (default 8080)
#   --public-url URL    External URL used in Teams/email links (default http://<this host's IP>:<port>)
#   --demo              Turn on demo mode (offline mock models and simulated devices)
#   --no-demo           Turn demo mode off again
#   --ca-cert FILE      Trust an extra CA certificate (TLS-inspecting proxy) for pip and for the service
#   --uninstall         Stop and remove the service and code; keep settings and data
#   --purge             With --uninstall: also delete settings, data and the service user
#   -h, --help          Show this help
set -euo pipefail

PREFIX=/opt/netagent
CONF_DIR=/etc/netagent
ENV_FILE=$CONF_DIR/netagent.env
DATA_DIR=/var/lib/netagent
SERVICE_USER=netagent
UNIT=/etc/systemd/system/netagent.service
WRAPPER=/usr/local/bin/netagent
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PORT="" PUBLIC_URL="" DEMO="" CA_CERT="" UNINSTALL=0 PURGE=0

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:?--port needs a value}"; shift 2 ;;
    --public-url) PUBLIC_URL="${2:?--public-url needs a value}"; shift 2 ;;
    --demo) DEMO=1; shift ;;
    --no-demo) DEMO=0; shift ;;
    --ca-cert) CA_CERT="${2:?--ca-cert needs a file}"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    --purge) PURGE=1; shift ;;
    -h|--help) usage 0 ;;
    *) warn "unknown option: $1"; usage 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run as root (for example: sudo $0)"
[ -d /run/systemd/system ] || die "systemd is not running on this system; it is required for the service"
if [ -n "$PORT" ] && ! [[ "$PORT" =~ ^[0-9]+$ && "$PORT" -ge 1 && "$PORT" -le 65535 ]]; then
  die "--port must be a number between 1 and 65535"
fi

# Set KEY=VALUE in the env file, replacing an existing (possibly commented-out) line.
set_env() {
  local key="$1" value="$2"
  if grep -qE "^#? *${key}=" "$ENV_FILE"; then
    sed -i -E "s|^#? *${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}
get_env() { sed -n -E "s/^${1}=(.*)$/\1/p" "$ENV_FILE" 2>/dev/null | tail -n 1; }

# ---------------------------------------------------------------------------------------------- uninstall
if [ "$UNINSTALL" -eq 1 ]; then
  say "Removing the NetAgent service"
  systemctl disable --now netagent 2>/dev/null || true
  rm -f "$UNIT" "$WRAPPER"
  systemctl daemon-reload
  rm -rf "$PREFIX"
  if [ "$PURGE" -eq 1 ]; then
    say "Deleting settings, data and the $SERVICE_USER user"
    rm -rf "$CONF_DIR" "$DATA_DIR"
    userdel "$SERVICE_USER" 2>/dev/null || true
  else
    say "Kept $CONF_DIR and $DATA_DIR (use --purge to delete them)"
  fi
  exit 0
fi

[ -f "$SRC_DIR/netagent/__main__.py" ] || die "run this script from a copy of the NetAgent repository (not found in $SRC_DIR)"
command -v apt-get >/dev/null || die "this installer supports Debian and Ubuntu (apt-get not found)"

# ----------------------------------------------------------------------------------------- packages
say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends python3 python3-venv ca-certificates rsync curl >/dev/null
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Python 3.10 or newer is required; this system has $(python3 --version 2>&1). Use Debian 12+ or Ubuntu 22.04+."

if [ -n "$CA_CERT" ]; then
  [ -f "$CA_CERT" ] || die "CA certificate not found: $CA_CERT"
  say "Adding CA certificate to the system trust store"
  install -m 0644 "$CA_CERT" /usr/local/share/ca-certificates/netagent-extra-ca.crt
  update-ca-certificates >/dev/null
fi
SYSTEM_BUNDLE=/etc/ssl/certs/ca-certificates.crt
if [ -n "$CA_CERT" ] || [ -f /usr/local/share/ca-certificates/netagent-extra-ca.crt ]; then
  export PIP_CERT="$SYSTEM_BUNDLE"  # pip uses its own bundle unless told otherwise
fi

# --------------------------------------------------------------------------------------- user, dirs
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  say "Creating service user $SERVICE_USER"
  useradd --system --user-group --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -o root -g root -m 0755 "$PREFIX"
install -d -o root -g "$SERVICE_USER" -m 0750 "$CONF_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$DATA_DIR"

# --------------------------------------------------------------------------------------------- code
say "Copying the application to $PREFIX"
# The package is replaced exactly; profiles, playbooks and config keep any files you added.
rsync -a --delete --exclude '__pycache__' "$SRC_DIR/netagent/" "$PREFIX/netagent/"
for dir in profiles knowledge config; do
  rsync -a --exclude '__pycache__' --exclude 'config.yaml' "$SRC_DIR/$dir/" "$PREFIX/$dir/"
done
install -m 0644 "$SRC_DIR/requirements.txt" "$PREFIX/requirements.txt"
[ -f "$SRC_DIR/README.md" ] && install -m 0644 "$SRC_DIR/README.md" "$PREFIX/README.md"

SYS_PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
VENV_PY_VERSION="$("$PREFIX/.venv/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
if [ "$VENV_PY_VERSION" != "$SYS_PY_VERSION" ]; then
  [ -n "$VENV_PY_VERSION" ] && say "Python changed ($VENV_PY_VERSION -> $SYS_PY_VERSION); rebuilding the virtualenv"
  rm -rf "$PREFIX/.venv"
  python3 -m venv "$PREFIX/.venv"
fi
say "Installing Python dependencies (this can take a few minutes the first time)"
"$PREFIX/.venv/bin/python" -m pip install -q --upgrade pip
"$PREFIX/.venv/bin/python" -m pip install -q -r "$PREFIX/requirements.txt"
"$PREFIX/.venv/bin/python" -m compileall -q "$PREFIX/netagent" >/dev/null || true
chown -R root:root "$PREFIX"
chmod -R u+rwX,go+rX,go-w "$PREFIX"

# ------------------------------------------------------------------------------------------ settings
FIRST_INSTALL=0
if [ ! -f "$ENV_FILE" ]; then
  FIRST_INSTALL=1
  say "Writing $ENV_FILE"
  cat > "$ENV_FILE" <<CONF
# BlackHawk NetAgent settings. Restart after changes: systemctl restart netagent
NETAGENT_HOST=0.0.0.0
NETAGENT_PORT=8080
NETAGENT_PUBLIC_URL=
NETAGENT_LOG_LEVEL=INFO
# Uncomment to fix the console sign-in token (otherwise one is generated: netagent token)
#NETAGENT_ADMIN_TOKEN=
# Cloud models (Claude). Keys can also be stored per model in the console instead.
#ANTHROPIC_API_KEY=
# Local models: Ollama in this container, on another LXC/VM, or on a GPU host
OLLAMA_BASE_URL=http://localhost:11434
#NETAGENT_DEMO=1
CONF
fi
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

OLD_PORT="$(get_env NETAGENT_PORT)"; OLD_PORT="${OLD_PORT:-8080}"
[ -n "$PORT" ] && set_env NETAGENT_PORT "$PORT"
PORT="$(get_env NETAGENT_PORT)"; PORT="${PORT:-8080}"
CURRENT_URL="$(get_env NETAGENT_PUBLIC_URL)"
if [ -n "$PUBLIC_URL" ]; then
  set_env NETAGENT_PUBLIC_URL "${PUBLIC_URL%/}"
elif [ -z "$CURRENT_URL" ]; then
  HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  set_env NETAGENT_PUBLIC_URL "http://${HOST_IP:-localhost}:$PORT"
elif [ "$PORT" != "$OLD_PORT" ] && [[ "$CURRENT_URL" =~ ^http://[^/]+:${OLD_PORT}$ ]]; then
  # The URL was derived from the old port; keep it in step. A URL set by hand is left alone.
  set_env NETAGENT_PUBLIC_URL "${CURRENT_URL%:"$OLD_PORT"}:$PORT"
fi
[ "$DEMO" = "1" ] && set_env NETAGENT_DEMO 1
[ "$DEMO" = "0" ] && sed -i -E 's/^NETAGENT_DEMO=.*/#NETAGENT_DEMO=1/' "$ENV_FILE"
if [ -f /usr/local/share/ca-certificates/netagent-extra-ca.crt ]; then
  # Python HTTP clients use their own CA bundle by default; point them at the system one.
  set_env SSL_CERT_FILE "$SYSTEM_BUNDLE"
  set_env REQUESTS_CA_BUNDLE "$SYSTEM_BUNDLE"
fi

# ------------------------------------------------------------------------------- service and CLI
say "Installing the systemd service"
install -m 0644 "$SRC_DIR/scripts/netagent.service" "$UNIT"
cat > "$WRAPPER" <<'CLI'
#!/bin/sh
# BlackHawk NetAgent command line, run as the service user with the service's settings.
#   netagent token                       print the console sign-in token
#   netagent create-key NAME [--scope]   create an API key
set -e
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || { echo "run as root" >&2; exit 1; }
  exec sudo "$0" "$@"
fi
set -a
. /etc/netagent/netagent.env
NETAGENT_DATA_DIR=/var/lib/netagent
set +a
cd /opt/netagent
exec runuser -u netagent -- /opt/netagent/.venv/bin/python -m netagent "$@"
CLI
chmod 0755 "$WRAPPER"
systemctl daemon-reload
systemctl enable -q netagent
systemctl restart netagent

say "Waiting for the service to answer on port $PORT"
for _ in $(seq 1 60); do
  if curl -fs -o /dev/null "http://127.0.0.1:$PORT/api/v1/health"; then
    URL="$(get_env NETAGENT_PUBLIC_URL)"
    printf '\n  \033[1mBlackHawk NetAgent is running\033[0m\n'
    printf '  Console:      %s\n' "$URL"
    if [ -n "$(get_env NETAGENT_ADMIN_TOKEN)" ]; then
      printf '  Admin token:  set in %s\n' "$ENV_FILE"
    else
      printf '  Admin token:  %s   (show again: netagent token)\n' "$("$WRAPPER" token)"
    fi
    printf '  Settings:     %s\n  Data:         %s   (back this up; it holds the encryption key)\n' "$ENV_FILE" "$DATA_DIR"
    printf '  Logs:         journalctl -u netagent -f\n'
    if [ "$FIRST_INSTALL" -eq 1 ] && [ "$(get_env NETAGENT_DEMO)" != "1" ]; then
      printf '\n  Next: add a model (ANTHROPIC_API_KEY in %s, or Ollama), or try --demo.\n' "$ENV_FILE"
    fi
    exit 0
  fi
  sleep 1
done
systemctl --no-pager --lines=30 status netagent || true
die "the service did not come up on port $PORT; see: journalctl -u netagent"
