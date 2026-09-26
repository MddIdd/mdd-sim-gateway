#!/bin/bash
# Engine entrypoint: render config -> hold PIN -> bring up the ePDG (SWu) tunnel with the
# pure-Python IKEv2/IPsec implementation (swu_ike.py) -> discover P-CSCF -> start Asterisk
# (IMS registration + voice/SMS).
#
# SWu tunnel: swu_ike.py (fasferraz/SWu-IKEv2, patched) is the sole ePDG tunnel path. It does
# IKEv2 + EAP-AKA (verifying the SIM PIN in its own PC/SC connection), userspace ESP over a
# tun device named "ipsec0" (so pjsip's bind_interface is unchanged), assigns the IPv6 inner
# address, and requests the IPv6 P-CSCF. A supervisor restarts it on exit; because every fresh
# start re-runs EAP-AKA WITH the PIN verify, the tunnel self-heals after a rekey/reauth teardown.
#
# PC/SC: this container is a pcscd CLIENT — it talks to the HOST pcscd via the bind-mounted
# /run/pcscd socket. The pcsc-lite client library is pinned to the same version as the host
# pcscd (see Dockerfile PCSC_VERSION) so the client/server protocol always matches.
set -u

export MDD_RUNDIR="${MDD_RUNDIR:-/run/mdd-sim-gateway}"
# /logs is bind-mounted from the host and survives both a container restart and a rebuild, so
# Asterisk's own logs live there (astlogdir in asterisk.conf). Until this existed, every
# investigation into "the engine restarted on its own" had to work from `docker logs` alone,
# which loses everything the moment the manager recreates the container.
export MDD_AST_LOGDIR="${MDD_AST_LOGDIR:-/logs/asterisk}"
mkdir -p "$MDD_RUNDIR" /logs "$MDD_AST_LOGDIR" /etc/asterisk

log() { echo "[entrypoint] $*"; }

# One machine-readable line per supervised lifecycle transition (Asterisk exit, swu_ike exit,
# backoff reset). The manager reads this to tell a docker-restart-policy bounce apart from a
# rebuild it performed itself; `docker logs` cannot express that difference.
supervisor_record() {
    python3 - "$@" <<'PY' 2>/dev/null || true
import json, os, sys, time
rec = {"ts": int(time.time()), "event": sys.argv[1]}
for pair in sys.argv[2:]:
    key, _, value = pair.partition("=")
    if not key:
        continue
    try:
        rec[key] = int(value)
    except ValueError:
        rec[key] = value
path = os.path.join(os.environ.get("MDD_AST_LOGDIR", "/logs/asterisk"), "supervisor.jsonl")
try:
    # Bounded: this file must never be the reason a 15G SD card fills up.
    lines = []
    if os.path.exists(path):
        with open(path) as handle:
            lines = handle.read().splitlines()[-499:]
    lines.append(json.dumps(rec, sort_keys=True))
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
except OSError:
    pass
PY
}

# Keep the persisted Asterisk logs bounded. At the shipped verbosity these grow ~12 KB/day, so
# the cap is never reached in practice; it exists so a debug session left switched on cannot
# fill the card.
AST_LOG_MAX_BYTES="${MDD_AST_LOG_MAX_BYTES:-8388608}"
AST_LOG_KEEP="${MDD_AST_LOG_KEEP:-3}"

rotate_asterisk_logs() {
    for name in full messages; do
        path="$MDD_AST_LOGDIR/$name"
        [ -f "$path" ] || continue
        size=$(stat -c %s "$path" 2>/dev/null || echo 0)
        [ "$size" -lt "$AST_LOG_MAX_BYTES" ] && continue
        mv -f "$path" "$path.$(date +%Y%m%d-%H%M%S)" 2>/dev/null || continue
        # Ask the running Asterisk to reopen its files; harmless if it is not up yet.
        asterisk -rx "logger reload" >/dev/null 2>&1 || true
    done
    # shellcheck disable=SC2012
    ls -1t "$MDD_AST_LOGDIR"/full.* "$MDD_AST_LOGDIR"/messages.* 2>/dev/null \
        | tail -n +$((AST_LOG_KEEP * 2 + 1)) | while read -r old; do rm -f "$old"; done
}

rotate_asterisk_logs

# --- 1. Render configs from /config/instance.json --------------------------------
log "rendering configs..."
python3 /usr/local/bin/render.py || { log "render failed"; exit 1; }
# shellcheck disable=SC1091
set -a; . "$MDD_RUNDIR/engine.env"; set +a
export USIM_PIN USIM_READER USIM_READER_INDEX USIM_READER_PORT USIM_IMSI MDD_ID MANAGER_URL MANAGER_EVENT_TOKEN MDD_RUNDIR
export PIN_USIM_READER
export SWU_SOURCE SWU_EPDG SWU_APN SWU_MCC SWU_MNC SWU_IMEI SWU_IMEISV SWU_CHILD_REKEY_MINUTES SWU_IDR_MODE SWU_CP_MODE SWU_CP_MODE_ORDER
export SWU_ACCEPT_EPDG_ESP_REKEY

