#!/usr/bin/env bash
# Create a Debian LXC container on a Proxmox VE host and install BlackHawk NetAgent in it,
# or upgrade NetAgent in a container created earlier.
#
# Run on the Proxmox host as root, from a copy of this repository:
#
#   ./deploy/proxmox/netagent-lxc.sh create [options]
#   ./deploy/proxmox/netagent-lxc.sh upgrade <ctid> [--ca-cert FILE]
#
# create options (all optional):
#   --ctid N              Container ID (default: next free ID)
#   --hostname NAME       Hostname (default: netagent)
#   --cores N             CPU cores (default: 2)
#   --memory MB           Memory in MB (default: 2048)
#   --disk GB             Root disk size in GB (default: 8)
#   --storage NAME        Storage for the root disk (default: local-lvm, else local-zfs, else first that fits)
#   --template-storage N  Storage for the Debian template (default: local)
#   --debian 12|13        Debian release (default: 13 on Proxmox VE 9+, otherwise 12)
#   --bridge NAME         Network bridge (default: vmbr0)
#   --vlan TAG            VLAN tag on the bridge port
#   --ip CIDR|dhcp        IPv4 address, e.g. 10.10.20.50/24 (default: dhcp)
#   --gw IP               IPv4 gateway (required with a static --ip)
#   --port N              Console/API port inside the container (default: 8080)
#   --public-url URL      External URL for Teams/email links (default: http://<container IP>:<port>)
#   --ssh-key FILE        Public key file to authorise for root SSH into the container
#   --ca-cert FILE        Extra CA certificate to trust (TLS-inspecting proxy)
#   --demo                Start in demo mode (mock models, simulated devices)
#   --dry-run             Print the Proxmox commands without running them
#   -h, --help            Show this help
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CTID="" HOSTNAME_="netagent" CORES=2 MEMORY=2048 SWAP=512 DISK=8 STORAGE="" TSTORAGE="local" DEBIAN=""
BRIDGE="vmbr0" VLAN="" IP="dhcp" GW="" PORT=8080 PUBLIC_URL="" SSH_KEY="" CA_CERT="" DEMO=0 DRY_RUN=0

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit "${1:-0}"; }
run() {
  if [ "$DRY_RUN" -eq 1 ]; then printf '+'; printf ' %q' "$@"; printf '\n'; else "$@"; fi
}
TARBALL=""
trap 'rm -f "$TARBALL"' EXIT

ACTION="${1:-}"
case "$ACTION" in
  create|upgrade) shift ;;
  -h|--help|"") usage 0 ;;
  *) die "unknown action '$ACTION' (expected: create or upgrade)" ;;
esac
if [ "$ACTION" = "upgrade" ]; then
  CTID="${1:-}"; [ -n "$CTID" ] || die "usage: $0 upgrade <ctid> [--ca-cert FILE]"; shift
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --ctid) CTID="$2"; shift 2 ;;
    --hostname) HOSTNAME_="$2"; shift 2 ;;
    --cores) CORES="$2"; shift 2 ;;
    --memory) MEMORY="$2"; shift 2 ;;
    --disk) DISK="$2"; shift 2 ;;
    --storage) STORAGE="$2"; shift 2 ;;
    --template-storage) TSTORAGE="$2"; shift 2 ;;
    --debian) DEBIAN="$2"; shift 2 ;;
    --bridge) BRIDGE="$2"; shift 2 ;;
    --vlan) VLAN="$2"; shift 2 ;;
    --ip) IP="$2"; shift 2 ;;
    --gw) GW="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --public-url) PUBLIC_URL="$2"; shift 2 ;;
    --ssh-key) SSH_KEY="$2"; shift 2 ;;
    --ca-cert) CA_CERT="$2"; shift 2 ;;
    --demo) DEMO=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || [ "$DRY_RUN" -eq 1 ] || die "run as root on the Proxmox VE host"
command -v pct >/dev/null || die "pct not found: run this on a Proxmox VE host"
[ -f "$SRC_DIR/scripts/install-service.sh" ] || die "repository not found next to this script ($SRC_DIR)"
for n in "$CORES" "$MEMORY" "$DISK" "$PORT"; do [[ "$n" =~ ^[0-9]+$ ]] || die "expected a number, got '$n'"; done
[ -z "$CA_CERT" ] || [ -f "$CA_CERT" ] || die "CA certificate not found: $CA_CERT"
[ -z "$SSH_KEY" ] || [ -f "$SSH_KEY" ] || die "SSH key file not found: $SSH_KEY"

# Copy the repository into the container and run the installer there.
install_into() {
  local ctid="$1"; shift
  TARBALL="$(mktemp --suffix=.tar.gz)"
  tar -C "$SRC_DIR" -czf "$TARBALL" \
    --exclude=.git --exclude=.venv --exclude=data --exclude=__pycache__ --exclude=.pytest_cache --exclude=.env .
  run pct push "$ctid" "$TARBALL" /root/netagent-src.tar.gz
  local args=("$@")
  if [ -n "$CA_CERT" ]; then
    run pct push "$ctid" "$CA_CERT" /root/netagent-extra-ca.crt
    args+=(--ca-cert /root/netagent-extra-ca.crt)
  fi
  local quoted; quoted="$(printf ' %q' "${args[@]}")"
  run pct exec "$ctid" -- bash -c "set -e; rm -rf /root/netagent-src; mkdir -p /root/netagent-src; \
tar -xzf /root/netagent-src.tar.gz -C /root/netagent-src; rm -f /root/netagent-src.tar.gz; \
bash /root/netagent-src/scripts/install-service.sh${quoted}"
}

