#!/usr/bin/env python3
"""Private D-Bus and ModemManager supervisor for the Hardware container.

DSM does not run udev and therefore has no udev database. ModemManager's
supported no-udev test interface is used to report only modem-related kernel
objects. NetworkManager is configured fail-closed for wwan/cdc-wdm devices;
every other NAS interface is unmanaged and every cellular profile is
non-autoconnecting and never-default.
"""
import argparse
import collections
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

try:
    import serial
except ImportError:  # the image inherits pyserial from Control; tests may not have it
    serial = None


EVENT_ROOTS = (
    ("tty", Path("/sys/class/tty"), re.compile(r"tty(?:USB|ACM)\d+")),
    ("usbmisc", Path("/sys/class/usbmisc"), re.compile(r"cdc-wdm\d+")),
    ("net", Path("/sys/class/net"), re.compile(r"wwan\d+")),
)
# (vid, pid, AT interface, display name), the same default as control/app/config.py.
DEFAULT_MODEM_PROFILES = (("2c7c", "0125", 2, "DJI/Quectel EC25"),)
BASE_VPCD_PORT = 0x3C00
VPCD_PORT_STRIDE = 0x100
VPCD_SLOTS = 3
# Support bundles are the only consumer of host-diagnostics.json and none of them check
# its age, while producing it means a seek-read of every bridge log plus a write of a few
# tens of kilobytes. There is no reason to pay that on every reconcile pass.
DIAGNOSTICS_INTERVAL = 15
# A reconcile pass forks mmcli and nmcli several times per modem, which on a Raspberry Pi
# cost about a fifth of a core at the old fixed 3 s cadence. Once nothing is in flight the
# pass runs this often instead, while a cheap check every WAKE_CHECK_SECONDS starts the next
# pass at once when hardware, the requested state or a bridge changes. The health check wants
# a status younger than 15 s, so this plus one pass must stay well inside that.
IDLE_INTERVAL = 8.0
WAKE_CHECK_SECONDS = 0.5
# How long a present modem may go without a ModemManager object before it is reset over
# its AT port. ModemManager's own probing of a freshly enumerated EC25 finishes well inside
# a minute, and a timed-out QMI port is only declared invalid after ten attempts, so two
# minutes does not race a probe that is still going to succeed.
UNCLAIMED_RESET_GRACE = 120
# Shared by both firmware-reset paths: a reset re-enumerates the modem, which takes
# ModemManager tens of seconds to probe again; resetting faster than that only restarts it.
MODEM_RESET_INTERVAL = 300


