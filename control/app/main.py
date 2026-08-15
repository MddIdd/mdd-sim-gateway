"""
main.py - MDD Sim Gateway control surface (FastAPI).

Serves the management REST API + WebSocket live feed + the built WebUI, and (for the
browser softphone) proxies provisioning. Runs natively or in a container; talks to
engine containers via the Docker SDK (engine.py) and Asterisk AMI (ami.py). HTTPS with
an auto-generated self-signed cert by default.
"""
from __future__ import annotations

import asyncio
import base64
import glob
import hmac
import ipaddress
import json
import logging
import os
import random
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config as cfg
from . import (store, engine, status as status_mod, sim, card, notify_push, lpa, auth,
               estkme, usbreader, egress, device_state, operations, update_check, cellular_sms,
               sysinfo, failover, carrier_id, allowance, cellular_call)
from .version import VERSION
from .ami import AmiClient
from .runtime import RuntimeRegistry

STATUS_OK_GRACE_SECONDS = 20
STATUS_POLL_FAST_SECONDS = 4.0
STATUS_POLL_HEALTHY_SECONDS = 15.0
# Once Asterisk has completed its own bounded REGISTER transaction and explicitly reports no
# response, another two minutes of same-session retries cannot repair the stale carrier-side
# P-CSCF/ESP state.  Rebuild promptly, but leave enough time for the diagnostic worker to capture
# and remove the expected container generation before auto-start runs.  Per-line rate limiting
# prevents a broken carrier from turning this fast path into a rebuild loop.
REG_UNANSWERED_RECOVERY_DELAY_SECONDS = float(
    os.environ.get("MDD_REG_UNANSWERED_RECOVERY_DELAY", "10"))
REG_UNANSWERED_MIN_INTERVAL_SECONDS = float(
    os.environ.get("MDD_REG_UNANSWERED_MIN_INTERVAL", "300"))
# Number portability is a rare administrative event. Verification forces one REGISTER so the
# carrier emits a fresh public identity; six-hour cadence detects a change promptly without
# perturbing every healthy IMS registration every ten minutes.
MSISDN_VERIFY_INTERVAL_SECONDS = float(os.environ.get("MDD_MSISDN_VERIFY", "21600"))
MSISDN_VERIFY_FAILURE_RETRY_SECONDS = float(
    os.environ.get("MDD_MSISDN_VERIFY_FAILURE_RETRY", "600"))
MSISDN_VERIFY_SETTLE_SECONDS = float(os.environ.get("MDD_MSISDN_VERIFY_SETTLE", "8"))
# Conditions that are genuinely measured but routinely spike for one sample. Starting a
# container on a memory-tight box pages a batch back in; that is the cost of the operation,
# not a problem anyone can act on. Only a rate that holds across consecutive polls is one.
SUSTAINED_ALERT_CODES = {"swap_pressure"}
SUSTAINED_ALERT_SAMPLES = int(os.environ.get("MDD_SUSTAINED_ALERT_SAMPLES", "3"))
# Connectivity timeline shown per line. The window follows how much history has actually
# accumulated so a fresh install is not stretched across an empty two-day axis.
LINE_HISTORY_MAX_SECONDS = 2 * 24 * 3600
LINE_HISTORY_MIN_SECONDS = 3600
LINE_HISTORY_PRUNE_INTERVAL_SECONDS = 3600
# Comfortably below store.LINE_STATE_CONTINUITY_SECONDS, so throttled writes still read back
# as one uninterrupted observation.
LINE_STATE_WRITE_INTERVAL_SECONDS = 30
_line_state_written: dict[str, tuple[str, float]] = {}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("vowifi.main")

WEBUI_DIR = os.environ.get("MDD_WEBUI", os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "webui", "dist"))


def _carrier_identity(value) -> dict:
    """Extract locally-held SIM matching attributes without inventing missing values."""
    nested = (value.get("carrier_identity") if isinstance(value, dict)
              else getattr(value, "carrier_identity", None)) or {}
    result = {key: nested[key] for key in ("spn", "gid1", "gid2") if key in nested}
    for key in ("spn", "gid1", "gid2"):
        raw = value.get(key) if isinstance(value, dict) else getattr(value, key, None)
        if raw is not None:
            result[key] = str(raw)
    return result


def _carrier_identity_update(value) -> dict:
    identity = _carrier_identity(value)
    result = {"carrier_identity": identity} if identity else {}
    mnc_len = value.get("mnc_len") if isinstance(value, dict) else getattr(value, "mnc_len", None)
    if mnc_len in (2, 3):
        result["mnc_len"] = int(mnc_len)
    return result


def _carrier_description(inst: dict | None, card_info: dict | None,
                         cellular: dict | None = None) -> dict:
    """Resolve a safe display value; never return IMSI, ICCID, SPN or GID."""
    inst, card_info = inst or {}, card_info or {}
    identity = {**inst, **{key: value for key, value in card_info.items()
                           if value not in (None, "")}}
    identity["carrier_identity"] = _carrier_identity(card_info) or _carrier_identity(inst)
    resolved = carrier_id.lookup(identity) or {
        "name": "", "home_network": "",
        "plmn": "-".join(filter(None, (str(identity.get("mcc") or ""),
                                          str(identity.get("mnc") or "")))),
        "match_source": "mccmnc", "database": "none",
    }
    current = str((cellular or {}).get("operator") or "").strip()
    if current.casefold() in {"--", "unknown", "none", "n/a"}:
        current = ""
    return {**resolved, "current_network": current}


def _client_card_info(value: dict) -> dict:
    """Card monitor view for authenticated clients, without carrier matching material."""
    return {key: item for key, item in value.items()
            if key not in {"carrier_identity", "spn", "gid1", "gid2"}}


def _client_cards(values: list[dict] | None = None) -> list[dict]:
    return [_client_card_info(value) for value in (values if values is not None
                                                    else hub.cards_list())]


def _modem_identity_for_reader(reader_name: str | None) -> dict | None:
    """Identify a modem only from its generated VPCD reader name, never from SIM ICCID."""
    reader_name = str(reader_name or "")
    hardware_id = device_state.vpcd_modem_hardware_id(reader_name)
    if not hardware_id:
        return None
    for path in glob.glob(os.path.join(cfg.DATA_DIR, "modems", "*.json")):
        try:
            with open(path, encoding="utf-8") as handle:
                identity = json.load(handle)
            if str(identity.get("hardware_id") or "") == hardware_id:
                imei = cfg.normalize_imei(identity.get("imei", ""))
                if len(imei) == 15:
                    return {**identity, "imei": imei}
        except (OSError, ValueError, TypeError):
            continue
    # The generated reader can outlive bridge metadata across an unplug/restart.
    # Its name still contains the stable physical id, which is sufficient to keep
    # the empty virtual slots grouped under the original offline modem.
    return {"hardware_id": hardware_id, "slots": 1}


def _with_detected_imei(cards: list[dict]) -> list[dict]:
    """Annotate native readers and collapse a modem's internal VPCD slots into one device."""
    enriched = []
    consumed = set()
    modem_identities = []
    assignment_names = {}
    try:
        with open(os.path.join(cfg.DATA_DIR, "orchestrator", "hardware-state.json"),
                  encoding="utf-8") as handle:
            hardware_state = json.load(handle)
        assignment_names = {
            str(device_id): str(value.get("name") or "")
            for device_id, value in (hardware_state.get("assignments") or {}).items()
        }
    except (OSError, ValueError, TypeError):
        pass
    for path in glob.glob(os.path.join(cfg.DATA_DIR, "modems", "*.json")):
        try:
            identity = json.load(open(path, encoding="utf-8"))
            if identity.get("hardware_id"):
                modem_identities.append(identity)
        except (OSError, ValueError, TypeError):
            pass
    for original in cards:
        if original.get("name") in consumed:
            continue
        card_info = dict(original)
        # Generated VPCD reader names carry the stable hardware id. SIM ICCID is deliberately
        # not considered: the same SIM may move to a native reader while an offline modem's
        # metadata still contains that card's last identity.
        hardware_id = device_state.vpcd_modem_hardware_id(card_info.get("name"))
        identity = next((x for x in modem_identities
                         if str(x.get("hardware_id")) == hardware_id), None)
        if hardware_id and not identity:
            identity = {"hardware_id": hardware_id, "slots": 1}
        if identity:
            imei = cfg.normalize_imei(identity.get("imei", ""))
            if len(imei) == 15:
                card_info["imei"] = imei
                card_info["imei_source"] = "modem"
            count = max(1, int(identity.get("slots") or 1))
            hwid = str(identity.get("hardware_id") or "")
            siblings = [dict(c) for c in cards if
                        device_state.vpcd_modem_hardware_id(c.get("name")) == hwid]
            siblings.sort(key=lambda c: (c.get("index") is None, c.get("index") or 0))
            if len(siblings) > 1:
                consumed.update(c.get("name") for c in siblings[1:])
                card_info = {**siblings[0], **card_info}
                card_info["hardware_kind"] = "modem"
                card_info["hardware_id"] = identity.get("hardware_id") or identity.get("modem")
                card_info["display_name"] = (assignment_names.get(hwid)
                                             or "Cellular modem")
                card_info["virtual_slots"] = [
                    {"index": c.get("index"), "name": c.get("name")} for c in siblings[:count]]
        else:
            card_info["hardware_kind"] = "reader"
        card_info["country"] = egress.country_for_mcc(card_info.get("mcc"))
        enriched.append(card_info)
    return enriched


def _next_instance_id() -> str:
    """Return the lowest unused numeric line id without assuming ids are contiguous."""
    used = {str(item.get("id")) for item in cfg.list_instances()}
    candidate = 1
    while str(candidate) in used:
        candidate += 1
    return str(candidate)


def _ensure_card_draft(info: dict) -> dict | None:
    """Persist a safe, stopped line draft as soon as a new SIM identity is readable.

    A draft makes hotplug the normal creation path while deliberately avoiding engine startup
    until mandatory identity fields (notably IMEI on a native reader) are available.
    """
    iccid = str(info.get("iccid") or "").strip()
    if not iccid:
        return None
    existing = _match_instance_by_iccid(iccid)
    if existing:
        return existing
    identity = _modem_identity_for_reader(info.get("name")) or {}
    mcc, mnc = str(info.get("mcc") or ""), str(info.get("mnc") or "")
    try:
        inst = cfg.upsert_instance({
            "id": _next_instance_id(),
            "name": cfg.default_instance_name(mcc, mnc, iccid),
            "provisioning_state": "draft",
            "iccid": iccid,
            "imsi": str(info.get("imsi") or ""),
            "mcc": mcc,
            "mnc": mnc,
            **_carrier_identity_update(info),
            "imei": identity.get("imei") or "",
            "reader": f"imsi:{info['imsi']}" if info.get("imsi") else "",
            "reader_index": int(info.get("index") or 0),
            "reader_port": str(info.get("reader_port") or ""),
            "smsc": str(info.get("smsc") or ""),
            "proxy_country": "",
            "enabled": False,
            "apn": "ims",
            "idr_mode": "apn",
            "cp_mode": "auto",
            "sip": {**cfg.carrier_sip_defaults(mcc, mnc, iccid),
                    "listen_addr": "0.0.0.0", "transport": "udp", "external": [],
                    "webrtc": {"enable": True}},
            "debug": {"asterisk": False, "charon": False},
        }, unique_name=True)
    except cfg.LineLimitError:
        log.warning("SIM line limit reached; ignoring newly detected SIM %s",
                    iccid[-4:])
        return None
    egress.publish()
    return inst


def _exit_ledger_path() -> str:
    return os.path.join(cfg.DATA_DIR, "exit-failover.json")


def _load_exit_ledgers() -> dict:
    try:
        with open(_exit_ledger_path(), encoding="utf-8") as handle:
            loaded = json.load(handle)
        return {str(k): v for k, v in loaded.items() if isinstance(v, dict)}
    except (OSError, ValueError, AttributeError):
        return {}


class Hub:
    """Holds AMI clients per instance and broadcasts events to WebSocket clients."""
    def __init__(self):
        self.ami: dict[str, AmiClient] = {}
        self.ami_generation: dict[str, str | None] = {}
        self._ami_locks: dict[str, asyncio.Lock] = {}  # per-instance ami_for serialisation
        self.runtime = RuntimeRegistry()
        self.status_wakeup = asyncio.Event()
        self.clients: set[WebSocket] = set()
        self.cards: dict[str, dict] = {}     # reader NAME -> detected card/reader info
        self.scanned = False                 # card_monitor completed its first scan
        self._learning: set[str] = set()     # instances currently learning MSISDN
        self._msisdn_tries: dict[str, int] = {}
        self._msisdn_checked: dict[str, float] = {}   # last passive re-check
        # Serialise route selection and submission per line. In particular, two concurrent
        # ``auto`` requests must not both decide that the preferred route is unavailable and
        # submit the same user action through different transports.
        self.sms_send_locks: dict[str, asyncio.Lock] = {}
        # Per-line exit failover ledger. Persisted: a control-plane restart must not
        # re-announce a give-up it already reported, nor re-walk an exhausted pool.
        self.exit_ledgers: dict[str, dict] = _load_exit_ledgers()
        self.health: dict[str, dict] = {}    # per-instance retry/health tracking
        # Kept outside health: a successful registration resets health, but must not erase the
        # anti-churn interval for the next stale-session failure on the replacement container.
        self.reg_unanswered_recovery_at: dict[str, float] = {}
        self.status_cache: dict[str, dict] = {}  # background sampled; HTTP never probes devices
        self.status_sampled_at: dict[str, float] = {}  # last authoritative status observation
        self._pushed_calls: set[int] = set() # call-record ids already push-notified (dedupe)
        # Per-reader serialization for PC/SC APDU access (sim.read_card / PIN / lpac).
        # lpac opens SCARD_SHARE_EXCLUSIVE; concurrent connect/APDU on the same reader
        # fails with sharing violations or corrupts eUICC sessions.
        self.reader_locks: dict[str, asyncio.Lock] = {}
        self.lpa_busy: dict[str, bool] = {}  # readers currently owned by an LPA op
        self.lpa_downloads: dict[str, dict] = {}  # reader_name -> active download handle
        self.hotplug_starts: set[str] = set()  # debounce duplicate modem VPCD slots
        # When each line last became healthy, so a failure can be attributed. A line that
        # carried IMS for a long time and then broke is not evidence against its exit node.
        self.ok_since: dict[str, float] = {}
        # Latest host snapshot, sampled by host_health_poller so HTTP never shells out.
        self.host_snapshot: dict = {}
        self.host_alerts: list[dict] = []
        # Shared with the acknowledgement endpoint. The poller owns condition lifecycle;
        # the API only marks currently visible items handled.
        self.host_alert_state: dict | None = None

    def cards_list(self) -> list[dict]:
        """Reader/card entries sorted by current PC/SC index (the UI display order)."""
        cards = sorted(self.cards.values(),
                       key=lambda c: (c.get("index") is None, c.get("index") or 0,
                                      c.get("name") or ""))
        return _with_detected_imei(cards)

    def reader_lock(self, name: str) -> asyncio.Lock:
        if name not in self.reader_locks:
            self.reader_locks[name] = asyncio.Lock()
        return self.reader_locks[name]

    def health_for(self, iid: str) -> dict:
        return self.health.setdefault(str(iid), {
            "fail_start": None, "retry_count": 0, "frozen_code": None,
            "frozen_reason": None, "last_state": None, "next_retry_at": None,
            "auto_retrying": False,
        })

    def reset_health(self, iid: str):
        iid = str(iid)
        self.health[iid] = {"fail_start": None, "retry_count": 0, "frozen_code": None,
                                 "frozen_reason": None, "last_state": None,
                                 "next_retry_at": None, "auto_retrying": False}
        self.status_cache.pop(iid, None)
        self.status_sampled_at.pop(iid, None)

    async def drop_ami(self, iid: str):
        """Tear down and forget the AMI client for an instance. MUST be called whenever the
        engine container is stopped or recreated (stop/start/reprovision): the client's
        panoramisk Manager auto-reconnects forever, so a client left pointing at a removed or
        recreated container keeps dialing it — and if the new container has a different AMI
        secret (or the docker IP was reused by another line) it floods that Asterisk with
        'failed to authenticate' every few seconds. close() sets the client's closed flag which
        neutralises the pending reconnect."""
        c = self.ami.pop(str(iid), None)
        self.ami_generation.pop(str(iid), None)
        if c:
            await c.close()

    async def runtime_changed(self, iid: str, runtime: dict, _action: str) -> None:
        """Retire stale AMI immediately and wake the adaptive status sampler."""
        generation = runtime.get("container_id")
        if (not runtime.get("running")
                or self.ami_generation.get(str(iid)) not in (None, generation)):
            await self.drop_ami(iid)
        self.status_wakeup.set()

    async def broadcast(self, msg: dict):
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def ami_for(self, iid: str, runtime: dict | None = None) -> AmiClient | None:
        iid = str(iid)
        # Serialise per-instance so concurrent callers (the 4s status_poller + API handlers) can't
        # each build a client and orphan the other's: an orphaned AmiClient is never close()d, so
        # its panoramisk Manager reconnects forever (flooding the engine's Asterisk with AMI auth
        # failures once a container reuses its docker IP).
        lock = self._ami_locks.setdefault(iid, asyncio.Lock())
        async with lock:
            inst = cfg.get_instance(iid)
            # Docker's API can pause for seconds when the daemon is busy. AMI discovery runs
            # in the background, but synchronous Docker calls here still froze every HTTP
            # request sharing this event loop.
            if runtime is None:
                runtime = await self.runtime.get(iid)
            running = bool(inst) and bool(runtime.get("running"))
            ip = runtime.get("ip") if running else None
            generation = runtime.get("container_id")
            client = self.ami.get(iid)
            # Reuse only a healthy client still pointed at the current container.
            if (client and running and ip and client.connected and client.host == ip
                    and self.ami_generation.get(iid) == generation):
                return client
            # Any other cached client is stale/unusable — drop it (close stops its reconnect loop)
            # so it can't linger and reconnect. This is the leak the old early-returns caused: they
            # returned None when the container was gone/IP-less WITHOUT closing the cached client.
            if client:
                await self.drop_ami(iid)
            if not running or not ip:
                return None
            client = AmiClient(iid, ip, 5038, inst.get("ami_user", "vowifi"),
                               inst["ami_secret"], realm=cfg.ims_realm(inst["mcc"], inst["mnc"]),
                               msisdn=inst.get("msisdn", ""), smsc=inst.get("smsc", ""))
            await client.connect()
            self.ami[iid] = client
            self.ami_generation[iid] = generation
            return client


hub = Hub()
capability_lock = asyncio.Lock()
PCSC_MAINTENANCE_WINDOW_SECONDS = 45


def _start_engine_checked(inst: dict, settings: dict, dev_mounts: bool = False,
                          reason: str = "manual"):
    """Translate fail-closed egress errors into an actionable API response."""
    if not cfg.line_allowed(str(inst.get("id") or "")):
        raise HTTPException(409, {
            "code": "line_limit",
            "message": (f"MDD Sim Gateway supports at most "
                        f"{cfg.MAX_SIM_LINES} SIM lines. Delete an existing line "
                        "before starting this one."),
        })
    try:
        # A line follows its SIM; its device identity follows the physical modem/reader
        # currently holding that SIM. Refresh the rendered snapshot on every start.
        inst = _apply_current_hardware_imei(inst)
        # A restart begins a new healthy stretch; the old one says nothing about the exit
        # this container will end up using.
        hub.ok_since.pop(str(inst.get("id") or ""), None)
        return engine.start(inst, settings, dev_mounts=dev_mounts, reason=reason)
    except egress.EgressError as exc:
        raise HTTPException(503, {"code": "egress_unavailable", "message": str(exc)})


def _match_instance_by_iccid(iccid):
    if not iccid:
        return None
    for i in cfg.list_instances():
        if i.get("iccid") == iccid:
            return i
    return None


def _random_svn() -> str:
    """Random 2-digit Software Version Number for an auto-derived IMEISV."""
    return f"{random.randint(0, 99):02d}"


def _find_running_by_reader(name: str):
    """The running instance whose pin_keeper reports using this reader NAME
    (pin_status.json "reader") — per-reader correct with multiple SIMs."""
    if not name:
        return None
    for i in cfg.list_instances():
        if not engine.is_running(str(i["id"])):
            continue
        ps = engine.read_run_json(str(i["id"]), "pin_status.json") or {}
        if ps.get("reader") == name:
            return i
    return None


async def _on_card_insert(name, idx):
    info = {"index": idx, "name": name, "present": True, "iccid": None,
            "pin_enabled": None, "pin_tries": None, "matched": None, "imsi": None,
            "mcc": None, "mnc": None, "mnc_len": None, "smsc": None,
            "carrier_identity": {}, "reader_port": None}
    # Resolve the STABLE physical USB port for this reader index (DIRECT connect, no APDU —
    # safe even if a running engine holds the card). This is the binding a line pins to, so it
    # survives pcscd re-enumerating two identical readers into a different order.
    try:
        info["reader_port"] = await asyncio.to_thread(usbreader.port_for_index, idx)
    except Exception as e:  # noqa
        log.debug("reader_port resolve failed for idx %s: %r", idx, e)
    # A running engine may already hold this card (manager restart, or pcscd flapped
    # while the engine kept running) — probing it could clash with the engine's card
    # access. Always map the reader to the running instance whose pin_keeper reports
    # using THIS reader name first, and only probe when no running engine claims it.
    # Also skip probing while an LPA (lpac) operation holds the reader exclusively —
    # profile enable/disable triggers eUICC REFRESH that looks like remove+insert.
    inst = await asyncio.to_thread(_find_running_by_reader, name)
    if inst is not None:
        info.update(iccid=inst.get("iccid"), imsi=inst.get("imsi"), matched=inst["id"],
                    smsc=inst.get("smsc"), mcc=inst.get("mcc"), mnc=inst.get("mnc"),
                    mnc_len=inst.get("mnc_len"),
                    carrier_identity=inst.get("carrier_identity") or {})
    elif hub.lpa_busy.get(name):
        prev = hub.cards.get(name) or {}
        info.update(iccid=prev.get("iccid"), imsi=prev.get("imsi"),
                    matched=prev.get("matched"), smsc=prev.get("smsc"),
                    mcc=prev.get("mcc"), mnc=prev.get("mnc"),
                    mnc_len=prev.get("mnc_len"),
                    carrier_identity=prev.get("carrier_identity") or {},
                    pin_enabled=prev.get("pin_enabled"), pin_tries=prev.get("pin_tries"))
        log.info("card insert during LPA busy — skipping probe reader=%s", name)
    else:
        lock = hub.reader_lock(name)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=0.05)
        except asyncio.TimeoutError:
            prev = hub.cards.get(name) or {}
            info.update(iccid=prev.get("iccid"), imsi=prev.get("imsi"),
                        matched=prev.get("matched"), smsc=prev.get("smsc"),
                        mcc=prev.get("mcc"), mnc=prev.get("mnc"),
                        mnc_len=prev.get("mnc_len"),
                        carrier_identity=prev.get("carrier_identity") or {})
            hub.cards[name] = info
            log.debug("card probe skipped — reader lock busy: %s", name)
            return
        try:
            c = await asyncio.to_thread(sim.read_card, idx)
            info.update(iccid=c.iccid, pin_enabled=c.pin_enabled, pin_tries=c.pin_tries,
                        imsi=c.imsi, mcc=c.mcc, mnc=c.mnc,
                        mnc_len=getattr(c, "mnc_len", None), smsc=c.smsc,
                        carrier_identity=_carrier_identity(c))
        except Exception as e:  # noqa
            log.debug("card probe failed: %r", e)
        finally:
            lock.release()
        inst = _match_instance_by_iccid(info["iccid"])
        if inst:
            info["matched"] = inst["id"]
            info["imsi"] = info["imsi"] or inst.get("imsi")
            # A SIM line follows the card, not the reader it occupied previously. Refresh
            # the live binding immediately on hotplug so a swap cannot leave a native USB
            # port pinned on a line that has moved into a modem (or vice versa).
            modem_identity = _modem_identity_for_reader(name)
            update = {"id": str(inst["id"]), **_carrier_identity_update(info)}
            if modem_identity:
                logical = modem_identity.get("logical_channels") or []
                swu_slot = next((int(item.get("slot")) for item in logical
                                 if item.get("role") == "swu"), 1)
                try:
                    current_slot = int(str(name).rsplit(" ", 1)[-1])
                except ValueError:
                    current_slot = -1
                if current_slot == swu_slot:
                    update.update(reader_index=idx, reader_port="")
            else:
                update.update(reader_index=idx,
                              reader_port=str(info.get("reader_port") or ""))
            if any(inst.get(key) != value for key, value in update.items() if key != "id"):
                inst = await asyncio.to_thread(cfg.upsert_instance, update)
        elif info.get("iccid") and cfg.card_auto_create_suppressed(info["iccid"]):
            # The user explicitly deleted this SIM line while the card was still inserted.
            # Keep it visibly unconfigured, but do not immediately recreate the record behind
            # their back. Physical removal clears the suppression; a later insertion is new
            # intent and follows the normal automatic provisioning flow again.
            log.info("deleted SIM remains inserted; automatic line creation is paused")
        elif info.get("iccid"):
            # A newly inserted SIM becomes a stopped line draft automatically. The UI only asks
            # for fields that cannot be learned from this hardware (for example a native reader
            # has no IMEI) and then promotes this same id through /api/provision.
            inst = await asyncio.to_thread(_ensure_card_draft, info)
            if inst:
                info["matched"] = inst["id"]
    hub.cards[name] = info
    log.info("card inserted reader=%s (%s) identity=%s matched=%s", idx, name,
             "available" if info["iccid"] else "unknown", info["matched"])
    if info.get("matched"):
        asyncio.create_task(_auto_start_hotplugged_line(str(info["matched"])))


async def _auto_start_hotplugged_line(iid: str) -> None:
    """Start one enabled matched line after reader enumeration settles.

    A modem exposes the same SIM through several VPCD slots, so insert events arrive more
    than once. The per-line guard collapses them into one attempt; the normal health policy
    remains responsible for bounded registration retries after the container has started.
    """
    if iid in hub.hotplug_starts:
        return
    hub.hotplug_starts.add(iid)
    try:
        await asyncio.sleep(6)
        inst = cfg.get_instance(iid)
        if not inst or await asyncio.to_thread(engine.is_running, iid):
            return
        cards = hub.cards_list()
        card_info = next((item for item in cards if item.get("present")
                          and str(item.get("iccid") or "") == str(inst.get("iccid") or "")), None)
        if not card_info:
            return
        device_id, device_type = _device_for_card(card_info, cards)
        desired = device_state.desired()
        wanted = ((desired.get("devices") or {}).get(device_id)
                  or desired.get("defaults") or {})
        if not wanted.get("vowifi_enabled", True):
            return

        # A newly-seen SIM is intentionally persisted as a stopped draft while the card
        # monitor is still learning its identity. Once the settled hotplug snapshot has all
        # mandatory values, promote that same line automatically. This makes inserting a
        # modem/SIM a complete operation instead of leaving the user to discover and submit
        # the manual provisioning form. Only drafts are promoted here: a ready line that the
        # user explicitly disabled remains disabled.
        if inst.get("provisioning_state") == "draft":
            inst = await asyncio.to_thread(_auto_promote_card_draft, inst, card_info, cards)
            if inst.get("provisioning_state") == "draft":
                log.info("hotplug draft %s awaiting: %s", iid,
                         ", ".join(inst.get("auto_provision_missing") or []))
                return
        if not inst.get("enabled", True):
            return
        await asyncio.to_thread(_start_engine_checked, inst, cfg.get_settings(),
                                os.environ.get("MDD_DEV_MOUNTS", "") == "1")
        hub.reset_health(iid)
        await hub.broadcast({"type": "engine", "instance": iid, "event": "hotplug_started",
                             "args": []})
    except Exception as exc:  # noqa
        log.warning("hotplug auto-start failed for %s: %s", iid, getattr(exc, "detail", exc))
    finally:
        hub.hotplug_starts.discard(iid)