# ------------------------------------------------------------------------------------------- upgrade
if [ "$ACTION" = "upgrade" ]; then
  pct status "$CTID" >/dev/null 2>&1 || die "container $CTID does not exist"
  pct status "$CTID" | grep -q running || run pct start "$CTID"
  say "Upgrading NetAgent in container $CTID (settings and data are kept)"
  install_into "$CTID"
  exit 0
fi

# -------------------------------------------------------------------------------------------- create
if [ -z "$CTID" ]; then CTID="$(pvesh get /cluster/nextid)"; fi
[[ "$CTID" =~ ^[0-9]+$ ]] || die "invalid container ID '$CTID'"
if pct status "$CTID" >/dev/null 2>&1; then die "container $CTID already exists (use upgrade, or pick another --ctid)"; fi
if [ "$IP" != "dhcp" ]; then
  [[ "$IP" == */* ]] || die "--ip must be in CIDR form, e.g. 10.10.20.50/24 (or dhcp)"
  [ -n "$GW" ] || die "--gw is required with a static --ip"
fi

if [ -z "$STORAGE" ]; then
  STORAGES="$(pvesm status --content rootdir 2>/dev/null | awk 'NR > 1 && $3 == "active" {print $1}')"
  for candidate in local-lvm local-zfs; do
    if grep -qx "$candidate" <<<"$STORAGES"; then STORAGE="$candidate"; break; fi
  done
  STORAGE="${STORAGE:-$(head -n 1 <<<"$STORAGES")}"
  [ -n "$STORAGE" ] || die "no active storage accepts container disks; pass --storage"
fi

if [ -z "$DEBIAN" ]; then
  PVE_MAJOR="$(pveversion | sed -n 's#^pve-manager/\([0-9]*\)\..*#\1#p')"
  if [ "${PVE_MAJOR:-0}" -ge 9 ]; then DEBIAN=13; else DEBIAN=12; fi
fi
[[ "$DEBIAN" =~ ^(12|13)$ ]] || die "--debian must be 12 or 13"

say "Finding the Debian $DEBIAN container template"
run pveam update >/dev/null || true
TEMPLATE="$(pveam available --section system | awk -v d="debian-$DEBIAN-standard" '$2 ~ "^"d {print $2}' | sort -V | tail -n 1)"
[ -n "$TEMPLATE" ] || die "no debian-$DEBIAN-standard template is offered by pveam on this host"
if pveam list "$TSTORAGE" | awk '{print $1}' | grep -q "/$TEMPLATE\$"; then
  say "Template $TEMPLATE already downloaded"
else
  say "Downloading $TEMPLATE to $TSTORAGE"
  run pveam download "$TSTORAGE" "$TEMPLATE"
fi

NET="name=eth0,bridge=$BRIDGE,ip=$IP"
[ -n "$GW" ] && NET="$NET,gw=$GW"
[ -n "$VLAN" ] && NET="$NET,tag=$VLAN"
CREATE=(pct create "$CTID" "$TSTORAGE:vztmpl/$TEMPLATE"
  --hostname "$HOSTNAME_" --cores "$CORES" --memory "$MEMORY" --swap "$SWAP"
  --rootfs "$STORAGE:$DISK" --net0 "$NET" --ostype debian
  --unprivileged 1 --features nesting=1 --onboot 1 --timezone host
  --tags netagent --description "BlackHawk NetAgent - console on port $PORT")
[ -n "$SSH_KEY" ] && CREATE+=(--ssh-public-keys "$SSH_KEY")

say "Creating container $CTID ($HOSTNAME_: $CORES cores, ${MEMORY} MB RAM, ${DISK} GB on $STORAGE, $BRIDGE $IP)"
run "${CREATE[@]}"
run pct start "$CTID"

if [ "$DRY_RUN" -eq 0 ]; then
  say "Waiting for the container network"
  for i in $(seq 1 90); do
    if pct exec "$CTID" -- sh -c 'getent hosts deb.debian.org >/dev/null 2>&1'; then break; fi
    [ "$i" -eq 90 ] && die "container $CTID has no working network/DNS after 90 seconds (check --bridge/--ip/--gw/--vlan)"
    sleep 1
  done
  CT_IP="$(pct exec "$CTID" -- sh -c "hostname -I | awk '{print \$1}'")"
else
  CT_IP="<container-ip>"
fi

ARGS=(--port "$PORT" --public-url "${PUBLIC_URL:-http://$CT_IP:$PORT}")
[ "$DEMO" -eq 1 ] && ARGS+=(--demo)
say "Installing NetAgent inside container $CTID"
install_into "$CTID" "${ARGS[@]}"

cat <<DONE

  Container $CTID ($HOSTNAME_) is ready.
  Console:        ${PUBLIC_URL:-http://$CT_IP:$PORT}
  Sign-in token:  pct exec $CTID -- netagent token
  Settings:       pct exec $CTID -- nano /etc/netagent/netagent.env   (then: pct exec $CTID -- systemctl restart netagent)
  Logs:           pct exec $CTID -- journalctl -u netagent -f
  Upgrade later:  $0 upgrade $CTID
  Back up:        vzdump $CTID   (includes /var/lib/netagent, the database and its encryption key)
DONE