# --- 1b. Relay media mode: the media interface accepts call media only ------------
# The relay can reach this container's media address, and AMI, SIP and the softphone WebSocket
# listen on every address. render.py wrote the ruleset that admits only UDP to the RTP range
# there. If it cannot be loaded, the interface goes down instead: browser calls lose audio,
# registration and SMS carry on, and nothing else is left reachable.
if [ "${MDD_MEDIA_MODE:-direct}" = relay ]; then
  if [ -z "${MDD_MEDIA_IF:-}" ]; then
    media_state=no_media_address
  elif nft -f "$MDD_RUNDIR/media.nft"; then
    media_state=ready
  else
    ip link set dev "$MDD_MEDIA_IF" down || true
    media_state=firewall_failed
  fi
  log "relay media: $media_state"
  printf '{"mode": "relay", "state": "%s"}\n' "$media_state" > "$MDD_RUNDIR/media.json"
fi

# --- 2. Start PIN keeper and wait for the SIM to be usable ------------------------
# pin_keeper holds CHV1 verified for ami_usim's SIP IMS-AKA. swu_ike verifies the PIN itself
# in its own connection for EAP-AKA, so both auth paths work on PIN-enabled SIMs.
log "starting pin_keeper (reader=${PIN_USIM_READER:-$USIM_READER})..."
USIM_READER="${PIN_USIM_READER:-$USIM_READER}" python3 -u /usr/local/bin/pin_keeper.py &
KEEPER_PID=$!

wait_pin() {
    for _ in $(seq 1 30); do
        st=$(python3 -c "import json;print(json.load(open('$MDD_RUNDIR/pin_status.json'))['state'])" 2>/dev/null || echo "")
        case "$st" in
            VERIFIED|PIN_DISABLED) log "PIN state: $st"; return 0 ;;
            WRONG_PIN|PIN_BLOCKED) log "PIN problem: $st - continuing (manager will surface)"; return 1 ;;
        esac
        sleep 1
    done
    log "PIN keeper did not reach VERIFIED in time - continuing anyway"
    return 1
}
wait_pin || true

# --- 3. Bring up the SWu (python IKEv2/IPsec) tunnel, supervised ------------------
log "starting SWu IKEv2 tunnel (epdg=$SWU_EPDG apn=$SWU_APN reader=$USIM_READER_INDEX port=${USIM_READER_PORT:-none})..."
rm -f "$MDD_RUNDIR/swu.ctl" "$MDD_RUNDIR/swu_status.json"

# swu_ike is very chatty (per-packet IKE decode dumps). Send ITS stdout+stderr ONLY to the IKE
# log (run/charon.log) through log_capture.py, which timestamps every physical line and rotates
# complete segments into persistent /logs/ike storage. This keeps it separate from Asterisk's
# console (docker logs); the manager surfaces IKE and Asterisk as two separate views.
# The supervisor's own status lines still go to the container stdout via log().
# A run that carried traffic for this long counts as a successful connection, so the next
# teardown starts from the short delay again. Without the reset the delay only ever doubled
# (4 -> 8 -> ... -> 60) for the life of the process; that stayed invisible only because the
# container itself was being restarted after nearly every teardown, which re-seeded it to 4.
# Once Asterisk stops taking the container down with it, an unreset backoff would leave a
# healthy line waiting a full minute to re-establish.
SWU_STABLE_SECONDS="${SWU_STABLE_SECONDS:-120}"
(
  backoff=4
  while true; do
    log "swu_ike starting"
    started=$(date +%s)
    python3 -u /usr/local/bin/swu_ike.py \
        -m "${USIM_READER_INDEX:-0}" \
        -s "$SWU_SOURCE" \
        -d "$SWU_EPDG" \
        -a "${SWU_APN:-ims}" \
        -I "$USIM_IMSI" \
        -M "$SWU_MCC" \
        -N "$SWU_MNC" \
        -E "${SWU_IMEI:-}" \
        -V "${SWU_IMEISV:-}" 2>&1 | \
      python3 -u /usr/local/bin/log_capture.py \
        --current "$MDD_RUNDIR/charon.log" \
        --archive-dir /logs/ike
    rc=${PIPESTATUS[0]}
    ran=$(( $(date +%s) - started ))
    if [ "$ran" -ge "$SWU_STABLE_SECONDS" ]; then
      if [ "$backoff" -ne 4 ]; then
        log "swu_ike had been up ${ran}s; resetting reconnect delay ${backoff}s -> 4s"
        supervisor_record swu_backoff_reset "ran_seconds=$ran" "previous_backoff=$backoff"
      fi
      backoff=4
    fi
    log "swu_ike exited (rc=$rc); reconnecting in ${backoff}s"
    supervisor_record swu_ike_exited "rc=$rc" "ran_seconds=$ran" "backoff_seconds=$backoff"
    sleep "$backoff"; backoff=$((backoff*2)); [ "$backoff" -gt 60 ] && backoff=60
  done
) &
SWU_PID=$!