def _line_auto_start_allowed(inst: dict) -> tuple[bool, str]:
    """Whether background maintenance may create this line's engine right now.

    A saved line is not proof that its SIM is inserted: offline lines deliberately remain in
    config so their history and settings survive.  Every non-interactive start/recovery must
    therefore re-check the live card monitor and the physical device's VoWiFi desired state.
    Explicit user starts keep their existing PIN/card preflight and actionable API errors.
    """
    if not inst.get("enabled", True):
        return False, "line_disabled"
    iid = str(inst.get("id") or "")
    iccid = str(inst.get("iccid") or "")
    cards = hub.cards_list()
    card_info = next((item for item in cards if item.get("present") and (
        (iccid and str(item.get("iccid") or "") == iccid)
        or str(item.get("matched") or "") == iid)), None)
    if card_info is None:
        return False, "no_card"
    device_id, _device_type = _device_for_card(card_info, cards)
    desired = device_state.desired()
    wanted = ((desired.get("devices") or {}).get(device_id)
              or desired.get("defaults") or {})
    if not wanted.get("vowifi_enabled", True):
        return False, "vowifi_disabled"
    return True, ""


def _auto_promote_card_draft(inst: dict, card_info: dict, cards: list[dict]) -> dict:
    """Promote a complete auto-created draft, or return it with missing-field hints.

    Hardware identity follows the physical reader/modem; SIM identity follows the ICCID.
    Keeping this as a synchronous helper makes the promotion rules independently testable.
    """
    if inst.get("provisioning_state") != "draft":
        return inst

    imsi = str(card_info.get("imsi") or inst.get("imsi") or "").strip()
    mcc = str(card_info.get("mcc") or inst.get("mcc") or (imsi[:3] if len(imsi) >= 3 else ""))
    mnc = str(card_info.get("mnc") or inst.get("mnc") or "")
    smsc = str(card_info.get("smsc") or inst.get("smsc") or "").strip()
    imei, hardware_id, _device_type = _hardware_imei_for_card(card_info, cards)
    missing = []
    if not imsi:
        missing.append("IMSI")
    if not mcc or not mnc:
        missing.append("MCC/MNC")
    if len(imei) != 15:
        missing.append("IMEI")
    if not smsc:
        missing.append("SMSC")
    if card_info.get("pin_enabled") is True and not inst.get("pin"):
        missing.append("SIM PIN")
    if missing:
        return {**inst, "auto_provision_missing": missing}

    previous_imeisv = str(inst.get("imeisv") or "")
    svn = (previous_imeisv[-2:] if len(previous_imeisv) == 16
           and previous_imeisv[-2:].isdigit() else _random_svn())
    sip = cfg.merge_carrier_sip_defaults(
        mcc, mnc, card_info.get("iccid") or imsi or imei, inst.get("sip"))
    # A draft normally arrives already named; only a name generated here needs deduplicating.
    resolved_iccid = str(card_info.get("iccid") or inst.get("iccid") or "")
    generated_name = not str(inst.get("name") or "").strip()
    update = {
        "id": str(inst["id"]),
        "name": (inst.get("name")
                 or cfg.default_instance_name(mcc, mnc, resolved_iccid)),
        "provisioning_state": "ready",
        "enabled": True,
        "imsi": imsi,
        "mcc": mcc,
        "mnc": mnc,
        **_carrier_identity_update(card_info),
        "iccid": str(card_info.get("iccid") or inst.get("iccid") or ""),
        "smsc": smsc,
        "imei": imei,
        "imei_source_device_id": hardware_id,
        "imeisv": cfg.imeisv_from_imei(imei, svn=svn),
        "reader": f"imsi:{imsi}",
        "sip": sip,
        # Production logs must not expose IMS-AKA material through Asterisk debug output.
        "debug": {**(inst.get("debug") or {}), "asterisk": False},
    }
    virtual = card_info.get("virtual_slots") or []
    if virtual:
        def slot(pos: int) -> dict:
            return virtual[min(pos, len(virtual) - 1)]

        update.update({
            "pin_reader": slot(0).get("name") or str(slot(0).get("index", 0)),
            "swu_reader": slot(1).get("name") or str(slot(1).get("index", 0)),
            "ami_reader": slot(2).get("name") or str(slot(2).get("index", 0)),
            "reader_index": int(slot(1).get("index") or card_info.get("index") or 0),
            "reader_port": "",
        })
    else:
        update.update({
            "reader_index": int(card_info.get("index") or inst.get("reader_index") or 0),
            "reader_port": str(card_info.get("reader_port") or inst.get("reader_port") or ""),
        })
    promoted = cfg.upsert_instance(update, unique_name=generated_name)
    egress.publish()
    log.info("hotplug draft %s auto-provisioned for MCC %s", inst["id"], mcc)
    return promoted


async def _on_card_remove(entry: dict, reader_unplugged: bool = False) -> bool:
    """Card pulled from a reader, or (reader_unplugged) the whole reader disconnected.
    Stops the SIP engine container serving that card. The entry must be the reader's
    LAST-KNOWN state (name/matched/iccid) — the caller must not blank it first.
    Returns True when a running line was stopped."""
    name, idx = entry.get("name", ""), entry.get("index")
    matched, iccid = entry.get("matched"), entry.get("iccid")
    if iccid:
        await asyncio.to_thread(cfg.unsuppress_card, iccid)
    if not reader_unplugged:
        hub.cards[name] = {"index": idx, "name": name, "present": False, "iccid": None,
                           "matched": None, "imsi": None, "pin_enabled": None,
                           "pin_tries": None}
    log.info("%s reader=%s (%s) (identity=%s matched=%s)",
             "reader unplugged" if reader_unplugged else "card removed",
             idx, name, "available" if iccid else "unknown", matched)
    target = None
    if matched:
        target = cfg.get_instance(matched)
    if target is None and iccid:
        target = _match_instance_by_iccid(iccid)
    if target is None:
        # Unknown/unmatched identity: map by the reader NAME the running engine reports
        # using (pin_status.json). This is the only safe fallback — guessing "the single
        # running instance" could stop a healthy line on ANOTHER reader.
        target = await asyncio.to_thread(_find_running_by_reader, name)
    # Card removal is an explicit terminal condition for the current run. Cancel a frozen
    # cooldown even when its container was already removed: otherwise that in-memory recovery
    # timer can recreate an engine minutes after the SIM disappeared.
    if target:
        hub.reset_health(str(target["id"]))
    if target and await asyncio.to_thread(engine.is_running, str(target["id"])):
        # Stop the SIP server + docker container on card/reader removal.
        await asyncio.to_thread(engine.stop, str(target["id"]))
        await hub.drop_ami(str(target["id"]))
        await hub.broadcast({"type": "engine", "instance": target["id"],
                             "event": "reader_lost" if reader_unplugged else "card_removed",
                             "args": [name]})
        stopped_status = {"state": "NO_CARD",
                          "label": "Reader unplugged" if reader_unplugged
                                   else "No SIM card (removed)",
                          "reason_code": "no_card", "reason": "SIM card is not available.",
                          "detail": {}}
        stopped_status = _with_status_activity(str(target["id"]), stopped_status)
        hub.status_cache[str(target["id"])] = stopped_status
        hub.status_sampled_at[str(target["id"])] = time.monotonic()
        await hub.broadcast({"type": "status", "instance": str(target["id"]),
                             **stopped_status})
        return True
    return False


async def card_monitor():
    """Real-time monitor for BOTH reader hotplug (plug/unplug) and card insert/remove.
    State is keyed by reader NAME: PC/SC indices shift when a reader is unplugged, so
    names are the stable identity; each entry's `index` field is refreshed every scan for
    the API calls that take reader_index. Between scans it blocks in
    card.wait_for_change (PnP-aware SCardGetStatusChange), so hotplug is reflected
    near-instantly without hammering pcscd."""
    first = True
    while True:
        try:
            states = await asyncio.to_thread(card.reader_states)
            if states is None:
                # Transient PC/SC error (pcscd restarting?) — NOT "all readers gone".
                # Skip this cycle; keep known state and engines untouched.
                log.debug("card monitor: PC/SC unavailable, skipping scan")
                await asyncio.sleep(1.2)
                continue
            current = {st["name"]: st for st in states}
            changed = False

            # The host orchestrator briefly restarts pcscd after changing generated VPCD reader
            # stanzas.  Treat that explicit maintenance window as enumeration churn, not as a
            # physical unplug, so healthy engine containers are not stopped.
            maintenance = False
            marker = os.path.join(cfg.DATA_DIR, "orchestrator", "pcsc-maintenance")
            try:
                # Rebuilding sing-box, ModemManager ownership and all virtual readers can take
                # more than 15 seconds on a Pi. Keep this comfortably above the observed full
                # orchestrator restart time so planned churn cannot be mistaken for an unplug.
                maintenance = (time.time() - os.path.getmtime(marker)
                               < PCSC_MAINTENANCE_WINDOW_SECONDS)
            except OSError:
                pass
            if maintenance:
                await asyncio.sleep(0.5)
                continue

            # reader unplugged -> drop its row + stop any engine bound to it
            for name in [n for n in hub.cards if n not in current]:
                entry = hub.cards.pop(name)
                stopped = await _on_card_remove(entry, reader_unplugged=True)
                if not stopped:
                    # _on_card_remove already broadcast the (more informative)
                    # "reader_lost — line stopped" event; only emit the generic one
                    # when no line was affected, so the UI shows a single toast.
                    await hub.broadcast({"type": "engine", "instance": "",
                                         "event": "reader_removed", "args": [name]})
                changed = True

            for name, st in current.items():
                entry = hub.cards.get(name)
                # LPA holds the reader exclusively and enable/disable triggers REFRESH
                # (looks like remove+insert). Keep last-known state; skip insert/remove.
                if hub.lpa_busy.get(name):
                    if entry is None:
                        hub.cards[name] = {**st, "iccid": None, "matched": None,
                                           "imsi": None, "pin_enabled": None,
                                           "pin_tries": None}
                        changed = True
                    elif entry.get("index") != st["index"]:
                        entry["index"] = st["index"]
                        changed = True
                    continue
                if entry is None:
                    # reader newly plugged in (or first scan after manager start)
                    if not first:
                        log.info("reader plugged in: %s", name)
                        await hub.broadcast({"type": "engine", "instance": "",
                                             "event": "reader_added", "args": [name]})
                    if st["present"]:
                        await _on_card_insert(name, st["index"])
                    else:
                        hub.cards[name] = {**st, "iccid": None, "matched": None,
                                           "imsi": None, "pin_enabled": None,
                                           "pin_tries": None}
                    changed = True
                    continue
                if entry.get("index") != st["index"]:
                    entry["index"] = st["index"]     # indices shift on unplug
                    # The physical reader behind this name/index may have changed — refresh the
                    # stable USB port binding so the display + ICCID->port learning stay correct.
                    try:
                        entry["reader_port"] = await asyncio.to_thread(
                            usbreader.port_for_index, st["index"])
                    except Exception:  # noqa
                        pass
                    changed = True
                if bool(entry.get("present")) != st["present"]:
                    # eUICC REFRESH during LPA looks like remove+insert — keep last-known
                    # state and do not stop engines / probe until the LPA op finishes.
                    if hub.lpa_busy.get(name):
                        entry["present"] = st["present"]
                        changed = True
                        continue
                    if st["present"]:
                        await _on_card_insert(name, st["index"])
                    else:
                        await _on_card_remove(entry)
                    changed = True
            # The first completed scan is always announced, even when it found nothing:
            # it is what turns the UI's "detecting devices" state into a real answer.
            if changed or first:
                await hub.broadcast({"type": "cards", "cards": _client_cards()})
            # Only a completed scan counts: a failed first scan must retry as "first"
            # (readers seen later may belong to already-running engines).
            hub.scanned = True
            first = False
        except Exception as e:  # noqa
            log.debug("card monitor error: %r", e)
        # Instant wake on any reader/card change; the timeout bounds the worst case for
        # changes that slip between a scan and the next wait (fresh-snapshot window).
        # The short sleep bounds the rescan rate if something reports changes endlessly.
        await asyncio.to_thread(card.wait_for_change, 2.5)
        await asyncio.sleep(0.25)


def extract_msisdn(iid):
    """Learn the registered MSISDN from the P-Associated-URI in the engine SIP logs."""
    logs = engine.logs(iid, 1200)
    matches = re.findall(r'P-Associated-Uri:\s*<(?:tel:|sip:)(\+\d+)', logs, re.I)
    return matches[-1] if matches else None


def _needs_ims_msisdn_learning(inst: dict) -> bool:
    """Whether IMS has to be asked for the line number, by re-registering to produce one.

    ModemManager OwnNumbers is only a hint: modems commonly retain a stale value across SIM
    swaps, omit the leading '+', or expose a service number instead of the IMS public identity.
    Only an unknown or hinted number justifies the initial learning loop; a number already learned
    from IMS is re-checked on a much slower controlled cadence (see _verify_ims_msisdn).
    """
    return (not str(inst.get("msisdn") or "").strip()
            or inst.get("msisdn_source") == "modemmanager")


async def _verify_ims_msisdn(iid: str, inst: dict) -> None:
    """Follow the number the carrier hands out at registration.

    A ported number is exactly this: the same SIM, registering normally, answered with a
    different public identity. Treating the first IMS answer as permanent left the line
    presenting its previous number as caller identity indefinitely. PJSIP logs the identity only
    while its packet logger is enabled, so the slow verification cadence performs one controlled
    registration refresh and immediately disables packet logging again.

    A manually entered number is never overridden: that is a deliberate operator choice.
    """
    if inst.get("msisdn_source") != "ims":
        return
    # A freshly booted host can have a monotonic clock below the interval.  Treat a missing
    # entry as "never checked" instead of comparing it with the clock's zero point.
    if (iid in hub._msisdn_checked
            and time.monotonic() - hub._msisdn_checked[iid]
            < MSISDN_VERIFY_INTERVAL_SECONDS):
        return
    hub._msisdn_checked[iid] = time.monotonic()
    # P-Associated-URI is visible only while Asterisk's PJSIP packet logger is enabled. A
    # container rebuild resets that runtime flag, so merely tailing old logs makes this feature
    # silently stop working. Turn it on only for one controlled REGISTER (leaving it enabled would
    # retain authentication headers), then immediately turn it off again.
    logger_enabled = False
    try:
        await asyncio.to_thread(engine.exec_cli, iid, "pjsip set logger on")
        logger_enabled = True
        await asyncio.to_thread(engine.exec_cli, iid, "pjsip send register volte_ims")
        await asyncio.sleep(MSISDN_VERIFY_SETTLE_SECONDS)
        observed = await asyncio.to_thread(extract_msisdn, iid)
    except Exception as exc:  # noqa: a transient CLI failure is retried on the next interval
        log.debug("IMS number verification failed for line %s: %s", iid, type(exc).__name__)
        hub._msisdn_checked[iid] = (
            time.monotonic() - MSISDN_VERIFY_INTERVAL_SECONDS
            + MSISDN_VERIFY_FAILURE_RETRY_SECONDS)
        return
    finally:
        if logger_enabled:
            try:
                await asyncio.to_thread(engine.exec_cli, iid, "pjsip set logger off")
            except Exception:
                pass
    stored = str(inst.get("msisdn") or "")
    if not observed or observed == stored:
        return
    log.warning("line %s number changed at the carrier: %s -> %s", iid, stored, observed)
    # instance.json and Asterisk's dialplan are snapshots taken at container start, so the
    # line would keep presenting the old number as caller identity until it is rebuilt. Rebuild
    # BEFORE committing the new number: if fail-closed egress or Docker rejects the rebuild, the
    # stored old value makes the next verification retry instead of declaring a half-applied
    # change complete.
    candidate = {**inst, "id": iid, "msisdn": observed, "msisdn_source": "ims"}
    try:
        await hub.drop_ami(iid)
        await asyncio.to_thread(_start_engine_checked, candidate, cfg.get_settings(),
                                os.environ.get("MDD_DEV_MOUNTS", "") == "1",
                                "number-changed")
        updated = await asyncio.to_thread(
            cfg.upsert_instance, {"id": iid, "msisdn": observed, "msisdn_source": "ims"})
    except Exception as exc:  # noqa: background verification must never leak an unhandled task
        hub._msisdn_checked[iid] = (
            time.monotonic() - MSISDN_VERIFY_INTERVAL_SECONDS
            + MSISDN_VERIFY_FAILURE_RETRY_SECONDS)
        log.warning("IMS number change could not be applied for line %s (%s); will retry",
                    iid, type(exc).__name__)
        return
    client = hub.ami.get(iid)
    if client:
        client.msisdn = observed
    await hub.broadcast({"type": "engine", "instance": iid,
                         "event": "msisdn_updated", "args": []})
    asyncio.create_task(asyncio.to_thread(
        notify_push.dispatch, cfg.get_settings(), notify_push.EV_NUMBER_CHANGED, updated,
        observed, f"{stored or '(未知)'} → {observed}\n"
                  "运营商在 IMS 注册时下发了新号码（通常是携号转网）。线路已重建，"
                  "主叫身份和短信发信人已同步更新。"))


async def learn_msisdn(iid):
    """One-shot: enable the SIP logger, re-register to produce a fresh 200 OK, then parse
    the P-Associated-URI. Capped attempts so we don't re-register forever."""
    try:
        await asyncio.to_thread(engine.exec_cli, iid, "pjsip set logger on")
        await asyncio.to_thread(engine.exec_cli, iid, "pjsip send register volte_ims")
        await asyncio.sleep(8)
        msisdn = await asyncio.to_thread(extract_msisdn, iid)
        if msisdn:
            current = cfg.get_instance(iid) or {}
            # IMS registration is authoritative and may correct an OwnNumbers value
            # previously learned from ModemManager. Never overwrite a manual value.
            if current.get("msisdn") and current.get("msisdn_source") != "modemmanager":
                return
            identity_changed = str(current.get("msisdn") or "") != msisdn
            updated = cfg.upsert_instance({"id": iid, "msisdn": msisdn,
                                           "msisdn_source": "ims"})
            c = hub.ami.get(iid)
            if c:
                c.msisdn = msisdn
            log.info("learned line number for instance %s", iid)
            # instance.json and Asterisk's pjsip/dialplan are snapshots from container start.
            # When IMS corrected a ModemManager hint, persist-only is insufficient: outgoing
            # INVITEs would keep sending the stale P-Preferred-Identity and the carrier would
            # immediately terminate them (observed as 487 -> browser-side 603). Recreate the
            # running engine once so registration, From and PPI all use the authoritative value.
            if identity_changed and await asyncio.to_thread(engine.is_running, iid):
                await hub.drop_ami(iid)
                await asyncio.to_thread(_start_engine_checked, updated, cfg.get_settings(),
                                        os.environ.get("MDD_DEV_MOUNTS", "") == "1")
                hub.reset_health(iid)
                log.info("restarted instance %s to apply IMS line identity", iid)
            await hub.broadcast({"type": "engine", "instance": iid, "event": "msisdn", "args": [msisdn]})
    except Exception as e:  # noqa
        log.debug("learn_msisdn error: %r", e)
    finally:
        hub._learning.discard(iid)


async def sync_modem_msisdns():
    """Fill empty line numbers from ModemManager, gated by the current SIM ICCID.

    OwnNumbers can lag behind a physical SIM swap on some modems. Requiring both a
    non-empty current SIM ICCID and an exact configured-line match prevents a stale
    modem value from being assigned to whichever line happens to use the device.
    """
    observed = device_state.status().get("devices") or {}
    for device in observed.values():
        cellular = (device or {}).get("cellular") or {}
        msisdn = str(cellular.get("msisdn") or "").strip()
        sim_iccid = str(cellular.get("sim_iccid") or "").strip()
        if not msisdn or not sim_iccid:
            continue
        inst = _match_instance_by_iccid(sim_iccid)
        if not inst or inst.get("msisdn"):
            continue
        iid = str(inst["id"])
        cfg.upsert_instance({"id": iid, "msisdn": msisdn,
                             "msisdn_source": "modemmanager"})
        client = hub.ami.get(iid)
        if client:
            client.msisdn = msisdn
        log.info("learned line number from modem for instance %s", iid)
        await hub.broadcast({"type": "engine", "instance": iid,
                             "event": "msisdn_updated", "args": []})


def _line_state_kind(st: dict) -> str | None:
    """Map the status machine onto the states the connectivity timeline records.

    Returns None when a sample carries no evidence about connectivity, which must not be
    written as a disconnect. compute() only reaches REGISTERING after the tunnel is
    installed, so a registration of "unknown" there means the read itself failed — the
    management timeout this codebase already refuses to treat as a carrier failure. Charting
    it as an outage makes a healthy line look like it keeps dropping.
    """
    state = str((st or {}).get("state") or "").upper()
    if state == "OK":
        return "up"
    if state == "STOPPED":
        return "off"
    detail = (st or {}).get("detail") or {}
    if state == "REGISTERING" and str(detail.get("registration") or "unknown").lower() == "unknown":
        return None
    return "down"


def _outage_detail(st: dict) -> str:
    """Compact structured evidence behind a disconnect's reason code.

    The database keeps this JSON as text for schema compatibility. The WebUI localises its
    evidence code into a short "which side failed to answer what" sentence; older free-form
    rows still render unchanged.
    """
    code = str((st or {}).get("reason_code") or "")
    detail = (st or {}).get("detail") or {}
    fqdn = str(detail.get("epdg_fqdn") or "")
    def evidence(evidence_code: str, **values) -> str:
        return json.dumps({"code": evidence_code,
                          **{key: value for key, value in values.items() if value not in (None, "", [])}},
                         ensure_ascii=False, separators=(",", ":"))

    if code == "epdg_unresolved":
        return evidence("client_dns_unresolved", peer=fqdn,
                        servers=[str(s) for s in (detail.get("nameservers") or [])])
    if code == "tunnel_child_rekey_timeout":
        return evidence("server_epdg_child_rekey_unanswered", peer=fqdn)
    if code == "tunnel_ike_rekey_timeout":
        return evidence("server_epdg_ike_rekey_unanswered", peer=fqdn)
    if code == "tunnel_rekey_send_error":
        return evidence("client_rekey_send_failed", peer=fqdn)
    if code == "tunnel_network":
        return evidence("server_epdg_ike_unanswered", peer=fqdn)
    if code == "tunnel_sim_auth":
        return evidence("client_sim_auth_failed", peer=fqdn)
    if code == "tunnel_not_authorized":
        return evidence("server_epdg_identity_rejected", peer=fqdn)
    if code == "tunnel_proposal":
        return evidence("server_epdg_proposal_rejected", peer=fqdn)
    if code == "tunnel_setup":
        # CONNECTING is a recovery state, not a root cause. If no timeout, rejection or
        # local send failure has appeared yet, say the earlier fault was not captured
        # instead of presenting the rebuild itself as the reason for the outage.
        return evidence("tunnel_cause_not_captured", peer=fqdn)
    if code == "reg_unanswered":
        return evidence("server_pcscf_register_unanswered", peer=detail.get("pcscf"))
    if code in {"reg_rejected", "reg_reauth_failed"}:
        return evidence("server_pcscf_sip_rejected", peer=detail.get("pcscf"),
                        status=detail.get("sip_status"))
    if code == "registering":
        return evidence("client_registration_incomplete", peer=detail.get("pcscf"))
    if code == "maintenance_rebuild":
        return evidence("client_maintenance_rebuild")
    if code == "client_engine_failure":
        return evidence("client_engine_worker_failed")
    return ""


async def _record_line_state(iid: str, st: dict) -> None:
    """Persist one observation, skipping writes that would only repeat the known state.

    Status is sampled every few seconds; committing that to SQLite unchanged would be a
    constant write load on the SD card an appliance boots from. A transition is always
    written immediately — it is the event the timeline exists to show — and an unchanged
    state is refreshed often enough to stay well inside the segment continuity window.
    """
    iid = str(iid)
    kind = _line_state_kind(st)
    if kind is None:
        # Leave the timeline untouched. A brief blind spot is absorbed by the segment
        # continuity window; a long one falls outside it and surfaces as `unknown`, which is
        # exactly what the record can honestly claim. Forget the last write so the next real
        # observation is committed immediately rather than waiting out the refresh interval.
        _line_state_written.pop(iid, None)
        return
    previous = _line_state_written.get(iid)
    if (previous and previous[0] == kind
            and time.monotonic() - previous[1] < LINE_STATE_WRITE_INTERVAL_SECONDS):
        return
    _line_state_written[iid] = (kind, time.monotonic())
    # Only a disconnect needs explaining. The first down sample carries the cause; the store
    # keeps it for the whole segment.
    reason = str((st or {}).get("reason_code") or "") if kind == "down" else ""
    try:
        await asyncio.to_thread(store.record_line_state, iid, kind, reason=reason,
                                detail=_outage_detail(st) if reason else "")
    except Exception as exc:  # noqa
        # History is diagnostic only; never let it interrupt status sampling.
        _line_state_written.pop(iid, None)
        log.debug("line state record failed instance=%s: %r", iid, exc)


def _status_poll_delay(instances: list[dict]) -> float:
    """Fast only while a running line is actively converging.

    Enabled lines can legitimately have no container while their reader is absent. PC/SC and
    Docker events wake those paths immediately, so treating STOPPED/NO_CARD as perpetually busy
    kept the whole gateway on the four-second cadence for no useful reason.
    """
    active = []
    for inst in instances:
        if not inst.get("enabled", True):
            continue
        active.append((hub.status_cache.get(str(inst["id"])) or {}).get("state"))
    return (STATUS_POLL_FAST_SECONDS
            if any(state in {"REGISTERING", "TUNNEL_DOWN"} for state in active)
            else STATUS_POLL_HEALTHY_SECONDS)


async def status_poller():
    last_prune = 0.0
    while True:
        # Clear before sampling: an event arriving during the work remains set and causes an
        # immediate next pass instead of being lost between the sample and the wait.
        hub.status_wakeup.clear()
        instances = []
        try:
            instances = cfg.list_instances()
            await sync_modem_msisdns()
            await asyncio.gather(*(_poll_instance_status(inst)
                                   for inst in instances))
            if time.monotonic() - last_prune >= LINE_HISTORY_PRUNE_INTERVAL_SECONDS:
                last_prune = time.monotonic()
                await asyncio.to_thread(store.prune_line_states,
                                        int(time.time()) - store.LINE_STATE_RETENTION_SECONDS)
        except Exception as e:  # noqa
            log.debug("poller error: %r", e)
        try:
            await asyncio.wait_for(hub.status_wakeup.wait(), timeout=_status_poll_delay(instances))
        except asyncio.TimeoutError:
            pass


