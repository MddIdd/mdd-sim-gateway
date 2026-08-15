#!/bin/sh
# install.sh — one-click installer / lifecycle manager for MDD Sim Gateway.
#
# Deploys the whole system on any Docker-capable Linux with a PC/SC reader, in one of two
# DEPLOY MODES:
#
#   local   (DEFAULT) — control plane runs NATIVELY on the host (Python venv + systemd unit);
#                       Docker is used only as the ENGINE layer (per-SIM engine containers).
#                       The WebUI is still compiled with a throwaway Node container, so the
#                       host needs no Node/JS toolchain.
#   docker            — control plane AND engine both run in containers (control plane in a
#                       privileged container with the host Docker + pcscd sockets bind-mounted).
#
# In BOTH modes:
#   - Docker + host pcscd are installed and running
#   - pcsc-lite is version-LOCKED (PCSC_VERSION) across the host + every container image so the
#     PC/SC client/server protocol always matches (distro defaults differ -> "Failed to
#     establish context")
#   - the engine image is built from source with all bug-fix patches baked in (engine/patches/*)
#
# Usage:
#   sudo ./install.sh install [--mode local|docker]   # full install (default mode: local)
#   sudo ./install.sh reload  [--mode local|docker] [--no-cache] [--engines|--no-engines]
#   sudo ./install.sh build-lpac [--lpac-src PATH] [--dest DIR]   # local eSIM LPA (lpac)
#   sudo ./install.sh enable-autostart | disable-autostart
#   sudo ./install.sh uninstall [--purge]             # --purge also deletes ./data (+ venv)
#   ./install.sh status | logs
#
# The chosen mode is remembered (persisted under the data dir), so reload/status/logs/uninstall
# don't need --mode again. An explicit --mode or the MDD_MODE env var always overrides.
#
# Config via env (or a .env file next to this script):
#   MDD_MODE            deploy mode: local | docker                (default local)
#   MDD_PORT            host port to publish/serve the WebUI on    (default 8443)
#   MDD_DATA_DIR        runtime data dir                           (default <repo>/data)
#   MDD_ADVERTISE_ADDR  host LAN IP for SIP/WebRTC media           (default: auto-detect)
#   MDD_BIND            control bind addr                          (default 0.0.0.0)
#   MDD_ENGINE_BASE_IMAGE optional trusted local engine image for an offline overlay migration
#   MDD_REUSE_WEBUI     set to 1 to reuse a prebuilt, reviewed webui/dist in an offline install
#   PCSC_VERSION           pinned pcsc-lite version                   (default 2.3.3)
#   LPAC_SRC               optional path to lpac source (for build-lpac)
#   CMAKE_FETCH_VER        Kitware cmake version if system is too old (default 3.31.12)
set -eu

# ------------------------------------------------------------------ paths & config
SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
REPO_DIR="$SELF_DIR"
[ -f "$REPO_DIR/.env" ] && . "$REPO_DIR/.env"

MDD_PORT="${MDD_PORT:-8443}"
DATA_DIR_STATE="/etc/mdd-sim-gateway/data-dir"
if [ -n "${MDD_DATA_DIR+x}" ]; then
  MDD_DATA_DIR=$MDD_DATA_DIR