# --- 4. Wait for the tunnel, then (re)render pjsip with the discovered P-CSCF ------
log "waiting for SWu tunnel to establish..."
for _ in $(seq 1 90); do
  st=$(python3 -c "import json;print(json.load(open('$MDD_RUNDIR/swu_status.json'))['state'])" 2>/dev/null || echo "")
  [ "$st" = "CONNECTED" ] && { log "SWu tunnel CONNECTED"; break; }
  sleep 1
done

log "waiting for P-CSCF discovery..."
for _ in $(seq 1 30); do
  [ -s "$MDD_RUNDIR/pcscf" ] && break
  sleep 1
done
addr=$(cat "$MDD_RUNDIR/pcscf" 2>/dev/null)
if [ -n "$addr" ]; then
  log "discovered P-CSCF: $addr"
  python3 /usr/local/bin/render.py || true   # re-render pjsip.conf with pcscf
  # Seed the applied-marker so swu_ike's in-process P-CSCF watcher only re-renders + reloads
  # Asterisk on a LATER change (reconnect/reauth), not redundantly right after this render.
  printf '%s' "$addr" > "$MDD_RUNDIR/pcscf.applied"
else
  log "no P-CSCF discovered yet - continuing (manager will surface tunnel state)"
fi

# --- 5. Start USIM<->AMI bridge and Asterisk -------------------------------------
log "starting ami_usim bridge..."
python3 -u /usr/local/bin/ami_usim.py /usr/local/etc/ami_usim.ini &

# Asterisk used to be exec'd as PID 1, which meant that when it went away the container simply
# vanished and the only surviving evidence was "ExitCode=0" from `docker inspect` — not enough
# to tell a clean shutdown from a crash, and `exit 0` with no kernel segfault record fits
# neither. Supervise it instead so the exact disposition (exit status vs terminating signal) is
# recorded before the container goes down. Signals are forwarded so `docker stop` still stops
# Asterisk the way it did before.
#
# -g makes Asterisk dump core if it is ever actually killed by a signal; the container needs a
# core ulimit for that to produce a file (the manager sets it). It costs nothing when, as the
# evidence so far suggests, the process is leaving through a normal exit path.
ASTERISK_ARGS="-f"
[ "${MDD_ASTERISK_CORE_DUMP:-1}" = "1" ] && ASTERISK_ARGS="$ASTERISK_ARGS -g"

log "starting Asterisk... (args: $ASTERISK_ARGS)"
# shellcheck disable=SC2086
asterisk $ASTERISK_ARGS &
AST_PID=$!
AST_RC=0
# Asterisk's own logs are now persistent, so nothing prunes them when the container stays up
# for weeks. Check hourly; rotate_asterisk_logs is a no-op below the size cap.
(
  while true; do
    sleep 3600
    rotate_asterisk_logs
  done
) &
forward_signal() {
    kill -"$1" "$AST_PID" 2>/dev/null || true
}
trap 'forward_signal TERM' TERM
trap 'forward_signal INT' INT
trap 'forward_signal HUP' HUP

# `wait` returns immediately with 128+signo when a trap fires, so keep waiting until the child
# is genuinely gone; otherwise a forwarded SIGTERM would look like Asterisk had exited.
while kill -0 "$AST_PID" 2>/dev/null; do
    wait "$AST_PID"
    AST_RC=$?
done

if [ "$AST_RC" -gt 128 ]; then
    AST_SIGNAL=$((AST_RC - 128))
    log "asterisk terminated by signal $AST_SIGNAL (rc=$AST_RC)"
    supervisor_record asterisk_exited "rc=$AST_RC" "signal=$AST_SIGNAL" "disposition=signal"
else
    log "asterisk exited normally with status $AST_RC"
    supervisor_record asterisk_exited "rc=$AST_RC" "disposition=exit"
fi
python3 /usr/local/bin/notify.py engine_stopped "$AST_RC" >/dev/null 2>&1 || true
exit "$AST_RC"