HOST_ALERT_POLL_SECONDS = 60.0
# Re-announce a condition that simply persists, rather than staying silent for days.
HOST_ALERT_REPEAT_SECONDS = float(os.environ.get("MDD_HOST_ALERT_REPEAT", "21600"))
# A condition has to stay absent this long before it counts as recovered. Without it, a
# measurement sitting near its threshold crosses back and forth all day and each re-entry
# looks like a new problem worth notifying about.
HOST_ALERT_CLEAR_SECONDS = float(os.environ.get("MDD_HOST_ALERT_CLEAR", "1800"))
ALLOWANCE_REMINDER_POLL_SECONDS = float(
    os.environ.get("MDD_ALLOWANCE_REMINDER_POLL", "3600"))

HOST_ALERT_TEXT = {
    "undervoltage_now": "供电电压不足（正在发生）。网口、蜂窝模块和读卡器共用同一路供电，"
                        "欠压会让所有线路同时掉线。请更换更强的电源或为 USB 设备加独立供电。",
    "undervoltage_seen": "检测到历史欠压事件。所有线路会在欠压瞬间同时中断。",
    "throttled_now": "CPU 正在降频/节流，处理能力下降。",
    "temperature_high": "主机温度过高，已接近或进入热节流。",
    "disk_critical": "磁盘空间即将耗尽，可能损坏历史数据并导致线路无法写入运行状态。",
    "disk_low": "磁盘空间偏低。",
    "swap_pressure": "正在频繁换页。在 SD 卡上换页会拖慢所有操作并造成状态查询超时。",
    "default_route_changed": "默认路由在上行之间发生了切换，所有出站连接的源地址随之改变。",
}


async def allowance_reminder_poller():
    """Send one reminder per line/expiry/day at 3, 2 and 1 days before expiry."""
    while True:
        try:
            settings = cfg.get_settings()
            if notify_push.has_enabled_channel(settings, notify_push.EV_ACTIVATION_REMINDER):
                try:
                    local_zone = ZoneInfo(str(settings.get("timezone") or "Asia/Shanghai"))
                except ZoneInfoNotFoundError:
                    local_zone = ZoneInfo("UTC")
                now = datetime.now(local_zone)
                for inst in cfg.list_instances():
                    iid = str(inst.get("id") or "")
                    snapshot = await asyncio.to_thread(store.get_allowance, iid)
                    days = allowance.reminder_days(snapshot, now.date())
                    expiry = allowance.parse_expiry_date(snapshot.get("valid_until"))
                    if days is None or expiry is None:
                        continue
                    claimed = await asyncio.to_thread(
                        store.claim_allowance_reminder, iid, expiry.isoformat(), days,
                        int(now.timestamp()))
                    if not claimed:
                        continue
                    text = (f"线路 {inst.get('name') or iid} 将于 {expiry.isoformat()} 到期，"
                            f"还剩 {days} 天。激活时间：{snapshot.get('activated_at')}。"
                            "请及时续期或重新激活。")
                    await asyncio.to_thread(
                        notify_push.dispatch, settings, notify_push.EV_ACTIVATION_REMINDER,
                        inst, expiry.isoformat(), text)
        except Exception as exc:  # noqa
            log.debug("allowance reminder poll failed: %r", exc)
        await asyncio.sleep(max(60.0, ALLOWANCE_REMINDER_POLL_SECONDS))


def _host_alert_summary(alerts: list[dict]) -> str:
    lines = []
    for item in alerts:
        detail = ", ".join(f"{k}={v}" for k, v in (item.get("detail") or {}).items())
        text = HOST_ALERT_TEXT.get(item["code"], item["code"])
        lines.append(f"[{item['severity']}] {text}" + (f" ({detail})" if detail else ""))
    return "\n".join(lines)


def _visible_host_alerts(alerts: list[dict], state: dict) -> list[dict]:
    """Hide acknowledged conditions until the poller observes a sustained recovery."""
    return [item for item in alerts
            if not (state.get(item["code"]) or {}).get("acknowledged")]


async def host_health_poller():
    """Announce host conditions that take every line down at once.

    Displaying this only in a panel is not enough: the whole point is that it explains an
    outage nobody would otherwise attribute to the box. Notifications fire on the transition
    into a condition, then only on a long repeat interval, so a persistent brown-out does not
    become a stream nobody reads.

    The suppression state is persisted because it is measured in hours: keeping it in memory
    meant every manager restart re-announced everything that was already known, and an
    appliance is restarted for upgrades far more often than these conditions change.
    """
    if hub.host_alert_state is None:
        hub.host_alert_state = _load_host_alert_state()
    state = hub.host_alert_state
    streaks: dict[str, int] = {}
    previous_alerts = None
    while True:
        try:
            snapshot = await asyncio.to_thread(sysinfo.collect, cfg.DATA_DIR)
            # Rate-based conditions need the previous sample; the first pass reports none.
            alerts = sysinfo.alerts(snapshot, hub.host_snapshot or None)
            alerts = _sustained_alerts(alerts, streaks)
            now = time.time()
            codes = {item["code"] for item in alerts}
            for code, entry in list(state.items()):
                if code in codes:
                    entry.pop("missing_since", None)
                    continue
                missing_since = entry.setdefault("missing_since", now)
                if now - missing_since >= HOST_ALERT_CLEAR_SECONDS:
                    state.pop(code, None)       # genuinely recovered; a return may notify again
            visible = _visible_host_alerts(alerts, state)
            hub.host_snapshot, hub.host_alerts = snapshot, visible
            fresh = [item for item in visible
                     if now - float((state.get(item["code"]) or {}).get("at", 0))
                     >= HOST_ALERT_REPEAT_SECONDS]
            if fresh:
                for item in fresh:
                    state[item["code"]] = {"at": now}
                    log.warning("host alert %s (%s): %s", item["code"], item["severity"],
                                item.get("detail"))
                await hub.broadcast({"type": "host_alert",
                                     "alerts": [item["code"] for item in fresh]})
                asyncio.create_task(asyncio.to_thread(
                    notify_push.dispatch, cfg.get_settings(), notify_push.EV_HOST_ALERT,
                    {"id": "host", "name": snapshot.get("model") or "gateway"},
                    snapshot.get("model") or "gateway", _host_alert_summary(fresh)))
            if fresh or codes != previous_alerts:
                previous_alerts = codes
                await asyncio.to_thread(_save_host_alert_state, state)
        except Exception as exc:  # noqa
            log.debug("host health poll failed: %r", exc)
        await asyncio.sleep(HOST_ALERT_POLL_SECONDS)


def _sustained_alerts(alerts: list[dict], streaks: dict[str, int]) -> list[dict]:
    """Drop conditions that have not held long enough to be worth acting on.

    A one-sample spike is real but not actionable: it is what a container start costs on this
    hardware. Reporting it anyway is how an indicator earns the reputation that makes people
    ignore the one explaining a genuine outage.
    """
    present = {item["code"] for item in alerts}
    for code in list(streaks):
        if code not in present:
            del streaks[code]
    kept = []
    for item in alerts:
        code = item["code"]
        if code not in SUSTAINED_ALERT_CODES:
            kept.append(item)
            continue
        streaks[code] = streaks.get(code, 0) + 1
        if streaks[code] >= SUSTAINED_ALERT_SAMPLES:
            kept.append({**item, "detail": {**(item.get("detail") or {}),
                                            "samples": streaks[code]}})
    return kept


def _save_exit_ledgers() -> None:
    path = _exit_ledger_path()
    try:
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(hub.exit_ledgers, handle)
        os.replace(temporary, path)
    except OSError as exc:
        log.debug("cannot persist exit failover ledger: %r", exc)


def _peer_line_registered(iid: str, country: str) -> bool:
    """Whether another line of the same country is registered right now.

    Lines of one country share one exit. A registered peer is living proof the exit can carry
    IMS — and a tunnel that moving the exit would tear down, which is the disruption this
    design exists to avoid. While one holds, eviction is off the table.
    """
    if not country:
        return False
    for other in cfg.list_instances():
        oid = str(other.get("id") or "")
        if oid == str(iid) or not other.get("enabled", True):
            continue
        if egress.line_country(other) != country:
            continue
        if (hub.status_cache.get(oid) or {}).get("state") == "OK":
            return True
    return False


def _judge_exit_failure(iid: str, inst: dict, st: dict, stable_for: float) -> str:
    """Attribute one line freeze and act on it: hold, move the exit, back off, or stop.

    Runs on the freeze path only, so reading the tunnel's own evidence here costs nothing in
    the steady state. Both reads are deliberately cheap — the full diagnostic snapshot is
    captured separately and must not be on this decision's critical path.
    """
    iid = str(iid)
    country = egress.line_country(inst)
    exits = (egress.status().get("exits") or {}).get(country) or {}
    node = str(exits.get("node") or "")
    candidates = [str(name) for name in (exits.get("candidates") or [])]
    pinned = exits.get("selection") == "manual"
    peer_registered = _peer_line_registered(iid, country)
    try:
        swu = (engine.read_run_json(iid, "swu_status.json") or {}).get("state") or ""
        retransmits = int((engine.ike_evidence(iid) or {}).get("retransmits") or 0)
    except Exception as exc:  # noqa
        log.debug("cannot read tunnel evidence for line %s: %r", iid, exc)
        swu, retransmits = "", 0
    verdict = failover.classify(swu, retransmits, stable_for,
                                egress.RESELECT_MIN_STABLE_SECONDS)
    was_backing_off = bool((hub.exit_ledgers.get(iid) or {}).get("exhausted"))
    action, ledger = failover.record(hub.exit_ledgers.get(iid), verdict, node,
                                     pinned, candidates, peer_registered=peer_registered)
    hub.exit_ledgers[iid] = ledger
    _save_exit_ledgers()
    log.info("line %s froze (%s) after %.0fs healthy; tunnel=%s ike_retransmits=%d "
             "-> blames %s, action %s (node=%s strikes=%d tried=%d/%d peer=%s)",
             iid, st.get("reason_code"), stable_for, swu or "unknown", retransmits,
             verdict, action, node or "unknown", ledger.get("strikes") or 0,
             len(ledger.get("tried") or []), len(candidates), peer_registered)
    if action == failover.SWITCH:
        try:
            egress.request_reselect(inst, f"health-freeze:{st['reason_code']}",
                                    stable_for=stable_for)
        except Exception as exc:  # noqa
            log.warning("exit reselect request failed for line %s: %s", iid, exc)
    elif action in (failover.GIVE_UP, failover.REPORT) or (
            action == failover.BACK_OFF and not was_backing_off):
        text = failover.summarise(ledger, action, country, pinned)
        log.warning("line %s: %s", iid, text)
        asyncio.create_task(asyncio.to_thread(
            notify_push.dispatch, cfg.get_settings(), notify_push.EV_LINE_UNRECOVERABLE,
            inst, node or country.upper(), text))
    return action


def _host_alert_state_path() -> str:
    return os.path.join(cfg.DATA_DIR, "host-alert-state.json")


def _load_host_alert_state() -> dict:
    try:
        with open(_host_alert_state_path(), encoding="utf-8") as handle:
            loaded = json.load(handle)
        return {str(k): v for k, v in loaded.items() if isinstance(v, dict)}
    except (OSError, ValueError, AttributeError):
        return {}


def _save_host_alert_state(state: dict) -> None:
    path = _host_alert_state_path()
    try:
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(temporary, path)
    except OSError as exc:
        log.debug("cannot persist host alert state: %r", exc)


async def cellular_sms_poller():
    """Import SMS received by the 4G modem even when its VoWiFi engine is stopped."""
    scanner = cellular_sms.Scanner(local_sms_tracker=store)
    while True:
        try:
            discovered = await asyncio.to_thread(scanner.discover, cfg.list_instances())
            for item in discovered:
                rec = await asyncio.to_thread(
                    store.add_imported_message, item["fingerprint"], item["instance"],
                    item["direction"], item["peer"], item["body"], item["ts"],
                    item["transport"])
                if not rec:
                    continue
                await hub.broadcast({"type": "sms", "instance": rec["instance"],
                                     "message": rec})
                if rec["direction"] == "in":
                    _dispatch_push(notify_push.EV_INCOMING_SMS, rec["instance"],
                                   rec["peer"], rec["body"])
        except Exception as exc:  # noqa
            log.debug("cellular SMS poll failed: %r", exc)
        await asyncio.sleep(5)


async def _poll_instance_status(inst: dict) -> None:
    """Sample one line in the background; slow carrier state never blocks HTTP pages."""
    iid = str(inst["id"])
    try:
        # One inspect supplies both running state and bridge IP to the whole sample. Previously
        # ami_for(), compute() and the grace-path each queried Docker independently.
        runtime = await hub.runtime.get(iid)
        # A disabled line is authoritative user intent. Automatic recovery must never
        # resurrect a stale container left behind by an earlier retry or process restart;
        # doing so can retain the SIM/PCSC channel and disrupt another active line.
        if not inst.get("enabled", True):
            # The poll list is a snapshot. A reader-enable request may have persisted ON after
            # this iteration began; re-read before enforcing OFF so an old snapshot cannot stop
            # the container that the request is about to start.
            current = await asyncio.to_thread(cfg.get_instance, iid)
            if current and current.get("enabled", True):
                inst = current
            else:
                if runtime["running"]:
                    await asyncio.to_thread(engine.stop, iid)
                    await hub.drop_ami(iid)
                hub.reset_health(iid)
                stopped = _with_status_activity(iid, {
                    "state": "STOPPED", "label": status_mod.LABELS["STOPPED"],
                    "reason_code": "stopped", "reason": "Stopped.", "detail": {},
                    "retry": {"count": 0, "max": 0}})
                hub.status_cache[iid] = stopped
                hub.status_sampled_at[iid] = time.monotonic()
                await _record_line_state(iid, stopped)
                await hub.broadcast({"type": "status", "instance": iid, **stopped})
                return
        ami = await hub.ami_for(iid, runtime)
        st = await status_mod.compute(inst, ami, runtime)
        registration = str((st.get("detail") or {}).get("registration") or "unknown")
        previous = hub.status_cache.get(iid)
        previous_sampled_at = hub.status_sampled_at.get(iid)
        observed_at = time.monotonic()
        held_previous = False
        # A single management timeout is not evidence that a known-good registration vanished.
        # Hold the last confirmed OK briefly, but never refresh that timestamp from unknown
        # samples: a dead Asterisk must become unhealthy after the bounded grace period.
        if (st.get("state") == "REGISTERING" and registration == "unknown"
                and (previous or {}).get("state") == "OK"
                and observed_at - hub.status_sampled_at.get(iid, 0) <= STATUS_OK_GRACE_SECONDS
                and runtime["running"]):
            st = previous
            held_previous = True
        if st["state"] == "OK" and _needs_ims_msisdn_learning(inst) \
                and iid not in hub._learning and hub._msisdn_tries.get(iid, 0) < 4:
            hub._learning.add(iid)
            hub._msisdn_tries[iid] = hub._msisdn_tries.get(iid, 0) + 1
            asyncio.create_task(learn_msisdn(iid))
        elif st["state"] == "OK":
            asyncio.create_task(_verify_ims_msisdn(iid, inst))
        st = _with_status_activity(
            iid, apply_health(iid, inst, st, runtime.get("container_id")))
        hub.status_cache[iid] = st
        if held_previous and previous_sampled_at is not None:
            # apply_health(OK) clears health and its related cache bookkeeping. Restore the
            # original authoritative timestamp, never the current poll time, so unknown
            # samples cannot extend the grace window indefinitely.
            hub.status_sampled_at[iid] = previous_sampled_at
        elif not held_previous:
            hub.status_sampled_at[iid] = observed_at
        await _record_line_state(iid, st)
        await hub.broadcast({"type": "status", "instance": iid, **st})
    except Exception as exc:  # noqa
        log.debug("status sample failed instance=%s: %r", iid, exc)


def _cached_line_status(inst: dict) -> dict:
    """Return an immediate status snapshot without contacting Docker/Asterisk/AMI."""
    iid = str(inst["id"])
    cached = hub.status_cache.get(iid)
    if cached:
        return dict(cached)
    if not inst.get("enabled", True):
        return _with_status_activity(iid, {
            "state": "STOPPED", "label": status_mod.LABELS["STOPPED"],
            "reason_code": "stopped", "reason": "Stopped.", "detail": {}})
    return _with_status_activity(iid, {
        "state": "REGISTERING", "label": status_mod.LABELS["REGISTERING"],
        "reason_code": "registering", "reason": "Refreshing line status…", "detail": {}})


def _with_status_activity(iid: str, st: dict) -> dict:
    """Explain the status machine in user terms: now, why, and what happens next."""
    st = dict(st)
    state = str(st.get("state") or "").upper()
    detail = st.get("detail") or {}
    retry = st.get("retry") or {}
    health = hub.health_for(str(iid))
    retrying = bool(health.get("auto_retrying"))
    remaining = st.get("automatic_retry_in")

    if retrying:
        current = "Rebuilding the VoWiFi line automatically"
        next_action = "The SIM will be read again, then ePDG and IMS will reconnect."
    elif st.get("frozen") and remaining:
        current = "Automatic recovery is waiting"
        next_action = "The VoWiFi line will be rebuilt in {seconds} seconds."
    elif st.get("frozen"):
        current = st.get("reason") or "Waiting for manual attention"
        next_action = ("Verify the SIM PIN before automatic setup can continue."
                       if str(st.get("reason_code") or "").startswith("pin_")
                       else "Restart the line after resolving the reported problem.")
    elif state == "OK":
        current = "IMS is registered and the line is being monitored"
        next_action = "Automatic recovery will run if the connection is lost."
    elif state == "STOPPED":
        current = "The VoWiFi line is stopped"
        next_action = "Enable VoWiFi to start the line."
    elif state == "NO_CARD":
        current = "Waiting for the SIM card"
        next_action = "Insert the SIM card to continue automatically."
    elif state == "PIN_PROBLEM":
        current = "Waiting for SIM PIN attention"
        next_action = "Verify the SIM PIN before automatic setup can continue."
    elif state == "EPDG_UNRESOLVED":
        current = "Resolving the carrier ePDG gateway"
        next_action = "The backend will retry automatically."
    elif state == "TUNNEL_DOWN":
        current = "Establishing the secure ePDG tunnel"
        next_action = "If the tunnel remains unavailable, the line will be rebuilt automatically."
    elif state == "REGISTERING" and detail.get("pcscf"):
        current = "Contacting the carrier IMS through P-CSCF"
        next_action = "If IMS remains unavailable, the ePDG session will be rebuilt automatically."
    elif state == "REGISTERING":
        current = "Waiting for carrier P-CSCF discovery"
        next_action = "IMS registration starts automatically after discovery."
    else:
        current = st.get("label") or "Checking the VoWiFi line"
        next_action = "The backend will keep monitoring the line."

    st["activity"] = {
        "current": current,
        "next": next_action,
        "automatic": (state not in {"STOPPED", "NO_CARD", "PIN_PROBLEM"}
                      and not (st.get("frozen") and not remaining)),
        "retry_count": int(retry.get("count") or 0),
        "retry_max": int(retry.get("max") or 0),
        "seconds": int(remaining or 0),
    }
    return st


def _frozen(h, st, rmax):
    remaining = max(0, int((h.get("next_retry_at") or 0) - time.monotonic()))
    return {"state": "ERROR", "label": status_mod.LABELS["ERROR"],
            "reason_code": h["frozen_code"], "reason": h["frozen_reason"],
            "detail": st.get("detail", {}), "retry": {"count": rmax, "max": rmax},
            "frozen": True, "automatic_retry_in": remaining or None}


async def _auto_recover_instance(iid: str, inst: dict, delay: int):
    h = hub.health_for(iid)
    try:
        allowed, blocked_reason = _line_auto_start_allowed(inst)
        if not allowed:
            hub.reset_health(iid)
            no_card = blocked_reason == "no_card"
            stopped = _with_status_activity(iid, {
                "state": "NO_CARD" if no_card else "STOPPED",
                "label": "No SIM card" if no_card else status_mod.LABELS["STOPPED"],
                "reason_code": blocked_reason,
                "reason": ("SIM card is not available." if no_card
                           else "The line or its device VoWiFi switch is disabled."),
                "detail": {}, "retry": {"count": 0, "max": 0}})
            hub.status_cache[str(iid)] = stopped
            hub.status_sampled_at[str(iid)] = time.monotonic()
            await hub.broadcast({"type": "status", "instance": str(iid), **stopped})
            return
        recovering = _with_status_activity(iid, {
            "state": "REGISTERING", "label": status_mod.LABELS["REGISTERING"],
            "reason_code": h.get("frozen_code") or "registering",
            "reason": h.get("frozen_reason") or "Automatic recovery is rebuilding the line.",
            "detail": {}, "retry": {"count": 0, "max": 0}})
        hub.status_cache[str(iid)] = recovering
        hub.status_sampled_at[str(iid)] = time.monotonic()
        await hub.broadcast({"type": "status", "instance": str(iid), **recovering})
        await asyncio.to_thread(
            _start_engine_checked, inst, cfg.get_settings(),
            os.environ.get("MDD_DEV_MOUNTS", "") == "1",
            # Records why the health policy gave up on the previous container, so the captured
            # snapshot explains itself without cross-referencing the journal.
            f"auto-recover:{h.get('frozen_code') or 'unhealthy'}")
        hub.reset_health(iid)
        starting = _with_status_activity(iid, {
            "state": "REGISTERING", "label": status_mod.LABELS["REGISTERING"],
            "reason_code": "registering", "reason": "The line was rebuilt successfully.",
            "detail": {}, "retry": {"count": 0, "max": 0}})
        hub.status_cache[str(iid)] = starting
        hub.status_sampled_at[str(iid)] = time.monotonic()
        await hub.broadcast({"type": "status", "instance": str(iid), **starting})
    except Exception as exc:
        h["auto_retrying"] = False
        h["next_retry_at"] = time.monotonic() + delay
        h["frozen_reason"] = str(getattr(exc, "detail", exc))