def tail_lines(path, count=25, max_bytes=128 * 1024):
    """Read a bounded tail; VPCD logs are append-only and may be very large."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read(max_bytes)
        lines = data.decode("utf-8", errors="replace").splitlines()
        if size > max_bytes and lines:
            lines = lines[1:]
        return [line for line in lines if line.strip()][-count:]
    except OSError:
        return []


def compact_log(path, limit=4 * 1024 * 1024, keep=128 * 1024):
    """Bound an append-only bridge log while preserving its useful diagnostic tail."""
    try:
        if path.stat().st_size <= limit:
            return
        with path.open("rb") as handle:
            handle.seek(-min(keep, path.stat().st_size), os.SEEK_END)
            tail = handle.read()
        with path.open("wb") as handle:
            handle.write(tail)
    except OSError:
        pass


def kernel_objects():
    return {(subsystem, path.name)
            for subsystem, root, pattern in EVENT_ROOTS
            for path in root.glob("*") if pattern.fullmatch(path.name)}


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class ATPort(serial.Serial if serial else object):
    """A Serial that tolerates absent modem control lines.

    Mirrors ATSerial in host/vpcd_modem_bridge.py, which this process cannot import: it
    runs as /app/runtime/hardware.py and /app is not on its path. pyserial raises DTR and
    RTS inside open(); on virtualised USB passthrough that control transfer can fail with
    EPROTO and a port with no modem-control support answers ENOTTY. Writing one AT
    command needs neither line.
    """

    _TOLERATED = (errno.EPROTO, errno.ENOTTY)

    def _update_dtr_state(self):
        try:
            super()._update_dtr_state()
        except OSError as exc:
            if exc.errno not in self._TOLERATED:
                raise

    def _update_rts_state(self):
        try:
            super()._update_rts_state()
        except OSError as exc:
            if exc.errno not in self._TOLERATED:
                raise


class HardwareSupervisor:
    def __init__(self, status_path=Path("/run/mdd-hardware/status.json"), interval=1.0,
                 data_path=Path("/data")):
        self.status_path = status_path
        self.interval = interval
        self.data_path = data_path
        self.stop = False
        self.dbus = None
        self.modemmanager = None
        self.networkmanager = None
        self.pcscd = None
        self.bridges = {}
        self.bridge_retry_at = {}
        self.pcsc_fingerprint = ""
        self.reported = set()
        self.cellular_states = {}
        self.data_attempt_at = {}
        self.qmi_reset_at = {}
        # device id -> monotonic time this process first saw it present with no
        # ModemManager object; see recover_unclaimed_modem().
        self.unclaimed_since = {}
        self.unclaimed_reset_at = {}
        self.bridge_restart_request_dir = (
            self.data_path / "orchestrator" / "bridge-restart-requests")
        self.bridge_restart_status_dir = (
            self.data_path / "orchestrator" / "bridge-restart-status")
        self.bridge_restarts = {}
        self.log_ring = collections.deque(maxlen=200)
        # Whether the last pass left nothing in flight; only then may the loop slow down.
        self.settled = False
        self.transitioning = True
        # Cleared at the start of every reconcile pass; see mmcli_keyvalue().
        self._mmcli_details = {}
        # `None` means "never published", which is not the same as "published at time zero".
        self._diagnostics_at = None
        for path in self.bridge_restart_status_dir.glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            request_id = str(value.get("request_id") or "")
            if request_id and value.get("state") not in {"channels_ready", "failed"}:
                self.bridge_restarts[request_id] = value

    @staticmethod
    def command(*args, check=False):
        return subprocess.run(args, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, check=check,
                              env={**os.environ, "LC_ALL": "C"})

    def mmcli_keyvalue(self, flag, target):
        """Cached `mmcli <flag> <target> --output-keyvalue` for one reconcile pass.

        A pass asks about the same modem repeatedly — once to match a tty to its object,
        then again for every snapshot — and each call is a fork, a D-Bus round trip and a
        parse. The cache is dropped at the start of each pass and whenever a command
        changes the modem, so it never serves a view from before a state change.
        """
        key = (flag, target)
        cached = self._mmcli_details.get(key)
        if cached is None:
            cached = self.command("mmcli", flag, target, "--output-keyvalue")
            self._mmcli_details[key] = cached
        return cached

    def forget_mmcli_details(self):
        self._mmcli_details.clear()

    def log(self, message):
        line = f"{time.strftime('%F %T')} {message}"
        self.log_ring.append(line)
        print(line, flush=True)

    def start(self):
        Path("/run/dbus").mkdir(parents=True, exist_ok=True)
        self.dbus = subprocess.Popen(
            ["dbus-daemon", "--system", "--nofork", "--nopidfile"],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        socket_path = Path("/run/dbus/system_bus_socket")
        for _ in range(50):
            if self.dbus.poll() is not None:
                raise RuntimeError("private D-Bus exited during startup")
            if socket_path.exists():
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("private D-Bus socket was not created")
        self.modemmanager = subprocess.Popen(
            ["ModemManager", "--debug", "--test-no-udev", "--test-enable"],
            stdout=None, stderr=subprocess.STDOUT)
        logging_error = ""
        for _ in range(100):
            if self.modemmanager.poll() is not None:
                raise RuntimeError("ModemManager exited during startup")
            if self.command("mmcli", "-L").returncode == 0:
                # --debug enables the guarded AT command interface used by the SIM
                # bridge.  The D-Bus name can be visible just before the logging
                # command interface is ready, so retry it within the startup bound.
                result = self.command("mmcli", "--set-logging=INFO")
                if result.returncode == 0:
                    break
                logging_error = result.stdout.strip()[-300:]
            time.sleep(0.1)
        detail = f": {logging_error}" if logging_error else ""
        if self.command("mmcli", "-L").returncode != 0:
            raise RuntimeError("ModemManager command interface did not become ready" + detail)

        Path("/run/NetworkManager").mkdir(parents=True, exist_ok=True)
        self.networkmanager = subprocess.Popen(
            ["NetworkManager", "--no-daemon"], stdout=None, stderr=subprocess.STDOUT)
        nm_error = ""
        for _ in range(100):
            if self.networkmanager.poll() is not None:
                raise RuntimeError("NetworkManager exited during startup")
            result = self.command("nmcli", "general", "status")
            if result.returncode == 0:
                return
            nm_error = result.stdout.strip()[-300:]
            time.sleep(0.1)
        detail = f": {nm_error}" if nm_error else ""
        raise RuntimeError("NetworkManager did not become ready" + detail)

    def report_event(self, action, subsystem, name):
        result = self.command(
            "mmcli", f"--report-kernel-event=action={action},subsystem={subsystem},name={name}")
        if result.returncode:
            raise RuntimeError(f"ModemManager rejected {action} event for {subsystem}/{name}")

    def modem_profiles(self):
        """Modem profiles from the Control configuration, else the built-in default.

        Control keeps its configuration in config.yaml. This used to read a config.json that
        no container deployment has, so a configured profile was never seen and every modem
        was named "Cellular modem" instead of the profile's name, as a native install shows.
        Parsed once per file change: this runs on every reconcile pass.
        """
        path = self.data_path / "config.yaml"
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return list(DEFAULT_MODEM_PROFILES)
        cached = getattr(self, "_modem_profiles_cache", None)
        if cached and cached[0] == stamp:
            return list(cached[1])
        profiles = list(DEFAULT_MODEM_PROFILES)
        try:
            import yaml  # pylint: disable=import-outside-toplevel
            loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
            configured = yaml.load(path.read_text(encoding="utf-8"), Loader=loader) or {}
            values = (configured.get("hardware") or {}).get("modem_profiles") or []
            parsed = [(str(item["vid"]).lower(), str(item["pid"]).lower(),
                       int(item.get("at_interface", 2)),
                       str(item.get("name") or "").strip()[:120] or "USB modem")
                      for item in values]
            if parsed:
                profiles = parsed
        except Exception:  # pylint: disable=broad-except  # a bad file keeps the default
            pass
        self._modem_profiles_cache = (stamp, tuple(profiles))
        return profiles

    def discover_modems(self):
        profiles = {(vid, pid): (interface, name)
                    for vid, pid, interface, name in self.modem_profiles()}
        modems = []
        for usb in Path("/sys/bus/usb/devices").glob("*"):
            try:
                vid = usb.joinpath("idVendor").read_text().strip().lower()
                pid = usb.joinpath("idProduct").read_text().strip().lower()
            except OSError:
                continue
            profile = profiles.get((vid, pid))
            if profile is None:
                continue
            interface, name = profile
            ports = sorted(Path("/sys/bus/usb/devices").glob(
                f"{usb.name}:1.{interface}/ttyUSB*"))
            ports += sorted(Path("/sys/bus/usb/devices").glob(
                f"{usb.name}:1.{interface}/ttyACM*"))
            if not ports:
                continue
            serial = ""
            try:
                serial = usb.joinpath("serial").read_text().strip()
                if (not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", serial)
                        or serial.lower().startswith("ffffff")):
                    serial = ""
            except (OSError, UnicodeError):
                pass
            suffix = serial or usb.name
            hardware_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{vid}-{pid}-{suffix}").strip("-")
            modems.append({"id": hardware_id, "tty": "/dev/" + ports[0].name,
                           "usb_path": usb.name, "vid": vid, "pid": pid, "name": name})
        return sorted(modems, key=lambda item: item["id"])

    def modem_object_for_tty(self, tty, objects):
        basename = Path(tty).name
        for obj in objects:
            detail = self.mmcli_keyvalue("-m", obj)
            if detail.returncode == 0 and re.search(
                    rf"(?<![A-Za-z0-9_.-]){re.escape(basename)}(?![A-Za-z0-9_.-])",
                    detail.stdout):
                return obj
        return ""

    def stop_pcsc(self):
        for process in self.bridges.values():
            if process.poll() is None:
                process.terminate()
        for process in self.bridges.values():
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        self.bridges.clear()
        if self.pcscd and self.pcscd.poll() is None:
            self.pcscd.terminate()
            try:
                self.pcscd.wait(5)
            except subprocess.TimeoutExpired:
                self.pcscd.kill()
                self.pcscd.wait()
        self.pcscd = None

    def bridge_restart_status(self, request, state, **extra):
        value = {**request, **extra, "state": state, "updated_at": time.time()}
        request_id = str(value["request_id"])
        atomic_json(self.bridge_restart_status_dir / f"{request_id}.json", value)
        self.bridge_restarts[request_id] = value
        return value

    def process_bridge_restart_requests(self):
        """Stop only the modem bridge requested after an eUICC profile change."""
        self.bridge_restart_request_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for path in sorted(self.bridge_restart_request_dir.glob("*.json")):
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                request = {}
            try:
                path.unlink()
            except OSError:
                pass
            request_id = str(request.get("request_id") or "")
            device_id = str(request.get("device_id") or "")
            expected = str(request.get("expected_iccid_sha256") or "")
            valid_request_id = re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", request_id)
            if (not valid_request_id or request_id != path.stem
                    or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", device_id)
                    or not re.fullmatch(r"[0-9a-f]{64}", expected)):
                if valid_request_id:
                    self.bridge_restart_status(
                        {"request_id": request_id, "device_id": device_id}, "failed",
                        error="invalid bridge restart request")
                continue
            try:
                requested_at = float(request.get("requested_at") or time.time())
            except (TypeError, ValueError):
                requested_at = time.time()
            request = {"request_id": request_id, "device_id": device_id,
                       "expected_iccid_sha256": expected,
                       "requested_at": requested_at, "started_at": time.time()}
            self.bridge_restart_status(request, "stopping")
            maintenance = self.data_path / "orchestrator" / "pcsc-maintenance"
            maintenance.parent.mkdir(parents=True, exist_ok=True)
            maintenance.write_text(str(int(time.time())), encoding="ascii")
            process = self.bridges.pop(device_id, None)
            self.bridge_retry_at.pop(device_id, None)
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            self.bridge_restart_status(request, "stopped")
            self.log(f"stopped VPCD bridge for eUICC profile refresh: {device_id}")

    def finish_bridge_restart_requests(self, present_ids):
        """Confirm the replacement PID, logical channels and requested active ICCID."""
        now = time.time()
        for request_id, request in list(self.bridge_restarts.items()):
            state = str(request.get("state") or "")
            if state in {"channels_ready", "failed"}:
                self.bridge_restarts.pop(request_id, None)
                continue
            device_id = str(request.get("device_id") or "")
            if device_id not in present_ids:
                self.bridge_restart_status(
                    request, "failed", error="modem disappeared during bridge rebuild")
                continue
            if now - float(request.get("started_at") or now) > 45:
                self.bridge_restart_status(
                    request, "failed", error="timed out rebuilding the VPCD bridge")
                continue
            process = self.bridges.get(device_id)
            if not process or process.poll() is not None:
                continue
            if state != "spawned":
                request = self.bridge_restart_status(
                    request, "spawned", bridge_pid=int(process.pid))
            try:
                identity = json.loads((self.data_path / "modems" /
                                       f"{device_id}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if int(identity.get("bridge_pid") or 0) != int(process.pid):
                continue
            if (identity.get("channel_status") != "ready"
                    or int(identity.get("channel_allocated") or 0) < 1):
                continue
            expected = str(request.get("expected_iccid_sha256") or "")
            actual = str(identity.get("iccid") or "")
            if hashlib.sha256(actual.encode()).hexdigest() != expected:
                continue
            self.bridge_restart_status(
                request, "channels_ready", bridge_pid=int(process.pid),
                channel_allocated=int(identity.get("channel_allocated") or 0))
            self.log(f"VPCD bridge ready after eUICC profile refresh: {device_id}")

    def render_reader_config(self, modems):
        driver = Path("/usr/lib/pcsc/drivers/serial/libifdvpcd.so")
        instances = Path("/var/lib/mdd-vpcd")
        instances.mkdir(parents=True, exist_ok=True)
        stanzas = []
        for index, modem in enumerate(modems):
            base = BASE_VPCD_PORT + index * VPCD_PORT_STRIDE
            isolated = instances / f"libifdvpcd-{base:04x}.so"
            shutil.copyfile(driver, isolated)
            stanzas.append(
                f'FRIENDLYNAME "VoWiFi Modem {modem["id"]}"\n'
                f"DEVICENAME /dev/null:0x{base:04X}\n"
                f"LIBPATH {isolated}\nCHANNELID 0x{base:04X}\n")
            modem["base_port"] = base
        return "\n".join(stanzas)

    def reconcile_pcsc(self, modems, objects):
        for index, modem in enumerate(modems):
            modem["base_port"] = BASE_VPCD_PORT + index * VPCD_PORT_STRIDE
        layout = [(item["id"], item["tty"]) for item in modems]
        fingerprint = hashlib.sha256(json.dumps(layout).encode()).hexdigest()
        if fingerprint != self.pcsc_fingerprint:
            self.stop_pcsc()
            config = self.render_reader_config(modems)
            config_path = Path("/etc/reader.conf.d/mdd-sim-gateway-modems")
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(config, encoding="utf-8")
            Path("/run/pcscd").mkdir(parents=True, exist_ok=True)
            self.pcscd = subprocess.Popen(["pcscd", "--foreground"],
                                          stdout=None, stderr=subprocess.STDOUT)
            self.pcsc_fingerprint = fingerprint
            time.sleep(0.5)
            if self.pcscd.poll() is not None:
                raise RuntimeError("pcscd exited during startup")

        live = {item["id"] for item in modems}
        for hardware_id, process in list(self.bridges.items()):
            if hardware_id not in live or process.poll() is not None:
                if process.poll() is None:
                    process.terminate()
                    process.wait(5)
                else:
                    self.bridge_retry_at[hardware_id] = time.monotonic() + 5
                self.bridges.pop(hardware_id, None)
        for modem in modems:
            if modem["id"] in self.bridges:
                continue
            if time.monotonic() < self.bridge_retry_at.get(modem["id"], 0):
                continue
            obj = self.modem_object_for_tty(modem["tty"], objects)
            if not obj:
                continue
            metadata = self.data_path / "modems" / f'{modem["id"]}.json'
            metadata.parent.mkdir(parents=True, exist_ok=True)
            log_path = self.data_path / "orchestrator" / f'vpcd-{modem["id"]}.log'
            log_path.parent.mkdir(parents=True, exist_ok=True)
            sink = open(log_path, "ab", buffering=0)
            command = [sys.executable, "/app/host/vpcd_modem_bridge.py",
                       "--modem", modem["tty"], "--modemmanager", obj,
                       "--slots", str(VPCD_SLOTS), "--base-port", str(modem["base_port"]),
                       "--metadata-file", str(metadata), "--hardware-id", modem["id"],
                       "--identity-refresh", "60"]
            self.bridges[modem["id"]] = subprocess.Popen(
                command, stdout=sink, stderr=subprocess.STDOUT)
            sink.close()

    @staticmethod
    def _kv(text, key):
        match = re.search(rf"^{re.escape(key)}\s*:\s*(.*?)\s*$", text or "", re.MULTILINE)
        return match.group(1).strip() if match else ""

    @staticmethod
    def normalize_iccid(value):
        """Same rule as host/mdd_orchestrator.py (not shipped in this image): mmcli
        renders an unreadable property as "--", which must mean unknown, not an ICCID."""
        text = str(value or "").strip()
        if not text or text.casefold() in {"--", "unknown", "none", "n/a"}:
            return ""
        digits = re.sub(r"\D", "", text)
        return digits if digits.startswith("89") and 18 <= len(digits) <= 20 else ""

    @staticmethod
    def normalize_msisdn(value):
        """Same rule as host/mdd_orchestrator.py: never persist a driver placeholder."""
        text = str(value or "").strip()
        if not text or text in {"--", "unknown", "none"}:
            return ""
        if not re.fullmatch(r"\+?[0-9 ()-]+", text):
            return ""
        number = ("+" if text.startswith("+") else "") + re.sub(r"\D", "", text)
        digits = number.lstrip("+")
        return number if 5 <= len(digits) <= 20 else ""

    @staticmethod
    def cellular_profile_name(device_id):
        digest = hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:12]
        return f"mdd-cell-{digest}"

    def desired_devices(self, discovered):
        try:
            document = json.loads((self.data_path / "orchestrator" /
                                   "devices-desired.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            document = {}
        defaults = document.get("defaults") or {
            "cellular_enabled": False, "vowifi_enabled": True, "flight_mode": False}
        configured = document.get("devices") or {}
        return {modem["id"]: {
            "cellular_enabled": bool((configured.get(modem["id"]) or defaults).get(
                "cellular_enabled", False)),
            "vowifi_enabled": bool((configured.get(modem["id"]) or defaults).get(
                "vowifi_enabled", True)),
            "flight_mode": bool((configured.get(modem["id"]) or defaults).get(
                "flight_mode", False)),
        } for modem in discovered}

    def migrate_device_ids(self, discovered):
        """Carry a serial-less modem's saved identity across a changed USB path.

        Synology exposes the physical port in the sysfs name. A module without a usable USB
        serial therefore changes id when it is unplugged and returned through a different hub
        path. Wait until both bridge records prove the hardware IMEI matches, then retire the
        old id and move its per-device settings to the live id.
        """
        root = self.data_path / "orchestrator"
        desired_path = root / "devices-desired.json"
        try:
            document = json.loads(desired_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return []
        configured = document.get("devices")
        if not isinstance(configured, dict):
            return []
        current = {modem["id"] for modem in discovered}

        def family(device_id):
            return "-".join(str(device_id).split("-")[:2])

        def imei(device_id):
            try:
                record = json.loads((self.data_path / "modems" /
                                     f"{device_id}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return ""
            digits = re.sub(r"\D", "", str(record.get("imei") or ""))
            return digits if len(digits) == 15 else ""

        moved = []
        for modem in discovered:
            new_id = modem["id"]
            if new_id in configured:
                continue
            stale = [old_id for old_id in configured
                     if old_id not in current and family(old_id) == family(new_id)]
            if len(stale) != 1:
                continue
            old_id = stale[0]
            if not imei(old_id) or imei(old_id) != imei(new_id):
                continue
            configured[new_id] = configured.pop(old_id)
            moved.append((old_id, new_id))

            for path, key in ((root / "hardware-state.json", "assignments"),
                              (root / "devices-status.json", "devices"),
                              (root / "devices-hardware.json", "devices")):
                try:
                    state = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
                records = state.get(key)
                if not isinstance(records, dict) or old_id not in records:
                    continue
                if new_id not in records:
                    records[new_id] = records[old_id]
                records.pop(old_id, None)
                state["updated_at"] = int(time.time())
                atomic_json(path, state)

            old_identity = self.data_path / "modems" / f"{old_id}.json"
            new_identity = self.data_path / "modems" / f"{new_id}.json"
            try:
                if not new_identity.exists() and old_identity.exists():
                    identity = json.loads(old_identity.read_text(encoding="utf-8"))
                    identity["hardware_id"] = new_id
                    atomic_json(new_identity, identity)
                old_identity.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                self.log(f"could not retire modem identity {old_id}: {exc}")
            self.log(f"device id migrated: {old_id} -> {new_id} "
                     f"(same {family(new_id)} hardware IMEI on a new USB path)")
        if moved:
            document["devices"] = configured
            document["updated_at"] = int(time.time())
            atomic_json(desired_path, document)
        return moved

    def assert_networkmanager_isolated(self):
        """Fail before dialing if NetworkManager claimed any NAS interface."""
        result = self.command("nmcli", "-t", "-f", "DEVICE,STATE", "device", "status")
        if result.returncode:
            raise RuntimeError("cannot verify NetworkManager device isolation")
        unexpected = []
        for line in result.stdout.splitlines():
            device, separator, state = line.rpartition(":")
            device = device.replace(r"\:", ":")
            if (separator and device and not re.fullmatch(r"(?:wwan|cdc-wdm)\d+", device)
                    and state.strip().lower() != "unmanaged"):
                unexpected.append(device)
        if unexpected:
            raise RuntimeError("NetworkManager claimed non-cellular interface(s): " +
                               ", ".join(sorted(unexpected)))

    def modem_snapshot(self, modem, objects):
        obj = self.modem_object_for_tty(modem["tty"], objects)
        empty = {"available": False, "registration": "unknown", "data_active": False,
                 "profile": self.cellular_profile_name(modem["id"])}
        if not obj:
            return empty
        detail = self.mmcli_keyvalue("-m", obj)
        if detail.returncode:
            return empty
        text = detail.stdout or ""
        state = self._kv(text, "modem.generic.state").lower()
        power = self._kv(text, "modem.generic.power-state").lower()
        registration = self._kv(text, "modem.3gpp.registration-state").lower() or "unknown"
        if registration in {"--", "none", "n/a"}:
            registration = "unknown"
        ports = re.findall(
            r"modem\.generic\.ports\.value\[\d+\]\s*:\s*([^ ]+) \(([^)]+)\)", text)
        network_port = next((name for name, kind in ports if kind == "net"), "")
        signal = self._kv(text, "modem.generic.signal-quality.value")
        own_numbers = re.findall(
            r"^modem\.generic\.own-numbers\.value\[\d+\]\s*:\s*(.*?)\s*$",
            text, re.MULTILINE)
        msisdn = next((number for raw in own_numbers
                       if (number := self.normalize_msisdn(raw))), "")
        # The control plane decides "SIM inserted" from the VPCD reader or from this ICCID.
        # Without it, any bridge failure made a registered modem read as having no SIM.
        sim_iccid = ""
        sim_object = self._kv(text, "modem.generic.sim")
        if sim_object and sim_object not in {"--", "/"}:
            sim_detail = self.mmcli_keyvalue("-i", sim_object)
            if sim_detail.returncode == 0:
                sim_iccid = self.normalize_iccid(
                    self._kv(sim_detail.stdout or "", "sim.properties.iccid"))
        snapshot = {
            "available": True, "mm_object": obj, "powered": power == "on",
            "radio_enabled": power == "on" and state not in {
                "disabled", "disabling", "failed", "unknown"},
            "state": state, "registration": registration,
            "operator": self._kv(text, "modem.3gpp.operator-name").replace("--", ""),
            "signal": int(signal) if signal.isdigit() else None,
            "primary_port": self._kv(text, "modem.generic.primary-port"),
            "network_interface": network_port,
            "data_active": state == "connected", "apn": "", "ip": "",
            "rx_bytes": 0, "tx_bytes": 0,
            "profile": self.cellular_profile_name(modem["id"]),
            # Sensitive; support bundles redact both by key.
            "msisdn": msisdn, "sim_iccid": sim_iccid,
        }
        bearer_paths = re.findall(
            r"modem\.generic\.bearers\.value\[\d+\]\s*:\s*(\S+)", text)
        for bearer in bearer_paths:
            info = self.mmcli_keyvalue("-b", bearer)
            if info.returncode:
                continue
            body = info.stdout or ""
            apn = self._kv(body, "bearer.properties.apn")
            if apn and not snapshot["apn"]:
                snapshot["apn"] = apn
            if self._kv(body, "bearer.status.connected").lower() == "yes":
                snapshot["data_active"] = True
                snapshot["apn"] = apn
                snapshot["ip"] = (self._kv(body, "bearer.ipv4-config.address") or
                                  self._kv(body, "bearer.ipv6-config.address"))
                rx = self._kv(body, "bearer.stats.rx-bytes")
                tx = self._kv(body, "bearer.stats.tx-bytes")
                snapshot["rx_bytes"] = int(rx) if rx.isdigit() else 0
                snapshot["tx_bytes"] = int(tx) if tx.isdigit() else 0
                break
        return snapshot

    def active_gsm_profiles(self):
        result = self.command(
            "nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active")
        profiles = []
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.rsplit(":", 2)
                if len(parts) == 3 and parts[1] == "gsm":
                    profiles.append((parts[0].replace(r"\:", ":"), parts[2]))
        return profiles

    @staticmethod
    def modem_profile_policy():
        return ["connection.autoconnect", "no",
                "ipv4.never-default", "yes", "ipv6.never-default", "yes"]

    def ensure_modem_data(self, modem, snapshot):
        """Bring up this modem's bearer. Returns whether the modem state was touched."""
        if not snapshot.get("powered") or snapshot.get("data_active"):
            return False
        if snapshot.get("registration") not in {"home", "roaming", "registered"}:
            return False
        device_id = modem["id"]
        # `None` means "never attempted". Defaulting the timestamp to 0 instead would compare
        # against a monotonic clock that starts near zero on a freshly booted host, so the
        # first attempts would be suppressed for the first 45 seconds of uptime.
        last_attempt = self.data_attempt_at.get(device_id)
        if last_attempt is not None and time.monotonic() - last_attempt < 45:
            return False
        self.data_attempt_at[device_id] = time.monotonic()
        primary = snapshot.get("primary_port") or snapshot.get("network_interface")
        if not primary or any(device == primary for _name, device in self.active_gsm_profiles()):
            return False
        profile = self.cellular_profile_name(device_id)
        apn = str(snapshot.get("apn") or "").strip()
        exists = self.command("nmcli", "connection", "show", profile).returncode == 0
        if not exists:
            command = ["nmcli", "connection", "add", "type", "gsm", "ifname", primary,
                       "con-name", profile, "connection.autoconnect-retries", "0",
                       *self.modem_profile_policy()]
            command += (["gsm.apn", apn, "gsm.auto-config", "no"] if apn else
                        ["gsm.auto-config", "yes"])
        else:
            command = ["nmcli", "connection", "modify", profile,
                       *self.modem_profile_policy()]
            if apn:
                command += ["gsm.apn", apn, "gsm.auto-config", "no"]
        result = self.command(*command)
        if result.returncode:
            self.log(f"could not configure cellular profile for {device_id}: "
                     f"{result.stdout.strip()[-300:]}")
            return False
        result = self.command("nmcli", "connection", "up", profile)
        if result.returncode:
            self.log(f"could not activate cellular profile for {device_id}: "
                     f"{result.stdout.strip()[-300:]}")
        return True

    def disconnect_modem_data(self, snapshot):
        """Tear down this modem's bearer. Returns whether the modem state was touched.

        `connection modify` only rewrites the saved policy, so only an actual `down`
        counts as a change worth re-reading the modem for.
        """
        profile = str(snapshot.get("profile") or "")
        if profile and self.command("nmcli", "connection", "show", profile).returncode == 0:
            self.command("nmcli", "connection", "modify", profile,
                         *self.modem_profile_policy())
        primary = snapshot.get("primary_port") or snapshot.get("network_interface")
        disconnected = False
        for name, device in self.active_gsm_profiles():
            if name == profile or (primary and device == primary):
                self.command("nmcli", "connection", "down", name)
                disconnected = True
        return disconnected

    @staticmethod
    def terminate_qmi_proxy():
        """Release a proxy left behind by a stopped ModemManager before firmware reset."""
        killed = False
        for entry in Path("/proc").glob("[0-9]*"):
            try:
                if entry.joinpath("comm").read_text(encoding="ascii").strip() != "qmi-proxy":
                    continue
                os.kill(int(entry.name), signal.SIGTERM)
                killed = True
            except (OSError, ValueError):
                continue
        if killed:
            time.sleep(0.5)

    def reconcile_cellular(self, discovered, objects):
        self.assert_networkmanager_isolated()
        desired = self.desired_devices(discovered)
        live = set()
        for modem in discovered:
            device_id = modem["id"]
            live.add(device_id)
            wanted = desired[device_id]
            snapshot = self.modem_snapshot(modem, objects)
            obj = snapshot.get("mm_object")
            # A container restart can terminate qmi-proxy while the modem still holds its
            # old QMI session. ModemManager then creates an AT-only object and marks both
            # cdc-wdm0 and wwan0 ignored. A firmware reset through that usable AT object
            # releases the stale session and re-enumerates the complete QMI modem.
            qmi_present = any(path.exists() for path in Path("/sys/class/usbmisc").glob(
                "cdc-wdm*"))
            net_present = any(path.exists() for path in Path("/sys/class/net").glob("wwan*"))
            # Same reason as data_attempt_at: a 0 default is indistinguishable from a reset
            # performed at monotonic zero, which suppressed this recovery for the first five
            # minutes of host uptime — exactly when a restarted Hardware container is most
            # likely to be holding a stale QMI session.
            last_reset = self.qmi_reset_at.get(device_id)
            if (obj and qmi_present and net_present and not snapshot.get("network_interface")
                    and (last_reset is None
                         or time.monotonic() - last_reset >= MODEM_RESET_INTERVAL)):
                self.qmi_reset_at[device_id] = time.monotonic()
                self.terminate_qmi_proxy()
                self.command("mmcli", "-m", obj, "--reset")
                self.forget_mmcli_details()
                self.cellular_states[device_id] = snapshot
                continue
            if obj:
                self.unclaimed_since.pop(device_id, None)
            elif self.recover_unclaimed_modem(modem):
                self.cellular_states[device_id] = snapshot
                continue
            radio_enabled = not wanted["flight_mode"]
            if obj and snapshot.get("radio_enabled") != radio_enabled:
                result = self.command(
                    "mmcli", "-m", obj, "--enable" if radio_enabled else "--disable")
                self.forget_mmcli_details()
                if result.returncode and "already" not in result.stdout.lower():
                    self.cellular_states[device_id] = snapshot
                    continue
                snapshot = self.modem_snapshot(modem, objects)
            if wanted["cellular_enabled"] and radio_enabled:
                changed = self.ensure_modem_data(modem, snapshot)
            else:
                changed = self.disconnect_modem_data(snapshot)
            # Re-reading costs three subprocesses. In the steady state nothing above
            # touched the modem, so the snapshot already in hand is the current one.
            if changed:
                self.forget_mmcli_details()
                snapshot = self.modem_snapshot(modem, objects)
            self.cellular_states[device_id] = snapshot
        self.cellular_states = {key: value for key, value in self.cellular_states.items()
                                if key in live}
        # An unplugged modem that comes back is a fresh enumeration and earns a fresh grace
        # period; its reset rate limit is kept, since replugging is what a reset looks like.
        self.unclaimed_since = {key: value for key, value in self.unclaimed_since.items()
                                if key in live}

    def recover_unclaimed_modem(self, modem):
        """Reset a modem ModemManager has given up on, through its bare AT port.

        Seen on an EC25 whose QMI port hit a USB protocol error (-71) at enumeration:
        ModemManager timed out on cdc-wdm0 ten times, marked the modem invalid and dropped
        it, leaving no object at all. The AT-only recovery above needs an object to send
        `--reset` through, so nothing ever retried and 4G stayed down, although the AT port
        itself was registered on LTE. AT+CFUN=1,1 re-enumerates the modem and ModemManager
        then claims it normally. ModemManager holds no port of a modem it has dropped, so
        a non-exclusive write here cannot interleave with its own AT traffic.

        Returns True only when the reset command was written.
        """
        device_id = modem["id"]
        now = time.monotonic()
        first_seen = self.unclaimed_since.setdefault(device_id, now)
        if now - first_seen < UNCLAIMED_RESET_GRACE:
            return False
        # Missing means "never reset", not "reset at monotonic zero"; see the qmi_reset_at
        # comment in reconcile_cellular() for what a 0 default cost at boot.
        last_reset = self.unclaimed_reset_at.get(device_id)
        if last_reset is not None and now - last_reset < MODEM_RESET_INTERVAL:
            return False
        # Charged before the attempt: a port that fails to open must not be retried every
        # pass, and the grace clock restarts so the next try again waits for ModemManager.
        self.unclaimed_reset_at[device_id] = now
        self.unclaimed_since.pop(device_id, None)
        tty = modem["tty"]
        self.log(f"modem {device_id} has had no ModemManager object for "
                 f"{int(now - first_seen)}s; resetting it with AT+CFUN=1,1 on {tty}")
        if serial is None:
            self.log(f"modem {device_id} reset skipped: pyserial is not installed")
            return False
        written = False
        reply = ""
        try:
            with ATPort(tty, 115200, timeout=1, write_timeout=2, exclusive=False) as port:
                port.write(b"AT+CFUN=1,1\r")
                port.flush()
                written = True
                # The modem answers OK before it drops off the bus; no reply is not a
                # failure, since the reset may already have taken the port away.
                reply = port.read(64).decode("ascii", errors="replace").strip()
        except Exception as exc:  # noqa: BLE001 - a failed reset must not fail the pass
            # pyserial wraps most failures in SerialException (an OSError), but the port
            # vanishing mid-reset can surface as others; any of them would otherwise abort
            # reconcile and mark every other modem unhealthy for this one.
            if not written:
                self.log(f"modem {device_id} reset via {tty} failed: {exc}")
                return False
            reply = reply or f"port lost after write ({exc})"
        self.forget_mmcli_details()
        self.log(f"modem {device_id} reset via {tty} sent; reply: {reply or '(none)'}")
        return True

    def publish_control_state(self, discovered, ready_ids, bridge_errors=None):
        """Publish the subset of the host-orchestrator contract this container owns.

        NetworkManager is restricted to modem interfaces and every GSM profile is
        non-autoconnecting and never-default, so cellular capability can be exposed
        without allowing it to become the NAS uplink.
        """
        root = self.data_path / "orchestrator"
        try:
            desired = json.loads((root / "devices-desired.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            desired = {}
        defaults = desired.get("defaults") or {
            "cellular_enabled": False, "vowifi_enabled": True, "flight_mode": False}
        desired_devices = desired.get("devices") or {}
        assignments = {}
        devices = {}
        for modem in discovered:
            device_id = modem["id"]
            wanted = desired_devices.get(device_id) or defaults
            ready = device_id in ready_ids
            cellular = self.cellular_states.get(device_id) or {
                "available": False, "registration": "unknown", "data_active": False}
            target_data = bool(wanted.get("cellular_enabled")) and not bool(
                wanted.get("flight_mode"))
            assignment = {
                "name": modem.get("name") or "Cellular modem", "tty": modem["tty"],
                "usb_path": modem["usb_path"], "vid": modem["vid"], "pid": modem["pid"],
            }
            assignments[device_id] = assignment
            bridge_error = (bridge_errors or {}).get(device_id, "") if not ready else ""
            if bridge_error:
                # Name the card problem instead of "starting": the page otherwise showed a
                # registered modem with a working bearer as stuck starting, forever.
                error = f"SIM card access failed: {bridge_error}"
            elif ready and cellular.get("available"):
                error = ""
            else:
                error = "Cellular modem is starting"
            devices[device_id] = {
                "id": device_id, **assignment, "present": True,
                "desired": wanted,
                "actual": {
                    "cellular_backend_active": True,
                    "cellular_radio_enabled": cellular.get("radio_enabled"),
                    "flight_mode_active": cellular.get("radio_enabled") is False,
                    "vowifi_bridge_active": ready,
                    "vowifi_backend": "modemmanager" if ready else "",
                    "cellular_supported": True,
                },
                "cellular": cellular,
                "transitioning": ((not ready and not bridge_error) or target_data != bool(
                    cellular.get("data_active"))),
                "error": error,
            }
        now = int(time.time())
        self.transitioning = any(item["transitioning"] for item in devices.values())
        atomic_json(root / "hardware-state.json", {
            "version": 1, "updated_at": now, "assignments": assignments})
        atomic_json(root / "devices-status.json", {
            "version": 2, "updated_at": now, "devices": devices,
            "shared": {
                "cellular_backend": "container-networkmanager-per-device",
                "modem_backend": "container",
                "modemmanager_active": True,
                "transitioning": any(item["transitioning"] for item in devices.values()),
                "error": "", "disruption": "", "affected_devices": [],
                "isolation": "NetworkManager manages only wwan/cdc-wdm; cellular profiles never provide the NAS default route",
            },
        })

    @staticmethod
    def listening_tcp_ports():
        ports = set()
        for name in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                lines = Path(name).read_text(encoding="ascii").splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                parts = line.split()
                try:
                    if len(parts) > 3 and parts[3] == "0A":
                        ports.add(int(parts[1].rsplit(":", 1)[1], 16))
                except (ValueError, IndexError):
                    continue
        return ports

    def publish_host_diagnostics(self, discovered, modem_objects):
        """Publish the container-owned hardware evidence used by support bundles."""
        now = int(time.time())
        listening = self.listening_tcp_ports()
        assignments = {}
        bridges = {}
        vpcd_ports = {}
        for modem in discovered:
            device_id = modem["id"]
            assignments[device_id] = {
                "name": modem.get("name") or "Cellular modem", "tty": modem["tty"],
                "usb_path": modem["usb_path"], "vid": modem["vid"], "pid": modem["pid"],
                "base_port": modem.get("base_port"),
            }
            base = int(modem.get("base_port") or 0)
            if base:
                vpcd_ports[device_id] = {
                    str(base + slot): base + slot in listening for slot in range(VPCD_SLOTS)}
            process = self.bridges.get(device_id)
            if not process:
                continue
            try:
                identity = json.loads((self.data_path / "modems" /
                                       f"{device_id}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                identity = {}

            def nonnegative_int(value):
                try:
                    return max(0, int(value or 0))
                except (TypeError, ValueError, OverflowError):
                    return 0

            imei = re.sub(r"\D", "", str(identity.get("imei") or ""))
            iccid = re.sub(r"\D", "", str(identity.get("iccid") or ""))
            updated = nonnegative_int(identity.get("updated_at"))
            requested = nonnegative_int(identity.get("channel_requested"))
            allocated = nonnegative_int(identity.get("channel_allocated"))
            log_path = self.data_path / "orchestrator" / f"vpcd-{device_id}.log"
            compact_log(log_path)
            log_tail = tail_lines(log_path)
            bridges[device_id] = {
                "pid": int(process.pid), "running": process.poll() is None,
                "metadata_age_seconds": max(0, now - updated) if updated else None,
                "imei_valid": len(imei) == 15,
                "iccid_valid": iccid.startswith("89") and 18 <= len(iccid) <= 22,
                # Every requested slot is served, on its own channel or a shared one.
                "channels_ready": (identity.get("channel_status") == "ready" and requested > 0
                                   and allocated > 0 and nonnegative_int(
                                       identity.get("slots_served", allocated)) == requested),
                "log_tail": log_tail,
            }
        try:
            reader_definitions = sorted(path.name for path in
                                        Path("/etc/reader.conf.d").iterdir())
        except OSError:
            reader_definitions = []
        atomic_json(self.data_path / "orchestrator" / "host-diagnostics.json", {
            "version": 1, "updated_at": now, "virtualization": "docker",
            "modem_backend": "container",
            "modemmanager": {
                "unit_active": bool(self.modemmanager and self.modemmanager.poll() is None),
                "required": True,
                "applied": bool(self.modemmanager and self.modemmanager.poll() is None),
                "unclaimed": {}, "degraded_to_direct_serial": {}, "bridge_failures": {},
            },
            "modem_objects": sorted(modem_objects),
            "discovered_modems": discovered,
            "assignments": assignments,
            "bridges": bridges,
            "vpcd_ports_listening": vpcd_ports,
            "reader_definitions": reader_definitions,
            "country_egress_required": any(
                state.get("vowifi_enabled") for state in self.desired_devices(discovered).values()),
            "reader_config": {"path": "/etc/reader.conf.d/mdd-sim-gateway-modems",
                              "stanzas": len(discovered)},
            "recent_log": list(self.log_ring),
        })

    def reconcile(self):
        self.forget_mmcli_details()
        current = kernel_objects()
        for subsystem, name in sorted(self.reported - current):
            self.report_event("remove", subsystem, name)
        for subsystem, name in sorted(current - self.reported):
            self.report_event("add", subsystem, name)
        self.reported = current
        listing = self.command("mmcli", "-L")
        if listing.returncode:
            raise RuntimeError("ModemManager listing failed")
        modems = sorted(set(re.findall(r"/org/freedesktop/ModemManager1/Modem/\d+", listing.stdout)))
        discovered = self.discover_modems()
        self.process_bridge_restart_requests()
        self.reconcile_pcsc(discovered, modems)
        self.migrate_device_ids(discovered)
        self.reconcile_cellular(discovered, modems)
        ready_ids = set()
        bridge_errors = {}
        for modem in discovered:
            process = self.bridges.get(modem["id"])
            try:
                metadata = json.loads((self.data_path / "modems" /
                                       f'{modem["id"]}.json').read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                metadata = {}
            if (process and process.poll() is None
                    and metadata.get("bridge_pid") == process.pid
                    and metadata.get("channel_status") == "ready"):
                ready_ids.add(modem["id"])
            elif metadata.get("channel_status") == "error":
                # The bridge reached the card and the card refused it. That is a fact about
                # this SIM, not a hardware plane still coming up; retrying will not change it.
                bridge_errors[modem["id"]] = str(
                    metadata.get("channel_error") or "SIM logical channel allocation failed")
        self.finish_bridge_restart_requests({modem["id"] for modem in discovered})
        ready_bridges = len(ready_ids)
        maintenance_ids = {
            str(request.get("device_id") or "") for request in self.bridge_restarts.values()
            if request.get("state") not in {"channels_ready", "failed"}
        }
        atomic_json(self.status_path, {
            "version": 1,
            "updated_at": int(time.time()),
            "modem_count": len(modems),
            "modems": modems,
            "hardware_count": len(discovered),
            "bridge_count": sum(process.poll() is None for process in self.bridges.values()),
            "ready_bridge_count": ready_bridges,
            "maintenance_bridge_count": len(maintenance_ids),
            # Counted as settled by the health check: one unusable SIM must not hold the
            # whole stack (Control waits on this container being healthy) hostage.
            "card_failed_bridge_count": len(bridge_errors),
            "pcsc_reader_count": len(discovered) * 4,
            "logical_channel_count": len(discovered) * VPCD_SLOTS,
            "networkmanager_active": True,
            "kernel_objects": [f"{subsystem}/{name}" for subsystem, name in sorted(current)],
        })
        self.publish_control_state(discovered, ready_ids, bridge_errors)
        self.settled = (not maintenance_ids and not self.transitioning
                        and ready_bridges + len(bridge_errors) >= len(discovered))
        if (self._diagnostics_at is None
                or time.monotonic() - self._diagnostics_at >= DIAGNOSTICS_INTERVAL):
            self._diagnostics_at = time.monotonic()
            self.publish_host_diagnostics(discovered, modems)

    def publish_reconcile_error(self, exc):
        """Keep the hardware plane alive while making one failed pass explicit and fail-closed."""
        now = int(time.time())
        try:
            status = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            status = {"version": 1, "modem_count": 0, "hardware_count": 0,
                      "ready_bridge_count": 0, "maintenance_bridge_count": 0}
        status.update({"updated_at": now, "reconcile_error": type(exc).__name__})
        atomic_json(self.status_path, status)
        state_path = self.data_path / "orchestrator" / "devices-status.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return
        shared = state.setdefault("shared", {})
        shared.update({"transitioning": True, "error": "Hardware reconciliation is retrying",
                       "disruption": "hardware_reconcile_failed"})
        state["updated_at"] = now
        atomic_json(state_path, state)

    def publish_offline(self):
        """Retire container-owned presence before the process stops."""
        now = int(time.time())
        state_path = self.data_path / "orchestrator" / "devices-status.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            state = {"version": 2, "devices": {}, "shared": {}}
        for device in (state.get("devices") or {}).values():
            if isinstance(device, dict):
                device["present"] = False
                device["transitioning"] = False
                actual = device.get("actual")
                if isinstance(actual, dict):
                    actual["vowifi_bridge_active"] = False
                    actual["cellular_backend_active"] = False
        state["updated_at"] = now
        state.setdefault("shared", {}).update({
            "modemmanager_active": False, "transitioning": False,
            "error": "Hardware service is offline", "disruption": "hardware_offline"})
        atomic_json(state_path, state)
        atomic_json(self.status_path, {
            "version": 1, "updated_at": now, "modem_count": 0, "hardware_count": 0,
            "bridge_count": 0, "ready_bridge_count": 0, "maintenance_bridge_count": 0,
            "pcsc_reader_count": 0, "logical_channel_count": 0,
            "networkmanager_active": False, "kernel_objects": [], "stopped": True})

    def loop(self):
        self.start()
        while not self.stop:
            if (self.dbus.poll() is not None or self.modemmanager.poll() is not None
                    or self.networkmanager.poll() is not None):
                raise RuntimeError("hardware service exited")
            try:
                self.reconcile()
            except Exception as exc:  # one transient tool failure must not tear down PC/SC
                self.log(f"hardware reconcile failed; retrying: {type(exc).__name__}: {exc}")
                try:
                    self.publish_reconcile_error(exc)
                except Exception as publish_exc:
                    self.log("could not publish hardware retry state: "
                             f"{type(publish_exc).__name__}: {publish_exc}")
            self.wait_for_next_pass()

    def wake_signature(self):
        """Everything that should start a pass at once, cheap enough to check twice a second:
        a kernel device, the requested per-device state, a bridge restart request, a bridge
        process exiting."""
        root = self.data_path / "orchestrator"
        try:
            desired = (root / "devices-desired.json").stat().st_mtime_ns
        except OSError:
            desired = None
        try:
            requests = tuple(sorted(path.name for path in
                                    self.bridge_restart_request_dir.glob("*.json")))
        except OSError:
            requests = ()
        bridges = tuple(sorted((device_id, process.poll() is None)
                               for device_id, process in self.bridges.items()))
        return (frozenset(kernel_objects()), desired, requests, bridges)

    def wait_for_next_pass(self):
        interval = max(self.interval, IDLE_INTERVAL) if self.settled else self.interval
        signature = self.wake_signature() if self.settled else None
        now = time.monotonic()
        deadline, next_check = now + interval, now + WAKE_CHECK_SECONDS
        while not self.stop and time.monotonic() < deadline:
            time.sleep(0.1)
            if signature is not None and time.monotonic() >= next_check:
                next_check = time.monotonic() + WAKE_CHECK_SECONDS
                if self.wake_signature() != signature:
                    return

    def close(self):
        try:
            self.publish_offline()
        except Exception as exc:
            self.log(f"could not publish hardware offline state: {type(exc).__name__}: {exc}")
        self.stop_pcsc()
        for process in (self.networkmanager, self.modemmanager, self.dbus):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", type=Path, default=Path("/run/mdd-hardware/status.json"))
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--data", type=Path, default=Path("/data"))
    args = parser.parse_args()
    app = HardwareSupervisor(args.status, max(0.2, args.interval), args.data)
    def stop(*_):
        app.stop = True
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        app.loop()
    finally:
        app.close()


if __name__ == "__main__":
    main()