elif [ -r "$DATA_DIR_STATE" ]; then
  MDD_DATA_DIR=$(sed -n '1p' "$DATA_DIR_STATE")
  case "$MDD_DATA_DIR" in /*) ;; *) MDD_DATA_DIR="$REPO_DIR/data" ;; esac
else
  MDD_DATA_DIR="$REPO_DIR/data"
fi
MDD_BIND="${MDD_BIND:-0.0.0.0}"
MDD_ADVERTISE_ADDR="${MDD_ADVERTISE_ADDR:-}"

CONTROL_IMAGE="mdd-sim-gateway/control"
ENGINE_IMAGE="mdd-sim-gateway/engine"
CONTROL_NAME="mdd-sim-gateway-control"
ENGINE_PREFIX="mdd-sim-gateway-engine-"
MDD_DOCKER_LABEL="io.mdd-sim-gateway.managed"
WEBUI_BUILD_IMAGE="node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32"

# Native (local-mode) control plane bits.
VENV_DIR="$REPO_DIR/control/.venv"
WEBUI_DIST="$REPO_DIR/webui/dist"
SYSTEMD_UNIT="/etc/systemd/system/mdd-sim-gateway-control.service"
ORCHESTRATOR_UNIT="/etc/systemd/system/mdd-sim-gateway-orchestrator.service"
# Where the selected deploy mode is remembered between invocations.
MODE_STATE="$MDD_DATA_DIR/install-mode"

# pcsc-lite version pinned across host + both container images so the PC/SC client/server
# protocol always matches (distro defaults differ -> "Failed to establish context").
PCSC_VERSION="${PCSC_VERSION:-2.3.3}"
PCSC_SHA256="00b667aa71504ed1d39a48ad377de048c70dbe47229e8c48a3239ab62979c70f"
# Set by ensure_pcscd: 1 if we source-built pcsc-lite on the host (headers already in /usr),
# 0 if the distro package already matched the pin (need the distro -dev pkg for pyscard headers).
PCSC_SOURCE_BUILT=0
# CCID USB driver version, built from source ON THE HOST with local fixes (patches/ccid/*).
# NOT installed by default — run `sudo ./install.sh patch` to build + install it. Needed only
# for the HSIC CCID-Reader (1d99:0016): its firmware always answers "no ICC present" to
# GetSlotStatus even while a card is inserted and powered, so stock libccid never powers
# the card. NotifySlotChange only sets a pending flag; IFDHICCPresence tick probes
# via IccPowerOn/ATR (debounced) and restores prior power state.
# Keep this >= 1.6.2: that release added 1d99:0016 to the supported-reader table, so the
# built driver recognizes the VID/PID out of the box (older/distro libccid < 1.6.2 would
# additionally need the device whitelisted by hand in the bundle's Info.plist).
CCID_VERSION="${CCID_VERSION:-1.6.2}"
CCID_SHA256="6d5e6a6884090831ed155ee75cbc03aed252bd8158d94f507a94f05ebaba296c"

# Virtual smart-card driver, built from source because the packaged one is compiled for two
# slots. A module's SIM is exposed as one virtual reader per logical channel, and this gateway
# uses three (PIN keeper, SWu tunnel, IMS), so on the distro build the third channel has no
# socket behind it: its bridge thread dials a port pcscd never listens on and the channel is
# simply unavailable. The count is a compile-time constant (--enable-vpcdslots, upstream
# default 2), so a rebuild is the only way to raise it. Same upstream release Debian packages
# as 3.3+dfsg, so this changes the slot count and nothing else.
VPCD_VERSION="${VPCD_VERSION:-0.8}"
VPCD_SHA256="b428c399d5f014a350db0e8e5947ce69392429cc1aebdf3830af3c7f8078b18f"
VPCD_SLOTS="${VPCD_SLOTS:-4}"

# Host-side runtime dependencies. Versions and hashes are pinned so an upstream replacement
# cannot silently change what this root installer executes. Override only for a reviewed release.
SINGBOX_VERSION="${MDD_SINGBOX_VERSION:-1.13.15}"
SINGBOX_SHA256_AMD64="a3a3ff223b23c3f4731d0a17cb0ef94c97ce257c70721a5b07dc7ca079203c9f"
SINGBOX_SHA256_ARM64="f0810bbb5722ae36635687c421019defcc8b328d31a0b3c287901f331747ca93"
XRAY_VERSION="${MDD_XRAY_VERSION:-26.3.27}"
XRAY_SHA256_AMD64="23cd9af937744d97776ee35ecad4972cf4b2109d1e0fe6be9930467608f7c8ae"
XRAY_SHA256_ARM64="4d30283ae614e3057f730f67cd088a42be6fdf91f8639d82cb69e48cde80413c"
LPAC_VERSION="${MDD_LPAC_VERSION:-2.3.0}"
LPAC_COMMIT="c2fcf5e4b21c712d54e35a11da2ad9ad134fb821"
CMAKE_SHA256_AMD64="0dc2e9a6860f06bf10bd8fadc03e35d9eeb4df46e33763a7e480e987758f385c"
CMAKE_SHA256_ARM64="83f8fd91d2038a56556e1400390fcfe42f79602940c494f6c6f1cdae7f9e7f40"

# ------------------------------------------------------------------ pretty output
if [ -t 1 ]; then B=$(printf '\033[1m'); G=$(printf '\033[32m'); Y=$(printf '\033[33m'); R=$(printf '\033[31m'); N=$(printf '\033[0m'); else B=; G=; Y=; R=; N=; fi
info() { printf '%s==>%s %s\n' "$G$B" "$N" "$*"; }
warn() { printf '%s!!%s %s\n'  "$Y$B" "$N" "$*"; }
err()  { printf '%sxx%s %s\n'  "$R$B" "$N" "$*" >&2; }
die()  { err "$@"; exit 1; }

# ------------------------------------------------------------------ helpers
need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    die "this command needs root — re-run with: sudo $0 $CMD"
  fi
}

have() { command -v "$1" >/dev/null 2>&1; }

sha256_file() {
  if have sha256sum; then sha256sum "$1" | awk '{print $1}'
  elif have shasum; then shasum -a 256 "$1" | awk '{print $1}'
  else die "SHA-256 tool not found (install coreutils)"
  fi
}

download_verified() {
  url=$1 destination=$2 expected=$3
  have curl || pkg_install curl ca-certificates
  curl --fail --location --proto '=https' --tlsv1.2 --retry 3 --output "$destination" "$url"
  actual=$(sha256_file "$destination")
  [ "$actual" = "$expected" ] || die "checksum mismatch for $url (expected $expected, got $actual)"
}

host_arch() {
  case "$(uname -m)" in
    x86_64|amd64) printf '%s' amd64 ;;
    aarch64|arm64) printf '%s' arm64 ;;
    *) die "unsupported CPU architecture: $(uname -m)" ;;
  esac
}

ensure_singbox() {
  arch=$(host_arch)
  if have sing-box && sing-box version 2>/dev/null | grep -q "$SINGBOX_VERSION"; then
    info "sing-box $SINGBOX_VERSION already installed"
    return
  fi
  case "$arch" in
    amd64) digest=$SINGBOX_SHA256_AMD64 ;;
    arm64) digest=$SINGBOX_SHA256_ARM64 ;;
  esac
  archive="sing-box-${SINGBOX_VERSION}-linux-${arch}.tar.gz"
  temporary=$(mktemp -d /tmp/mdd-singbox.XXXXXX)
  trap 'rm -rf "$temporary"' EXIT HUP INT TERM
  info "installing sing-box $SINGBOX_VERSION ($arch)…"
  download_verified "https://github.com/SagerNet/sing-box/releases/download/v${SINGBOX_VERSION}/${archive}" "$temporary/$archive" "$digest"
  tar xzf "$temporary/$archive" -C "$temporary"
  install -m 0755 "$temporary/sing-box-${SINGBOX_VERSION}-linux-${arch}/sing-box" /usr/local/bin/sing-box
  rm -rf "$temporary"
  trap - EXIT HUP INT TERM
  sing-box version | head -n1
}

ensure_xray() {
  arch=$(host_arch)
  if have xray && xray version 2>/dev/null | grep -q "$XRAY_VERSION"; then
    info "Xray-core $XRAY_VERSION already installed"
    return
  fi
  case "$arch" in
    amd64) asset="Xray-linux-64.zip"; digest=$XRAY_SHA256_AMD64 ;;
    arm64) asset="Xray-linux-arm64-v8a.zip"; digest=$XRAY_SHA256_ARM64 ;;
  esac
  have unzip || pkg_install unzip
  temporary=$(mktemp -d /tmp/mdd-xray.XXXXXX)
  trap 'rm -rf "$temporary"' EXIT HUP INT TERM
  info "installing Xray-core $XRAY_VERSION ($arch)…"
  download_verified "https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VERSION}/${asset}" "$temporary/$asset" "$digest"
  unzip -q "$temporary/$asset" -d "$temporary/xray"
  install -m 0755 "$temporary/xray/xray" /usr/local/bin/xray
  rm -rf "$temporary"
  trap - EXIT HUP INT TERM
  xray version | head -n1
}

ensure_cellular_tools() {
  if have apt-get; then pkg_install modemmanager network-manager dbus
  elif have dnf || have yum; then pkg_install ModemManager NetworkManager dbus
  elif have pacman; then pkg_install modemmanager networkmanager dbus
  fi
  have mmcli || die "ModemManager command mmcli is unavailable"
  have nmcli || die "NetworkManager command nmcli is unavailable"
  ensure_modemmanager_command_interface
}

# The module SIM bridge sends APDUs through ModemManager's guarded AT command API.  Upstream
# exposes that API only when started with --debug; immediately lower its runtime logging back to
# INFO so this does not produce debug-volume logs.  The drop-in is MDD-owned and installed only
# when systemd manages ModemManager.
ensure_modemmanager_command_interface() {
  have systemctl || return 0
  modemmanager_bin=$(command -v ModemManager 2>/dev/null || true)
  [ -n "$modemmanager_bin" ] || die "ModemManager executable is unavailable"
  dropin_dir=/etc/systemd/system/ModemManager.service.d
  dropin_file=$dropin_dir/90-mdd-command-interface.conf
  temporary=$(mktemp /tmp/mdd-modemmanager.XXXXXX)
  cat >"$temporary" <<EOF
[Service]
ExecStart=
ExecStart=$modemmanager_bin --debug
ExecStartPost=-/usr/bin/busctl call org.freedesktop.ModemManager1 /org/freedesktop/ModemManager1 org.freedesktop.ModemManager1 SetLogging s INFO
EOF
  if [ ! -f "$dropin_file" ] || ! cmp -s "$temporary" "$dropin_file"; then
    install -d -m 0755 "$dropin_dir"
    install -m 0644 "$temporary" "$dropin_file"
    systemctl daemon-reload
    if systemctl is-active ModemManager.service >/dev/null 2>&1; then
      info "enabling ModemManager command interface for the module SIM bridge…"
      systemctl restart ModemManager.service
    fi
  fi
  rm -f "$temporary"
}

# Best-effort primary LAN IPv4 of THIS host (the address SIP/WebRTC clients must reach).
detect_lan_ip() {
  ip=""
  if have ip; then
    ip=$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1)
  fi
  if [ -z "$ip" ] && have hostname; then
    ip=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.' | grep -v '^127\.' | head -n1)
  fi
  printf '%s' "$ip"
}

# Detect the host package manager and install the given packages.
pkg_install() {
  if   have apt-get; then apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
  elif have dnf;     then dnf install -y "$@"
  elif have yum;     then yum install -y "$@"
  elif have pacman;  then pacman -Sy --noconfirm "$@"
  elif have zypper;  then zypper install -y "$@"
  elif have apk;     then apk add --no-cache "$@"
  else die "no supported package manager found (apt/dnf/yum/pacman/zypper/apk)"
  fi
}

svc_enable_start() {
  # Enable+start a system service across init systems (best-effort).
  if have systemctl; then systemctl enable --now "$1" 2>/dev/null || systemctl start "$1" 2>/dev/null || true
  elif have rc-update; then rc-update add "$1" default 2>/dev/null || true; rc-service "$1" start 2>/dev/null || true
  elif have service; then service "$1" start 2>/dev/null || true
  fi
}

# Ensure pcscd is running AND set to start on boot. pcscd ships differently across distros:
# some socket-activate it (pcscd.socket starts the daemon on first client), some don't enable
# it at all (seen on Armbian) — so a plain `systemctl enable --now pcscd` may leave it stopped
# and not persistent. We enable both the socket and the service, start the daemon now, and
# verify it actually came up (falling back to a manual daemon launch if the units don't exist).
enable_pcscd_autostart() {
  if have systemctl; then
    # Enable the socket (on-boot autostart, incl. socket-activated setups) and the service.
    systemctl enable pcscd.socket 2>/dev/null || true
    systemctl enable pcscd.service 2>/dev/null || systemctl enable pcscd 2>/dev/null || true
    # Start now: the socket triggers the daemon on first use; also start the service directly
    # for distros where the service isn't socket-activated.
    systemctl start pcscd.socket 2>/dev/null || true
    systemctl start pcscd.service 2>/dev/null || systemctl start pcscd 2>/dev/null || true
    # Verify. If neither the daemon nor its socket is present, force the service up once.
    if ! systemctl is-active --quiet pcscd 2>/dev/null && [ ! -S /run/pcscd/pcscd.comm ]; then
      systemctl restart pcscd.service 2>/dev/null || systemctl restart pcscd 2>/dev/null || true
    fi
  else
    svc_enable_start pcscd
  fi
  # Last-resort: if systemd units are absent/broken but the binary exists, launch it detached so
  # the reader is usable this session (autostart on non-systemd hosts is best-effort).
  if [ ! -S /run/pcscd/pcscd.comm ] && have pcscd && ! pgrep -x pcscd >/dev/null 2>&1; then
    mkdir -p /run/pcscd
    pcscd 2>/dev/null || true   # daemonizes by default; harmless if a unit already owns it
  fi
}

data_dir_abs() { CDPATH= cd -- "$MDD_DATA_DIR" 2>/dev/null && pwd -P || printf '%s' "$MDD_DATA_DIR"; }

# ------------------------------------------------------------------ deploy-mode state
persist_mode() {
  mkdir -p "$MDD_DATA_DIR"
  install -d -m 0755 "$(dirname -- "$DATA_DIR_STATE")"
  printf '%s\n' "$(data_dir_abs)" > "$DATA_DIR_STATE"
  chmod 0644 "$DATA_DIR_STATE"
  printf '%s\n' "$1" > "$MODE_STATE" 2>/dev/null || true
}

# Detect an EXISTING installation from real artifacts (not just the state file), and which
# plane it uses. Prints: local | docker | none.
#   local  = native systemd unit present
#   docker = control container present
detect_installed_mode() {
  if [ -f "$SYSTEMD_UNIT" ]; then echo local; return; fi
  if have docker && docker container inspect "$CONTROL_NAME" >/dev/null 2>&1; then echo docker; return; fi
  echo none
}

# Resolve the deploy mode. For lifecycle commands (start/stop/status/…): prefer what's actually
# installed, so controls act on the real plane regardless of the state file. Precedence:
#   --mode arg > MDD_MODE env > detected-from-artifacts > persisted state > default(local).
resolve_mode() {
  m="${MODE_ARG:-}"
  [ -z "$m" ] && m="${MDD_MODE:-}"
  if [ -z "$m" ]; then d=$(detect_installed_mode); [ "$d" != none ] && m="$d"; fi
  [ -z "$m" ] && [ -f "$MODE_STATE" ] && m=$(cat "$MODE_STATE" 2>/dev/null || true)
  [ -z "$m" ] && m="local"
  case "$m" in
    local|docker) ;;
    *) die "invalid deploy mode '$m' (use: local | docker)";;
  esac
  MODE="$m"
}

# True (0) if the control plane is currently running.
control_running() {
  if [ "${MODE:-}" = local ]; then
    have systemctl && systemctl is-active --quiet mdd-sim-gateway-control
  else
    [ "$(docker inspect -f '{{.State.Running}}' "$CONTROL_NAME" 2>/dev/null)" = true ]
  fi
}

# ------------------------------------------------------------------ prerequisites
ensure_docker() {
  if have docker && docker info >/dev/null 2>&1; then
    info "Docker present ($(docker --version | awk '{print $3}' | tr -d ,))"
    return
  fi
  if ! have docker; then
    info "installing Docker from the host distribution…"
    if have apt-get; then pkg_install docker.io
    elif have dnf || have yum; then pkg_install docker
    elif have pacman; then pkg_install docker
    elif have zypper; then pkg_install docker
    elif have apk; then pkg_install docker
    else die "install Docker from your operating-system vendor, then re-run"
    fi
  fi
  svc_enable_start docker
  docker info >/dev/null 2>&1 || die "Docker installed but the daemon is not running"
  info "Docker ready"
}

docker_container_owned() {
  name=$1
  label=$(docker inspect -f "{{ index .Config.Labels \"$MDD_DOCKER_LABEL\" }}" "$name" 2>/dev/null || true)
  image=$(docker inspect -f '{{.Config.Image}}' "$name" 2>/dev/null || true)
  [ "$label" = true ] || case "$image" in mdd-sim-gateway/*) return 0;; *) return 1;; esac
}

engine_names() {
  docker ps -a --format '{{.Names}}' 2>/dev/null | while IFS= read -r name; do
    case "$name" in
      "$ENGINE_PREFIX"*) docker_container_owned "$name" && printf '%s\n' "$name";;
    esac
  done
}

docker_preflight() {
  case "$MDD_PORT" in ''|*[!0-9]*) die "MDD_PORT must be a number between 1 and 65535";; esac
  [ "$MDD_PORT" -ge 1 ] && [ "$MDD_PORT" -le 65535 ] || die "MDD_PORT must be between 1 and 65535"

  security=$(docker info --format '{{json .SecurityOptions}}' 2>/dev/null || true)
  printf '%s' "$security" | grep -qi rootless && \
    die "rootless Docker is not supported: engine containers need privileged TUN and host PC/SC access"

  if docker inspect "$CONTROL_NAME" >/dev/null 2>&1 && ! docker_container_owned "$CONTROL_NAME"; then
    die "container name '$CONTROL_NAME' is already used by another project"
  fi
  for name in $(docker ps -a --format '{{.Names}}' 2>/dev/null); do
    case "$name" in
      "$ENGINE_PREFIX"*) docker_container_owned "$name" || \
        die "container name '$name' looks like MDD but is owned by another project";;
    esac
  done

  publishers=$(docker ps --filter "publish=$MDD_PORT" --format '{{.Names}}' 2>/dev/null || true)
  for name in $publishers; do
    [ "$name" = "$CONTROL_NAME" ] && docker_container_owned "$name" && continue
    die "TCP port $MDD_PORT is already published by Docker container '$name'"
  done
  if [ -z "$publishers" ] && have ss && ss -ltnH 2>/dev/null | awk '{print $4}' | \
      grep -Eq "(^|:)$MDD_PORT$"; then
    if ! { have systemctl && systemctl is-active --quiet mdd-sim-gateway-control; }; then
      die "TCP port $MDD_PORT is already in use by a non-MDD process"
    fi
  fi
  info "existing Docker daemon passed ownership, port and privilege checks; daemon configuration was left unchanged"
}

managed_control_exists() {
  docker inspect "$CONTROL_NAME" >/dev/null 2>&1 || return 1
  docker_container_owned "$CONTROL_NAME" || die "refusing to operate on foreign container '$CONTROL_NAME'"
}

ensure_pcscd() {
  # We PIN pcsc-lite to $PCSC_VERSION everywhere (host pcscd + container client libs) so the
  # PC/SC client/server protocol always matches — distro-default versions differ and cause
  # "Failed to establish context" between a client and the host daemon.  In BOTH deploy modes
  # the reader is owned by the HOST pcscd; engine containers (and, in docker mode, the control
  # container) are pcscd clients over the shared /run/pcscd socket.
  installed_ver=""
  if have pcscd; then installed_ver=$(pcscd --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1); fi

  if [ "$installed_ver" = "$PCSC_VERSION" ]; then
    info "host pcscd already at pinned version $PCSC_VERSION"
    PCSC_SOURCE_BUILT=0
  else
    # Install the CCID USB driver from the distro (its IFDHandler ABI is stable across pcscd
    # 2.x, so the distro driver works with our source-built pcscd), plus build deps.
    info "host pcscd is '${installed_ver:-none}', pinning to $PCSC_VERSION (building from source)…"
    if   have apt-get; then
      pkg_install libccid libudev-dev libsystemd-dev meson ninja-build flex pkg-config gcc wget ca-certificates
      # Debian 13 split systemd.pc out of the systemd package into systemd-dev, and that
      # file is what meson's dependency('systemd') resolves. Without it the pcsc-lite build
      # stops at "Dependency systemd found: NO". Older releases have no such package and
      # ship the file with systemd itself, so its absence must not fail the install.
      apt-get install -y systemd-dev >/dev/null 2>&1 || true
    elif have dnf || have yum; then pkg_install ccid systemd-devel meson ninja-build flex pkgconf-pkg-config gcc perl-podlators wget
    elif have pacman;  then pkg_install ccid meson ninja flex pkgconf gcc wget
    elif have zypper;  then pkg_install pcsc-ccid systemd-devel meson ninja flex pkg-config gcc wget
    else pkg_install ccid meson ninja flex gcc wget
    fi
    _build_pcsclite_host
    PCSC_SOURCE_BUILT=1
  fi
  enable_pcscd_autostart
  # pcscd may be socket-activated (daemon starts on first client), so the socket can be absent
  # until a reader is used — that's fine. If it IS present, confirm; else just note it.
  if [ -S /run/pcscd/pcscd.comm ] || { have systemctl && systemctl is-active --quiet pcscd 2>/dev/null; }; then
    info "host pcscd running + set to start on boot"
  else
    warn "pcscd not active yet — it is enabled for boot and will start on first reader use"
  fi
}

# Build + install the CCID USB driver $CCID_VERSION from source with a chosen SET of the
# fixes under patches/ccid/* applied. Args: $1 = short set label (used for the idempotency
# marker + logs), remaining args = patch filenames (under patches/ccid/) to apply in order.
# Patch sets (see the `patch*` subcommands):
#   01_hsic_slot_status.patch   HSIC 1d99:0016 broken GetSlotStatus — firmware always reports
#                               "no ICC present". NotifySlotChange sets pending; IFDHICCPresence
#                               tick probes via IccPowerOn/ATR (debounced). Base fix; safe for all cards.
#   02_hsic_malformed_atr.patch HSIC firmware drops the final TCK byte from the ATR; this
#                               patch synthesizes it at power-on (ISO 7816-3 XOR) and falls back
#                               to relaxed validation if repair fails. Fixes SCardConnect 607
#                               for (U)SIMs that work on other readers.
#   03_scr_prime_reader.patch   Adds SCR Prime 04d9:c001 to the supported-reader table. The
#                               device exposes a standard CCID interface but is absent upstream.
# The build installs into the pcsc-lite usbdropdir, replacing the distro libccid bundle files.
# Idempotent via a per-set marker file (switching sets rebuilds); on apt systems the distro
# libccid package is held so an upgrade can't clobber the patched driver.
# OPT-IN: not part of `install` — run `sudo ./install.sh patch | patch2 | patchall`.
ensure_ccid_host() {
  set_label="$1"; shift
  ccid_patches="$*"
  drivers_dir=$(pkg-config libpcsclite --variable usbdropdir 2>/dev/null || true)
  [ -n "$drivers_dir" ] || drivers_dir=/usr/lib/pcsc/drivers
  ccid_marker="$drivers_dir/ifd-ccid.bundle/Contents/.mdd-ccid-${CCID_VERSION}-${set_label}"
  if [ -f "$ccid_marker" ]; then
    info "patched CCID driver $CCID_VERSION (set: $set_label) already installed ($drivers_dir)"
    return
  fi
  info "building CCID driver $CCID_VERSION from source — patch set '$set_label' ($ccid_patches)…"
  if   have apt-get; then
    pkg_install meson ninja-build flex gcc pkg-config perl patch wget ca-certificates libusb-1.0-0-dev zlib1g-dev
    [ -f /usr/include/PCSC/pcsclite.h ] || pkg_install libpcsclite-dev
  elif have dnf || have yum; then
    pkg_install meson ninja-build flex gcc pkgconf-pkg-config perl patch wget libusb1-devel zlib-devel
    [ -f /usr/include/PCSC/pcsclite.h ] || pkg_install pcsc-lite-devel
  elif have pacman;  then
    pkg_install meson ninja flex gcc pkgconf perl patch wget libusb zlib
  elif have zypper;  then
    pkg_install meson ninja flex gcc pkg-config perl patch wget libusb-1_0-devel zlib-devel
    [ -f /usr/include/PCSC/pcsclite.h ] || pkg_install pcsc-lite-devel
  elif have apk;     then
    pkg_install meson ninja flex gcc pkgconfig perl patch wget musl-dev libusb-dev zlib-dev
    [ -f /usr/include/PCSC/pcsclite.h ] || pkg_install pcsc-lite-dev
  fi
  tmp=$(mktemp -d)
  download_verified "https://github.com/LudovicRousseau/CCID/archive/refs/tags/${CCID_VERSION}.tar.gz" "$tmp/ccid.tar.gz" "$CCID_SHA256"
  ( cd "$tmp" \
    && tar xf ccid.tar.gz && cd "CCID-${CCID_VERSION}" \
    && for p in $ccid_patches; do echo "applying $p"; patch -p1 < "$REPO_DIR/patches/ccid/$p" || exit 1; done \
    && meson setup builddir \
    && ninja -C builddir && ninja -C builddir install \
  ) || die "failed to build CCID driver $CCID_VERSION from source"
  rm -rf "$tmp"
  # drop any other-set markers so `status`/idempotency reflect the set just installed
  rm -f "$drivers_dir/ifd-ccid.bundle/Contents/.mdd-ccid-${CCID_VERSION}-"* 2>/dev/null || true
  touch "$ccid_marker" 2>/dev/null || true
  # keep a distro libccid upgrade from clobbering the patched bundle files
  if have apt-mark; then apt-mark hold libccid >/dev/null 2>&1 || true; fi
  # Reload the driver if pcscd is already running. Publish the same maintenance marker used
  # by the host orchestrator first, so the control-plane card monitor does not mistake this
  # planned enumeration gap for a physical unplug and stop healthy VoWiFi engines.
  if have systemctl; then
    install -d -m 0700 "$MDD_DATA_DIR/orchestrator"
    : > "$MDD_DATA_DIR/orchestrator/pcsc-maintenance"
    chmod 0600 "$MDD_DATA_DIR/orchestrator/pcsc-maintenance" 2>/dev/null || true
    systemctl restart pcscd 2>/dev/null || true
  fi
  info "patched CCID driver $CCID_VERSION (set: $set_label) installed to $drivers_dir"
}

_build_pcsclite_host() {
  # Build + install pcsc-lite $PCSC_VERSION to /usr on the host (client lib + headers + daemon),
  # and install a systemd unit so it runs as the reader daemon. Idempotent.
  tmp=$(mktemp -d)
  download_verified "https://github.com/LudovicRousseau/PCSC/archive/refs/tags/${PCSC_VERSION}.tar.gz" "$tmp/pcsc.tar.gz" "$PCSC_SHA256"
  ( cd "$tmp" \
    && tar xf pcsc.tar.gz && cd "PCSC-${PCSC_VERSION}" \
    && meson setup builddir --prefix=/usr -Dpolkit=false \
    && ninja -C builddir && ninja -C builddir install \
  ) || die "failed to build pcsc-lite $PCSC_VERSION from source"
  ldconfig 2>/dev/null || true
  rm -rf "$tmp"
  # Ensure a systemd unit + socket exist (meson installs them under /usr/lib/systemd/system).
  if have systemctl; then systemctl daemon-reload 2>/dev/null || true; fi
  info "host pcscd built + installed at $PCSC_VERSION"
}

# ------------------------------------------------------------------ image builds
# Build the engine image ONLY if it is missing, or if forced ($1 non-empty / --no-cache).
# The engine image is large and slow to build (~10-15 min: compiles Asterisk + pcsc-lite + the
# Python SWu tunnel deps), and it bakes every bug-fix patch under engine/patches/* via the
# Dockerfile — so an unforced reinstall reuses the existing patched image instead of rebuilding it.
# Files the overlay can refresh on its own, versus the ones that decide what the base contains.
# Splitting them is what lets an update ship an engine fix without a 15-minute Asterisk rebuild.
ENGINE_RUNTIME_FILES="pin_keeper.py ami_usim.py swu_ike.py log_capture.py render.py notify.py entrypoint.sh"
ENGINE_BASE_TAG="mdd-sim-gateway/engine-base:trusted"

engine_fingerprint() {
  # $1: runtime|base. Hash of the inputs that class owns; order is fixed so it is reproducible.
  eng="$REPO_DIR/engine"
  if [ "$1" = runtime ]; then
    # shellcheck disable=SC2086
    { for f in $ENGINE_RUNTIME_FILES; do [ -f "$eng/$f" ] && cat "$eng/$f"; done
      find "$eng/templates" -type f 2>/dev/null | LC_ALL=C sort | while read -r t; do cat "$t"; done
    } | sha256sum | cut -d' ' -f1
  else
    { cat "$eng/Dockerfile" 2>/dev/null
      echo "pcsc=$PCSC_VERSION"
      find "$eng/patches" -type f 2>/dev/null | LC_ALL=C sort | while read -r p; do cat "$p"; done
    } | sha256sum | cut -d' ' -f1
  fi
}

engine_image_label() {
  docker image inspect "$1" --format "{{index .Config.Labels \"$2\"}}" 2>/dev/null || true
}

# Decide how to produce the engine image, and say why. An unforced reload used to reuse whatever
# image was already there, so an engine-side fix shipped in a release never reached the box that
# self-updated — silently, because the control plane did update. The image now carries
# fingerprints of what went into it: only Asterisk-level inputs (Dockerfile, patches, pcsc
# version) cost a full rebuild, while a changed script is a seconds-long overlay that needs no
# registry at all. Forcing still overrides everything.
ensure_engine_image() {
  force="${1:-}"
  runtime_fp=$(engine_fingerprint runtime)
  base_fp=$(engine_fingerprint base)
  have_image=0
  docker image inspect "$ENGINE_IMAGE" >/dev/null 2>&1 && have_image=1

  if [ "$have_image" = 1 ] && [ -z "$force" ] && [ -z "$NOCACHE_FLAG" ]; then
    image_runtime=$(engine_image_label "$ENGINE_IMAGE" io.mdd-sim-gateway.runtime-fp)
    image_base=$(engine_image_label "$ENGINE_IMAGE" io.mdd-sim-gateway.base-fp)
    if [ "$image_base" = "$base_fp" ] && [ "$image_runtime" = "$runtime_fp" ]; then
      info "engine image $ENGINE_IMAGE matches this checkout — reusing"
      return
    fi
    if [ -n "$image_base" ] && [ "$image_base" = "$base_fp" ]; then
      # Only runtime-owned files moved: refresh them onto the image already installed.
      info "engine scripts changed — refreshing them onto the existing image (no rebuild)"
      engine_overlay_build "$ENGINE_IMAGE" "$runtime_fp" "$base_fp" && return
      warn "overlay refresh failed; falling back to a full engine rebuild"
    elif [ -z "$image_base" ]; then
      # Built before fingerprints existed: adopt it as the base and stamp it, rather than
      # forcing every existing install through a rebuild it may not be able to complete.
      info "engine image predates fingerprinting — refreshing scripts onto it and stamping it"
      engine_overlay_build "$ENGINE_IMAGE" "$runtime_fp" "$base_fp" && return
      warn "overlay refresh failed; falling back to a full engine rebuild"
    else
      info "engine base inputs changed (Dockerfile/patches/pcsc) — full rebuild required"
    fi
  fi

  if [ -n "${MDD_ENGINE_BASE_IMAGE:-}" ]; then
    docker image inspect "$MDD_ENGINE_BASE_IMAGE" >/dev/null 2>&1 || \
      die "trusted local engine base image not found: $MDD_ENGINE_BASE_IMAGE"
    info "building offline engine overlay from trusted local image $MDD_ENGINE_BASE_IMAGE"
    engine_overlay_build "$MDD_ENGINE_BASE_IMAGE" "$runtime_fp" "$base_fp" || \
      die "offline engine overlay build failed"
  else
    info "building engine image ($ENGINE_IMAGE) from source — long; compiles Asterisk+pcsc-lite+Python SWu tunnel deps and bakes engine/patches/*…"
    # shellcheck disable=SC2086
    docker build $NOCACHE_FLAG --build-arg "PCSC_VERSION=$PCSC_VERSION" \
      --build-arg "RUNTIME_FP=$runtime_fp" --build-arg "BASE_FP=$base_fp" \
      -t "$ENGINE_IMAGE" "$REPO_DIR/engine"
    # Keep the full build as the base every future overlay starts from, so repeated updates
    # stack one layer on a known-good image instead of a layer per update.
    docker tag "$ENGINE_IMAGE" "$ENGINE_BASE_TAG" >/dev/null 2>&1 || true
  fi
  info "engine image built"
}

# Overlay the runtime-owned files onto $1 and retag the result as the engine image. Needs no
# network, so it works on a host that cannot reach a registry. The previous image is kept as
# :previous for rollback; the base it was built from stays tagged for the next overlay.
engine_overlay_build() {
  overlay_base="$1"; overlay_runtime_fp="$2"; overlay_base_fp="$3"
  # Prefer the recorded base over the running image, so overlays never stack on each other.
  if docker image inspect "$ENGINE_BASE_TAG" >/dev/null 2>&1; then
    overlay_base="$ENGINE_BASE_TAG"
  else
    docker tag "$overlay_base" "$ENGINE_BASE_TAG" >/dev/null 2>&1 || true
  fi
  docker image inspect "$ENGINE_IMAGE" >/dev/null 2>&1 && \
    docker tag "$ENGINE_IMAGE" "$ENGINE_IMAGE:previous" >/dev/null 2>&1
  docker build --build-arg "BASE_IMAGE=$overlay_base" \
    --build-arg "RUNTIME_FP=$overlay_runtime_fp" --build-arg "BASE_FP=$overlay_base_fp" \
    -t "$ENGINE_IMAGE" -f "$REPO_DIR/engine/Dockerfile.overlay" "$REPO_DIR/engine"
}

build_control_image() {
  info "building control image ($CONTROL_IMAGE) from source (WebUI + FastAPI)…"
  # shellcheck disable=SC2086
  docker build $NOCACHE_FLAG --build-arg "PCSC_VERSION=$PCSC_VERSION" -t "$CONTROL_IMAGE" -f "$REPO_DIR/control/Dockerfile" "$REPO_DIR"
  info "control image built"
}

# Compile the React WebUI to webui/dist using a throwaway Node container — so LOCAL mode needs
# no Node/JS toolchain on the host. Builds in an isolated dir inside the container (ignores any
# host node_modules that might be built for another arch), then copies dist back to the host.
build_webui_local() {
  if [ "${MDD_REUSE_WEBUI:-0}" = 1 ]; then
    [ -f "$WEBUI_DIST/index.html" ] || die "MDD_REUSE_WEBUI=1 but webui/dist/index.html is missing"
    info "reusing reviewed prebuilt WebUI at $WEBUI_DIST"
    return
  fi
  # v1.3.3 bootstrap: v1.3.2's updater downloads GitHub's tag archive, which cannot contain
  # ignored CI artifacts. Reuse the already-installed dist only when every file matches the
  # reviewed manifest committed with this release. Newer updaters install a checksummed Release
  # asset and set MDD_REUSE_WEBUI=1 explicitly, so this is not a trust-on-first-use shortcut.
  webui_manifest="$REPO_DIR/webui/release-dist.SHA256SUMS"
  if [ -f "$webui_manifest" ] && [ -f "$WEBUI_DIST/index.html" ] && have sha256sum && \
      (cd "$REPO_DIR/webui" && sha256sum -c "$(basename -- "$webui_manifest")" >/dev/null 2>&1); then
    info "reusing WebUI verified by the release manifest at $WEBUI_DIST"
    return
  fi
  info "building WebUI (webui/dist) via a pinned throwaway Node container (no host Node needed)…"
  docker run --rm -v "$REPO_DIR/webui":/host-webui "$WEBUI_BUILD_IMAGE" sh -euc '
    cp -a /host-webui /build && cd /build && rm -rf node_modules dist
    npm ci
    npm run build
    rm -rf /host-webui/dist && cp -a /build/dist /host-webui/dist
  '
  [ -f "$WEBUI_DIST/index.html" ] || die "WebUI build produced no dist/index.html"
  info "WebUI built at $WEBUI_DIST"
}

# ------------------------------------------------------------------ native (local) control plane
# Install host packages the native control plane needs: python venv + the toolchain to build
# pyscard (SWIG binding) against the pinned pcsc-lite, plus headers for cryptography if wheels
# are unavailable. pcsc-lite headers come from our source build (if we did one) or the distro
# -dev package (version already matches the pin, since ensure_pcscd aligned it).
ensure_control_local_deps() {
  info "installing native control-plane dependencies (python venv + pyscard build toolchain)…"
  if   have apt-get; then
    pkg_install python3 python3-venv python3-pip python3-dev swig gcc pkg-config libffi-dev libssl-dev
    if [ "$PCSC_SOURCE_BUILT" = 0 ] && [ ! -f /usr/include/PCSC/pcsclite.h ]; then pkg_install libpcsclite-dev; fi
  elif have dnf || have yum; then
    pkg_install python3 python3-pip python3-devel swig gcc pkgconf-pkg-config libffi-devel openssl-devel
    if [ "$PCSC_SOURCE_BUILT" = 0 ] && [ ! -f /usr/include/PCSC/pcsclite.h ]; then pkg_install pcsc-lite-devel; fi
  elif have pacman;  then
    pkg_install python python-pip swig gcc pkgconf libffi openssl
    if [ "$PCSC_SOURCE_BUILT" = 0 ] && [ ! -f /usr/include/PCSC/pcsclite.h ]; then pkg_install pcsclite; fi
  elif have zypper;  then
    pkg_install python3 python3-pip python3-devel swig gcc pkg-config libffi-devel libopenssl-devel
    if [ "$PCSC_SOURCE_BUILT" = 0 ] && [ ! -f /usr/include/PCSC/pcsclite.h ]; then pkg_install pcsc-lite-devel; fi
  elif have apk;     then
    pkg_install python3 python3-dev py3-pip swig gcc musl-dev pkgconfig libffi-dev openssl-dev
    if [ "$PCSC_SOURCE_BUILT" = 0 ] && [ ! -f /usr/include/PCSC/pcsclite.h ]; then pkg_install pcsc-lite-dev; fi
  fi
}

setup_venv() {
  info "creating Python venv + installing control requirements ($VENV_DIR)…"
  [ -d "$VENV_DIR" ] || python3 -m venv "$VENV_DIR"
  # Most reloads do not change Python dependencies. Prove the pinned requirements are already
  # present without consulting an index first: this keeps a release download proxy out of pip's
  # vendored networking stack and lets a fully provisioned host reload offline. Only a genuinely
  # missing or changed dependency needs the package index. Do not upgrade pip on every reload;
  # replacing the installer itself creates needless network and compatibility risk.
  if "$VENV_DIR/bin/pip" install --quiet --no-index \
      -r "$REPO_DIR/control/requirements.txt" >/dev/null 2>&1; then
    info "control requirements already satisfied — reusing the installed packages"
  else
    "$VENV_DIR/bin/pip" install --quiet wheel -r "$REPO_DIR/control/requirements.txt"
  fi
  info "venv ready"
}

# Write + (re)start the native control-plane systemd unit. Runs as root (needs the reader via
# host pcscd + the Docker socket to manage engine containers). Because the control plane runs
# ON the host, MDD_HOST_DATA == MDD_DATA (the real host path the manager hands to engine
# bind-mounts). Engines reach it back over host.docker.internal:<port> (engine.start adds the
# host-gateway extra_host), same as in docker mode.
run_control_local() {
  have systemctl || die "local mode needs systemd (systemctl not found). Re-run with --mode docker."
  install -d -m 0700 "$MDD_DATA_DIR"
  DATA_ABS=$(data_dir_abs)
  LAN_IP="$MDD_ADVERTISE_ADDR"
  [ -z "$LAN_IP" ] && LAN_IP=$(detect_lan_ip)
  [ -z "$LAN_IP" ] && warn "could not auto-detect a LAN IP; set MDD_ADVERTISE_ADDR — SIP/WebRTC audio needs a routable host address"

  info "installing systemd unit $SYSTEMD_UNIT (native control plane)"
  cat > "$SYSTEMD_UNIT" <<EOF
[Unit]
Description=MDD Sim Gateway control surface (native / manager + WebUI)
After=network-online.target pcscd.service docker.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR/control
Environment=MDD_DATA=$DATA_ABS
Environment=MDD_HOST_DATA=$DATA_ABS
Environment=MDD_WEBUI=$WEBUI_DIST
Environment=MDD_HTTP_PORT=$MDD_PORT
Environment=MDD_BIND=$MDD_BIND
Environment=MDD_ADVERTISE_ADDR=$LAN_IP
Environment=MDD_ENGINE_IMAGE=$ENGINE_IMAGE
Environment=MDD_MANAGER_URL=https://host.docker.internal:$MDD_PORT
Environment=MDD_PCSCD_DIR=/run/pcscd
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_DIR/bin/python run.py
Restart=on-failure
RestartSec=3
User=root
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable mdd-sim-gateway-control >/dev/null 2>&1 || true
  systemctl restart mdd-sim-gateway-control
  info "started native control plane on https://${LAN_IP:-<host>}:${MDD_PORT}"
}

remove_control_local() {
  if have systemctl; then
    systemctl disable --now mdd-sim-gateway-control >/dev/null 2>&1 || true
  fi
  if [ -f "$SYSTEMD_UNIT" ]; then
    rm -f "$SYSTEMD_UNIT"
    have systemctl && systemctl daemon-reload 2>/dev/null || true
  fi
}

# The vsmartcard-vpcd package ships its own /etc/reader.conf.d/vpcd: a two-slot "Virtual PCD"
# reader on vpcd's default port, present whether or not a modem is. pcscd cannot bind that port
# for it AND for a modem reader, and directory order decides who wins, so on some hosts the modem
# readers never appeared while two phantom devices did. Rename it out of the way — pcsc-lite skips
# dot files — instead of deleting a package file, so it can be restored. The orchestrator repeats
# this check on every pass, which also covers a later reinstall of the package.
VPCD_PACKAGED_READER=/etc/reader.conf.d/vpcd
VPCD_PACKAGED_READER_DISABLED=/etc/reader.conf.d/.vpcd.mdd-disabled
disable_packaged_vpcd_reader() {
  [ -f "$VPCD_PACKAGED_READER" ] || return 0
  mv -f "$VPCD_PACKAGED_READER" "$VPCD_PACKAGED_READER_DISABLED"
  info "disabled the packaged 'Virtual PCD' reader definition (collides with modem readers)"
  systemctl restart pcscd.service >/dev/null 2>&1 || true
}

restore_packaged_vpcd_reader() {
  [ -f "$VPCD_PACKAGED_READER_DISABLED" ] || return 0
  [ -e "$VPCD_PACKAGED_READER" ] && { rm -f "$VPCD_PACKAGED_READER_DISABLED"; return 0; }
  mv -f "$VPCD_PACKAGED_READER_DISABLED" "$VPCD_PACKAGED_READER"
  systemctl restart pcscd.service >/dev/null 2>&1 || true
}

# Slot count of the installed libifdvpcd, read from the constant the build compiles in. Used to
# decide whether a rebuild is needed and to tell the orchestrator what it may ask for; a driver
# that cannot be inspected is reported as the upstream default rather than guessed upwards.
installed_vpcd_slots() {
  lib=$(find /usr/local/lib /usr/lib -name 'libifdvpcd.so*' -print -quit 2>/dev/null || true)
  [ -n "$lib" ] || { printf '0'; return; }
  marker=$(find /usr/local/lib /usr/lib -name ".mdd-vpcd-slots-*" -print -quit 2>/dev/null || true)
  case "$marker" in *.mdd-vpcd-slots-*) printf '%s' "${marker##*-}"; return ;; esac
  printf '2'
}

# Build + install libifdvpcd with enough slots for this gateway's logical channels. The packaged
# driver is compiled for two and the count has no runtime override, so the third channel is
# unreachable until the driver itself is replaced. Idempotent via a slot-tagged marker beside the
# driver; a distro reinstall drops the marker and the next run rebuilds.
ensure_vpcd_host() {
  # Ask pcsc-lite where its drivers live rather than assuming, the way the CCID build does.
  # Debian keeps this out of the multiarch tree today, but a distribution that does not would
  # otherwise get a driver installed somewhere pcscd never looks — and the only symptom would
  # be the two-slot behaviour this exists to fix, with the build reporting success.
  drivers_dir=$(pkg-config libpcsclite --variable usbdropdir 2>/dev/null || true)
  [ -n "$drivers_dir" ] && drivers_dir="$drivers_dir/serial"
  # Fall back to wherever the packaged driver already sits, then to the historical path.
  [ -d "$drivers_dir" ] || drivers_dir=$(dirname "$(find /usr/lib /usr/local/lib -name 'libifdvpcd.so*' -print -quit 2>/dev/null || true)" 2>/dev/null)
  [ -n "$drivers_dir" ] && [ -d "$drivers_dir" ] || drivers_dir=/usr/lib/pcsc/drivers/serial
  vpcd_marker="$drivers_dir/.mdd-vpcd-slots-${VPCD_SLOTS}"
  if [ -f "$vpcd_marker" ]; then
    info "virtual smart-card driver already provides $VPCD_SLOTS slots ($drivers_dir)"
    return 0
  fi
  info "building the virtual smart-card driver from source for $VPCD_SLOTS slots…"
  if   have apt-get; then
    pkg_install autoconf automake libtool pkg-config gcc make wget ca-certificates help2man
    [ -f /usr/include/PCSC/pcsclite.h ] || pkg_install libpcsclite-dev
  elif have dnf || have yum; then
    pkg_install autoconf automake libtool pkgconf-pkg-config gcc make wget help2man
    [ -f /usr/include/PCSC/pcsclite.h ] || pkg_install pcsc-lite-devel
  elif have pacman;  then pkg_install autoconf automake libtool pkgconf gcc make wget help2man
  elif have zypper;  then
    pkg_install autoconf automake libtool pkg-config gcc make wget help2man
    [ -f /usr/include/PCSC/pcsclite.h ] || pkg_install pcsc-lite-devel
  elif have apk;     then
    pkg_install autoconf automake libtool pkgconfig gcc make wget musl-dev help2man
    [ -f /usr/include/PCSC/pcsclite.h ] || pkg_install pcsc-lite-dev
  fi
  tmp=$(mktemp -d)
  download_verified \
    "https://github.com/frankmorgner/vsmartcard/archive/refs/tags/virtualsmartcard-${VPCD_VERSION}.tar.gz" \
    "$tmp/vsmartcard.tar.gz" "$VPCD_SHA256"
  # Only the driver and the library it links against are built. The rest of the tree is the
  # Python virtual card, the Android relay and pcsc-relay — none of which this gateway uses,
  # and each dragging in its own dependencies. src/vpcd must come first: src/ifd-vpcd links
  # libvpcd.la and its makefile has no rule to build it.
  ( cd "$tmp" \
    && tar xf vsmartcard.tar.gz \
    && cd "vsmartcard-virtualsmartcard-${VPCD_VERSION}/virtualsmartcard" \
    && autoreconf -vif . >/dev/null 2>&1 \
    && ./configure --enable-serialconfdir=/etc/reader.conf.d \
                   --enable-serialdropdir="$drivers_dir" \
                   --enable-vpcdslots="$VPCD_SLOTS" \
                   --disable-dependency-tracking >/dev/null \
    && make -C src/vpcd >/dev/null \
    && make -C src/ifd-vpcd >/dev/null \
    && make -C src/ifd-vpcd install >/dev/null \
  ) || { rm -rf "$tmp"; warn "could not build the virtual smart-card driver; the packaged two-slot driver stays in place and a module's third logical channel will be unavailable"; return 1; }
  rm -rf "$tmp"
  # configure installs its own reader definition; this gateway writes per-modem ones instead.
  disable_packaged_vpcd_reader
  rm -f "$drivers_dir/.mdd-vpcd-slots-"* 2>/dev/null || true
  touch "$vpcd_marker" 2>/dev/null || true
  if have apt-mark; then apt-mark hold vsmartcard-vpcd >/dev/null 2>&1 || true; fi
  if have systemctl; then
    # Same maintenance marker the orchestrator publishes, so the control plane does not read
    # this planned enumeration gap as readers being unplugged and stop healthy engines.
    install -d -m 0700 "$MDD_DATA_DIR/orchestrator"
    : > "$MDD_DATA_DIR/orchestrator/pcsc-maintenance"
    chmod 0600 "$MDD_DATA_DIR/orchestrator/pcsc-maintenance" 2>/dev/null || true
    systemctl restart pcscd 2>/dev/null || true
  fi
  info "virtual smart-card driver installed with $VPCD_SLOTS slots ($drivers_dir)"
}

# Country routes and USB modem serial ports live in the host namespace even when the manager is
# containerized, so this small privileged service is installed in both deployment modes.
run_orchestrator() {
  have systemctl || { warn "systemd unavailable — country routing/modem auto-detection disabled"; return; }
  DATA_ABS=$(data_dir_abs)
  # Debian/Ubuntu/Armbian package name. Other distributions can provide libifdvpcd.so manually;
  # native PC/SC readers continue to work when it is absent.
  if [ ! -e /usr/local/lib/libifdvpcd.so ] && [ ! -e /usr/lib/libifdvpcd.so ] && have apt-get; then
    pkg_install vsmartcard-vpcd
  fi
  VPCD_LIB=$(find /usr/local/lib /usr/lib -name libifdvpcd.so -print -quit 2>/dev/null || true)
  [ -n "$VPCD_LIB" ] && info "virtual smart-card driver: $VPCD_LIB" \
    || warn "libifdvpcd.so not found; native readers work, modem virtual slots will stay unavailable"
  # The packaged driver only has two slots, one short of the logical channels a module needs.
  [ -n "$VPCD_LIB" ] && ensure_vpcd_host
  disable_packaged_vpcd_reader
  have sing-box || die "sing-box installation failed"
  have xray || die "Xray-core installation failed"
  cat > "$ORCHESTRATOR_UNIT" <<EOF
[Unit]
Description=MDD Sim Gateway host egress and modem orchestrator
After=network-online.target pcscd.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_DIR/bin/python $REPO_DIR/host/mdd_orchestrator.py --data $DATA_ABS --repo $REPO_DIR
Restart=always
RestartSec=3
User=root
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable mdd-sim-gateway-orchestrator >/dev/null 2>&1 || true
  systemctl restart mdd-sim-gateway-orchestrator
}

remove_orchestrator() {
  if have systemctl; then systemctl disable --now mdd-sim-gateway-orchestrator >/dev/null 2>&1 || true; fi
  rm -f "$ORCHESTRATOR_UNIT" /etc/reader.conf.d/mdd-sim-gateway-modems
  # Nothing of ours is left to collide with it, so hand the packaged reader back.
  restore_packaged_vpcd_reader
  have systemctl && systemctl daemon-reload 2>/dev/null || true
}

# ------------------------------------------------------------------ containerized control plane
run_control() {
  install -d -m 0700 "$MDD_DATA_DIR"
  DATA_ABS=$(data_dir_abs)
  LAN_IP="$MDD_ADVERTISE_ADDR"
  [ -z "$LAN_IP" ] && LAN_IP=$(detect_lan_ip)
  [ -z "$LAN_IP" ] && warn "could not auto-detect a LAN IP; set MDD_ADVERTISE_ADDR — SIP/WebRTC audio needs a routable host address"

  if docker inspect "$CONTROL_NAME" >/dev/null 2>&1; then
    docker_container_owned "$CONTROL_NAME" || die "refusing to replace foreign container '$CONTROL_NAME'"
    docker rm -f "$CONTROL_NAME" >/dev/null
  fi
  info "starting control plane container ($CONTROL_NAME) on https://${LAN_IP:-<host>}:${MDD_PORT}"
  docker run -d --name "$CONTROL_NAME" \
    --label "$MDD_DOCKER_LABEL=true" \
    --label "io.mdd-sim-gateway.component=control" \
    --privileged \
    --restart unless-stopped \
    -p "${MDD_PORT}:8443" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v /run/pcscd:/run/pcscd \
    -v /run/dbus:/run/dbus:ro \
    -v "${DATA_ABS}:/data" \
    -e MDD_DATA=/data \
    -e MDD_HOST_DATA="${DATA_ABS}" \
    -e MDD_HTTP_PORT=8443 \
    -e MDD_BIND="${MDD_BIND}" \
    -e MDD_ADVERTISE_ADDR="${LAN_IP}" \
    -e MDD_MANAGER_URL="https://host.docker.internal:${MDD_PORT}" \
    -e MDD_ENGINE_IMAGE="${ENGINE_IMAGE}" \
    -e MDD_PCSCD_DIR=/run/pcscd \
    "$CONTROL_IMAGE"
}

# ------------------------------------------------------------------ subcommands
cmd_install() {
  need_root
  # Release assets carry this one-transition marker only so v1.3.4 can hand off safely.
  # It is not product configuration and must not remain in the installed source tree.
  rm -f "$REPO_DIR/EDITION"
  resolve_mode
  info "MDD Sim Gateway install — repo: $REPO_DIR  (mode: ${B}$MODE${N})"
  # The engine image compiles Asterisk + pcsc-lite + the Python SWu tunnel deps from source. On
  # low-power ARM boards (Raspberry Pi, Armbian SBCs) this first build can take 20-30 minutes —
  # only once, since later installs reuse the built image. Warn up front so a long, quiet build
  # isn't mistaken for a hang.
  ensure_docker
  docker_preflight
  if ! docker image inspect "$ENGINE_IMAGE" >/dev/null 2>&1; then
    warn "the engine image builds from source (Asterisk + pcsc-lite + SWu tunnel deps). On low-power ARM"
    warn "machines this can take 20-30 minutes — this is normal, please be patient. It runs only once;"
    warn "later installs/reloads reuse the built image."
  fi
  ensure_pcscd
  ensure_singbox
  ensure_xray
  ensure_cellular_tools
  if [ ! -x "$MDD_DATA_DIR/lpac/lpac" ]; then
    saved_args=$ARGS; ARGS=""; cmd_build_lpac; ARGS=$saved_args
  else
    info "lpac already installed at $MDD_DATA_DIR/lpac/lpac"
  fi
  ensure_engine_image
  persist_mode "$MODE"
  if [ "$MODE" = docker ]; then
    setup_venv
    build_control_image
    run_control
  else
    build_webui_local
    ensure_control_local_deps
    setup_venv
    run_control_local
  fi
  run_orchestrator
  DATA_ABS=$(data_dir_abs)
  LAN_IP="${MDD_ADVERTISE_ADDR:-$(detect_lan_ip)}"
  printf '\n'
  info "install complete (mode: $MODE)"
  printf '   %sWebUI:%s   https://%s:%s\n' "$B" "$N" "${LAN_IP:-<host-ip>}" "$MDD_PORT"
  printf '   %sData:%s    %s\n' "$B" "$N" "$DATA_ABS"
  if [ "$MODE" = local ]; then
    printf '   %sControl:%s native systemd service (mdd-sim-gateway-control); engines run in Docker\n' "$B" "$N"
  else
    printf '   %sControl:%s Docker container (%s); engines run in Docker\n' "$B" "$N" "$CONTROL_NAME"
  fi
  printf '   %sManage:%s  %s status | logs | reload | disable-autostart | uninstall\n' "$B" "$N" "$0"
  printf '   Accept the self-signed cert in your browser, then provision your SIM in the dashboard.\n'
}

cmd_reload() {
  need_root
  # See cmd_install: remove the compatibility marker after the old updater applies the archive.
  rm -f "$REPO_DIR/EDITION"
  resolve_mode
  RECREATE_ENGINES=0
  PRESERVE_ENGINES=0
  for a in $ARGS; do
    [ "$a" = "--engines" ] && RECREATE_ENGINES=1
    [ "$a" = "--no-engines" ] && PRESERVE_ENGINES=1
  done
  [ "$RECREATE_ENGINES" = 1 ] && [ "$PRESERVE_ENGINES" = 1 ] && \
    die "--engines and --no-engines cannot be used together"
  [ "$PRESERVE_ENGINES" = 1 ] && [ -n "$NOCACHE_FLAG" ] && \
    die "--no-cache and --no-engines cannot be used together"
  info "reload (mode: $MODE)"
  # Engine image: ensure_engine_image compares the checkout against the image's fingerprints and
  # picks reuse / script-overlay / full rebuild by itself. --engines and --no-cache force the
  # full rebuild, which is what you want after changing anything the base owns.
  ensure_docker
  docker_preflight
  ensure_singbox
  ensure_xray
  ensure_cellular_tools
  if [ "$PRESERVE_ENGINES" = 1 ]; then
    docker image inspect "$ENGINE_IMAGE" >/dev/null 2>&1 || \
      die "--no-engines requires the existing engine image $ENGINE_IMAGE"
    info "preserving the installed engine image (--no-engines)"
  elif [ "$RECREATE_ENGINES" = 1 ] || [ -n "$NOCACHE_FLAG" ]; then
    ensure_engine_image force
  else
    ensure_engine_image
  fi
  if [ "$MODE" = docker ]; then
    setup_venv
    build_control_image
    run_control
  else
    build_webui_local
    ensure_control_local_deps
    setup_venv
    run_control_local
  fi
  run_orchestrator
  if [ "$RECREATE_ENGINES" = 1 ]; then
    warn "engines will be re-created by the control plane on next start/provision (image updated)"
    for n in $(engine_names); do docker rm -f "$n" >/dev/null 2>&1 || true; done
  fi
  info "reload complete (data preserved)"
}

cmd_start() {
  need_root
  resolve_mode
  if [ "$MODE" = local ]; then
    have systemctl || die "local mode needs systemd"
    systemctl start mdd-sim-gateway-control && info "control plane started (systemd)" || warn "could not start mdd-sim-gateway-control"
  else
    if managed_control_exists; then
      docker start "$CONTROL_NAME" >/dev/null && info "control plane started (docker)"
    else warn "control container not found"; fi
  fi
  have systemctl && systemctl start mdd-sim-gateway-orchestrator >/dev/null 2>&1 || true
}

cmd_stop() {
  need_root
  resolve_mode
  if [ "$MODE" = local ]; then
    have systemctl || die "local mode needs systemd"
    systemctl stop mdd-sim-gateway-control && info "control plane stopped (systemd)" || warn "could not stop mdd-sim-gateway-control"
  else
    if managed_control_exists; then
      docker stop "$CONTROL_NAME" >/dev/null && info "control plane stopped (docker)"
    else warn "control container not found"; fi
  fi
  have systemctl && systemctl stop mdd-sim-gateway-orchestrator >/dev/null 2>&1 || true
  info "note: engine containers keep running; the control plane just stops managing them until restarted"
}

cmd_restart() {
  need_root
  resolve_mode
  if [ "$MODE" = local ]; then
    have systemctl || die "local mode needs systemd"
    systemctl restart mdd-sim-gateway-control && info "control plane restarted (systemd)" || warn "could not restart mdd-sim-gateway-control"
  else
    if managed_control_exists; then
      docker restart "$CONTROL_NAME" >/dev/null && info "control plane restarted (docker)"
    else warn "control container not found"; fi
  fi
  have systemctl && systemctl restart mdd-sim-gateway-orchestrator >/dev/null 2>&1 || true
}

cmd_enable_autostart() {
  need_root
  resolve_mode
  if [ "$MODE" = local ]; then
    have systemctl && systemctl enable mdd-sim-gateway-control >/dev/null 2>&1 && info "control autostart ON (systemd)" || warn "systemd unit not found"
  else
    if managed_control_exists; then
      docker update --restart unless-stopped "$CONTROL_NAME" >/dev/null && info "control autostart ON"
    else warn "control container not found"; fi
  fi
  have systemctl && systemctl enable mdd-sim-gateway-orchestrator >/dev/null 2>&1 || true
  for n in $(engine_names); do docker update --restart unless-stopped "$n" >/dev/null 2>&1 || true; done
}

cmd_disable_autostart() {
  need_root
  resolve_mode
  if [ "$MODE" = local ]; then
    have systemctl && systemctl disable mdd-sim-gateway-control >/dev/null 2>&1 && info "control autostart OFF (systemd)" || warn "systemd unit not found"
  else
    if managed_control_exists; then
      docker update --restart no "$CONTROL_NAME" >/dev/null && info "control autostart OFF"
    else warn "control container not found"; fi
  fi
  have systemctl && systemctl disable mdd-sim-gateway-orchestrator >/dev/null 2>&1 || true
  for n in $(engine_names); do docker update --restart no "$n" >/dev/null 2>&1 || true; done
  info "note: already-running components keep running until stopped or the host reboots"
}

cmd_uninstall() {
  need_root
  resolve_mode
  PURGE=0
  for a in $ARGS; do [ "$a" = "--purge" ] && PURGE=1; done
  info "uninstalling (mode: $MODE)…"
  # Tear down BOTH possible control planes so switching modes leaves nothing behind.
  info "removing native control plane (if any)…"
  remove_control_local
  remove_orchestrator
  if [ -f /etc/systemd/system/ModemManager.service.d/90-mdd-command-interface.conf ]; then
    rm -f /etc/systemd/system/ModemManager.service.d/90-mdd-command-interface.conf
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl try-restart ModemManager.service >/dev/null 2>&1 || true
  fi
  info "removing MDD containers…"
  if managed_control_exists; then docker rm -f "$CONTROL_NAME" >/dev/null; fi
  for n in $(engine_names); do docker rm -f "$n" >/dev/null 2>&1 || true; done
  if [ "$PURGE" = 1 ]; then
    # Full teardown: also drop images (incl. the slow, patched engine image) and data+venv.
    info "removing MDD images…"
    docker rmi -f "$CONTROL_IMAGE" "$ENGINE_IMAGE" >/dev/null 2>&1 || true
    warn "purging data dir: $(data_dir_abs) and venv $VENV_DIR"
    rm -rf "$MDD_DATA_DIR" "$VENV_DIR"
    rm -f "$DATA_DIR_STATE"
  else
    # Keep the ~15-20 min patched engine image (and the control image) so a reinstall reuses
    # them; only the running components are torn down. Data + venv are preserved too.
    docker rmi -f "$CONTROL_IMAGE" >/dev/null 2>&1 || true
    info "data kept at $(data_dir_abs); engine image + venv preserved (use --purge to delete all). Docker & pcscd left installed."
  fi
  info "uninstall complete"
}

cmd_status() {
  resolve_mode
  printf '%sVersion:%s %s\n' "$B" "$N" "$(cat "$REPO_DIR/VERSION" 2>/dev/null || echo unknown)"
  printf '%sMode:%s    %s\n' "$B" "$N" "$MODE"
  printf '%sControl:%s\n' "$B" "$N"
  if [ "$MODE" = local ]; then
    if have systemctl; then
      systemctl is-active mdd-sim-gateway-control >/dev/null 2>&1 \
        && printf '  mdd-sim-gateway-control  %s\n' "$(systemctl show -p ActiveState -p SubState --value mdd-sim-gateway-control 2>/dev/null | tr '\n' ' ')" \
        || printf '  mdd-sim-gateway-control  (not running)\n'
    fi
  else
    docker ps -a --filter "name=^${CONTROL_NAME}$" --format '  {{.Names}}  {{.Status}}  {{.Ports}}' 2>/dev/null || true
  fi
  printf '%sHost orchestrator:%s\n' "$B" "$N"
  if have systemctl && systemctl is-active mdd-sim-gateway-orchestrator >/dev/null 2>&1; then
    printf '  mdd-sim-gateway-orchestrator  %s\n' "$(systemctl show -p ActiveState -p SubState --value mdd-sim-gateway-orchestrator 2>/dev/null | tr '\n' ' ')"
  else
    printf '  mdd-sim-gateway-orchestrator  (not running)\n'
  fi
  printf '%sEngines:%s\n' "$B" "$N"
  docker ps -a --filter "name=^${ENGINE_PREFIX}" --format '  {{.Names}}  {{.Status}}' 2>/dev/null || true
  printf '%sDependencies:%s\n' "$B" "$N"
  if have sing-box; then printf '  sing-box  %s\n' "$(sing-box version 2>/dev/null | head -1)"; else printf '  sing-box  (not installed)\n'; fi
  if have xray; then printf '  Xray-core  %s\n' "$(xray version 2>/dev/null | head -1)"; else printf '  Xray-core  (not installed)\n'; fi
  if have mmcli; then printf '  ModemManager  %s\n' "$(mmcli --version 2>/dev/null | head -1)"; else printf '  ModemManager  (not installed)\n'; fi
  # Slot count decides whether a module's third logical channel exists at all, and the build
  # that raises it only warns when it fails — so it has to be visible without reading a log.
  vpcd_slots=$(installed_vpcd_slots)
  case "$vpcd_slots" in
    0) printf '  virtual reader driver  (not installed)\n' ;;
    2) printf '  virtual reader driver  %s slots — run `%s vpcd` for a third logical channel\n' "$vpcd_slots" "$0" ;;
    *) printf '  virtual reader driver  %s slots\n' "$vpcd_slots" ;;
  esac
  if [ -x "$MDD_DATA_DIR/lpac/lpac" ]; then printf '  lpac  installed\n'; else printf '  lpac  (not installed)\n'; fi
}

# One command whose output can be pasted into a bug report: everything needed to tell a card
# path apart from a radio path, with SIM identities masked on the way out. Long digit runs are
# ICCID/IMSI/IMEI/EID here, so only their last four characters survive.
diag_redact() { sed -E 's/[0-9]{6,}([0-9]{4})/****\1/g'; }

diag_section() { printf '\n%s== %s ==%s\n' "$B" "$1" "$N"; }

diag_readers() {
  if [ -x "$VENV_DIR/bin/python" ]; then
    "$VENV_DIR/bin/python" - <<'PY' 2>/dev/null || true
try:
    from smartcard.System import readers
    for index, reader in enumerate(readers()):
        print("%d: %s" % (index, reader))
except Exception as exc:  # noqa - a diagnostic must never fail the whole report
    print("reader enumeration failed: %r" % (exc,))
PY
  elif have pcsc_scan; then
    pcsc_scan -r 2>/dev/null || true
  else
    printf '(no pyscard venv and no pcsc_scan — install pcsc-tools to list readers)\n'
  fi
}

cmd_diagnose() {
  need_root
  resolve_mode
  DATA_ABS=$(data_dir_abs)
  diag_section "Install"
  printf 'version: %s (%s)\n' "$(cat "$REPO_DIR/VERSION" 2>/dev/null || echo unknown)" \
    "$(git -C "$REPO_DIR" describe --always --dirty 2>/dev/null || echo 'no git')"
  printf 'mode:    %s\ndata:    %s\n' "$MODE" "$DATA_ABS"
  printf 'host:    %s\n' "$(uname -srm)"
  if [ -r /etc/os-release ]; then
    (. /etc/os-release; printf 'distro:  %s\n' "${PRETTY_NAME:-unknown}") || true
  fi

  diag_section "PC/SC reader definitions"
  ls -la /etc/reader.conf.d/ 2>/dev/null || printf '(no /etc/reader.conf.d)\n'
  [ -f "$VPCD_PACKAGED_READER" ] \
    && printf '\n!! the packaged "Virtual PCD" definition is still active — it collides with modem readers\n' \
    || printf '\npackaged "Virtual PCD" definition: not active (good)\n'
  printf '\n--- /etc/reader.conf.d/mdd-sim-gateway-modems ---\n'
  cat /etc/reader.conf.d/mdd-sim-gateway-modems 2>/dev/null || printf '(absent)\n'

  diag_section "pcscd and live readers"
  if have systemctl; then systemctl is-active pcscd.service 2>/dev/null || true; fi
  diag_readers

  diag_section "Modem SIM bridges"
  pgrep -af vpcd_modem_bridge.py 2>/dev/null || printf '(no bridge process running)\n'
  printf '\n--- listening virtual-reader sockets (pcscd) ---\n'
  if have ss; then
    ss -lntp 2>/dev/null | grep -i pcscd || printf '(none — pcscd bound no virtual reader port)\n'
    printf '\n--- bridge connections ---\n'
    ss -ntp 2>/dev/null | grep -i "vpcd_modem_bridge\|python" || printf '(none)\n'
  else
    printf '(ss unavailable)\n'
  fi

  diag_section "Orchestrator state (masked)"
  for name in devices-desired.json devices-hardware.json devices-status.json; do
    printf -- '--- %s ---\n' "$name"
    if [ -f "$DATA_ABS/orchestrator/$name" ]; then
      diag_redact < "$DATA_ABS/orchestrator/$name"
    else
      printf '(absent)\n'
    fi
    printf '\n'
  done
  for path in "$DATA_ABS"/modems/*.json; do
    [ -f "$path" ] || continue
    printf -- '--- modems/%s ---\n' "$(basename "$path")"
    diag_redact < "$path"
    printf '\n'
  done

  diag_section "Orchestrator log (card path, masked)"
  if have journalctl; then
    # -A6 keeps the exception line under a traceback header: the header paths matched the
    # filter but the one line that names the error did not, and a whole support round trip
    # was spent recovering exactly that line by hand.
    journalctl -u mdd-sim-gateway-orchestrator -n 400 --no-pager 2>/dev/null \
      | grep -Ei -A6 'bridge|vpcd|reader|channel|modemmanager|traceback|error' \
      | tail -80 | diag_redact || printf '(nothing matched)\n'
  else
    printf '(journalctl unavailable)\n'
  fi

  diag_section "eSIM chip read over each modem reader (masked)"
  if [ ! -x "$MDD_DATA_DIR/lpac/lpac" ]; then
    printf 'lpac is not built — run: sudo ./install.sh build-lpac\n'
  else
    diag_readers | grep -F 'VoWiFi Modem' > /tmp/mdd-diag-readers.$$ 2>/dev/null || true
    if [ ! -s /tmp/mdd-diag-readers.$$ ]; then
      printf '(no modem reader present, so nothing to read)\n'
    else
      # A running line owns the reader exclusively; lpac then fails for that reason alone.
      while IFS= read -r entry; do
        name=${entry#*: }
        printf -- '--- %s ---\n' "$name"
        LPAC_APDU=pcsc LPAC_APDU_PCSC_DRV_NAME="$name" \
          "$MDD_DATA_DIR/lpac/lpac" chip info 2>&1 | head -30 | diag_redact || true
        printf '\n'
      done < /tmp/mdd-diag-readers.$$
    fi
    rm -f /tmp/mdd-diag-readers.$$
  fi

  diag_section "ModemManager"
  if have mmcli; then mmcli -L 2>&1 | diag_redact || true; else printf '(mmcli unavailable)\n'; fi
  printf '\n'
}

cmd_reset_admin() {
  need_root
  auth_file="$MDD_DATA_DIR/auth.json"
  [ -f "$auth_file" ] || { info "administrator account is already unconfigured"; return; }
  backup="$MDD_DATA_DIR/backups/auth-reset-$(date +%Y%m%d-%H%M%S).json"
  mkdir -p "$(dirname -- "$backup")"
  mv "$auth_file" "$backup"
  chmod 600 "$backup" 2>/dev/null || true
  info "administrator account reset; previous credential file preserved at $backup"
  info "open the WebUI to create a new administrator account"
}

# Opt-in: build + install the patched CCID driver (patches/ccid/*) on the host. Kept out of
# the default install because it replaces the distro libccid — only needed for quirky readers
# (e.g. HSIC 1d99:0016, whose GetSlotStatus always reports "no card").
#   patch     base fix only (01) — HSIC presence detection; safe for every card incl. eSIM.
#   patch2    compatibility fix only (02) — synthesize missing ATR TCK for HSIC reader.
#   patchprime SCR Prime device-table support only (03).
#   patchall  all three patches (01 + 02 + 03).
cmd_vpcd() {
  # Also reachable as part of install/reload. Exposed on its own because that build is the one
  # step that warns instead of aborting, so an operator who missed the warning — or who is on a
  # host where it first failed for a missing dependency — needs a way to retry just this.
  need_root
  ensure_vpcd_host
}

cmd_patch() {
  need_root
  ensure_ccid_host slot 01_hsic_slot_status.patch
}

cmd_patch2() {
  need_root
  ensure_ccid_host atr 02_hsic_malformed_atr.patch
}

cmd_patchprime() {
  need_root
  ensure_ccid_host prime 03_scr_prime_reader.patch
}

cmd_patchall() {
  need_root
  ensure_ccid_host all 01_hsic_slot_status.patch 02_hsic_malformed_atr.patch 03_scr_prime_reader.patch
}

cmd_build_lpac() {
  # Compile lpac (PC/SC + curl, STANDALONE) into $MDD_DATA_DIR/lpac for the eSIM UI.
  # The resulting host-side helper is shared by both local and Docker control-plane modes.
  #
  # lpac STANDALONE uses cmake_policy(CMP0177) which needs CMake >= 3.31.
  # Ubuntu 22.04 / Armbian ship 3.22 — we fetch a Kitware binary toolchain into
  # $MDD_DATA_DIR/tools/cmake when needed.
  need_root
  LPAC_SRC="${LPAC_SRC:-}"
  DEST=""
  CMAKE_MIN=3.31
  CMAKE_FETCH_VER="${CMAKE_FETCH_VER:-3.31.12}"
  # shellcheck disable=SC2086
  set -- $ARGS
  while [ $# -gt 0 ]; do
    case "$1" in
      --lpac-src)
        [ $# -ge 2 ] || die "--lpac-src requires a path"
        LPAC_SRC="$2"; shift 2 ;;
      --dest)
        [ $# -ge 2 ] || die "--dest requires a path"
        DEST="$2"; shift 2 ;;
      *) die "unknown build-lpac arg: $1 (supported: --lpac-src PATH, --dest DIR)" ;;
    esac
  done
  DEST="${DEST:-$MDD_DATA_DIR/lpac}"

  if [ -z "$LPAC_SRC" ]; then
    LPAC_SRC="$MDD_DATA_DIR/sources/lpac-$LPAC_VERSION"
    if [ ! -d "$LPAC_SRC/.git" ]; then
      info "fetching lpac v$LPAC_VERSION source…"
      mkdir -p "$MDD_DATA_DIR/sources"
      git clone --depth 1 --branch "v$LPAC_VERSION" https://github.com/estkme-group/lpac.git "$LPAC_SRC"
    fi
    [ "$(git -C "$LPAC_SRC" rev-parse HEAD)" = "$LPAC_COMMIT" ] || die "lpac source commit mismatch"
  fi
  [ -f "$LPAC_SRC/CMakeLists.txt" ] || die "not an lpac source tree: $LPAC_SRC"

  # Apply MDD fixes (patches/lpac/*) — e.g. the upstream pcsc reader-selection bug that
  # pins lpac to reader 0 and segfaults on LPAC_APDU_PCSC_DRV_IFID. Idempotent: a patch
  # that no longer applies cleanly is assumed to be present already (source commit is pinned).
  for lpatch in "$REPO_DIR"/patches/lpac/*.patch; do
    [ -f "$lpatch" ] || continue
    if patch -p1 -d "$LPAC_SRC" -N --dry-run < "$lpatch" >/dev/null 2>&1; then
      info "applying lpac patch $(basename "$lpatch")"
      patch -p1 -d "$LPAC_SRC" -N < "$lpatch"
    else
      info "lpac patch $(basename "$lpatch") already applied"
    fi
  done

  cmake_version_ok() {
    ver=$("$1" --version 2>/dev/null | head -1 | sed -n 's/.* \([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p')
    [ -n "$ver" ] || return 1
    major=${ver%%.*}
    minor=${ver#*.}; minor=${minor%%.*}
    need_major=${CMAKE_MIN%%.*}
    need_minor=${CMAKE_MIN#*.}; need_minor=${need_minor%%.*}
    if [ "$major" -gt "$need_major" ] 2>/dev/null; then return 0; fi
    if [ "$major" -eq "$need_major" ] && [ "$minor" -ge "$need_minor" ] 2>/dev/null; then return 0; fi
    return 1
  }

  ensure_cmake() {
    if have cmake && cmake_version_ok "$(command -v cmake)"; then
      CMAKE_BIN=$(command -v cmake)
      info "using system cmake $($CMAKE_BIN --version | head -1)"
      return 0
    fi
    TOOLS="$MDD_DATA_DIR/tools"
    CMAKE_HOME="$TOOLS/cmake-$CMAKE_FETCH_VER"
    CMAKE_BIN="$CMAKE_HOME/bin/cmake"
    if [ -x "$CMAKE_BIN" ] && cmake_version_ok "$CMAKE_BIN"; then
      info "using cached Kitware cmake at $CMAKE_BIN"
      export PATH="$CMAKE_HOME/bin:$PATH"
      return 0
    fi
    arch=$(uname -m)
    case "$arch" in
      x86_64|amd64)  cmake_arch=x86_64; cmake_digest=$CMAKE_SHA256_AMD64 ;;
      aarch64|arm64) cmake_arch=aarch64; cmake_digest=$CMAKE_SHA256_ARM64 ;;
      *) die "no Kitware cmake binary for arch=$arch; install cmake >= $CMAKE_MIN manually" ;;
    esac
    url="https://github.com/Kitware/CMake/releases/download/v${CMAKE_FETCH_VER}/cmake-${CMAKE_FETCH_VER}-linux-${cmake_arch}.tar.gz"
    info "system cmake too old (need >= $CMAKE_MIN); fetching Kitware $CMAKE_FETCH_VER ($cmake_arch)"
    mkdir -p "$TOOLS"
    tmp=$(mktemp -d)
    download_verified "$url" "$tmp/cmake.tgz" "$cmake_digest"
    tar -xzf "$tmp/cmake.tgz" -C "$tmp"
    rm -rf "$CMAKE_HOME"
    mv "$tmp/cmake-${CMAKE_FETCH_VER}-linux-${cmake_arch}" "$CMAKE_HOME"
    rm -rf "$tmp"
    export PATH="$CMAKE_HOME/bin:$PATH"
    CMAKE_BIN="$CMAKE_HOME/bin/cmake"
    info "installed $("$CMAKE_BIN" --version | head -1) -> $CMAKE_HOME"
  }

  info "building lpac into $DEST (src: $LPAC_SRC)"
  info "ensuring build dependencies"
  if have apt-get; then
    pkg_install build-essential pkg-config libpcsclite-dev libcurl4-openssl-dev git ca-certificates curl
  elif have dnf || have yum; then
    pkg_install gcc make pkgconfig pcsc-lite-devel libcurl-devel git curl
  elif have zypper; then
    pkg_install gcc make pkg-config pcsc-lite-devel libcurl-devel git curl
  elif have pacman; then
    pkg_install gcc make pkgconf pcsclite curl git
  else
    have gcc || die "missing gcc"
    have pkg-config || die "missing pkg-config"
  fi

  ensure_cmake

  BUILD_DIR="$LPAC_SRC/build-mdd"
  STAGE_DIR=$(mktemp -d)
  # shellcheck disable=SC2064
  trap 'rm -rf "$STAGE_DIR"' EXIT

  info "configuring lpac (STANDALONE, PCSC+curl) in $BUILD_DIR"
  rm -rf "$BUILD_DIR"
  "$CMAKE_BIN" -S "$LPAC_SRC" -B "$BUILD_DIR" \
    -DCMAKE_BUILD_TYPE=Release \
    -DSTANDALONE_MODE=ON \
    -DLPAC_WITH_APDU_PCSC=ON \
    -DLPAC_WITH_HTTP_CURL=ON \
    -DLPAC_WITH_APDU_AT=OFF \
    -DLPAC_WITH_APDU_QMI=OFF \
    -DLPAC_WITH_APDU_QMI_QRTR=OFF \
    -DLPAC_WITH_APDU_UQMI=OFF \
    -DLPAC_WITH_APDU_MBIM=OFF \
    -DLPAC_WITH_APDU_GBINDER=OFF

  info "building"
  "$CMAKE_BIN" --build "$BUILD_DIR" --parallel "$(nproc 2>/dev/null || echo 2)"

  info "installing to staging"
  DESTDIR="$STAGE_DIR" "$CMAKE_BIN" --install "$BUILD_DIR"

  SRC_BIN=""
  for candidate in "$STAGE_DIR/usr/local/bin" "$STAGE_DIR/usr/bin" \
                   "$STAGE_DIR/executables" "$STAGE_DIR/usr/executables"; do
    if [ -x "$candidate/lpac" ]; then SRC_BIN="$candidate"; break; fi
  done
  if [ -z "$SRC_BIN" ]; then
    find "$STAGE_DIR" -type f -name lpac 2>/dev/null || true
    die "lpac binary not found after install under $STAGE_DIR"
  fi

  info "deploying to $DEST"
  mkdir -p "$(dirname -- "$DEST")"
  rm -rf "$DEST.tmp"
  mkdir -p "$DEST.tmp"
  cp -a "$SRC_BIN/lpac" "$DEST.tmp/lpac"
  [ -d "$SRC_BIN/lib" ] && cp -a "$SRC_BIN/lib" "$DEST.tmp/lib"
  [ -d "$SRC_BIN/driver" ] && cp -a "$SRC_BIN/driver" "$DEST.tmp/driver"
  chmod +x "$DEST.tmp/lpac"
  rm -rf "$DEST"
  mv "$DEST.tmp" "$DEST"

  info "self-check: lpac driver apdu list"
  if ! "$DEST/lpac" driver apdu list >/dev/null; then
    warn "lpac driver apdu list failed (pcscd may be down); binary is installed at $DEST/lpac"
  else
    "$DEST/lpac" version || true
    info "OK: $DEST/lpac"
  fi
  trap - EXIT
  rm -rf "$STAGE_DIR"
}

cmd_logs() {
  resolve_mode
  # `|| true` so that Ctrl-C'ing out of a follow (non-zero exit) doesn't abort the caller —
  # matters when invoked from the interactive control menu.
  if [ "$MODE" = local ]; then
    have systemctl || die "systemd not available"
    journalctl -fu mdd-sim-gateway-control || true
  else
    docker logs -f "$CONTROL_NAME" || true
  fi
}

# Default action when invoked with no subcommand. Multi-functional:
#   - NOT installed  -> run the installer (needs root).
#   - installed      -> auto-detect the mode and offer basic controls. On a TTY this is an
#                       interactive menu; non-interactively it prints status + the command list.
cmd_auto() {
  installed=$(detect_installed_mode)
  if [ "$installed" = none ]; then
    info "no existing installation detected — installing…"
    cmd_install
    return
  fi
  MODE="$installed"
  info "existing installation detected — mode: ${B}$MODE${N}"
  cmd_status
  if [ ! -t 0 ]; then
    printf '\n%sControls:%s %s start | stop | restart | enable-autostart | disable-autostart | reload | logs | uninstall\n' \
      "$B" "$N" "$0"
    return
  fi
  # Interactive control menu.
  autostart_state() {
    if [ "$MODE" = local ]; then
      systemctl is-enabled --quiet mdd-sim-gateway-control 2>/dev/null && echo on || echo off
    else
      case "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$CONTROL_NAME" 2>/dev/null)" in
        ""|no) echo off;; *) echo on;; esac
    fi
  }
  while :; do
    control_running && run="running" || run="stopped"
    printf '\n%sMDD control (%s) — %s, autostart %s%s\n' "$B" "$MODE" "$run" "$(autostart_state)" "$N"
    printf '  1) %s control plane\n' "$([ "$run" = running ] && echo stop || echo start)"
    printf '  2) restart control plane\n'
    printf '  3) %s autostart\n' "$([ "$(autostart_state)" = on ] && echo disable || echo enable)"
    printf '  4) status\n'
    printf '  5) logs (follow)\n'
    printf '  6) reload (rebuild + restart)\n'
    printf '  7) uninstall\n'
    printf '  q) quit\n'
    printf 'choose: '
    read -r choice || break
    case "$choice" in
      1) [ "$run" = running ] && cmd_stop || cmd_start ;;
      2) cmd_restart ;;
      3) [ "$(autostart_state)" = on ] && cmd_disable_autostart || cmd_enable_autostart ;;
      4) cmd_status ;;
      5) cmd_logs ;;
      6) cmd_reload ;;
      7) cmd_uninstall; break ;;
      q|Q|"") break ;;
      *) warn "unknown choice: $choice" ;;
    esac
  done
}

usage() {
  cat <<EOF
${B}MDD Sim Gateway installer${N}

  $0                      auto: install if absent, else show status + control menu
  $0 install [--mode local|docker]   build + run (default mode: local)
  $0 reload  [--mode local|docker] [--no-cache] [--engines]   rebuild + restart (keep data)
  $0 start | stop | restart          control-plane lifecycle (systemd or docker per mode)
  $0 enable-autostart     start on boot
  $0 disable-autostart    do not start on boot
  $0 uninstall [--purge]  remove MDD containers/images/service (--purge also deletes data+venv)
  $0 status               show mode + component status
  $0 diagnose             print a masked card-path report (readers, bridges, lpac, logs)
  $0 reset-admin          reset the local administrator (old credential file is backed up)
  $0 logs                 follow control-plane logs
  $0 vpcd                 rebuild the virtual smart-card driver with enough card slots
  $0 build-lpac [--lpac-src PATH] [--dest DIR]   compile lpac into data/lpac (local eSIM LPA)
  $0 patch                build + install the CCID driver with the base HSIC fix (01) — opt-in,
                          for the HSIC 1d99:0016 reader (safe for every card, incl. physical eSIM)
  $0 patch2               add only the ATR-compatibility fix (02) for non-compliant (U)SIM ATRs
  $0 patchprime           add SCR Prime 04d9:c001 support (03)
  $0 patchall             apply all CCID patches (01 + 02 + 03)

${B}Modes:${N}
  local  (default) control plane runs natively (venv + systemd 'mdd-sim-gateway-control');
                   Docker is the engine layer only. WebUI compiled via a throwaway
                   Node container, so the host needs no Node.
  docker           control plane AND engine run in containers.
  Both modes version-lock pcsc-lite ($PCSC_VERSION) and bake engine patches.
  Run with no arguments to auto-install, or to manage an existing install.

Env: MDD_MODE(=local) MDD_PORT(=$MDD_PORT) MDD_DATA_DIR(=$MDD_DATA_DIR)
     MDD_ADVERTISE_ADDR(auto) MDD_BIND(=$MDD_BIND) PCSC_VERSION(=$PCSC_VERSION)
EOF
}

# ------------------------------------------------------------------ dispatch
CMD="${1:-}"; [ $# -gt 0 ] && shift || true

# Parse args: extract --mode <val> / --mode=<val>, collect the rest into ARGS, note --no-cache.
# The bottom `shift` is always safe: we only reach it inside `while [ $# -gt 0 ]`, and the
# `--mode <val>` branch consumes its value with `shift 2` + `continue` (avoids a bare `shift`
# on an empty arg list, which is a fatal special-builtin error in dash even with `|| true`).
MODE_ARG=""
ARGS=""
NOCACHE_FLAG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --mode)
      [ $# -ge 2 ] || die "--mode requires a value: local | docker"
      MODE_ARG="$2"; shift 2; continue ;;
    --mode=*)   MODE_ARG="${1#--mode=}" ;;
    --no-cache) NOCACHE_FLAG="--no-cache"; ARGS="$ARGS $1" ;;
    *)          ARGS="$ARGS $1" ;;
  esac
  shift
done

case "$CMD" in
  install)            cmd_install ;;
  reload)             cmd_reload ;;
  start)              cmd_start ;;
  stop)               cmd_stop ;;
  restart)            cmd_restart ;;
  enable-autostart)   cmd_enable_autostart ;;
  disable-autostart)  cmd_disable_autostart ;;
  uninstall)          cmd_uninstall ;;
  status)             cmd_status ;;
  diagnose)           cmd_diagnose ;;
  reset-admin)        cmd_reset_admin ;;
  logs)               cmd_logs ;;
  build-lpac)         cmd_build_lpac ;;
  patch)              cmd_patch ;;
  patch2)             cmd_patch2 ;;
  patchprime)         cmd_patchprime ;;
  vpcd)               cmd_vpcd ;;
  patchall)           cmd_patchall ;;
  "")                 cmd_auto ;;
  -h|--help|help)     usage ;;
  *) err "unknown command: $CMD"; usage; exit 1 ;;
esac