def apply_health(iid, inst, st, container_id: str | None = None):
    """Overlay bounded auto-retry state. After max attempts of continuous failure (with the
    SIM still present) the engine is stopped and the status frozen to ERROR + reason, until
    the user retries/re-provisions or the card is re-inserted."""
    rcfg = inst.get("retry") or cfg.get_settings().get("retry", {})
    rmax = max(1, int(rcfg.get("max", 3)))
    rint = max(5, int(rcfg.get("interval", 40)))
    h = hub.health_for(iid)
    state = st["state"]
    now = time.monotonic()

    if not inst.get("enabled", True):
        hub.reset_health(iid)
        return {"state": "STOPPED", "label": status_mod.LABELS["STOPPED"],
                "reason_code": "stopped", "reason": "Stopped.", "detail": {},
                "retry": {"count": 0, "max": rmax}}

    if state == "OK":
        # Set once per healthy stretch: this is when the current exit last proved it can
        # carry IMS, and reset_health() below would otherwise lose it.
        hub.ok_since.setdefault(str(iid), time.monotonic())
        # A registration proves the current exit works, so nothing the ledger holds against it
        # still stands. Clearing here is also what lets a reported line report again later.
        if hub.exit_ledgers.pop(str(iid), None) is not None:
            _save_exit_ledgers()
        hub.reset_health(iid)
        st["retry"] = {"count": 0, "max": rmax}
        return st
    if h.get("frozen_code"):
        # PIN/PUK failures require a person. Network, tunnel and IMS failures recover after a
        # cooldown so a brief carrier rejection never leaves WiFi Calling stopped forever.
        if (h.get("frozen_code") not in {"pin_wrong", "pin_blocked", "pin_required"}
                and time.monotonic() >= (h.get("next_retry_at") or float("inf"))
                and not h.get("auto_retrying")):
            h["auto_retrying"] = True
            asyncio.create_task(_auto_recover_instance(iid, inst, max(60, rint * 4)))
        return _frozen(h, st, rmax)
    if state == "STOPPED":
        st["retry"] = {"count": 0, "max": rmax}
        return st
    if state == "NO_CARD":
        # SIM removed/absent -> handled by the card monitor; don't count as a retry.
        h["fail_start"] = None
        h["retry_count"] = 0
        st["retry"] = {"count": 0, "max": rmax}
        return st
    if state == "PIN_PROBLEM":
        # wrong/blocked PIN won't recover by retrying — surface immediately.
        h["frozen_code"] = st["reason_code"]
        h["frozen_reason"] = st["reason"]
        h["next_retry_at"] = None
        return _frozen(h, st, rmax)

    # Asterisk has already spent a complete SIP transaction proving that this established
    # P-CSCF session no longer answers.  If AMI also proves no call is active, skip the generic
    # retry budget and rebuild the exact container generation.  Missing logs, missing AMI, an
    # active call, a missing generation, or the rate limiter all fall through unchanged.
    fast_unanswered = False
    if (st.get("reason_code") == "reg_unanswered"
            and (st.get("detail") or {}).get("active_channels") == 0
            and container_id):
        last_fast = hub.reg_unanswered_recovery_at.get(str(iid), float("-inf"))
        if now - last_fast >= REG_UNANSWERED_MIN_INTERVAL_SECONDS:
            fast_unanswered = True
            hub.reg_unanswered_recovery_at[str(iid)] = now
            # Reuse the established freeze/capture/failover path below.  Backdating fail_start
            # makes this observation exhaust only this reason's retry budget immediately.
            h["fail_start"] = now - (rmax * rint)
            log.warning("line %s IMS registration is unanswered with no active channels; "
                        "fast recovery will rebuild container generation %s", iid,
                        str(container_id)[:12])

    # EPDG_UNRESOLVED / TUNNEL_DOWN / REGISTERING -> the engine keeps retrying internally;
    # we bound the total time and then give up.
    if h["fail_start"] is None:
        h["fail_start"] = now
    elapsed = now - h["fail_start"]
    count = min(rmax, int(elapsed // rint) + 1)
    h["retry_count"] = count
    if elapsed >= rmax * rint:
        h["frozen_code"] = st["reason_code"]
        h["frozen_reason"] = st["reason"]
        # A connected tunnel with an unresponsive IMS/P-CSCF is commonly one bad carrier
        # session or endpoint. Re-establish it after one normal retry interval so discovery can
        # return a healthy P-CSCF; keep the long cooldown for auth/network/provisioning failures
        # to avoid hammering the carrier.
        if fast_unanswered:
            cooldown = max(1.0, REG_UNANSWERED_RECOVERY_DELAY_SECONDS)
        else:
            cooldown = (max(20, rint) if st["reason_code"] == "registering"
                        else max(60, rint * 4))
        h["next_retry_at"] = now + cooldown
        # Capture before removal, off the event loop: reading the container's logs and asking
        # Asterisk for its registration state both block, and the cooldown leaves ample time
        # before the rebuild. Any failure here still leaves the container for stop() to remove.
        asyncio.create_task(asyncio.to_thread(
            engine.capture_and_stop, iid, inst, f"health-freeze:{st['reason_code']}",
            container_id))
        # Giving up on a line is a signal that its exit node may be the problem — but only
        # when the line never worked on it. A node that carried a registered line for a long
        # time and then broke is being blamed for something else (a carrier-side problem, or
        # a rekey that a marginal path failed to survive), and moving the exit costs another
        # tunnel teardown while changing nothing. Blaming it also evicts a node the operator
        # deliberately pinned.
        stable_for = max(0.0, time.monotonic() - hub.ok_since.pop(str(iid), time.monotonic()))
        try:
            action = _judge_exit_failure(str(iid), inst, st, stable_for)
        except Exception as exc:  # noqa
            log.warning("exit failover judgement failed for line %s: %s", iid, exc)
            action = failover.HOLD
        if (action == failover.GIVE_UP
                or bool((hub.exit_ledgers.get(str(iid)) or {}).get("given_up"))):
            # Stop the automatic rebuild the same way a PIN problem does: the operator pinned
            # this exit and it has had its chances, so rebuilding again is pure churn. A
            # person (or a successful start) clears this.
            h["next_retry_at"] = None
        elif action == failover.BACK_OFF:
            # Every exit failed the same way, which points upstream of the nodes — a host-side
            # outage, a dead subscription. Those pass, so instead of stopping (or churning
            # every few minutes) the line re-tests its exit on a slow cadence and registers
            # by itself when the outside world comes back.
            h["next_retry_at"] = now + failover.EXHAUSTED_RETRY_SECONDS
        asyncio.create_task(hub.drop_ami(str(iid)))
        return _frozen(h, st, rmax)
    st["retry"] = {"count": count, "max": rmax}
    return st


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init()
    # An upgrade from an older/self-use build may inherit more than five running containers.
    # Keep every saved record, but stop excess engines before background recovery begins.
    for saved_line in cfg.list_instances():
        iid = str(saved_line.get("id") or "")
        if iid and not cfg.line_allowed(iid):
            try:
                await asyncio.to_thread(engine.stop, iid)
                log.warning("stopped line %s because it exceeds the SIM line limit", iid)
            except Exception as exc:  # noqa: BLE001 - startup must continue to surface status
                log.error("could not stop over-limit line %s: %s", iid, exc)
    # Legacy history used a free-form line name. Map only unique, non-numeric current names;
    # numeric ids are reusable and therefore unsafe to guess across deleted/recreated lines.
    aliases: dict[str, list[str]] = {}
    for item in cfg.list_instances():
        alias = re.sub(r"[^a-z0-9]+", "", str(item.get("name") or "").lower())
        if alias and not alias.isdigit():
            aliases.setdefault(alias, []).append(str(item["id"]))
    unique_aliases = {alias: ids[0] for alias, ids in aliases.items() if len(ids) == 1}
    migrated = store.migrate_legacy_history(unique_aliases)
    if migrated["calls"] or migrated["messages"]:
        log.info("merged legacy history: %d call(s), %d message(s)",
                 migrated["calls"], migrated["messages"])
    # Re-publish after every manager restart so the host orchestrator can reconstruct routes and
    # modem services from persistent config without waiting for a settings edit/line restart.
    egress.publish()
    await hub.runtime.start(hub.runtime_changed)
    poller = asyncio.create_task(status_poller())
    monitor = asyncio.create_task(card_monitor())
    sms_poller = asyncio.create_task(cellular_sms_poller())
    host_poller = asyncio.create_task(host_health_poller())
    allowance_poller = asyncio.create_task(allowance_reminder_poller())
    yield
    poller.cancel()
    monitor.cancel()
    sms_poller.cancel()
    host_poller.cancel()
    allowance_poller.cancel()
    # Reap the cancelled tasks (the monitor may be parked in a to_thread wait for up to
    # its timeout; awaiting keeps shutdown deterministic instead of leaking the error).
    await asyncio.gather(poller, monitor, sms_poller, host_poller, allowance_poller,
                         return_exceptions=True)
    await hub.runtime.close()
    for c in hub.ami.values():
        await c.close()
    await asyncio.to_thread(engine.close_client)


app = FastAPI(title="MDD Sim Gateway", lifespan=lifespan)

_AUTH_PUBLIC = {"/api/auth/status", "/api/auth/setup", "/api/auth/login"}


@app.middleware("http")
async def require_admin_session(request: Request, call_next):
    """Protect every management API and require CSRF on state changes.

    The engine callback is authenticated separately with the per-install internal token.
    Static assets remain public so the browser can render the login screen.
    """
    path = request.url.path
    if not path.startswith("/api/") or path in _AUTH_PUBLIC:
        return await call_next(request)
    if path == "/api/engine/event":
        expected = cfg.internal_event_token()
        supplied = request.headers.get("x-mdd-engine-token", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "invalid engine token"}, status_code=401)
        return await call_next(request)
    current = auth.session(request.cookies.get(auth.SESSION_COOKIE))
    if not current:
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        supplied = request.headers.get("x-mdd-csrf-token", "")
        if not hmac.compare_digest(supplied, current["csrf"]):
            return JSONResponse({"detail": "invalid CSRF token"}, status_code=403)
    request.state.admin_session = current
    return await call_next(request)


@app.get("/api/auth/status")
def api_auth_status(request: Request):
    current = auth.session(request.cookies.get(auth.SESSION_COOKIE))
    return {"configured": auth.configured(), "authenticated": bool(current),
            "username": auth.username(),
            "csrf": current.get("csrf") if current else ""}


@app.post("/api/auth/setup")
def api_auth_setup(body: dict, request: Request):
    if auth.configured():
        raise HTTPException(409, "administrator account is already configured")
    try:
        auth.setup(str(body.get("password") or ""), str(body.get("username") or "admin"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    result = auth.login(str(body.get("username") or "admin"), str(body.get("password") or ""),
                        request.client.host if request.client else "")
    token, csrf = result
    response = JSONResponse({"ok": True, "authenticated": True, "csrf": csrf})
    response.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_TTL, httponly=True,
                        secure=True, samesite="strict", path="/")
    return response


@app.post("/api/auth/login")
def api_auth_login(body: dict, request: Request):
    if not auth.configured():
        raise HTTPException(409, "administrator setup is required")
    peer = request.client.host if request.client else ""
    retry = auth.throttled(peer)
    if retry:
        return JSONResponse({"detail": "too many attempts", "retry_after": retry},
                            status_code=429, headers={"Retry-After": str(retry)})
    result = auth.login(str(body.get("username") or "admin"), str(body.get("password") or ""), peer)
    if not result:
        raise HTTPException(401, "invalid username or password")
    token, csrf = result
    response = JSONResponse({"ok": True, "authenticated": True, "csrf": csrf})
    response.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_TTL, httponly=True,
                        secure=True, samesite="strict", path="/")
    return response


@app.post("/api/auth/logout")
def api_auth_logout(request: Request):
    auth.logout(request.cookies.get(auth.SESSION_COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


@app.post("/api/auth/password")
def api_auth_password(body: dict, request: Request):
    try:
        auth.change_password(str(body.get("current_password") or ""),
                             str(body.get("new_password") or ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    response = JSONResponse({"ok": True, "reauthenticate": True})
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


def _audit_client(request: Request, settings: dict) -> str:
    peer = request.client.host if request.client else ""
    trusted = (settings.get("security") or {}).get("trusted_proxies") or []
    try:
        address = ipaddress.ip_address(peer)
        allowed = any(address in ipaddress.ip_network(str(item), strict=False) for item in trusted)
    except ValueError:
        allowed = False
    if allowed:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer


@app.middleware("http")
async def audit_mutations(request: Request, call_next):
    response = await call_next(request)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api/"):
        settings = cfg.get_settings()
        _write_audit_record({"at": int(time.time()), "method": request.method,
                             "path": request.url.path, "status": response.status_code,
                             "client": _audit_client(request, settings)}, settings)
    return response


def _write_audit_record(record: dict, settings: dict | None = None) -> None:
    """Append one administrative action from the authenticated control surface."""
    settings = settings if settings is not None else cfg.get_settings()
    if not (settings.get("security") or {}).get("audit_enabled", True):
        return
    path = os.path.join(cfg.DATA_DIR, "audit", "operations.jsonl")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        log.warning("could not write administrative audit record")


# ----------------------------- SIM / readers -----------------------------
@app.get("/api/readers")
def api_readers():
    try:
        return {"readers": sim.list_readers(), "stale": False}
    except Exception as exc:  # noqa
        # The card monitor already owns a last-known, index-sorted view. Returning it is
        # safer than telling the UI there are zero readers after one transient pcsc-lite
        # context failure. The stale flag asks the browser to retry in the background.
        cached = [str(item.get("name") or "") for item in hub.cards_list()
                  if item.get("name")]
        if cached:
            log.warning("PC/SC reader enumeration temporarily unavailable; using %d cached readers: %r",
                        len(cached), exc)
            return {"readers": cached, "stale": True}
        log.warning("PC/SC reader enumeration unavailable and no cache exists: %r", exc)
        raise HTTPException(503, "card readers are temporarily unavailable") from exc


@app.get("/api/sim/detect")
async def api_sim_detect(reader_index: int = 0):
    rlist = await asyncio.to_thread(sim.list_readers)
    if reader_index < 0 or reader_index >= len(rlist):
        raise HTTPException(400, "reader index out of range")
    name = rlist[reader_index]
    async with hub.reader_lock(name):
        return await asyncio.to_thread(
            lambda: _client_card_info(sim.read_card(reader_index).dict()))


def _resolve_reader_index(body: dict) -> int:
    """Resolve the target reader for index-taking SIM APIs. When the caller supplies the
    physical reader NAME we re-resolve the index at request time. A configured line's
    ``reader`` value may instead be a logical selector (``imsi:...``), which must never be
    mistaken for a PC/SC name. Prefer stable port/card identity over a cached enumeration
    index so hotplug cannot redirect a PIN or provisioning request to another SIM."""
    rlist = sim.list_readers()
    if not rlist:
        raise HTTPException(409, "no PC/SC readers connected")
    try:
        cached_idx = int(body.get("reader_index", 0))
    except (TypeError, ValueError):
        cached_idx = 0
    rname = str(body.get("reader") or "").strip()
    logical_selector = rname.startswith(("imsi:", "iccid:"))

    if rname and not logical_selector:
        if rname not in rlist:
            raise HTTPException(409, "the selected card reader is no longer connected")
        return rlist.index(rname)

    port = str(body.get("reader_port") or "").strip()
    if port:
        try:
            port_idx = usbreader.index_for_port(port)
        except Exception as exc:  # noqa
            log.debug("request port->index resolve failed for %s: %r", port, exc)
            port_idx = None
        if port_idx is not None and 0 <= int(port_idx) < len(rlist):
            return int(port_idx)

    iccid = str(body.get("iccid") or "").strip()
    imsi = str(body.get("imsi") or "").strip()
    if rname.startswith("iccid:"):
        iccid = rname.split(":", 1)[1].strip()
    elif rname.startswith("imsi:"):
        imsi = rname.split(":", 1)[1].strip()
    if iccid or imsi:
        for card_info in hub.cards.values():
            if not card_info.get("present"):
                continue
            if ((iccid and str(card_info.get("iccid") or "") == iccid)
                    or (imsi and str(card_info.get("imsi") or "") == imsi)):
                idx = card_info.get("index")
                if idx is not None and 0 <= int(idx) < len(rlist):
                    return int(idx)
        raise HTTPException(409, "the SIM for this line is no longer connected")

    if cached_idx < 0 or cached_idx >= len(rlist):
        raise HTTPException(409, "the selected card reader is no longer connected")
    return cached_idx


@app.post("/api/sim/verify-pin")
async def api_verify_pin(body: dict):
    idx = await asyncio.to_thread(_resolve_reader_index, body)
    rlist = await asyncio.to_thread(sim.list_readers)
    name = rlist[idx] if 0 <= idx < len(rlist) else ""
    async with hub.reader_lock(name or f"idx:{idx}"):
        res = await asyncio.to_thread(sim.verify_pin, body["pin"], idx)
        if res.get("ok"):
            # PIN now satisfied — re-read the (previously locked) IMSI + SMSC and refresh the
            # detected-card entry so the dashboard can move from "locked" to "ready to provision".
            try:
                c = await asyncio.to_thread(sim.read_card, idx, body["pin"])
                # Key strictly by the reader NAME the read actually used — an index-keyed
                # lookup could merge this card's identity into a stale entry of a reader
                # that was just unplugged.
                card_entry = hub.cards.get(c.reader) or {"index": idx, "name": c.reader,
                                                         "present": True}
                card_entry.update(present=True, iccid=c.iccid, imsi=c.imsi, mcc=c.mcc,
                                  mnc=c.mnc, mnc_len=getattr(c, "mnc_len", None),
                                  pin_enabled=c.pin_enabled, pin_tries=c.pin_tries,
                                  smsc=c.smsc, carrier_identity=_carrier_identity(c))
                inst = _match_instance_by_iccid(c.iccid)
                if inst and _carrier_identity_update(c):
                    await asyncio.to_thread(cfg.upsert_instance, {
                        "id": str(inst["id"]), **_carrier_identity_update(c)})
                card_entry["matched"] = inst["id"] if inst else None
                hub.cards[c.reader] = card_entry
                res["card"] = _client_card_info(card_entry)
                await hub.broadcast({"type": "cards", "cards": _client_cards()})
            except Exception as e:  # noqa
                log.debug("post-verify re-read failed: %r", e)
    return res


@app.post("/api/sim/change-pin")
async def api_change_pin(body: dict):
    idx = await asyncio.to_thread(_resolve_reader_index, body)
    rlist = await asyncio.to_thread(sim.list_readers)
    name = rlist[idx] if 0 <= idx < len(rlist) else f"idx:{idx}"
    async with hub.reader_lock(name):
        return await asyncio.to_thread(sim.change_pin, body["old"], body["new"], idx)


@app.post("/api/sim/pin-enabled")
async def api_pin_enabled(body: dict):
    idx = await asyncio.to_thread(_resolve_reader_index, body)
    rlist = await asyncio.to_thread(sim.list_readers)
    name = rlist[idx] if 0 <= idx < len(rlist) else f"idx:{idx}"
    async with hub.reader_lock(name):
        return await asyncio.to_thread(
            sim.set_pin_enabled, body["pin"], bool(body["enabled"]), idx)


def _refresh_card_matches():
    """Recompute each detected card's matched instance against current config. Only for
    entries whose ICCID is known — entries mapped via a running engine's pin_status
    (identity not probed) must keep that match instead of being wiped to None."""
    for c in hub.cards.values():
        if c.get("present") and c.get("iccid"):
            inst = _match_instance_by_iccid(c.get("iccid"))
            c["matched"] = inst["id"] if inst else None



def _esim_resolve_reader(reader_index: int | None = None, reader: str | None = None) -> tuple[str, int]:
    """Resolve (reader_name, index) for eSIM APIs. Prefer NAME when provided."""
    rlist = sim.list_readers()
    if not rlist:
        raise HTTPException(409, "no PC/SC readers connected")
    if reader:
        if reader not in rlist:
            raise HTTPException(409, f"reader '{reader}' is no longer connected")
        return reader, rlist.index(reader)
    idx = 0 if reader_index is None else int(reader_index)
    if idx < 0 or idx >= len(rlist):
        raise HTTPException(400, "reader index out of range")
    return rlist[idx], idx


def _esim_imei_for_reader(name: str, override: str | None = None) -> str:
    if override and str(override).strip():
        return str(override).strip()
    entry = hub.cards.get(name) or {}
    matched = entry.get("matched")
    if matched:
        inst = cfg.get_instance(matched)
        if inst and inst.get("imei"):
            return str(inst["imei"])
    # A profile switch changes the ICCID before a matching line exists. Native readers keep
    # their configured device identity in devices-hardware.json, so downloads and automatic
    # provisioning must still be able to resolve that physical reader's IMEI in this gap.
    hardware_imei, _device_id, _device_type = _hardware_imei_for_card(entry)
    if hardware_imei:
        return hardware_imei
    return ""


def _esim_resolve_se(
    name: str,
    idx: int,
    se_id: str | None = None,
    aid: str | None = None,
    *,
    require: bool = False,
) -> dict:
    """Resolve which ISD-R / SE to target. Dual-SE cards need se_id or aid when require=True."""
    ses = estkme.discover_ses(name, idx)
    try:
        if require and len(ses) > 1 and not (se_id or aid):
            raise KeyError("eUICC SE is required for dual-SE cards")
        return estkme.resolve_se(ses, se_id=se_id, aid=aid)
    except KeyError as e:
        raise HTTPException(400, str(e)) from e


def _esim_guard_engine(name: str):
    """Refuse LPA while a VoWiFi engine holds the card (lpac needs exclusive PC/SC)."""
    inst = _find_running_by_reader(name)
    if inst is not None:
        raise HTTPException(
            409,
            f"Line {inst.get('id')} is running on this reader — stop it before eSIM operations",
        )


async def _esim_refresh_card(name: str, idx: int):
    """Re-probe USIM identity after profile enable/disable/download and broadcast."""
    info = hub.cards.get(name) or {"index": idx, "name": name, "present": True}
    try:
        c = await asyncio.to_thread(sim.read_card, idx)
        info.update(
            present=True, index=idx, name=name,
            iccid=c.iccid, imsi=c.imsi, mcc=c.mcc, mnc=c.mnc,
            mnc_len=getattr(c, "mnc_len", None),
            pin_enabled=c.pin_enabled, pin_tries=c.pin_tries, smsc=c.smsc,
            carrier_identity=_carrier_identity(c),
        )
        inst = _match_instance_by_iccid(c.iccid)
        if inst and _carrier_identity_update(c):
            inst = await asyncio.to_thread(cfg.upsert_instance, {
                "id": str(inst["id"]), **_carrier_identity_update(c)})
        if not inst and c.iccid and not cfg.card_auto_create_suppressed(c.iccid):
            # REFRESH arrives while lpa_busy is set, so the normal card-insert callback
            # deliberately keeps the previous ICCID and cannot create/start the newly active
            # profile. Do it from the authoritative post-LPA probe instead.
            inst = await asyncio.to_thread(_ensure_card_draft, info)
        info["matched"] = inst["id"] if inst else None
    except Exception as e:  # noqa
        log.debug("post-LPA card refresh failed: %r", e)
        info.update(index=idx, name=name, present=True)
    hub.cards[name] = info
    await hub.broadcast({"type": "cards", "cards": _client_cards()})
    if info.get("matched"):
        asyncio.create_task(_auto_start_hotplugged_line(str(info["matched"])))
    return info


async def _esim_run(name: str, idx: int, coro, *, refresh: bool = False):
    """Serialize an LPA call: engine gate + per-reader lock + lpa_busy + optional refresh."""
    await asyncio.to_thread(_esim_guard_engine, name)
    async with hub.reader_lock(name):
        hub.lpa_busy[name] = True
        try:
            result = await coro
            if refresh:
                await _esim_refresh_card(name, idx)
            return result
        except lpa.LpaError as e:
            raise HTTPException(400, e.user_message()) from e
        except FileNotFoundError as e:
            raise HTTPException(503, str(e)) from e
        finally:
            hub.lpa_busy.pop(name, None)


@app.get("/api/cards")
async def api_cards():
    """Physically detected readers/cards (from the real-time monitor)."""
    if not hub.scanned:
        # The monitor hasn't finished its first scan yet (manager just started) — answer
        # from a live reader scan so the UI never sees a false "no readers" flash. Map
        # present cards to running engines by pin_status reader name (no card access).
        def scan():
            out = []
            for st in card.reader_states() or []:
                inst = _find_running_by_reader(st["name"]) if st["present"] else None
                out.append({**st,
                            "iccid": inst.get("iccid") if inst else None,
                            "imsi": inst.get("imsi") if inst else None,
                            "matched": inst["id"] if inst else None,
                            "pin_enabled": None, "pin_tries": None})
            return out
        cards = await asyncio.to_thread(scan)
        return {"cards": _with_detected_imei(cards)}
    _refresh_card_matches()
    return {"cards": _client_cards()}


@app.get("/api/ports/suggest")
def api_ports_suggest():
    """Preview the SIP port the automatic allocator would pick for a NEW line right now
    (conflict-checked against other lines + live host listeners). Lets the manual-port UI
    show a sensible default and the auto option show what it will use."""
    try:
        block = cfg.alloc_ports_auto(cfg.load())
        return {"auto_sip_udp": block["sip_udp"], "auto_sip_tls": block["sip_tls"],
                "min": cfg.MIN_USER_PORT, "max": cfg.MAX_USER_PORT}
    except Exception as e:  # noqa
        raise HTTPException(409, f"no free port block: {e}")


def _reader_index_for_instance(inst: dict) -> int | None:
    """Resolve the PC/SC reader index this instance should address, preferring the STABLE
    physical USB port binding over the (unstable) enumeration index/ICCID.

    Priority:
      1. inst.reader_port -> live index via the USB port map. This is authoritative: it sticks
         to the physical reader socket even when pcscd flips the indices of two identical
         readers. It does not require the card to be readable/matched by the monitor.
      2. ICCID match against the live card monitor (works once the card's identity is known).
    Returns None if neither resolves (card/reader not present)."""
    # A VPCD multi-slot bridge intentionally exposes the same physical SIM on
    # several logical readers. ICCID matching is therefore ambiguous; preserve
    # the dedicated SWu slot selected by the instance instead of collapsing to
    # the first matching slot (which is reserved for pin_keeper).
    swu_name = inst.get("swu_reader")
    if swu_name:
        for c in hub.cards.values():
            if c.get("name") == swu_name:
                return c.get("index")
    if "pin_reader" in inst and "ami_reader" in inst:
        try:
            return int(inst.get("reader_index", 1))
        except (TypeError, ValueError):
            return 1
    port = inst.get("reader_port")
    if port:
        try:
            idx = usbreader.index_for_port(port)
        except Exception as e:  # noqa
            log.debug("port->index resolve failed for %s: %r", port, e)
            idx = None
        if idx is not None:
            return idx
    iccid = inst.get("iccid")
    for c in hub.cards.values():
        if c.get("present") and iccid and c.get("iccid") == iccid:
            return c.get("index")
    return None


def _reader_port_for_instance(inst: dict) -> str | None:
    """The stable USB port path this instance's SIM currently sits at. Resolved from the live
    card monitor by ICCID (the port is captured per-reader on each scan). Used to (re)learn /
    refresh a line's reader_port binding at start time so it self-heals if the SIM was moved."""
    iccid = inst.get("iccid")
    if iccid:
        for c in hub.cards.values():
            if c.get("present") and c.get("iccid") == iccid and c.get("reader_port"):
                return c.get("reader_port")
    return None


def _card_identity_mismatch(inst: dict) -> dict | None:
    """Detect that the reader this line uses now holds a DIFFERENT SIM identity — the
    signature of an eSIM profile switch (enable/disable/download changes the eUICC's
    active profile, so the same physical reader re-enumerates with a new ICCID/IMSI).

    Starting the line anyway is what used to break things: the engine grabs whatever
    card is in the reader, runs EAP-AKA with the OLD line's IMSI against the NEW
    profile's keys, the carrier rejects it (tunnel_sim_auth), and the bounded retry
    loop stops the container. Refuse the start up-front with a structured error
    instead. Only a positive, known conflict blocks — absent readers/unknown ICCIDs
    keep the existing fail-open behavior (engine start surfaces NO_CARD as before)."""
    want = (inst.get("iccid") or "").strip()
    if not want:
        return None
    if _reader_index_for_instance(inst) is not None:
        return None      # this line's SIM/profile is present somewhere — all good
    # Prefer the stable USB port binding when present; fall back to stored index.
    port = (inst.get("reader_port") or "").strip()
    idx = inst.get("reader_index")
    for c in hub.cards.values():
        if not c.get("present"):
            continue
        if port and c.get("reader_port") == port:
            pass
        elif not port and c.get("index") == idx:
            pass
        else:
            continue
        got = (c.get("iccid") or "").strip()
        if got and got != want:
            return {"reader": c.get("name") or (f"USB {port}" if port else f"reader {idx}"),
                    "iccid": got}
    return None


def _raise_card_mismatch(inst: dict, mism: dict):
    raise HTTPException(409, {
        "code": "card_mismatch",
        "reader": mism["reader"],
        "card_iccid": mism["iccid"],
        "line_iccid": inst.get("iccid") or "",
        "message": (f"The card in {mism['reader']} now has a different identity "
                    f"(ICCID {mism['iccid']}; this line expects {inst.get('iccid')}). "
                    "This usually means the eSIM profile was switched. Provision the "
                    "active profile as its own line, or switch the eSIM back to this "
                    "profile, then start again."),
    })


def _preflight_pin_locked(inst: dict, idx: int) -> dict:
    """PIN preflight body — caller must already hold the reader asyncio.Lock.
    Sync so it can run under asyncio.to_thread (PC/SC is blocking)."""
    try:
        probe = sim.read_card(idx)          # no VERIFY: learns pin_enabled + presence
    except Exception as e:  # noqa
        log.debug("preflight probe failed: %r", e)
        return {"ok": True, "need_pin": bool(inst.get("pin"))}
    if not probe.present:
        return {"ok": False, "code": "no_card"}
    if probe.pin_enabled is False:
        return {"ok": True, "need_pin": False}
    saved = inst.get("pin")
    if not saved:
        return {"ok": False, "code": "pin_required", "tries": probe.pin_tries}
    try:
        chk = sim.read_card(idx, saved)
    except Exception as e:  # noqa
        log.debug("preflight verify failed: %r", e)
        return {"ok": True, "need_pin": True}     # couldn't verify now; let the engine try
    if chk.error and "PIN" in (chk.error or "").upper():
        return {"ok": False, "code": "pin_invalid", "clear": True, "tries": chk.pin_tries}
    return {"ok": True, "need_pin": True}


async def _preflight_pin(inst: dict) -> dict:
    """Actively check the SIM's PIN state BEFORE starting the engine (so we never spin up
    the SWu tunnel/IMS against a locked card). Reads the physical card:
      - card absent                         -> {ok:False, code:'no_card'}
      - PIN not required (disabled)          -> {ok:True,  need_pin:False}
      - PIN required, no saved PIN           -> {ok:False, code:'pin_required'}
      - PIN required, saved PIN verifies     -> {ok:True,  need_pin:True}
      - PIN required, saved PIN wrong/blocked -> {ok:False, code:'pin_invalid', clear:True}
    On 'pin_invalid' the saved PIN is stale and should be cleared so the user re-enters it.
    If the card can't be located/read we fail OPEN (ok:True) rather than block a start that
    might otherwise work (e.g. an engine already holds the card)."""
    idx = _reader_index_for_instance(inst)
    if idx is None:
        # Card not seen by the monitor — could be held by a running engine, or truly gone.
        # Don't block here; engine start + status FSM will surface NO_CARD if it's absent.
        return {"ok": True, "need_pin": bool(inst.get("pin"))}
    # Skip while LPA owns the reader (exclusive PC/SC) — let the engine try later.
    rlist = await asyncio.to_thread(sim.list_readers)
    rname = rlist[idx] if 0 <= idx < len(rlist) else None
    if rname and hub.lpa_busy.get(rname):
        return {"ok": True, "need_pin": bool(inst.get("pin"))}
    lock = hub.reader_lock(rname or f"idx:{idx}")
    # asyncio.Lock has no blocking=False; try a short acquire, fail-open if busy.
    try:
        await asyncio.wait_for(lock.acquire(), timeout=0.05)
    except asyncio.TimeoutError:
        return {"ok": True, "need_pin": bool(inst.get("pin"))}
    try:
        return await asyncio.to_thread(_preflight_pin_locked, inst, idx)
    finally:
        lock.release()


@app.post("/api/provision")
async def api_provision(body: dict):
    """Provision a detected card: verify PIN, read identity, create the line and start it.
    PIN is required only when CHV1 is enabled. IMEI is auto-read from bridge metadata when
    available, otherwise the caller must supply it. Optional: imeisv (auto-derived from imei if blank), name, smsc,
    reader_index, reader (name), sip, webrtc, id, port_mode ('auto'|'manual'), sip_port
    (int, when manual), apn (default 'ims'), idr_mode ('apn'|'fqdn', default 'apn')."""
    idx = await asyncio.to_thread(_resolve_reader_index, body)
    pin = body.get("pin", "")
    rlist = await asyncio.to_thread(sim.list_readers)
    rname = rlist[idx] if 0 <= idx < len(rlist) else body.get("reader") or f"idx:{idx}"
    async with hub.reader_lock(rname):
        c = await asyncio.to_thread(sim.read_card, idx, pin or None)
    if c.error and "PIN" in (c.error or "").upper():
        raise HTTPException(400, f"PIN error: {c.error} ({c.pin_tries} tries left)")
    if not c.imsi:
        raise HTTPException(400, "could not read IMSI (is the PIN correct?)")
    sip = cfg.merge_carrier_sip_defaults(
        c.mcc, c.mnc, c.iccid or c.imsi,
        body.get("sip") or {"listen_addr": "0.0.0.0", "transport": "udp",
                            "external": []})
    sip.setdefault("webrtc", {"enable": bool(body.get("webrtc", True))})
    # SMSC: manual override wins; otherwise read from the SIM (EF_SMSP, authoritative).
    # If the SIM can't provide it we ask the user to type it (no carrier presets).
    smsc = (body.get("smsc") or "").strip() or c.smsc
    if not smsc:
        raise HTTPException(422, "smsc_unreadable: could not read the SMS centre from the SIM — "
                                 "please provide it manually.")
    live_cards = hub.cards_list()
    live_card = next((item for item in live_cards
                      if (item.get("name") == c.reader or item.get("index") == idx
                          or (c.iccid and item.get("iccid") == c.iccid))), {})
    imei, _hardware_id, _hardware_type = _hardware_imei_for_card(live_card, live_cards)
    if len(imei) != 15:
        raise HTTPException(422, "imei_unavailable: configure a 15-digit IMEI in "
                                 "Device > Hardware before provisioning this SIM.")
    inst = {
        "id": str(body.get("id") or (len(cfg.list_instances()) + 1)),
        "name": body.get("name") or f"{c.mcc}-{c.mnc}",
        "provisioning_state": "ready",
        "imsi": c.imsi, "mcc": c.mcc, "mnc": c.mnc, "iccid": c.iccid,
        **_carrier_identity_update(c),
        # Blank means automatic MCC->ISO country mapping; a two-letter value is a per-line
        # override for MVNO/roaming/operator edge cases.
        "proxy_country": egress.normalize_country(body.get("proxy_country")),
        "imei": imei,
        "imei_source_device_id": _hardware_id,
        # IMEISV for DEVICE_IDENTITY: user value if provided, else auto-derive (14-digit IMEI
        # base + random 2-digit SVN) so each line looks like a distinct handset build.
        "imeisv": (body.get("imeisv") or "").strip()
                  or cfg.imeisv_from_imei(imei, svn=_random_svn()),
        "pin": pin,
        "reader": f"imsi:{c.imsi}",
        "reader_index": idx,  # store the physical reader index for USB device passthrough
        # Stable USB port path of the reader this SIM was provisioned in. This is the primary
        # binding used at start time (resolved back to a live index), so the line sticks to its
        # physical reader socket even if pcscd re-enumerates two identical readers in a different
        # order. Falls back to reader_index/ICCID when absent.
        "reader_port": c.reader_port or usbreader.port_for_index(idx) or "",
        "smsc": smsc,
        "msisdn": body.get("msisdn", ""),
        "msisdn_source": "manual" if str(body.get("msisdn") or "").strip() else "",
        "enabled": True, "sip": sip,
        # APN + ePDG identity (IDr) encoding for the SWu tunnel. apn defaults to 'ims'; idr_mode
        # defaults to 'fqdn' (real-UE APN-FQDN form). Normalised in config.render_instance_json.
        "apn": cfg.normalize_apn(body.get("apn", "")),
        "idr_mode": cfg.normalize_idr_mode(body.get("idr_mode", "")),
        # CFG request address family. Defaults to 'auto' (discovery ladder + carrier DB, seamless);
        # 'v6' Telus/EE, 'v4' Vodafone UK, 'dual'. Normalised in config.render_instance_json.
        "cp_mode": cfg.normalize_cp_mode(body.get("cp_mode", "")),
        # Full Asterisk debug contains SIP identities. The control plane only enables the
        # narrowly-scoped PJSIP logger temporarily when learning an IMS phone number.
        "debug": {**(body.get("debug") or {}), "asterisk": False},
    }
    # A modem is represented as one UI device but has three internal logical channels so PIN
    # keeping, SWu authentication and Asterisk/SMS can operate independently. Native readers
    # omit virtual_slots and keep the legacy single-reader behaviour.
    virtual = body.get("virtual_slots") or []
    if virtual:
        def slot(pos):
            return virtual[min(pos, len(virtual) - 1)]
        inst["pin_reader"] = slot(0).get("name") or str(slot(0).get("index", 0))
        inst["swu_reader"] = slot(1).get("name") or str(slot(1).get("index", idx))
        inst["ami_reader"] = slot(2).get("name") or str(slot(2).get("index", idx))
        inst["reader_index"] = int(slot(1).get("index", idx))
    # Port mapping: 'manual' pins the SIP UDP port the user chose (the rest of the block
    # derives from it, validated for range + host/instance conflicts). 'auto' (default)
    # allocates a conflict-free block now — and when re-provisioning an existing line it
    # RE-allocates (so switching an already-provisioned line back to Auto actually moves it
    # off a manual port), stepping past anything in use.
    iid = str(inst["id"])
    if body.get("port_mode") == "manual":
        try:
            inst["ports"] = cfg.ports_from_sip_base(cfg.load(), int(body.get("sip_port", 0)),
                                                    exclude_iid=iid)
        except (ValueError, TypeError) as e:
            raise HTTPException(422, f"port_error: {e}")
    else:
        try:
            inst["ports"] = cfg.alloc_ports_auto(cfg.load(), exclude_iid=iid)
        except ValueError as e:
            raise HTTPException(422, f"port_error: {e}")
    try:
        inst = cfg.upsert_instance(inst)
    except cfg.LineLimitError as exc:
        raise HTTPException(409, {
            "code": "line_limit", "message": str(exc)}) from exc
    hub._msisdn_tries.pop(str(inst["id"]), None)
    hub.reset_health(inst["id"])
    # engine.start force-removes any existing container; retire AMI first so a cached
    # client can't keep Login'ing the old (or IP-reused) engine with a stale secret.
    await hub.drop_ami(str(inst["id"]))
    await asyncio.to_thread(_start_engine_checked, inst, cfg.get_settings(),
                            dev_mounts=os.environ.get("MDD_DEV_MOUNTS", "") == "1")
    _refresh_card_matches()
    await hub.broadcast({"type": "cards", "cards": _client_cards()})
    safe = {k: v for k, v in inst.items() if k not in ("pin", "carrier_identity")}
    return {"ok": True, "instance": safe}


# ----------------------------- unified physical devices -----------------------------
def _read_json_file(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _device_sources() -> tuple[dict, dict, dict]:
    """Return desired, observed and hardware assignment state from the host orchestrator."""
    desired = device_state.desired()
    observed = device_state.status()
    hardware = _read_json_file(os.path.join(cfg.DATA_DIR, "orchestrator", "hardware-state.json"))
    return desired, observed, hardware.get("assignments") or {}


def _device_identities() -> dict[str, dict]:
    identities = {}
    for path in glob.glob(os.path.join(cfg.DATA_DIR, "modems", "*.json")):
        value = _read_json_file(path)
        device_id = str(value.get("hardware_id") or "")
        if device_id:
            identities[device_id] = value
    return identities


def _instance_for_device(device_id: str, identity: dict, cards: list[dict],
                         observed: dict | None = None) -> dict | None:
    """Match a line only from the SIM that is live in this device right now.

    Bridge identity files survive unplugging and intentionally retain hardware facts.
    Their last ICCID must never make an offline modem appear permanently attached to
    the last SIM it happened to contain.
    """
    # ModemManager owns the physical modem while 4G is active and remains the
    # authoritative source for the inserted SIM.  In that state the optional
    # PC/SC VoWiFi bridge can legitimately expose no card at all.
    live_iccid = str((((observed or {}).get("cellular") or {}).get("sim_iccid")) or "")
    if live_iccid:
        return _match_instance_by_iccid(live_iccid)
    card_info = next((item for item in cards
                      if item.get("hardware_id") == device_id and item.get("present")), None)
    if card_info and card_info.get("iccid"):
        return _match_instance_by_iccid(card_info["iccid"])
    return None


def _device_for_card(card_info: dict, cards: list[dict] | None = None) -> tuple[str, str]:
    """Return (device_id, device_type) for a live card-monitor entry."""
    cards = cards or hub.cards_list()
    hardware_id = str(card_info.get("hardware_id") or "")
    if card_info.get("hardware_kind") == "modem" and hardware_id:
        return hardware_id, "modem"
    name = str(card_info.get("name") or "")
    port = str(card_info.get("reader_port") or "")
    for device_id, candidate in device_state.native_reader_devices(cards).items():
        if ((name and candidate.get("name") == name)
                or (port and candidate.get("reader_port") == port)):
            return device_id, "reader"
    return "", ""


def _hardware_imei_for_card(card_info: dict, cards: list[dict] | None = None,
                            *, migrate_legacy: bool = True) -> tuple[str, str, str]:
    """Resolve device IMEI for the hardware currently holding a SIM."""
    cards = cards or hub.cards_list()
    device_id, device_type = _device_for_card(card_info, cards)
    if device_type == "modem":
        identity = _device_identities().get(device_id) or {}
        imei = cfg.normalize_imei(identity.get("imei", ""))
        return imei if len(imei) == 15 else "", device_id, device_type
    if device_type == "reader":
        record = device_state.hardware().get(device_id) or {}
        imei = cfg.normalize_imei(record.get("imei", ""))
        if len(imei) == 15:
            return imei, device_id, device_type
        # One-time migration: older releases stored a reader's device identity on
        # whichever SIM line was inserted. Move it to the physical reader record.
        inst = _match_instance_by_iccid(card_info.get("iccid"))
        legacy = cfg.normalize_imei((inst or {}).get("imei", ""))
        if migrate_legacy and len(legacy) == 15 and not (inst or {}).get("imei_source_device_id"):
            device_state.set_hardware(device_id, {
                "device_type": "reader", "name": card_info.get("name") or "Smart-card reader",
                "stable_path": card_info.get("reader_port") or "", "imei": legacy})
            cfg.upsert_instance({"id": str(inst["id"]), "imei_source_device_id": device_id})
            return legacy, device_id, device_type
    return "", device_id, device_type


def _apply_current_hardware_imei(inst: dict) -> dict:
    """Snapshot the current physical device IMEI into the engine line before start."""
    iccid = str(inst.get("iccid") or "")
    cards = hub.cards_list()
    card_info = next((item for item in cards
                      if item.get("present") and str(item.get("iccid") or "") == iccid), None)
    if not card_info:
        return inst
    imei, _device_id, _device_type = _hardware_imei_for_card(card_info, cards)
    if len(imei) != 15:
        raise HTTPException(409, {
            "code": "hardware_imei_required",
            "message": "configure a 15-digit IMEI in Device > Hardware before starting VoWiFi",
            "device_id": _device_id,
        })
    if imei == cfg.normalize_imei(inst.get("imei", "")):
        return inst
    previous_imeisv = str(inst.get("imeisv") or "")
    svn = previous_imeisv[-2:] if len(previous_imeisv) == 16 and previous_imeisv[-2:].isdigit() else _random_svn()
    return cfg.upsert_instance({"id": str(inst["id"]), "imei": imei,
                                "imei_source_device_id": _device_id,
                                "imeisv": cfg.imeisv_from_imei(imei, svn=svn)})


def _masked_identifier(value) -> str:
    text = str(value or "")
    return ("*" * max(0, len(text) - 4) + text[-4:]) if text else ""


def _vowifi_capability(desired: bool, observed: dict, running: bool,
                       line_status: dict | None) -> dict:
    transitioning = bool(observed.get("transitioning"))
    bridge = bool((observed.get("actual") or {}).get("vowifi_bridge_active"))
    error = str(observed.get("error") or "")
    if error:
        actual = "error"
    elif transitioning:
        actual = "starting" if desired else "stopping"
    elif not desired:
        # The bridge stays up for every present modem so the card remains readable, so it
        # says nothing about VoWiFi here — only a still-running line means "stopping".
        actual = "stopping" if running else "off"
    elif not bridge:
        actual = "starting"
    elif not running:
        actual, error = "degraded", "VoWiFi is enabled but no configured line is running"
    else:
        raw = str((line_status or {}).get("state") or (line_status or {}).get("label") or "").lower()
        if raw in {"ok", "working", "registered"}:
            actual = "on"
        elif raw in {"error", "failed", "stopped"}:
            actual = "error"
            error = str((line_status or {}).get("reason") or (line_status or {}).get("detail") or "")
        else:
            actual = "starting"
    return {"desired": desired, "actual": actual, "reason": error}


def _follow_imei_source(old_id: str, new_id: str) -> list[str]:
    """Repoint lines that name a device id the reader migration just retired.

    The field records which physical device supplied a line's IMEI, and doubles as the marker
    that the one-time legacy migration already ran for that line. It is not load-bearing — the
    IMEI is resolved from whichever device currently holds the card — so a stale value is
    harmless to the engine but reads as a device that no longer exists, and clearing it would
    be worse than leaving it: an empty marker lets the legacy migration run a second time.
    """
    followed = []
    for inst in cfg.list_instances():
        if str(inst.get("imei_source_device_id") or "") != old_id:
            continue
        cfg.upsert_instance({"id": str(inst["id"]), "imei_source_device_id": new_id})
        followed.append(str(inst["id"]))
    return followed


async def _unified_devices() -> list[dict]:
    desired_doc, observed_doc, assignments = _device_sources()
    desired_devices = desired_doc.get("devices") or {}
    observed_devices = observed_doc.get("devices") or {}
    identities = _device_identities()
    cards = hub.cards_list()
    native_readers = device_state.native_reader_devices(cards)
    # A reader that moved to a different USB port derives a new id and strands its saved
    # record, which this list would then render as a second, permanently offline copy of a
    # connected reader. Move the record first: it holds the IMEI the line presents.
    if cards:
        try:
            moved = await asyncio.to_thread(device_state.migrate_reader_records, native_readers)
            for old_id, new_id in moved:
                log.info("reader record migrated: %s -> %s (same reader on a new USB port)",
                         old_id, new_id)
                followed = await asyncio.to_thread(_follow_imei_source, old_id, new_id)
                if followed:
                    log.info("line(s) %s now name the migrated reader as their IMEI source",
                             ", ".join(followed))
        except Exception as exc:  # noqa
            log.debug("reader record migration failed: %r", exc)
    hardware_records = device_state.hardware()
    saved_reader_ids = {device_id for device_id, record in hardware_records.items()
                        if record.get("device_type") == "reader"}
    saved_modem_ids = set(hardware_records) - saved_reader_ids
    modem_ids = (set(assignments) | set(desired_devices) | set(observed_devices)
                 | set(identities) | saved_modem_ids)
    device_ids = sorted(modem_ids | set(native_readers) | saved_reader_ids)
    # A saved assignment says where a modem belongs, not that it is physically connected.
    # Presence must come from the live orchestrator observation; otherwise an unplugged modem
    # retains its desired toggles and is misleadingly rendered as "starting/stopping" forever.
    present_ids = sorted(device_id for device_id in modem_ids
                         if bool((observed_devices.get(device_id) or {}).get("present", False)))
    shared = observed_doc.get("shared") or {}

    settings = cfg.get_settings()
    configured_exits = settings.get("proxy", {}).get("exits", {}) or {}
    available_countries = sorted(country for country, value in configured_exits.items()
                                 if isinstance(value, dict) and value.get("enabled", False))
    result = []
    for device_id in device_ids:
        native_card = native_readers.get(device_id)
        hardware_record = hardware_records.get(device_id) or {}
        is_native_reader = native_card is not None or hardware_record.get("device_type") == "reader"
        assignment = assignments.get(device_id) or {}
        observed = observed_devices.get(device_id) or {}
        identity = identities.get(device_id) or {}
        device_present = (bool(native_card is not None) if is_native_reader
                          else bool(observed.get("present", False)))
        host_cell = observed.get("cellular") or {}
        inst = (_match_instance_by_iccid(native_card.get("iccid"))
                if native_card and native_card.get("present") and native_card.get("iccid")
                else _instance_for_device(device_id, identity, cards, observed)
                if device_present else None)
        wanted = ({"cellular_enabled": False,
                   "vowifi_enabled": bool((inst or {}).get("enabled", bool(inst))),
                   "flight_mode": False}
                  if is_native_reader else desired_devices.get(device_id)
                  or desired_doc.get("defaults") or {
                      "cellular_enabled": False, "vowifi_enabled": True,
                      "flight_mode": False})
        cell_desired = bool(wanted.get("cellular_enabled"))
        vowifi_desired = bool(wanted.get("vowifi_enabled"))
        flight_desired = bool(wanted.get("flight_mode"))
        line_status = _cached_line_status(inst) if inst else None
        running = bool(inst) and (line_status or {}).get("state") != "STOPPED"
        vowifi = (device_state.native_vowifi_capability(vowifi_desired, running, line_status)
                  if is_native_reader else
                  _vowifi_capability(vowifi_desired, observed, running, line_status))
        is_draft = bool(inst) and inst.get("provisioning_state") == "draft"
        if not device_present:
            vowifi.update(actual="off", available=False, reason="Device not connected")
        elif not inst:
            vowifi.update(available=False, reason="Insert a readable SIM before enabling VoWiFi")
        elif is_draft:
            vowifi.update(available=False,
                          reason="Automatic setup is waiting for SIM or hardware information")

        actual_state = observed.get("actual") or {}
        # Published by the orchestrator when this gateway is configured VoWiFi-only
        # (hardware.modem_backend = serial): ModemManager never runs, so cellular is a
        # capability this host does not have — not a fault to keep retrying.
        serial_only = actual_state.get("cellular_supported") is False
        is_cellular_target = (not is_native_reader and not serial_only
                              and device_id in present_ids)
        cell_reason = ""
        cell_actual = "unsupported" if (is_native_reader or serial_only) else "off"
        radio_on = bool(actual_state.get("cellular_radio_enabled"))
        if is_native_reader:
            cell_reason = "A smart-card reader supports VoWiFi only"
        elif serial_only:
            cell_reason = "This gateway is configured for VoWiFi only (ModemManager disabled)"
        cellular_view = None
        if is_cellular_target:
            if host_cell.get("available"):
                registration = str(host_cell.get("registration") or "unknown").lower()
                radio_on = bool(actual_state.get("cellular_radio_enabled",
                                                 host_cell.get("radio_enabled",
                                                               host_cell.get("powered"))))
                registered = registration in {"home", "roaming", "registered"}
                if not cell_desired and host_cell.get("data_active"):
                    cell_actual = "stopping"
                elif not cell_desired:
                    cell_actual = "off"
                elif flight_desired:
                    cell_actual, cell_reason = "off", "Flight mode is enabled"
                elif radio_on and registered and host_cell.get("data_active"):
                    cell_actual = "on"
                elif radio_on:
                    cell_actual = "starting"
                else:
                    cell_actual, cell_reason = "error", "Cellular radio is not enabled"
                cellular_view = {
                    "registration": registration, "operator": host_cell.get("operator") or "",
                    "signal": host_cell.get("signal"), "apn": host_cell.get("apn") or "",
                    "ip": host_cell.get("ip") or "",
                    "data_active": bool(host_cell.get("data_active")),
                    "roaming": registration == "roaming",
                    "rx_bytes": int(host_cell.get("rx_bytes") or 0),
                    "tx_bytes": int(host_cell.get("tx_bytes") or 0),
                    "profile": host_cell.get("profile") or "",
                    "interface": host_cell.get("network_interface") or "",
                }
            elif cell_desired:
                if shared.get("error"):
                    cell_actual = "error"
                    cell_reason = str(shared.get("error"))
                else:
                    cell_actual = "starting"
        card_info = native_card or next((item for item in cards
                                         if item.get("hardware_id") == device_id
                                         and item.get("present")), {})
        # Keep physical SIM state independent from the optional VoWiFi PC/SC
        # bridge.  A connected cellular modem can have a readable SIM even when
        # every virtual reader slot is empty or VoWiFi is disabled.
        live_modem_iccid = (str(host_cell.get("sim_iccid") or "")
                            if device_present and not is_native_reader else "")
        if live_modem_iccid and not card_info:
            card_info = {
                "present": True, "iccid": live_modem_iccid,
                "hardware_id": device_id, "hardware_kind": "modem",
                "mcc": (inst or {}).get("mcc", ""),
                "mnc": (inst or {}).get("mnc", ""),
                "imsi": (inst or {}).get("imsi", ""),
                "smsc": (inst or {}).get("smsc", ""),
                "mnc_len": (inst or {}).get("mnc_len"),
                "carrier_identity": (inst or {}).get("carrier_identity") or {},
            }
        carrier = _carrier_description(inst, card_info, cellular_view)
        if native_card:
            hardware_imei, _hardware_id, _hardware_type = _hardware_imei_for_card(
                native_card, cards)
            hardware_record = device_state.hardware().get(device_id) or hardware_record
        else:
            hardware_imei = cfg.normalize_imei(identity.get("imei", ""))
        masked_imei = _masked_identifier(hardware_imei)
        bridge_active = bool(actual_state.get("vowifi_bridge_active"))
        logical_channels = (None if is_native_reader else
                            device_state.logical_channel_view(identity, bridge_active))
        if not device_present:
            cell_actual, cell_reason = "off", "Device not connected"
            flight_actual, flight_available = "off", False
        else:
            flight_actual = ("unsupported" if is_native_reader or serial_only else
                             "on" if flight_desired and not radio_on else
                             "off" if not flight_desired and radio_on else
                             "starting" if flight_desired else "stopping")
            flight_available = not is_native_reader and not serial_only
        result.append({
            "id": device_id, "device_type": "reader" if is_native_reader else "modem",
            "name": (card_info.get("display_name") or card_info.get("name")
                     or hardware_record.get("name") or "Smart-card reader"
                     if is_native_reader else
                     assignment.get("name") or observed.get("name")
                     or hardware_record.get("name") or "Cellular modem"),
            "present": device_present,
            "model": identity.get("model") or observed.get("model") or "",
            "firmware": identity.get("firmware") or observed.get("firmware") or "",
            "imei": hardware_imei,
            "imei_masked": masked_imei,
            "stable_path": ((card_info.get("reader_port") or hardware_record.get("stable_path") or "")
                            if is_native_reader else
                            assignment.get("usb_path") or identity.get("usb_path")
                            or hardware_record.get("stable_path") or ""),
            "reader": card_info.get("name") or "", "instance_id": str(inst["id"]) if inst else None,
            "status": line_status,
            "logical_channels": logical_channels,
            "sim": {"name": (((inst or {}).get("name")
                             or (cellular_view or {}).get("operator") or "SIM") if inst else ""),
                    "number": (inst or {}).get("msisdn") or "",
                    "present": bool(card_info.get("present")),
                    "carrier": carrier},
            "cellular": cellular_view,
            "vowifi": {"epdg": (line_status or {}).get("detail") or "",
                       "ims": (line_status or {}).get("label") or "",
                       "rekey_minutes": (inst or {}).get("rekey_minutes",
                           (cfg.get_settings().get("rekey") or {}).get("minutes", 30))},
            "egress": {"node": (egress.status().get("lines") or {}).get(
                str(inst["id"]) if inst else "", {}).get("node") or "",
                # The picker lives on the settings page, so without these the device page shows
                # a node that silently disagrees with what the operator chose.
                **{key: ((egress.status().get("exits") or {}).get(
                    egress.line_country(inst or card_info), {}).get(key) or "")
                   for key in ("pinned_node", "pin_mode", "selection",
                               # Why the exit moved, and whether the pinned node is still
                               # serving a cooldown — otherwise a mismatch looks arbitrary.
                               "last_change", "pinned_cooldown_seconds")},
                "country": egress.line_country(inst or card_info),
                "detected_country": egress.country_for_mcc((inst or card_info).get("mcc")),
                "override": egress.normalize_country((inst or {}).get("proxy_country")),
                "available_countries": available_countries},
            "provisioning": {"state": "draft" if is_draft else "ready" if inst else "detecting",
                "missing": ([key for key, value in (
                    ("imsi", (inst or card_info).get("imsi")),
                    ("imei", hardware_imei),
                    ("smsc", (inst or card_info).get("smsc"))) if not value])},
            "capabilities": {"cellular": {"desired": cell_desired, "actual": cell_actual,
                                             "reason": cell_reason},
                             "flight": {"desired": flight_desired,
                                        "actual": flight_actual,
                                        "available": flight_available,
                                        "reason": ("Device not connected" if not device_present
                                                   else "This gateway is configured for VoWiFi "
                                                        "only (ModemManager disabled)"
                                                   if serial_only else "")},
                             "vowifi": vowifi},
            "shared": shared,
        })
    return result


@app.get("/api/devices")
async def api_devices():
    # Sessions are memory-only, so a sign-in usually follows a control-plane restart — right
    # when the card monitor is still completing its first scan and smart-card readers are not
    # in the list yet. `discovering` lets the UI say so instead of reporting a confident zero.
    return {"devices": await _unified_devices(), "discovering": not hub.scanned,
            "shared": device_state.status().get("shared") or {}}


@app.put("/api/devices/{device_id}/hardware")
async def api_device_hardware(device_id: str, body: dict):
    """Save user-managed physical hardware identity (currently native-reader IMEI)."""
    if set(body or {}) - {"imei"}:
        raise HTTPException(400, "only imei can be changed")
    device = next((item for item in await _unified_devices() if item["id"] == device_id), None)
    if not device:
        raise HTTPException(404, "no such physical device")
    if device.get("device_type") != "reader":
        raise HTTPException(400, "a modem reports its hardware IMEI automatically")
    raw = str((body or {}).get("imei") or "").strip()
    imei = cfg.normalize_imei(raw)
    if len(imei) != 15:
        raise HTTPException(422, "IMEI must contain exactly 15 digits")
    record = device_state.set_hardware(device_id, {
        "device_type": "reader", "name": device.get("name") or "Smart-card reader",
        "stable_path": device.get("stable_path") or "", "imei": imei})

    # A running line renders the device identity inside its container. Apply a hardware
    # change immediately to the SIM currently inserted in this reader.
    iid = str(device.get("instance_id") or "")
    applied = False
    if iid and imei:
        inst = cfg.get_instance(iid) or {}
        previous_imeisv = str(inst.get("imeisv") or "")
        svn = (previous_imeisv[-2:] if len(previous_imeisv) == 16
               and previous_imeisv[-2:].isdigit() else _random_svn())
        inst = cfg.upsert_instance({"id": iid, "imei": imei,
                                    "imei_source_device_id": device_id,
                                    "imeisv": cfg.imeisv_from_imei(imei, svn=svn)})
        if await asyncio.to_thread(engine.is_running, iid):
            await hub.drop_ami(iid)
            await asyncio.to_thread(_start_engine_checked, inst, cfg.get_settings(),
                                    dev_mounts=os.environ.get("MDD_DEV_MOUNTS", "") == "1")
            hub.reset_health(iid)
            applied = True
    await hub.broadcast({"type": "hardware", "device": device_id})
    return {"ok": True, "imei_masked": _masked_identifier(record.get("imei")),
            "applied": applied}


def _remove_device_from_document(path: str, device_id: str, mapping_key: str) -> None:
    document = _read_json_file(path)
    mapping = document.get(mapping_key)
    if not isinstance(mapping, dict) or device_id not in mapping:
        return
    mapping.pop(device_id, None)
    document["updated_at"] = int(time.time())
    temporary = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


@app.delete("/api/devices/{device_id}")
async def api_device_delete(device_id: str):
    """Forget an offline physical device without deleting any SIM/line configuration."""
    device = next((item for item in await _unified_devices() if item["id"] == device_id), None)
    if not device:
        raise HTTPException(404, "no such physical device")
    if device.get("present"):
        raise HTTPException(409, "disconnect the physical device before forgetting it")
    device_state.remove_desired(device_id)
    device_state.remove_hardware(device_id)
    orchestrator_root = os.path.join(cfg.DATA_DIR, "orchestrator")
    _remove_device_from_document(os.path.join(orchestrator_root, "hardware-state.json"),
                                 device_id, "assignments")
    _remove_device_from_document(os.path.join(orchestrator_root, "devices-status.json"),
                                 device_id, "devices")
    for path in glob.glob(os.path.join(cfg.DATA_DIR, "modems", "*.json")):
        identity = _read_json_file(path)
        if str(identity.get("hardware_id") or "") == device_id:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
    await hub.broadcast({"type": "hardware", "device": device_id, "event": "forgotten"})
    return {"ok": True, "device_id": device_id, "lines_preserved": True}


@app.get("/api/devices/{device_id}/cellular")
async def api_device_cellular(device_id: str):
    device = next((item for item in await _unified_devices() if item["id"] == device_id), None)
    if not device:
        raise HTTPException(404, "no such physical device")
    return {"device_id": device_id, "capability": device["capabilities"]["cellular"],
            "cellular": device.get("cellular")}


@app.post("/api/devices/{device_id}/diagnostics")
async def api_device_diagnostics(device_id: str):
    device = next((item for item in await _unified_devices() if item["id"] == device_id), None)
    if not device:
        raise HTTPException(404, "no such physical device")
    checks = [
        {"name": "hardware", "ok": bool(device.get("present")),
         "detail": "detected" if device.get("present") else "not detected"},
        {"name": "cellular", "ok": device["capabilities"]["cellular"]["actual"] in {"on", "off", "unsupported"},
         "detail": device["capabilities"]["cellular"]["actual"]},
        {"name": "vowifi", "ok": device["capabilities"]["vowifi"]["actual"] in {"on", "off"},
         "detail": device["capabilities"]["vowifi"]["actual"]},
        {"name": "country_egress", "ok": (not device["capabilities"]["vowifi"]["desired"]
                                             or bool(device.get("egress", {}).get("node"))),
         "detail": device.get("egress", {}).get("node") or "not selected"},
    ]
    return {"ok": all(item["ok"] for item in checks), "device_id": device_id,
            "checked_at": int(time.time()), "checks": checks}


async def _wait_for_device_request(device_id: str, wanted: dict, timeout: float = 120) -> dict:
    deadline = time.monotonic() + timeout
    latest = {}
    while time.monotonic() < deadline:
        latest = device_state.status()
        current = (latest.get("devices") or {}).get(device_id) or {}
        observed_wanted = current.get("desired") or {}
        if (all(observed_wanted.get(key) == value for key, value in wanted.items())
                and not current.get("transitioning")
                and not (latest.get("shared") or {}).get("transitioning")):
            # A shared MM shutdown resets the USB modem. The orchestrator can publish one
            # intermediate "not connected" sample after the desired state is applied; wait for
            # re-enumeration instead of reporting a failed toggle that actually succeeded.
            if not current.get("present", True) or current.get("error") == "device is not connected":
                await asyncio.sleep(.5)
                continue
            if current.get("error") or (latest.get("shared") or {}).get("error"):
                raise RuntimeError(current.get("error") or latest["shared"]["error"])
            return latest
        await asyncio.sleep(.5)
    raise TimeoutError("device capability transition timed out")


async def _resume_instances(instance_ids: set[str], skip: set[str] | None = None) -> dict:
    failed = {}
    for iid in sorted(instance_ids - (skip or set())):
        inst = cfg.get_instance(iid)
        if not inst:
            continue
        try:
            # Use the full manual-start path so a retry clears frozen health, refreshes the
            # current reader binding, checks PIN/card identity and drops a stale AMI client.
            await api_instance_start(iid)
        except Exception as exc:
            failed[iid] = str(getattr(exc, "detail", exc))
    return failed


@app.patch("/api/devices/{device_id}/capabilities")
async def api_device_capabilities(device_id: str, body: dict):
    allowed = {"cellular_enabled", "vowifi_enabled", "flight_mode"}
    if not body or not set(body).issubset(allowed):
        raise HTTPException(400, "provide cellular_enabled, vowifi_enabled and/or flight_mode only")
    if any(not isinstance(value, bool) for value in body.values()):
        raise HTTPException(400, "capability values must be boolean")

    async with capability_lock:
        unified = await _unified_devices()
        device = next((item for item in unified if item["id"] == device_id), None)
        if not device:
            raise HTTPException(404, "no such physical device")
        if device.get("device_type") == "reader":
            if "cellular_enabled" in body or "flight_mode" in body:
                raise HTTPException(400, "a smart-card reader has no cellular radio")
            iid = str(device.get("instance_id") or "")
            if not iid:
                if body.get("vowifi_enabled"):
                    raise HTTPException(409, "configure the SIM before enabling VoWiFi")
                return device
            inst = cfg.get_instance(iid)
            previous = bool((inst or {}).get("enabled", True))
            wanted = bool(body.get("vowifi_enabled", previous))
            retry = bool(wanted and not await asyncio.to_thread(engine.is_running, iid))
            if wanted == previous and not retry:
                return device
            if wanted:
                cfg.upsert_instance({"id": iid, "enabled": True})
                await api_instance_start(iid)
            else:
                cfg.upsert_instance({"id": iid, "enabled": False})
                await api_instance_stop(iid)
            refreshed = await _unified_devices()
            return next(item for item in refreshed if item["id"] == device_id)

        desired_doc, observed_doc, assignments = _device_sources()
        known = set(assignments) | set(desired_doc.get("devices") or {}) | set(observed_doc.get("devices") or {})
        if device_id not in known:
            raise HTTPException(404, "no such physical device")
        present = sorted(key for key in known if (observed_doc.get("devices") or {}).get(
            key, {}).get("present", key in assignments))
        previous = (desired_doc.get("devices") or {}).get(device_id) or desired_doc.get("defaults") or {
            "cellular_enabled": False, "vowifi_enabled": True, "flight_mode": False}
        wanted = {**previous, **body}
        cellular_changed = wanted["cellular_enabled"] != bool(previous.get("cellular_enabled"))
        vowifi_changed = wanted["vowifi_enabled"] != bool(previous.get("vowifi_enabled"))
        flight_changed = bool(wanted.get("flight_mode")) != bool(previous.get("flight_mode"))

        identities = _device_identities()
        cards = hub.cards_list()
        target_observed = (observed_doc.get("devices") or {}).get(device_id) or {}
        target_instance = _instance_for_device(
            device_id, identities.get(device_id) or {}, cards, target_observed)
        target_iid = str(target_instance["id"]) if target_instance else ""
        # Repeating an ON request is an explicit retry when the device-level intent says ON
        # but the line is disabled or its engine has stopped. Do not discard it as a no-op.
        vowifi_retry = bool(
            body.get("vowifi_enabled") is True and target_instance
            and (not target_instance.get("enabled", True)
                 or not await asyncio.to_thread(engine.is_running, target_iid)))
        if not cellular_changed and not vowifi_changed and not flight_changed and not vowifi_retry:
            devices = await _unified_devices()
            return next(item for item in devices if item["id"] == device_id)
        vowifi_action = vowifi_changed or vowifi_retry
        # Data bearer and flight-mode changes are reconciled underneath the existing line.
        # Only a VoWiFi toggle intentionally stops/starts that line.
        affected_instances = [target_instance] if vowifi_action and target_instance else []
        running_ids = []
        for inst in affected_instances:
            if inst and await asyncio.to_thread(engine.is_running, str(inst["id"])):
                running_ids.append(str(inst["id"]))
                await asyncio.to_thread(engine.stop, str(inst["id"]))
                await hub.drop_ami(str(inst["id"]))

        device_state.set_desired(device_id,
                                 cellular_enabled=wanted["cellular_enabled"],
                                 vowifi_enabled=wanted["vowifi_enabled"],
                                 flight_mode=bool(wanted.get("flight_mode")))
        if target_iid and vowifi_action:
            target_instance = cfg.upsert_instance({
                "id": target_iid, "enabled": bool(wanted["vowifi_enabled"])})
        egress.publish()
        skip_resume = {target_iid} if target_iid and not wanted["vowifi_enabled"] else set()
        try:
            await _wait_for_device_request(device_id, wanted)
        except TimeoutError as exc:
            await _resume_instances(set(running_ids), skip_resume)
            raise HTTPException(504, str(exc)) from exc
        except RuntimeError as exc:
            await _resume_instances(set(running_ids), skip_resume)
            raise HTTPException(503, str(exc)) from exc

        resume_ids = set(running_ids)
        if vowifi_action and wanted["vowifi_enabled"] and target_instance:
            resume_ids.add(str(target_instance["id"]))
        failed = await _resume_instances(resume_ids, skip_resume)
        await hub.broadcast({"type": "capability", "device": device_id, "desired": wanted,
                             "resume_failed": failed})
        devices = await _unified_devices()
        response = next(item for item in devices if item["id"] == device_id)
        if failed:
            response["resume_failed"] = failed
        return response


# ----------------------------- settings -----------------------------


@app.get("/api/settings")
def api_get_settings():
    return {key: value for key, value in cfg.get_settings().items() if key != "system_name"}


@app.put("/api/settings")
def api_put_settings(body: dict):
    # Ignore the legacy field from older cached clients. Product identity is fixed.
    body.pop("system_name", None)
    proxy = body.get("proxy")
    if proxy is not None:
        if not isinstance(proxy, dict) or not isinstance(proxy.get("profiles", {}), dict) \
                or not isinstance(proxy.get("exits", {}), dict):
            raise HTTPException(400, "invalid proxy library")
        profiles = proxy.get("profiles") or {}
        for profile_id, profile in profiles.items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", str(profile_id)) \
                    or not isinstance(profile, dict):
                raise HTTPException(400, "invalid proxy profile id")
            if str(profile.get("type") or "") not in {"subscription", "node", "socks5", "existing"}:
                raise HTTPException(400, "invalid proxy profile type")
            if not str(profile.get("name") or "").strip():
                raise HTTPException(400, "proxy profile name is required")
        for country, exit_cfg in (proxy.get("exits") or {}).items():
            if not egress.normalize_country(country) or not isinstance(exit_cfg, dict):
                raise HTTPException(400, "invalid country exit")
            profile_id = str(exit_cfg.get("profile_id") or "")
            if profile_id and profile_id not in profiles:
                raise HTTPException(400, f"country exit references unknown proxy {profile_id!r}")
    if "timezone" in body:
        try:
            ZoneInfo(str(body.get("timezone") or ""))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise HTTPException(400, "unknown timezone") from exc
    defaults = body.get("device_defaults")
    if defaults is not None:
        if not isinstance(defaults, dict) or any(
                key not in {"cellular_enabled", "vowifi_enabled", "flight_mode"}
                for key in defaults):
            raise HTTPException(400, "invalid new-device defaults")
        if any(not isinstance(value, bool) for value in defaults.values()):
            raise HTTPException(400, "new-device defaults must be boolean")
    webhook = body.get("webhook") or {}
    if webhook.get("enabled"):
        try:
            sample = notify_push.build_payload(
                notify_push.EV_INCOMING_SMS,
                {"id": "preview", "name": "SIM", "iccid": "", "msisdn": ""},
                "+10000000000", "123456")
            notify_push.build_webhook_request(webhook, sample)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(400, f"invalid webhook configuration: {exc}")
    # Telegram is notification-only. Ignore stale clients that
    # still submit a remote command configuration.
    (body.get("telegram") or {}).pop("commands", None)
    pushplus = body.get("pushplus") or {}
    if pushplus.get("enabled"):
        if not str(pushplus.get("token") or "").strip():
            raise HTTPException(400, "PushPlus token is required")
        if str(pushplus.get("template") or "html") not in {"html", "txt", "markdown", "json"}:
            raise HTTPException(400, "unsupported PushPlus template")
    if "updates" in body:
        try:
            body["updates"] = update_check.validate_network_settings(body.get("updates"))
        except update_check.UpdateNetworkError as exc:
            raise HTTPException(400, str(exc)) from exc
    hardware = body.get("hardware")
    if hardware is not None:
        if not isinstance(hardware, dict):
            raise HTTPException(400, "invalid hardware settings")
        backend = str(hardware.get("modem_backend", "auto") or "auto")
        if backend not in {"auto", "serial"}:
            raise HTTPException(400, "modem_backend must be auto or serial")
        # Settings updates replace top-level keys wholesale; a client sending only the
        # backend switch must not wipe the modem profiles beside it.
        body["hardware"] = {**cfg.get_settings().get("hardware", {}), **hardware,
                            "modem_backend": backend}
    saved = cfg.update_settings(body)
    if defaults is not None:
        device_state.set_defaults(**defaults)
    egress.publish(settings=saved)
    return {key: value for key, value in saved.items() if key != "system_name"}


@app.get("/api/egress/status")
def api_egress_status():
    return egress.status()


@app.post("/api/egress/refresh")
def api_egress_refresh():
    cache = os.path.join(cfg.DATA_DIR, "orchestrator", "subscription.yaml")
    try:
        os.remove(cache)
    except FileNotFoundError:
        pass
    cache_dir = os.path.join(cfg.DATA_DIR, "orchestrator", "subscriptions")
    try:
        for name in os.listdir(cache_dir):
            if name.endswith(".yaml"):
                os.remove(os.path.join(cache_dir, name))
    except FileNotFoundError:
        pass
    egress.publish()
    return {"ok": True, "requested_at": int(time.time())}


async def _test_egress_country(country: str):
    country = egress.normalize_country(country)
    exits = (cfg.get_settings().get("proxy") or {}).get("exits") or {}
    if not country or country not in exits:
        raise HTTPException(404, "country exit is not configured")
    egress.publish()
    deadline = time.monotonic() + 25
    latest = {}
    while time.monotonic() < deadline:
        latest = (egress.status().get("exits") or {}).get(country) or {}
        if latest.get("ready"):
            host, port = str(latest.get("proxy_host") or ""), int(latest.get("proxy_port") or 0)
            if not host or not port:
                raise HTTPException(503, "country exit has no UDP test endpoint")
            try:
                latency = await asyncio.to_thread(egress.test_udp_proxy, host, port)
            except egress.EgressError as exc:
                raise HTTPException(503, str(exc)) from exc
            return {"ok": True, "country": country, "node": latest.get("node") or "",
                    "interface": latest.get("interface") or "", "latency_ms": latency}
        if latest.get("error"):
            break
        await asyncio.sleep(.5)
    raise HTTPException(503, latest.get("error") or "no healthy UDP-capable node is ready")


@app.post("/api/egress/profile/{profile_id}/test")
async def api_egress_profile_test(profile_id: str, body: dict | None = None):
    saved = ((cfg.get_settings().get("proxy") or {}).get("profiles") or {}).get(profile_id)
    profile = body if isinstance(body, dict) and body else saved
    if not isinstance(profile, dict):
        raise HTTPException(404, "save this proxy before testing it")
    if profile.get("type") not in {"node", "socks5"}:
        raise HTTPException(400, "only individual nodes and SOCKS5 proxies can be tested here")
    try:
        latency = await asyncio.to_thread(egress.test_proxy_profile, profile)
    except egress.EgressError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"ok": True, "profile_id": profile_id, "latency_ms": latency}


@app.post("/api/egress/{country}/test")
async def api_egress_test(country: str):
    return await _test_egress_country(country)


def _test_push_payload() -> dict:
    return notify_push.build_payload(
        notify_push.EV_INCOMING_SMS,
        {"id": "test", "name": "Gateway test", "iccid": "", "msisdn": ""},
        "+10000000000", "MDD Sim Gateway notification test")


@app.post("/api/notifications/webhook/test")
async def api_webhook_test(body: dict):
    try:
        return await asyncio.to_thread(notify_push.send_webhook, body, _test_push_payload())
    except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
        raise HTTPException(400 if isinstance(exc, (ValueError, json.JSONDecodeError)) else 502,
                            str(exc)) from exc


@app.post("/api/notifications/telegram/test")
async def api_telegram_test(body: dict):
    try:
        return await asyncio.to_thread(notify_push.send_telegram, body, _test_push_payload())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/notifications/pushplus/test")
async def api_pushplus_test(body: dict):
    try:
        return await asyncio.to_thread(notify_push.send_pushplus, body, _test_push_payload())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/notifications/deliveries")
def api_notification_deliveries(limit: int = 100):
    return notify_push.delivery_status(limit)


@app.delete("/api/notifications/deliveries")
def api_notification_deliveries_clear():
    notify_push.clear_delivery_history()
    return {"ok": True}


@app.get("/api/system/status")
def api_system_status():
    settings = cfg.get_settings()
    # Served from the poller's sample: collecting here would shell out to vcgencmd/dmesg on
    # every page load of an already power-constrained box.
    host = hub.host_snapshot or sysinfo.collect(cfg.DATA_DIR)
    return {
        "system_name": "MDD Sim Gateway",
        "host": host,
        "host_alerts": hub.host_alerts if hub.host_snapshot else sysinfo.alerts(host),
        "timezone": settings.get("timezone") or "UTC",
        "version": VERSION,
        "repository_url": f"https://github.com/{update_check.repository()}",
        "backups": operations.list_local_backups(),
        "security": {
            "https": True,
            "certificate_mode": "self-signed" if (settings.get("tls") or {}).get("self_signed") else "custom",
            "audit_enabled": bool((settings.get("security") or {}).get("audit_enabled", True)),
        },
    }


@app.delete("/api/system/host-alerts")
def api_host_alerts_clear():
    """Acknowledge active host alerts until each condition genuinely clears."""
    if hub.host_alert_state is None:
        hub.host_alert_state = _load_host_alert_state()
    now = time.time()
    cleared = []
    for item in hub.host_alerts:
        code = str(item.get("code") or "")
        if not code:
            continue
        entry = hub.host_alert_state.setdefault(code, {})
        entry["acknowledged"] = True
        entry["acknowledged_at"] = now
        entry.setdefault("at", now)
        cleared.append(code)
    hub.host_alerts = []
    _save_host_alert_state(hub.host_alert_state)
    return {"ok": True, "cleared": cleared}


@app.get("/api/system/update/check")
async def api_system_update_check(force: bool = False):
    """Read-only release lookup. Requires an admin session (see _AUTH_PUBLIC).

    The periodic UI poll uses the short in-process cache; only an explicit "Check for updates"
    click passes force=true, so repeated logins/reloads cannot burn GitHub's unauthenticated
    rate limit.
    """
    return await asyncio.to_thread(update_check.check, force)


@app.post("/api/system/update/apply")
async def api_system_update_apply():
    """One-click update: publish a request for the host orchestrator, which runs the detached
    updater (host/mdd_update.py). Responds immediately; progress is polled separately."""
    return await asyncio.to_thread(update_check.request_apply)


@app.get("/api/system/update/progress")
def api_system_update_progress():
    return update_check.apply_status()


@app.post("/api/system/backups")
async def api_system_backup():
    settings = cfg.get_settings()
    return await asyncio.to_thread(
        operations.create_local_backup, "mdd-sim-gateway")


@app.post("/api/system/maintenance")
async def api_system_maintenance(body: dict):
    action = str(body.get("action") or "")
    if action == "clear_notification_history":
        notify_push.clear_delivery_history()
        return {"ok": True, "action": action}
    if action == "refresh_egress":
        return api_egress_refresh()
    if action == "restart_lines":
        restarted, failed = [], {}
        # A saved line may be offline because its SIM is absent or its physical device has
        # VoWiFi disabled. Snapshot only containers that were actually running when the
        # operator requested a restart; never turn a restart operation into "start all saved".
        running = []
        for inst in cfg.list_instances():
            if await asyncio.to_thread(engine.is_running, str(inst["id"])):
                running.append(inst)
        for inst in running:
            iid = str(inst["id"])
            try:
                await asyncio.to_thread(engine.stop, iid)
                await hub.drop_ami(iid)
                await asyncio.to_thread(_start_engine_checked, inst, cfg.get_settings(),
                                        dev_mounts=os.environ.get("MDD_DEV_MOUNTS", "") == "1")
                restarted.append(iid)
            except Exception as exc:
                failed[iid] = str(getattr(exc, "detail", exc))
        return {"ok": not failed, "action": action, "restarted": restarted, "failed": failed}
    raise HTTPException(400, "unknown maintenance action")


@app.get("/api/diagnostics/support-bundle")
async def api_support_bundle():
    settings = cfg.get_settings()
    documents = {"devices": device_state.status(), "egress": egress.status(),
                 # What pcscd is actually exposing right now — the one fact every card-path
                 # report needed and the bundle never carried. Passive: monitor state only.
                 "readers": [{"name": item.get("name"), "present": item.get("present"),
                              "hardware_kind": item.get("hardware_kind")}
                             for item in hub.cards_list()],
                 "instances": [{"id": item.get("id"), "name": item.get("name")}
                               for item in cfg.list_instances()]}
    content = await asyncio.to_thread(
        operations.support_bundle, documents,
        (settings.get("maintenance") or {}).get("support_bundle_log_lines", 500))
    headers = {"Content-Disposition": 'attachment; filename="vowifi-support-redacted.zip"'}
    return Response(content, media_type="application/zip", headers=headers)


# ----------------------------- instances -----------------------------
@app.get("/api/instances")
async def api_instances():
    out = []
    for inst in cfg.list_instances():
        st = _cached_line_status(inst)
        safe = {k: v for k, v in inst.items() if k not in ("pin", "carrier_identity")}
        safe["has_pin"] = bool(inst.get("pin"))
        safe["proxy_country_effective"] = egress.line_country(inst)
        # Report the reader index that PHYSICALLY holds this line's SIM right now (ICCID-matched
        # against the live monitor) instead of the stored one. PC/SC indices shift when readers
        # are unplugged, so a stored index can be stale and make the SIM-config "Detect card"
        # button probe a reader that no longer exists ("No SIM card in reader N").
        live_idx = _reader_index_for_instance(inst)
        if live_idx is not None:
            safe["reader_index"] = live_idx
        # Also report the SIM's current USB port (by ICCID from the live monitor) so the UI can
        # show the stable binding and re-persist it if the SIM was moved to another reader socket.
        live_port = _reader_port_for_instance(inst)
        if live_port:
            safe["reader_port"] = live_port
        out.append({**safe, "status": st})
    return {"instances": out}


@app.post("/api/instances")
async def api_instance_upsert(body: dict):
    if "id" not in body:
        raise HTTPException(400, "id required")
    iid = str(body["id"])
    body = {key: value for key, value in body.items() if key != "carrier_identity"}
    # Reject an explicit rename onto another line's name rather than silently suffixing it:
    # the operator asked for that exact label, and a duplicate makes the name useless as a
    # handle in the UI and audit history.
    if "name" in body and cfg.instance_name_taken(body.get("name"), exclude_iid=iid):
        raise HTTPException(409, "another line already uses that name")
    was_running = await asyncio.to_thread(engine.is_running, iid)
    try:
        inst = cfg.upsert_instance(body)
    except cfg.LineLimitError as exc:
        raise HTTPException(409, {
            "code": "line_limit", "message": str(exc)}) from exc
    applied = False
    # A running line holds its config in the engine container (rendered instance.json:
    # WebRTC credentials, IMEI, SMSC, User-Agent, …). Editing the config alone doesn't reach
    # the running Asterisk — so restart the container to re-render + reload the new config.
    if was_running:
        try:
            hub._msisdn_tries.pop(iid, None)
            hub.reset_health(iid)
            await hub.drop_ami(iid)
            await asyncio.to_thread(_start_engine_checked, inst, cfg.get_settings(),
                                    dev_mounts=os.environ.get("MDD_DEV_MOUNTS", "") == "1")
            applied = True
            asyncio.create_task(push_status(iid))
        except Exception as e:  # noqa
            log.warning("apply-on-save restart failed for %s: %r", iid, e)
    safe = {k: v for k, v in inst.items() if k not in ("pin", "carrier_identity")}
    safe["applied"] = applied      # true => config was re-applied to the running engine
    return safe


@app.put("/api/instances/{iid}/country")
async def api_instance_country(iid: str, body: dict):
    """Select a per-line country exit, or clear it to return to MCC auto-detection."""
    if not cfg.get_instance(iid):
        raise HTTPException(404, "no such instance")
    raw = str(body.get("country") or "").strip()
    country = egress.normalize_country(raw)
    if raw and not country:
        raise HTTPException(400, "country must be a two-letter ISO code")
    safe = await api_instance_upsert({"id": str(iid), "proxy_country": country})
    egress.publish()
    return {"ok": True, "country": country,
            "effective_country": egress.line_country(cfg.get_instance(iid) or {}),
            "applied": bool(safe.get("applied"))}


@app.delete("/api/instances/{iid}")
async def api_instance_delete(iid: str, delete_history: bool = True, confirm_id: str = ""):
    """Delete one SIM line and its engine data; optionally retain SMS/call history.

    If the card is still inserted, suppress automatic draft creation until it is physically
    removed. Otherwise the card monitor would recreate the line immediately and make a
    successful delete look broken.
    """
    if str(confirm_id) != str(iid):
        raise HTTPException(400, "confirm_id must exactly match the SIM line id")
    inst = cfg.get_instance(iid)
    if not inst:
        raise HTTPException(404, "no such instance")
    inserted = any(card_info.get("present") and (
        str(card_info.get("matched") or "") == str(iid)
        or (inst.get("iccid") and str(card_info.get("iccid") or "") == str(inst["iccid"])))
        for card_info in hub.cards_list())
    # Old migrations could leave two records for the same ICCID. Deleting one must not pause
    # or strand the surviving line that should take ownership of the still-inserted SIM.
    replacements = [item for item in cfg.list_instances()
                    if str(item.get("id")) != str(iid)
                    and inst.get("iccid")
                    and str(item.get("iccid") or "") == str(inst.get("iccid"))]
    if inserted and inst.get("iccid") and not replacements:
        await asyncio.to_thread(cfg.suppress_card_until_removal, inst["iccid"])
    await asyncio.to_thread(engine.stop, iid)
    await hub.drop_ami(iid)
    hub.status_cache.pop(str(iid), None)
    hub.status_sampled_at.pop(str(iid), None)
    hub.health.pop(str(iid), None)
    hub._msisdn_tries.pop(str(iid), None)
    cfg.delete_instance(iid)
    await asyncio.to_thread(engine.delete_instance_data, iid)
    deleted_messages = deleted_calls = 0
    if delete_history:
        deleted_messages, deleted_calls = await asyncio.gather(
            asyncio.to_thread(store.clear_messages, iid),
            asyncio.to_thread(store.clear_calls, iid))
    # Line ids are reused by the next created line, so its connectivity timeline always goes
    # with the line it describes — a new SIM must never inherit another SIM's outages.
    _line_state_written.pop(str(iid), None)
    await asyncio.to_thread(store.clear_line_states, iid)
    await asyncio.to_thread(store.clear_allowance_data, iid)
    _refresh_card_matches()
    if inserted and replacements:
        replacement = next((item for item in replacements if item.get("enabled", True)), None)
        if replacement:
            asyncio.create_task(_auto_start_hotplugged_line(str(replacement["id"])))
    await hub.broadcast({"type": "cards", "cards": _client_cards()})
    await hub.broadcast({"type": "line", "instance": str(iid), "event": "deleted"})
    if delete_history:
        await hub.broadcast({"type": "sms", "instance": str(iid),
                             "deleted": deleted_messages})
        await hub.broadcast({"type": "call", "instance": str(iid),
                             "deleted": deleted_calls})
    return {"ok": True, "history_deleted": bool(delete_history),
            "deleted_messages": deleted_messages, "deleted_calls": deleted_calls}


@app.post("/api/instances/{iid}/start")
async def api_instance_start(iid: str, body: dict | None = None):
    """Start (or restart) a line. Actively checks the SIM PIN state first: if the card
    requires a PIN and we have no valid saved one, the start is refused with a structured
    error so the UI can prompt for the PIN — we never bring up the IPsec/IMS engine against
    a locked card. A PIN supplied in the body (re-entry) is verified, saved, and used."""
    inst = cfg.get_instance(iid)
    if not inst:
        raise HTTPException(404, "no such instance")

    # eSIM-profile-switch guard: never start a line whose reader now holds a different
    # identity — EAP-AKA with mismatched IMSI/keys is guaranteed to be rejected by the
    # carrier (and can burn PIN tries on the wrong profile).
    mism = _card_identity_mismatch(inst)
    if mism:
        _raise_card_mismatch(inst, mism)

    # If the caller re-supplied a PIN (unlock flow), verify + persist it before preflight.
    supplied = (body or {}).get("pin")
    if supplied:
        idx = await asyncio.to_thread(_reader_index_for_instance, inst)
        if idx is not None:
            chk = await asyncio.to_thread(sim.read_card, idx, supplied)
            if chk.error and "PIN" in (chk.error or "").upper():
                raise HTTPException(400, f"PIN error: {chk.error}"
                                         + (f" ({chk.pin_tries} tries left)" if chk.pin_tries is not None else ""))
        inst = cfg.upsert_instance({"id": str(iid), "pin": supplied})

    pf = await _preflight_pin(inst)
    if not pf["ok"]:
        if pf.get("clear"):
            cfg.clear_pin(str(iid))     # stale saved PIN — force re-entry next time
        raise HTTPException(409, {"code": pf["code"], "tries": pf.get("tries")})

    settings = cfg.get_settings()
    dev = os.environ.get("MDD_DEV_MOUNTS", "") == "1"
    # Bind the line to the reader that CURRENTLY holds its SIM, keyed on the STABLE physical USB
    # port. Two identical readers (no serial) get their pcscd enumeration order — and thus their
    # indices — flipped at boot/pcscd-restart with the cables untouched; a stored index then points
    # at the wrong (or empty) reader, and the engine authenticates against no card -> DEFAULT
    # RES/CK/IK -> carrier rejects EAP-AKA. So:
    #   1. (Re)learn the SIM's current USB port (by ICCID from the live monitor) and persist it —
    #      this refreshes the binding if the SIM was physically moved to another socket.
    #   2. Resolve the live PC/SC index from that port (falls back to ICCID) and persist it too.
    # The engine also self-resolves the port->index in-container, so its self-heal restarts stay
    # correct without the control plane.
    updates: dict = {}
    live_port = await asyncio.to_thread(_reader_port_for_instance, inst)
    if live_port and live_port != inst.get("reader_port"):
        log.info("instance %s: reader port %s -> %s (live ICCID match)",
                 iid, inst.get("reader_port"), live_port)
        updates["reader_port"] = live_port
        inst = {**inst, "reader_port": live_port}
    live_idx = await asyncio.to_thread(_reader_index_for_instance, inst)
    if live_idx is not None and live_idx != inst.get("reader_index"):
        log.info("instance %s: reader index %s -> %s (port/ICCID resolve)",
                 iid, inst.get("reader_index"), live_idx)
        updates["reader_index"] = live_idx
    if updates:
        inst = cfg.upsert_instance({"id": str(iid), **updates})
    hub._msisdn_tries.pop(str(iid), None)
    hub.reset_health(iid)
    await hub.drop_ami(iid)      # engine.start recreates the container (maybe new IP) -> stale client
    cid = await asyncio.to_thread(_start_engine_checked, inst, settings, dev_mounts=dev)
    asyncio.create_task(push_status(str(iid)))
    return {"ok": True, "container": cid}


@app.post("/api/instances/{iid}/reprovision")
async def api_reprovision(iid: str, body: dict | None = None):
    """Manual re-provision: reset retry state and re-establish the line using the stored
    config (re-reads the SIM, no PIN re-entry). Optional body overrides fields (e.g. sip
    user_agent) before restart. Runs the same PIN preflight as start."""
    inst = cfg.get_instance(iid)
    if not inst:
        raise HTTPException(404, "no such instance")
    if body:
        inst = cfg.upsert_instance({"id": str(iid), **body})
    mism = _card_identity_mismatch(inst)
    if mism:
        _raise_card_mismatch(inst, mism)
    pf = await _preflight_pin(inst)
    if not pf["ok"]:
        if pf.get("clear"):
            cfg.clear_pin(str(iid))
        raise HTTPException(409, {"code": pf["code"], "tries": pf.get("tries")})
    hub._msisdn_tries.pop(str(iid), None)
    hub.reset_health(iid)
    await hub.drop_ami(iid)      # engine.start recreates the container (maybe new IP) -> stale client
    dev = os.environ.get("MDD_DEV_MOUNTS", "") == "1"
    cid = await asyncio.to_thread(_start_engine_checked, inst, cfg.get_settings(), dev_mounts=dev)
    asyncio.create_task(push_status(str(iid)))
    return {"ok": True, "container": cid}


@app.post("/api/instances/{iid}/pin/clear")
async def api_clear_pin(iid: str):
    """Delete the saved SIM PIN for a line. If it's running, stop it — the next start must
    re-run the PIN flow (the whole point of forgetting the PIN)."""
    inst = cfg.get_instance(iid)
    if not inst:
        raise HTTPException(404, "no such instance")
    had = cfg.clear_pin(str(iid))
    if await asyncio.to_thread(engine.is_running, str(iid)):
        await asyncio.to_thread(engine.stop, str(iid))
        await hub.drop_ami(str(iid))
        asyncio.create_task(push_status(str(iid)))
    return {"ok": True, "had_pin": had}


@app.post("/api/instances/{iid}/stop")
async def api_instance_stop(iid: str):
    # Cancel frozen cooldown intent before stopping. Otherwise a pending health recovery can
    # recreate the line after the user explicitly stopped it.
    hub.reset_health(iid)
    await asyncio.to_thread(engine.stop, iid)
    # Tear down the AMI client too — otherwise its Manager keeps auto-reconnecting to the
    # now-removed container (and floods a container that later reuses the docker IP).
    await hub.drop_ami(iid)
    hub.status_cache[str(iid)] = _with_status_activity(str(iid), {
        "state": "STOPPED", "label": status_mod.LABELS["STOPPED"],
        "reason_code": "stopped", "reason": "Stopped.", "detail": {}})
    hub.status_sampled_at[str(iid)] = time.monotonic()
    return {"ok": True}


@app.get("/api/instances/{iid}/status")
async def api_instance_status(iid: str):
    inst = cfg.get_instance(iid)
    if not inst:
        raise HTTPException(404, "no such instance")
    return _cached_line_status(inst)


def _availability_window(now: int, recorded_since: int | None) -> int:
    """How far back the chart reaches: as far as history goes, bounded on both sides."""
    span = (LINE_HISTORY_MIN_SECONDS if recorded_since is None
            else max(LINE_HISTORY_MIN_SECONDS, now - int(recorded_since)))
    return min(span, LINE_HISTORY_MAX_SECONDS)


@app.get("/api/instances/{iid}/availability")
async def api_instance_availability(iid: str):
    """VoWiFi connectivity history for one line, as a gap-aware up/down timeline."""
    inst = cfg.get_instance(str(iid))
    if not inst:
        raise HTTPException(404, "no such instance")
    now = int(time.time())
    recorded_since = await asyncio.to_thread(store.line_state_recorded_since, str(iid))
    span = _availability_window(now, recorded_since)
    start = now - span
    segments = await asyncio.to_thread(store.line_state_timeline, str(iid), start, now)
    return {"instance": str(iid), "start": start, "end": now, "span_seconds": span,
            "max_span_seconds": LINE_HISTORY_MAX_SECONDS,
            "recorded_since": int(recorded_since) if recorded_since is not None else None,
            "segments": segments, "summary": store.line_state_summary(segments)}


@app.get("/api/instances/{iid}/logs")
def api_instance_logs(iid: str, tail: int = 200):
    return {"engine": engine.logs(iid, tail),
            "charon": _read_run_text(iid, "charon.log", 200),
            # Survives container rebuilds, unlike the two above.
            "diagnostics": _read_log_text(iid, "diagnostics.jsonl", 50)}


def _read_run_text(iid, name, tail):
    return _read_instance_text(iid, "run", name, tail)


def _read_log_text(iid, name, tail):
    return _read_instance_text(iid, "logs", name, tail)


def _read_instance_text(iid, folder, name, tail):
    path = os.path.join(cfg.DATA_DIR, "instances", str(iid), folder, name)
    try:
        with open(path, errors="replace") as f:
            return "".join(f.readlines()[-tail:])
    except Exception:
        return ""


@app.post("/api/instances/{iid}/register")
async def api_instance_register(iid: str):
    return {"output": engine.exec_cli(iid, "pjsip send register volte_ims")}


# ----------------------------- SMS -----------------------------
@app.get("/api/instances/{iid}/messages/threads")
def api_threads(iid: str):
    return {"threads": store.list_threads(iid)}


@app.get("/api/instances/{iid}/messages/{peer}")
def api_messages(iid: str, peer: str):
    return {"messages": store.list_messages(iid, peer)}


@app.post("/api/instances/{iid}/messages/delete")
async def api_messages_delete(iid: str, body: dict):
    """Delete messages. Body: {ids:[...]} for specific messages, {peer:"..."} for a whole
    conversation, or {all:true} to wipe every message on the line. Broadcasts a refresh."""
    if body.get("all"):
        n = await asyncio.to_thread(store.clear_messages, iid)
    elif body.get("peer") is not None:
        n = await asyncio.to_thread(store.delete_thread, iid, body["peer"])
    elif body.get("ids"):
        n = await asyncio.to_thread(store.delete_messages, iid, body["ids"])
    else:
        raise HTTPException(400, "provide ids, peer, or all")
    await hub.broadcast({"type": "sms", "instance": str(iid), "deleted": n})
    return {"ok": True, "deleted": n}


SMS_RESP_RE = re.compile(r"Received SIP response")
# The patched (sysmocom) Asterisk logs the raw 3GPP RP PDU of every SMS it parses via
# res_pjsip_messaging.c parse_rpdata. For an MO SMS the SMSC returns an async RP-ACK / RP-ERROR
# "submit report" (an incoming application/vnd.3gpp.sms MESSAGE whose Call-ID is
# <our-outbound-Call-ID>:sm-submit-report) — THIS, not the SIP 202 Accepted, is the authoritative
# delivery verdict. Byte 0 low 3 bits = RP-MTI: 3 = RP-ACK (delivered), 5 = RP-ERROR (failed,
# followed by an RP-Cause). 1 = RP-DATA (a real inbound SMS) which we ignore here.
RPDATA_RE = re.compile(r"parse_rpdata:\s*SMS RP-DATA\s*'([0-9a-fA-F]+)'")
_RP_ACK_MTI = 3
_RP_ERROR_MTI = 5
# RP-Cause value (3GPP TS 24.011 §8.2.5.4, values per TS 24.008) -> human reason.
RP_CAUSE = {
    1: "unassigned/unallocated number", 8: "operator determined barring", 10: "call barred",
    11: "reserved", 21: "short message transfer rejected", 22: "memory capacity exceeded",
    27: "destination out of order", 28: "unidentified subscriber", 29: "facility rejected",
    30: "unknown subscriber", 38: "network out of order", 41: "temporary failure",
    42: "congestion", 47: "resources unavailable", 50: "requested facility not subscribed",
    69: "requested facility not implemented", 81: "invalid short message reference value",
    95: "invalid message", 96: "invalid mandatory information", 97: "message type non-existent",
    98: "message not compatible with SM protocol state", 99: "information element non-existent",
    111: "protocol error", 127: "interworking, unspecified",
}


def _decode_rp_report(pdu_hex: str) -> dict | None:
    """Decode an RP submit-report PDU (hex). Returns {ok, cause, reason} for an RP-ACK/RP-ERROR,
    or None when the PDU is not a submit report (e.g. RP-DATA, a real inbound SMS)."""
    try:
        b = bytes.fromhex(pdu_hex)
    except ValueError:
        return None
    if not b:
        return None
    mti = b[0] & 0x07
    if mti == _RP_ACK_MTI:
        return {"ok": True}
    if mti == _RP_ERROR_MTI:
        # octet0 MTI, octet1 msg-ref, octet2 RP-Cause IE length, octet3 cause value (bit8=ext).
        cause = (b[3] & 0x7f) if len(b) >= 4 else None
        reason = RP_CAUSE.get(cause, f"cause {cause}" if cause is not None else "delivery failed")
        return {"ok": False, "cause": cause, "reason": reason}
    return None


def detect_sms_result(iid: str, since=None) -> dict:
    """Determine the real MO SMS outcome. Two authoritative signals, checked in order:
      1. The SMSC's RP-ACK/RP-ERROR submit report (parse_rpdata) — the true delivery verdict.
      2. A SIP 4xx/5xx to our MESSAGE (IMS rejected it before the SMSC).
    A SIP 202/2xx is NOT success — the carrier accepts almost everything and reports the real
    result via the async RP submit report. Returns {ok: True|False|None, code?, reason?}."""
    raw = engine.logs(iid, 4000, since=since)
    raw = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    # 1. RP submit report (authoritative). Take the LAST ACK/ERROR seen in the window (our send's).
    for h in reversed(RPDATA_RE.findall(raw)):
        d = _decode_rp_report(h)
        if d is not None:
            if d["ok"]:
                return {"ok": True}
            return {"ok": False, "reason": d.get("reason", "delivery failed"),
                    "cause": d.get("cause")}
    # 2. Fall back to a negative SIP response to our MESSAGE.
    result = {"ok": None}
    for b in SMS_RESP_RE.split(raw)[1:]:
        m = re.search(r"SIP/2\.0 (\d{3})([^\n]*)", b)
        if not m:
            continue
        if re.search(r"CSeq:\s*\d+\s+MESSAGE", b):   # a response to our MESSAGE
            code = int(m.group(1))
            result = {"ok": 200 <= code < 300, "code": code, "reason": m.group(2).strip()}
    return result


async def _watch_sms_delivery(iid: str, mid: int, since: int, timeout: float = 40.0):
    """Asynchronously resolve an MO SMS's REAL delivery outcome after the IMS accepted it.
    The message is already stored as 'sent'; here we poll for the SMSC's RP submit report (or a
    SIP 4xx) and update the record to 'delivered' or 'failed' (+ reason), broadcasting each change
    so the open Messages view refreshes. On timeout the message stays 'sent' (accepted, delivery
    unconfirmed — e.g. Asterisk SMS debug off, or the network sent no report)."""
    iid = str(iid)
    loops = max(1, int(timeout // 2))
    for _ in range(loops):
        await asyncio.sleep(2)
        if not await asyncio.to_thread(engine.is_running, iid):
            return
        d = await asyncio.to_thread(detect_sms_result, iid, since)
        if d.get("ok") is True:
            store.set_message_status(mid, "delivered", None)
            await hub.broadcast({"type": "sms", "instance": iid,
                                 "message": {"id": mid, "status": "delivered",
                                             "direction": "out", "error": None}})
            return
        if d.get("ok") is False:
            reason = d.get("reason") or "unknown"
            code = d.get("code")
            err = (f"Carrier rejected the SMS: {reason}"
                   + (f" (SIP {code})" if code else "")).strip()
            store.set_message_status(mid, "failed", err)
            await hub.broadcast({"type": "sms", "instance": iid,
                                 "message": {"id": mid, "status": "failed",
                                             "direction": "out", "error": err}})
            return
    # no verdict within the window — leave as 'sent' (accepted, unconfirmed).


async def _send_sms_vowifi(iid: str, to: str, text: str,
                           ami: AmiClient | None = None) -> dict:
    """Submit one MO SMS through Asterisk/IMS and start its delivery watcher."""
    ami = ami or await hub.ami_for(iid)
    if not ami:
        return {"ok": False, "unavailable": True, "message": None,
                "error": "VoWiFi is not running / its control channel is unavailable.",
                "transport": "vowifi"}
    since = int(time.time())
    rec = store.add_message(iid, "out", to, text, status="pending", transport="vowifi")
    res = await ami.send_sms(to, text)

    if not res.get("ok"):
        # Asterisk itself refused to dispatch (endpoint down, bad address, etc.) — final failure.
        err = res.get("detail") or res.get("error") or "Send rejected by the line."
        store.set_message_status(rec["id"], "failed", err)
        rec["status"], rec["error"] = "failed", err
        await hub.broadcast({"type": "sms", "instance": str(iid), "message": rec})
        return {"ok": False, "message": rec, "error": err, "transport": "vowifi"}

    # IMS accepted the MESSAGE (SIP 202). That is NOT delivery confirmation — mark the message
    # 'sent' now and resolve the REAL outcome asynchronously from the SMSC's RP submit report,
    # flipping it to 'delivered' or 'failed' (+ reason) when it arrives. This keeps the send
    # snappy and stops the old false "success" on carrier/SMSC rejections.
    store.set_message_status(rec["id"], "sent", None)
    rec["status"], rec["error"] = "sent", None
    await hub.broadcast({"type": "sms", "instance": str(iid), "message": rec})
    asyncio.create_task(_watch_sms_delivery(iid, rec["id"], since))
    return {"ok": True, "message": rec, "error": None, "transport": "vowifi",
            "pending_delivery": True}


async def _registered_vowifi_ami(iid: str) -> AmiClient | None:
    """Return a sender only when IMS registration is confirmed before submission.

    This preflight is used solely by ``auto`` routing. If it cannot prove that VoWiFi is ready,
    no SMS has been attempted yet and selecting cellular is safe. Once either transport's send
    operation begins, ``auto`` never retries on the other transport: an action timeout may still
    mean that the first copy reached the SMSC.
    """
    ami = await hub.ami_for(iid)
    if not ami or not ami.connected:
        return None
    state = await ami.registration_state()
    return ami if state == "Registered" else None


async def _send_sms_cellular(iid: str, to: str, text: str) -> dict:
    """Submit one MO SMS through the physical modem managed by ModemManager."""
    instances = await asyncio.to_thread(cfg.list_instances)
    result = await asyncio.to_thread(
        cellular_sms.send, instances, iid, to, text, local_sms_tracker=store)
    reservation_id = result.pop("_reservation_id", None)
    if result.get("unavailable"):
        return {**result, "message": None}

    # ModemManager's successful ``Send`` means submitted, not handset delivery-confirmed.
    # A timeout is explicitly unknown and must remain visible as such; treating it as failed
    # encourages a retry that may create a duplicate and an extra roaming charge.
    message_status = ("sent" if result.get("ok") else
                      "unknown" if result.get("uncertain") else "failed")
    rec = (await asyncio.to_thread(store.local_modem_sms_message, reservation_id)
           if reservation_id is not None else None)
    if rec is None:
        rec = store.add_message(iid, "out", to, text, status=message_status,
                                transport="cellular")
    error = result.get("error")
    store.set_message_status(rec["id"], message_status, error)
    rec["status"], rec["error"] = message_status, error
    await hub.broadcast({"type": "sms", "instance": str(iid), "message": rec})
    return {**result, "message": rec, "transport": "cellular"}


async def send_sms_on_line(iid: str, to: str, text: str,
                           transport: str = "auto") -> dict:
    """Send one MO SMS using ``auto``, ``vowifi`` or ``cellular``.

    ``auto`` prefers a *confirmed registered* VoWiFi route. It selects cellular only before any
    VoWiFi submission has been attempted, and never retries across transports after an error or
    timeout because SMS has no cross-transport idempotency key.
    """
    iid, transport = str(iid), str(transport or "auto").lower()
    if transport not in {"auto", "vowifi", "cellular"}:
        return {"ok": False, "unavailable": True, "message": None,
                "error": "Unknown SMS transport; use auto, vowifi, or cellular."}

    lock = hub.sms_send_locks.setdefault(iid, asyncio.Lock())
    async with lock:
        if transport == "vowifi":
            return await _send_sms_vowifi(iid, to, text)
        if transport == "cellular":
            return await _send_sms_cellular(iid, to, text)

        ami = await _registered_vowifi_ami(iid)
        if ami:
            result = await _send_sms_vowifi(iid, to, text, ami=ami)
        else:
            result = await _send_sms_cellular(iid, to, text)
            if result.get("unavailable"):
                cellular_error = result.get("error") or "Cellular SMS is unavailable."
                result["error"] = f"VoWiFi is not registered. {cellular_error}"
        result["requested_transport"] = "auto"
        return result


@app.post("/api/instances/{iid}/sms/send")
async def api_sms_send(iid: str, body: dict):
    to = str((body or {}).get("to") or "").strip()
    text = (body or {}).get("body")
    transport = str((body or {}).get("transport") or "auto").lower()
    if not to or not isinstance(text, str) or not text:
        raise HTTPException(422, "recipient and non-empty message body are required")
    if transport not in {"auto", "vowifi", "cellular"}:
        raise HTTPException(422, "transport must be auto, vowifi, or cellular")
    result = await send_sms_on_line(iid, to, text, transport)
    if result.pop("unavailable", False):
        raise HTTPException(409, result["error"])
    return result


# ----------------------------- Allowance / balance -----------------------------
def _allowance_instance(iid: str) -> dict:
    inst = cfg.get_instance(str(iid))
    if not inst:
        raise HTTPException(404, "instance not found")
    return {**inst, "id": str(iid)}


def _allowance_rule(inst: dict) -> dict:
    return allowance.query_rule(inst, carrier_id.lookup(inst))


@app.get("/api/instances/{iid}/allowance")
def api_allowance(iid: str):
    _allowance_instance(iid)
    return {"allowance": allowance.reconcile(str(iid))}


@app.put("/api/instances/{iid}/allowance")
def api_allowance_save(iid: str, body: dict):
    _allowance_instance(iid)
    try:
        values = allowance.clean_allowance(body or {})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"allowance": store.save_allowance(str(iid), values, source="manual")}


@app.get("/api/instances/{iid}/allowance/query-rule")
def api_allowance_query_rule(iid: str):
    return {"rule": _allowance_rule(_allowance_instance(iid))}


@app.put("/api/instances/{iid}/allowance/query-rule")
def api_allowance_query_rule_save(iid: str, body: dict):
    inst = _allowance_instance(iid)
    try:
        recipient, text = allowance.validate_rule((body or {}).get("recipient"),
                                                   (body or {}).get("body"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    store.save_allowance_query_rule(str(iid), recipient, text)
    return {"rule": _allowance_rule(inst)}


@app.delete("/api/instances/{iid}/allowance/query-rule")
def api_allowance_query_rule_reset(iid: str):
    inst = _allowance_instance(iid)
    store.delete_allowance_query_rule(str(iid))
    return {"rule": _allowance_rule(inst)}


@app.post("/api/instances/{iid}/allowance/query")
async def api_allowance_query(iid: str, body: dict):
    inst = _allowance_instance(iid)
    rule = _allowance_rule(inst)
    effective = rule.get("effective")
    if not effective:
        raise HTTPException(409, "allowance query method is unknown; configure it in Messages")
    transport = str((body or {}).get("transport") or "auto").lower()
    if transport not in {"auto", "vowifi", "cellular"}:
        raise HTTPException(422, "transport must be auto, vowifi, or cellular")
    query = store.start_allowance_query(
        str(iid), effective["recipient"], effective["body"],
        rule.get("carrier_key") or "", transport)
    result = await send_sms_on_line(str(iid), effective["recipient"],
                                    effective["body"], transport)
    if result.get("unavailable"):
        store.set_allowance_query_status(query["id"], "failed")
        raise HTTPException(409, result.get("error") or "SMS transport unavailable")
    store.set_allowance_query_status(
        query["id"], "sent" if result.get("ok") else
        "unknown" if result.get("uncertain") else "failed")
    return {"ok": bool(result.get("ok")), "query": query, "rule": rule,
            "send": result}


# ----------------------------- Calls -----------------------------
@app.get("/api/instances/{iid}/calls")
def api_calls(iid: str):
    return {"calls": store.list_calls(iid)}


@app.post("/api/instances/{iid}/calls/delete")
async def api_calls_delete(iid: str, body: dict):
    """Delete call-log entries. Body: {ids:[...]} for specific calls or {all:true} to clear
    the whole log. Broadcasts a refresh so open Softphone views reload the list."""
    if body.get("all"):
        n = await asyncio.to_thread(store.clear_calls, iid)
    elif body.get("ids"):
        n = await asyncio.to_thread(store.delete_calls, iid, body["ids"])
    else:
        raise HTTPException(400, "provide ids or all")
    await hub.broadcast({"type": "call", "instance": str(iid), "deleted": n})
    return {"ok": True, "deleted": n}


async def place_call_on_line(iid: str, to: str, from_endpoint: str = "webrtc") -> dict:
    """Ring the browser endpoint and bridge it to `to` over IMS, logging the call."""
    ami = await hub.ami_for(iid)
    if not ami:
        return {"ok": False, "unavailable": True, "error": "instance not running"}
    res = await ami.originate(to, from_endpoint)
    store.add_call(iid, "out", to, status="ringing")
    return res


async def hangup_on_line(iid: str) -> dict:
    ami = await hub.ami_for(iid)
    if not ami:
        return {"ok": False, "unavailable": True, "error": "instance not running"}
    return await ami.hangup_all()


@app.post("/api/instances/{iid}/call")
async def api_call(iid: str, body: dict):
    result = await place_call_on_line(iid, body["to"], body.get("from_endpoint", "webrtc"))
    if result.pop("unavailable", False):
        raise HTTPException(409, result["error"])
    return result


@app.post("/api/instances/{iid}/hangup")
async def api_hangup(iid: str):
    result = await hangup_on_line(iid)
    if result.pop("unavailable", False):
        raise HTTPException(409, result["error"])
    return result


def _cellular_call_result_status(value: str) -> tuple[str, bool]:
    state = str(value or "").casefold()
    if state == "active":
        return "answered", False
    if state in {"dialing", "ringing-out"}:
        return "ringing", False
    if state in {"terminated", "ended", "idle"}:
        return "ended", True
    if state == "unknown":
        return "unknown", False
    return state or "unknown", False


def _sync_cellular_call_record(iid: str, state: str) -> dict | None:
    rec = store.get_open_call_for_transport(str(iid), "cellular")
    if not rec:
        return None
    status, ended = _cellular_call_result_status(state)
    store.update_call(rec["id"], status, ended=ended)
    rec["status"] = status
    if ended:
        rec["end_ts"] = int(time.time())
    return rec


@app.post("/api/instances/{iid}/cellular-call")
async def api_cellular_call(iid: str, body: dict):
    if not cfg.get_instance(str(iid)):
        raise HTTPException(404, "instance not found")
    number = str((body or {}).get("to") or "").strip()
    result = await asyncio.to_thread(
        cellular_call.dial, cfg.list_instances(), str(iid), number)
    if result.pop("unavailable", False):
        raise HTTPException(409, result.get("error") or "Cellular calling is unavailable")
    if result.get("ok") or result.get("uncertain"):
        rec = store.add_call(str(iid), "out", number,
                             status="unknown" if result.get("uncertain") else "ringing",
                             transport="cellular")
        result["record"] = rec
        await hub.broadcast({"type": "call", "instance": str(iid), "call": rec})
    return result


@app.get("/api/instances/{iid}/cellular-call/status")
async def api_cellular_call_status(iid: str):
    if not cfg.get_instance(str(iid)):
        raise HTTPException(404, "instance not found")
    result = await asyncio.to_thread(
        cellular_call.status, cfg.list_instances(), str(iid))
    if not result.get("unavailable"):
        rec = _sync_cellular_call_record(str(iid), result.get("status") or "")
        if rec:
            result["record"] = rec
    return result


@app.post("/api/instances/{iid}/cellular-call/hangup")
async def api_cellular_call_hangup(iid: str):
    if not cfg.get_instance(str(iid)):
        raise HTTPException(404, "instance not found")
    result = await asyncio.to_thread(
        cellular_call.hangup, cfg.list_instances(), str(iid))
    if result.pop("unavailable", False):
        raise HTTPException(409, result.get("error") or "Cellular calling is unavailable")
    if result.get("ok"):
        rec = _sync_cellular_call_record(str(iid), "ended")
        if rec:
            result["record"] = rec
            await hub.broadcast({"type": "call", "instance": str(iid), "call": rec})
    return result


@app.get("/api/instances/{iid}/softphone")
def api_softphone(iid: str, request: Request):
    """Provisioning for the browser softphone (JsSIP over WSS)."""
    inst = cfg.get_instance(iid)
    if not inst:
        raise HTTPException(404, "no such instance")
    sip = inst.get("sip", {}) or {}
    wr = sip.get("webrtc", {}) or {}
    ports = inst.get("ports", {})
    host = (request.headers.get("host") or "").split(":")[0] or request.url.hostname
    return {
        "enabled": bool(wr.get("enable", True)),
        "username": wr.get("username", "webrtc"),
        "password": wr.get("password", ""),
        "ws_port": ports.get("webrtc", 8089),
        "host": host,
        "realm": cfg.ims_realm(inst["mcc"], inst["mnc"]),
    }


# ----------------------------- engine event hook -----------------------------
def _call_disposition(dialstatus: str, cause: int, direction: str = "out") -> str:
    """Map Asterisk DIALSTATUS + Q.850 hangupcause to a friendly outcome. No retry — a
    rejected/busy/no-answer call is simply recorded as such. Incoming and outgoing read the
    same DIALSTATUS differently: for an inbound call the Dial targets our local softphone, so
    BUSY/decline means WE declined and CANCEL/NOANSWER means we missed it."""
    if dialstatus == "ANSWER":
        return "answered"
    if direction == "in":
        if dialstatus == "BUSY" or cause == 21:
            return "rejected"        # local softphone actively declined
        return "missed"              # remote hung up first, no answer, or rang out
    # outgoing
    if cause == 21:                     # 603 Decline — far end actively rejected
        return "rejected"
    if cause == 17 or dialstatus == "BUSY":
        return "busy"
    if dialstatus == "NOANSWER" or cause == 19:
        return "no answer"
    if dialstatus == "CANCEL":
        return "cancelled"
    if dialstatus in ("CONGESTION", "CHANUNAVAIL"):
        return "failed"
    # empty DIALSTATUS in the hangup handler => caller hung up before/while dialing.
    return (dialstatus.lower() if dialstatus else "cancelled")


@app.post("/api/engine/event")
async def api_engine_event(payload: dict):
    """Receives notify.py callbacks from engine containers."""
    iid = str(payload.get("instance", ""))
    event = payload.get("event", "")
    args = payload.get("args", [])
    if event == "sms_in" and len(args) >= 2:
        try:
            text = base64.b64decode(args[1]).decode(errors="replace")
        except Exception:
            text = args[1]
        sender = args[0] or ""
        # Drop inbound MESSAGEs that carry NO human-readable text (empty/whitespace body). Two
        # real sources produce these, and neither is a text the user should see:
        #   1. IMS-internal signalling: the carrier's IP-SM-GW / SMSC sends non-user MESSAGEs
        #      whose From is a bare private-IP SIP URI (e.g. <sip:10.183.150.10>).
        #   2. Binary / SIM-targeted SMS: OTA "SIM data-download" messages (3GPP TS 23.040
        #      TP-DCS 0xF6 = 8-bit, message-class 2) and other non-text PDUs — Asterisk decodes
        #      their user-data to an empty string because there is no displayable text (seen from
        #      short-codes like 20023). These are operator/service payloads for the SIM, not texts.
        # A genuine text always has a non-empty decoded body, so dropping on empty-body never
        # loses a real message. (An empty body with a normal sender is likewise nothing to show.)
        if not text.strip():
            log.info("dropping empty-body inbound SMS (internal signalling / binary/OTA "
                     "SIM message — no displayable text)")
            return {"ok": True, "dropped": "empty_body"}
        rec = store.add_message(iid, "in", sender, text)
        await hub.broadcast({"type": "sms", "instance": iid, "message": rec})
        _dispatch_push(notify_push.EV_INCOMING_SMS, iid, sender, text)
    elif event == "sms_out" and len(args) >= 2:
        pass  # already stored by the send path
    elif event == "call_in":
        # Log inbound calls even when the caller withholds/omits their number (peer "") so an
        # anonymous call still gets a record that the 'h' disposition can finalize. The IMS
        # delivers one INVITE several times (VoLTE preconditions / GRUU fork / retransmit),
        # firing call_in more than once per call — both while the record is still open AND as a
        # trailing retransmit a few seconds AFTER it was finalized. add_call_deduped coalesces
        # both into the single record so no ghost 'ringing' row is left behind.
        peer = args[0] if args else ""
        rec = store.add_call_deduped(iid, "in", peer, status="ringing")
        await hub.broadcast({"type": "call", "instance": iid, "call": rec})
        # Push-notify ONCE per real inbound call. IMS re-delivers call_in several times for
        # one call (VoLTE preconditions / GRUU fork / retransmit); add_call_deduped folds
        # them into a single record, so key the notification on that record id. An anonymous
        # first event ('') whose number arrives on a later duplicate would push before the
        # number is known — so only notify once we have the peer, or after ~4s if it stays
        # anonymous (caller genuinely withheld it).
        cid = rec.get("id")
        if cid is not None and cid not in hub._pushed_calls:
            if peer or int(time.time()) - int(rec.get("start_ts", 0)) >= 4:
                hub._pushed_calls.add(cid)
                if len(hub._pushed_calls) > 512:      # bound the dedupe set
                    hub._pushed_calls = set(list(hub._pushed_calls)[-256:])
                _dispatch_push(notify_push.EV_INCOMING_CALL, iid, rec.get("peer") or peer)
    elif event == "call_out" and args:
        rec = store.add_call(iid, "out", args[0], status="dialing")
        await hub.broadcast({"type": "call", "instance": iid, "call": rec})
    elif event == "call_result" and args:
        # New form: call_result <direction> <peer> <dialstatus> <cause> (fired from the 'h'
        # hangup handler for BOTH directions). Legacy form: call_result <peer> <dialstatus>
        # <cause> (outgoing only) — kept for engines running an older dialplan.
        if args[0] in ("in", "out"):
            direction = args[0]
            to = args[1] if len(args) > 1 else ""
            dialstatus = (args[2] if len(args) > 2 else "").upper()
            cause = int(args[3]) if len(args) > 3 and str(args[3]).isdigit() else 0
        else:
            direction = "out"
            to = args[0]
            dialstatus = (args[1] if len(args) > 1 else "").upper()
            cause = int(args[2]) if len(args) > 2 and str(args[2]).isdigit() else 0
        disp = _call_disposition(dialstatus, cause, direction)
        rec = store.update_last_call(iid, direction, to, disp)
        if not rec and to:
            # exact peer didn't match an open record (e.g. 'h' lost the number to a
            # masquerade and call_out stored a different form) — finalize the latest open
            # call of this direction instead so it never stays stuck on dialing/ringing.
            rec = store.update_last_call(iid, direction, None, disp)
        if rec:
            await hub.broadcast({"type": "call", "instance": iid, "call": rec})
    elif event == "cp_mode_resolved" and args:
        # CP auto-discovery success: the engine found the address family (v6/v4/dual) that yields a
        # usable PDN on this carrier. Repin the line from 'auto' to the resolved family so it stops
        # re-walking the ladder on future starts (fast, deterministic), and record that it was
        # auto-detected. Only acts on an auto line; a pinned line ignores a stray report.
        resolved = (args[0] or "").strip().lower()
        if resolved in ("v6", "v4", "dual"):
            inst = cfg.get_instance(iid)
            if inst and cfg.normalize_cp_mode(inst.get("cp_mode", "")) == "auto":
                try:
                    cfg.upsert_instance({"id": iid, "cp_mode": resolved, "cp_mode_source": "auto"})
                    log.info("instance %s: CP auto-discovery resolved to %s (repinned)", iid, resolved)
                except Exception as e:  # noqa
                    log.warning("cp_mode_resolved persist failed for %s: %r", iid, e)
            await hub.broadcast({"type": "engine", "instance": iid, "event": event, "args": args})
    else:
        await hub.broadcast({"type": "engine", "instance": iid, "event": event, "args": args})
    # real-time: any tunnel/registration transition triggers an immediate status push
    if event in ("tunnel_up", "tunnel_down", "pcscf", "registered", "unregistered"):
        asyncio.create_task(push_status(iid))
    return {"ok": True}


async def push_status(iid: str):
    """Compute + broadcast status for a single instance immediately (event-driven)."""
    inst = cfg.get_instance(iid)
    if not inst:
        return
    try:
        runtime = await hub.runtime.get(iid)
        ami = await hub.ami_for(iid, runtime)
        st = await status_mod.compute(inst, ami, runtime)
        st = _with_status_activity(
            iid, apply_health(iid, inst, st, runtime.get("container_id")))
        hub.status_cache[str(iid)] = st
        hub.status_sampled_at[str(iid)] = time.monotonic()
        await hub.broadcast({"type": "status", "instance": str(iid), **st})
    except Exception as e:  # noqa
        log.debug("push_status error: %r", e)


def _dispatch_push(event: str, iid: str, source: str, text: str | None = None):
    """Fire outbound push notifications for an incoming event, off the
    event path so a slow endpoint can't stall engine-event handling. No-op unless a channel
    is enabled for this event."""
    inst = cfg.get_instance(iid)
    if not inst:
        return
    settings = cfg.get_settings()
    wh = settings.get("webhook") or {}
    tg = settings.get("telegram") or {}
    pp = settings.get("pushplus") or {}
    if not (wh.get("enabled") or tg.get("enabled") or pp.get("enabled")):
        return
    asyncio.create_task(
        asyncio.to_thread(notify_push.dispatch, settings, event, inst, source, text))




# ----------------------------- eSIM / LPA (lpac) -----------------------------
@app.get("/api/esim/status")
async def api_esim_status():
    """Whether lpac is installed and basic settings."""
    settings = cfg.get_settings().get("esim") or {}
    bin_path = lpa.lpac_bin()
    return {
        "available": lpa.lpac_available(),
        "lpac_bin": bin_path,
        "download_timeout": int(settings.get("download_timeout") or 300),
        "auto_process_notifications": bool(settings.get("auto_process_notifications", True)),
        "busy_readers": list(hub.lpa_busy.keys()),
    }


# ---------------------------------------------------------------- eSIM chip cache
# Last successful chip read per eUICC (keyed by EID), persisted in the data dir so every
# browser/session can show the profile list — and switch profiles — without stopping a
# running line for a fresh exclusive read. Entries are matched to the inserted card via the
# ICCIDs of their profiles (the card monitor reads the active ICCID without exclusivity).
_ESIM_CACHE_PATH = os.path.join(cfg.DATA_DIR, "esim-chip-cache.json")


def _esim_cache_load() -> dict:
    doc = _read_json_file(_ESIM_CACHE_PATH)
    return doc if isinstance(doc, dict) else {}


def _esim_cache_write(data: dict):
    tmp = _ESIM_CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _ESIM_CACHE_PATH)


def _esim_cache_store(ses: list, imei: str):
    eid = next((str(se.get("eid")) for se in ses if se.get("eid")), "")
    # Only a fully successful read may overwrite the cache — a partial/failed load would
    # replace a good profile list with an empty one.
    if not eid or any(se.get("error") for se in ses):
        return
    data = _esim_cache_load()
    data[eid] = {"ses": ses, "imei": imei or "", "ts": int(time.time())}
    _esim_cache_write(data)


def _esim_cache_for_iccid(iccid: str) -> dict | None:
    if not iccid:
        return None
    for entry in _esim_cache_load().values():
        for se in entry.get("ses") or []:
            if any(p.get("iccid") == iccid for p in (se.get("profiles") or [])):
                return entry
    return None


def _esim_cache_update_profile(iccid: str, *, state: str | None = None,
                               nickname: str | None = None, remove: bool = False):
    """Mirror a successful enable/disable/delete/nickname onto the cached view."""
    data = _esim_cache_load()
    changed = False
    for entry in data.values():
        for se in entry.get("ses") or []:
            profiles = se.get("profiles") or []
            hit = next((p for p in profiles if p.get("iccid") == iccid), None)
            if hit is None:
                continue
            if remove:
                se["profiles"] = [p for p in profiles if p.get("iccid") != iccid]
            else:
                if state == "enabled":
                    for p in profiles:
                        if str(p.get("profileState") or "").lower() == "enabled":
                            p["profileState"] = "disabled"
                if state is not None:
                    hit["profileState"] = state
                if nickname is not None:
                    hit["profileNickname"] = nickname
            changed = True
    if changed:
        _esim_cache_write(data)


@app.get("/api/esim/chip/cached")
async def api_esim_chip_cached(reader_index: int = 0, reader: str | None = None):
    """Cached chip view for the card in this reader — never touches the card, so it is safe
    while a VoWiFi line holds the reader."""
    name, idx = await asyncio.to_thread(_esim_resolve_reader, reader_index, reader)
    iccid = str((hub.cards.get(name) or {}).get("iccid") or "")
    entry = await asyncio.to_thread(_esim_cache_for_iccid, iccid)
    if not entry:
        return {"ok": True, "cached": False, "reader": name, "reader_index": idx}
    return {"ok": True, "cached": True, "reader": name, "reader_index": idx,
            "ses": entry.get("ses") or [], "imei": entry.get("imei") or "",
            "ts": entry.get("ts") or 0}


@app.get("/api/esim/chip")
async def api_esim_chip(reader_index: int = 0, reader: str | None = None):
    """Load chip info for every SE on the card (dual SE → two entries)."""
    name, idx = await asyncio.to_thread(_esim_resolve_reader, reader_index, reader)
    running = await asyncio.to_thread(_find_running_by_reader, name)
    payload = await _esim_run(name, idx, lpa.load_all_ses(name, idx))
    ses = payload.get("ses") or []
    await asyncio.to_thread(_esim_cache_store, ses, _esim_imei_for_reader(name))
    # Backward-compatible single-chip view = first SE that loaded successfully.
    primary = next((s for s in ses if s.get("chip")), ses[0] if ses else None)
    return {
        "ok": True,
        "reader": name,
        "reader_index": idx,
        "dual": bool(payload.get("dual")),
        "ses": ses,
        "chip": (primary or {}).get("chip"),
        "imei": _esim_imei_for_reader(name),
        "line_running": bool(running),
        "matched_instance": running["id"] if running else (hub.cards.get(name) or {}).get("matched"),
    }


@app.get("/api/esim/profiles")
async def api_esim_profiles(reader_index: int = 0, reader: str | None = None):
    """List profiles grouped per SE (same load as chip — prefer /api/esim/chip for full view)."""
    name, idx = await asyncio.to_thread(_esim_resolve_reader, reader_index, reader)
    running = await asyncio.to_thread(_find_running_by_reader, name)
    payload = await _esim_run(name, idx, lpa.load_all_ses(name, idx))
    ses = payload.get("ses") or []
    flat = []
    for se in ses:
        flat.extend(se.get("profiles") or [])
    return {
        "ok": True,
        "reader": name,
        "reader_index": idx,
        "dual": bool(payload.get("dual")),
        "ses": ses,
        "profiles": flat,
        "imei": _esim_imei_for_reader(name),
        "line_running": bool(running),
        "matched_instance": running["id"] if running else (hub.cards.get(name) or {}).get("matched"),
        "lpa_busy": bool(hub.lpa_busy.get(name)),
    }


@app.post("/api/esim/profiles/{iccid}/enable")
async def api_esim_enable(iccid: str, body: dict | None = None):
    body = body or {}
    name, idx = await asyncio.to_thread(
        _esim_resolve_reader, body.get("reader_index", 0), body.get("reader"))
    se = await asyncio.to_thread(
        _esim_resolve_se, name, idx, body.get("se_id") or body.get("seId"), body.get("aid"),
        require=True)
    await _esim_run(
        name, idx, lpa.profile_enable(name, iccid, aid=se.get("aid")), refresh=True)
    await asyncio.to_thread(_esim_cache_update_profile, iccid, state="enabled")
    return {"ok": True, "iccid": iccid, "se_id": se["id"], "card": hub.cards.get(name)}


@app.post("/api/esim/profiles/{iccid}/disable")
async def api_esim_disable(iccid: str, body: dict | None = None):
    body = body or {}
    name, idx = await asyncio.to_thread(
        _esim_resolve_reader, body.get("reader_index", 0), body.get("reader"))
    se = await asyncio.to_thread(
        _esim_resolve_se, name, idx, body.get("se_id") or body.get("seId"), body.get("aid"),
        require=True)
    await _esim_run(
        name, idx, lpa.profile_disable(name, iccid, aid=se.get("aid")), refresh=True)
    await asyncio.to_thread(_esim_cache_update_profile, iccid, state="disabled")
    return {"ok": True, "iccid": iccid, "se_id": se["id"], "card": hub.cards.get(name)}


@app.delete("/api/esim/profiles/{iccid}")
async def api_esim_delete(
    iccid: str, reader_index: int = 0, reader: str | None = None,
    se_id: str | None = None, aid: str | None = None,
):
    name, idx = await asyncio.to_thread(_esim_resolve_reader, reader_index, reader)
    se = await asyncio.to_thread(_esim_resolve_se, name, idx, se_id, aid, require=True)
    await _esim_run(
        name, idx, lpa.profile_delete(name, iccid, aid=se.get("aid")), refresh=True)
    await asyncio.to_thread(_esim_cache_update_profile, iccid, remove=True)
    return {"ok": True, "iccid": iccid, "se_id": se["id"]}


@app.post("/api/esim/profiles/{iccid}/nickname")
async def api_esim_nickname(iccid: str, body: dict):
    name, idx = await asyncio.to_thread(
        _esim_resolve_reader, body.get("reader_index", 0), body.get("reader"))
    se = await asyncio.to_thread(
        _esim_resolve_se, name, idx, body.get("se_id") or body.get("seId"), body.get("aid"),
        require=True)
    nick = body.get("nickname", "")
    await _esim_run(
        name, idx, lpa.profile_nickname(name, iccid, nick, aid=se.get("aid")))
    await asyncio.to_thread(_esim_cache_update_profile, iccid, nickname=nick)
    return {"ok": True, "iccid": iccid, "nickname": nick, "se_id": se["id"]}


@app.post("/api/esim/download")
async def api_esim_download(body: dict):
    """Start a profile download as a background task; progress via WS type=esim_download."""
    name, idx = await asyncio.to_thread(
        _esim_resolve_reader, body.get("reader_index", 0), body.get("reader"))
    se = await asyncio.to_thread(
        _esim_resolve_se, name, idx, body.get("se_id") or body.get("seId"), body.get("aid"),
        require=True)
    if hub.lpa_busy.get(name):
        raise HTTPException(409, "an eSIM operation is already running on this reader")
    await asyncio.to_thread(_esim_guard_engine, name)
    imei = _esim_imei_for_reader(name, body.get("imei"))
    # Claim busy before returning so a second concurrent POST cannot start another job.
    hub.lpa_busy[name] = True
    se_id = se["id"]
    aid = se.get("aid")

    async def _job():
        try:
            async with hub.reader_lock(name):
                try:
                    await hub.broadcast({
                        "type": "esim_download", "reader": name, "reader_index": idx,
                        "se_id": se_id, "event": "started", "step": "started", "imei": imei,
                    })

                    async def on_progress(event):
                        # lpa.run_lpac passes {"step", "data", "code"}
                        step = (event or {}).get("step") or ""
                        data = (event or {}).get("data")
                        msg = {
                            "type": "esim_download", "reader": name, "reader_index": idx,
                            "se_id": se_id, "event": "progress", "step": step,
                        }
                        if isinstance(data, dict):
                            msg["metadata"] = data
                            msg["data"] = data
                        elif data is not None:
                            msg["data"] = data
                        if step == "es8p_metadata_parse" and isinstance(data, dict):
                            msg["event"] = "preview"
                        await hub.broadcast(msg)

                    result = await lpa.download(
                        name,
                        activation_code=body.get("activation_code"),
                        smdp=body.get("smdp"),
                        matching_id=body.get("matching_id"),
                        confirmation_code=body.get("confirmation_code"),
                        imei=imei or None,
                        aid=aid,
                        on_progress=on_progress,
                    )
                    await _esim_refresh_card(name, idx)
                    await hub.broadcast({
                        "type": "esim_download", "reader": name, "reader_index": idx,
                        "se_id": se_id, "event": "completed", "step": "completed",
                        "result": result, "card": hub.cards.get(name),
                    })
                except lpa.LpaError as e:
                    # lpac puts the failing function name in message (e.g. es9p_authenticate_client).
                    err = {
                        "type": "esim_download", "reader": name, "reader_index": idx,
                        "se_id": se_id, "event": "error",
                        "step": (e.message or "").strip() or None,
                        "error": e.user_message(),
                    }
                    await hub.broadcast(err)
                except Exception as e:  # noqa
                    log.exception("esim download failed")
                    await hub.broadcast({
                        "type": "esim_download", "reader": name, "reader_index": idx,
                        "se_id": se_id, "event": "error", "error": str(e),
                    })
        finally:
            hub.lpa_busy.pop(name, None)

    asyncio.create_task(_job())
    return {
        "ok": True, "started": True, "reader": name, "reader_index": idx,
        "se_id": se_id, "imei": imei,
    }


@app.post("/api/esim/download/cancel")
async def api_esim_download_cancel(body: dict | None = None):
    body = body or {}
    name, _idx = await asyncio.to_thread(
        _esim_resolve_reader, body.get("reader_index", 0), body.get("reader"))
    cancelled = lpa.cancel_download(name)
    if cancelled:
        await hub.broadcast({
            "type": "esim_download", "reader": name,
            "event": "cancelling", "step": "cancelling",
        })
    return {"ok": True, "cancelled": cancelled}


@app.post("/api/esim/discovery")
async def api_esim_discovery(body: dict | None = None):
    body = body or {}
    name, idx = await asyncio.to_thread(
        _esim_resolve_reader, body.get("reader_index", 0), body.get("reader"))
    se = await asyncio.to_thread(
        _esim_resolve_se, name, idx, body.get("se_id") or body.get("seId"), body.get("aid"),
        require=True)
    imei = _esim_imei_for_reader(name, body.get("imei"))
    entries = await _esim_run(
        name, idx,
        lpa.discovery(name, imei=imei or None, smds=body.get("smds"), aid=se.get("aid")))
    return {
        "ok": True, "reader": name, "se_id": se["id"],
        "entries": entries or [], "imei": imei,
    }


@app.get("/api/esim/notifications")
async def api_esim_notifications(reader_index: int = 0, reader: str | None = None):
    name, idx = await asyncio.to_thread(_esim_resolve_reader, reader_index, reader)
    payload = await _esim_run(name, idx, lpa.load_all_ses(name, idx))
    ses = payload.get("ses") or []
    flat = []
    for se in ses:
        flat.extend(se.get("notifications") or [])
    return {
        "ok": True, "reader": name, "dual": bool(payload.get("dual")),
        "ses": ses, "notifications": flat,
    }


@app.post("/api/esim/notifications/process")
async def api_esim_notifications_process(body: dict | None = None):
    body = body or {}
    name, idx = await asyncio.to_thread(
        _esim_resolve_reader, body.get("reader_index", 0), body.get("reader"))
    se = await asyncio.to_thread(
        _esim_resolve_se, name, idx, body.get("se_id") or body.get("seId"), body.get("aid"),
        require=True)
    seq = body.get("seq")
    remove = bool(body.get("remove", True))
    if seq is None:
        coro = lpa.notification_process(
            name, all_notifications=True, autoremove=remove, aid=se.get("aid"))
    else:
        coro = lpa.notification_process(
            name, int(seq), autoremove=remove, aid=se.get("aid"))
    await _esim_run(name, idx, coro)
    return {"ok": True, "se_id": se["id"]}


@app.delete("/api/esim/notifications/{seq}")
async def api_esim_notification_remove(
    seq: int, reader_index: int = 0, reader: str | None = None,
    se_id: str | None = None, aid: str | None = None,
):
    name, idx = await asyncio.to_thread(_esim_resolve_reader, reader_index, reader)
    se = await asyncio.to_thread(_esim_resolve_se, name, idx, se_id, aid, require=True)
    await _esim_run(name, idx, lpa.notification_remove(name, seq, aid=se.get("aid")))
    return {"ok": True, "seq": seq, "se_id": se["id"]}


# ----------------------------- WebSocket -----------------------------
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # Accept before the application-level close so browsers receive code 4401 instead of
    # treating the rejected handshake as an opaque HTTP 403 and reconnecting forever.
    await ws.accept()
    if not auth.session(ws.cookies.get(auth.SESSION_COOKIE)):
        if ws.query_params.get("auth_close") == "1":
            await ws.close(code=4401)
        else:
            # A tab loaded before this fix does not understand 4401 and reconnects every two
            # seconds after any close. Keep that unauthenticated legacy socket out of the hub
            # but quietly open until the user reloads or closes the tab.
            try:
                await ws.receive_text()
            except Exception:
                pass
        return
    hub.clients.add(ws)
    try:
        while True:
            await ws.receive_text()  # keepalive / ignore inbound
    except WebSocketDisconnect:
        hub.clients.discard(ws)
    except Exception:
        hub.clients.discard(ws)


# ----------------------------- static WebUI -----------------------------
if os.path.isdir(WEBUI_DIR):
    app.mount("/assets", StaticFiles(directory=os.path.join(WEBUI_DIR, "assets")), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        # Unknown API paths must stay real 404s. Returning index.html here makes clients parse a
        # missing endpoint as a successful empty feature (and hid the removed stack API).
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse({"detail": "API endpoint not found"}, status_code=404)
        candidate = os.path.join(WEBUI_DIR, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        index = os.path.join(WEBUI_DIR, "index.html")
        if os.path.isfile(index):
            return FileResponse(index)
        return JSONResponse({"error": "webui not built"}, status_code=404)
