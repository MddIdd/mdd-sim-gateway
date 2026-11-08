#!/usr/bin/env python3
"""Expose modem AT+CSIM as three independent VPCD slots.

Each VPCD slot is mapped to one ISO 7816 logical channel.  The modem has one
physical AT port, so all AT commands are serialized while the selected-file
state remains isolated by the UICC logical channels.  A card that grants fewer
channels than slots has slots share a channel (see ChannelArbiter).
"""

import argparse
import errno
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

try:
    import serial
except ImportError:  # ModemManager-backed mode does not need pyserial
    serial = None


DEFAULT_ATR = bytes.fromhex("3B9F95801FC78031E073FE211B66D0017797020C000B")
CSIM_RE = re.compile(rb'\+CSIM:\s*(\d+)\s*,\s*"([0-9A-Fa-f]*)"')
IMEI_RE = re.compile(rb'(?<!\d)(\d{15})(?!\d)')
ICCID_RE = re.compile(rb'(?<!\d)(89\d{17,20})(?!\d)')
LOGICAL_CHANNEL_CAPACITY = 3
LOGICAL_CHANNEL_ROLES = ("pin", "swu", "ims")
# A slot pcscd never opens a socket for is a normal steady state, not an incident: the reader
# may expose fewer slots than the modem offers. Retrying every second and logging every attempt
# turned that into a permanent write stream — one line per slot per second, forever, on hosts
# whose storage is an SD card. Retries back off to a minute and only the first failure of an
# outage is logged, so a genuinely broken slot is still visible without the repetition.
SLOT_RETRY_SECONDS = 1.0
SLOT_RETRY_CEILING_SECONDS = 60.0
# MANAGE CHANNEL OPEN answering with a channel this bridge already holds is not proof that the
# UICC is out of channels. On virtualized USB passthrough a late reply can be read as the answer
# to the next command, so the previous OPEN's channel number comes back verbatim. Settle the port
# and ask again before declaring the SIM unusable — the alternative is a bridge that exits and
# takes both lines down over a transport artefact.
CHANNEL_OPEN_ATTEMPTS = 3
CHANNEL_SETTLE_SECONDS = 0.5
# Some UICCs expose a single supplementary logical channel (a China Unicom USIM answered the
# second MANAGE CHANNEL OPEN with 6A81). Slots then share a channel, which is only safe if a
# slot finds the card where it left it: each slot's SELECTs are replayed when the channel
# changes hands, and a slot owed a GET RESPONSE keeps the channel until it asks for it.
PENDING_RESPONSE_HOLD_SECONDS = 2.0
MAX_SELECT_HISTORY = 16
SELECT_MF = bytes.fromhex("00A40004023F00")
SIM_ACCESS_FAILURES = 3
SIM_ACCESS_FAILURE_SECONDS = 30.0
SIM_ACCESS_RECOVERY_EXIT = 75
BRIDGE_RECOVERY_WINDOW = 3600.0
BRIDGE_RECOVERY_ATTEMPTS = 3
BRIDGE_STABLE_SECONDS = 60.0


class SIMAccessHealth:
    """Detect a stuck allocated channel, independently of traffic on other channels.

    Only modem transport errors and 'logical channel not supported' count. PIN failures,
    missing files and other card verdicts must never trigger a reset or replay an APDU.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.failed = threading.Event()
        self.streaks = {}

    def observe(self, channel, failed):
        with self.lock:
            if not failed:
                self.streaks.pop(channel, None)
                return
            now = time.monotonic()
            count, since = self.streaks.get(channel, (0, now))
            self.streaks[channel] = (count + 1, since)
            if count + 1 >= SIM_ACCESS_FAILURES and now - since >= SIM_ACCESS_FAILURE_SECONDS:
                self.failed.set()


class BridgeRecoveryBudget:
    """Persist a per-modem recovery limit, including failed replacement processes.

    At most three rebuilds per hour after a runtime SIM-access failure. Ordinary startup
    errors keep their existing retry policy; a live process never consumes a retry.
    """

    def __init__(self, path):
        self.path = str(path)
        try:
            with open(self.path, encoding="utf-8") as handle:
                self.records = json.load(handle)
            if not isinstance(self.records, dict):
                self.records = {}
            self.records = {key: value for key, value in self.records.items()
                            if isinstance(value, dict)
                            and isinstance(value.get("since"), (int, float))
                            and isinstance(value.get("retry_at"), (int, float))
                            and isinstance(value.get("failures"), int)}
        except (OSError, ValueError):
            self.records = {}

    def record_exit(self, device_id, returncode, uptime=0.0):
        now = time.time()
        # An ordinary exit after a stable replacement is a new incident. Keep counting
        # watchdog exits even after 60s: repeated stuck-channel failures often take longer
        # than that to accumulate, and must not bypass the rebuild limit.
        if returncode != SIM_ACCESS_RECOVERY_EXIT and uptime >= BRIDGE_STABLE_SECONDS:
            if self.records.pop(device_id, None) is not None:
                write_metadata(self.path, self.records)
            return
        previous = self.records.get(device_id, {})
        active = bool(previous) and now < previous.get("since", 0) + BRIDGE_RECOVERY_WINDOW
        if returncode != SIM_ACCESS_RECOVERY_EXIT and not active:
            return
        count = previous["failures"] + 1 if active else 1
        since = previous["since"] if active else now
        retry_at = (now + 15 * 2 ** (count - 1) if count <= BRIDGE_RECOVERY_ATTEMPTS
                    else since + BRIDGE_RECOVERY_WINDOW)
        self.records[device_id] = {"since": since, "at": now, "failures": count,
                                   "retry_at": retry_at,
                                   "limited": count > BRIDGE_RECOVERY_ATTEMPTS}
        write_metadata(self.path, self.records)

    def snapshot(self):
        """Detached, unexpired diagnostics using the host bridge_failures field names."""
        now = time.time()
        return {device_id: {"count": value["failures"],
                            "at": value.get("at", value["since"]),
                            "reason": self.error(device_id) or "SIM access recovery attempted",
                            "retry_at": value["retry_at"],
                            "limited": bool(value.get("limited"))}
                for device_id, value in self.records.items()
                if now < value["since"] + BRIDGE_RECOVERY_WINDOW}

    def retry_at(self, device_id):
        value = self.records.get(device_id, {})
        return value.get("retry_at", 0)

    def error(self, device_id):
        value = self.records.get(device_id, {})
        if time.time() >= self.retry_at(device_id):
            return ""
        if value.get("limited"):
            return ("SIM access recovery paused after three failed rebuilds; "
                    "automatic retry at %d" % value["retry_at"])
        return "SIM access failed; waiting to rebuild this modem's bridge"


class ModemError(RuntimeError):
    pass


def decode_bcd_iccid(data):
    """Decode EF_ICCID swapped-nibble BCD; empty string when the bytes are not an ICCID."""
    digits = []
    for byte in data:
        for nibble in (byte & 0x0F, byte >> 4):
            if nibble == 0x0F:
                continue
            if nibble > 9:
                return ""
            digits.append(str(nibble))
    value = "".join(digits)
    return value if value.startswith("89") and 18 <= len(value) <= 20 else ""


def logical_channel_metadata(channels, requested=3, status="ready", error="",
                             slot_channels=None):
    """Return stable, UI-safe resource metadata for one physical UICC.

    `slot_channels` maps every served slot to its channel; it repeats a channel when the
    card had fewer channels than slots and the slots share one.
    """
    values = [int(channel) for channel in channels]
    served = [int(channel) for channel in (values if slot_channels is None else slot_channels)]
    return {
        "channel_capacity": LOGICAL_CHANNEL_CAPACITY,
        "channel_requested": int(requested),
        "channel_allocated": len(values),
        "channel_status": str(status),
        "channel_error": str(error),
        "channel_shared": len(set(served)) < len(served),
        "slots_served": len(served),
        "logical_channels": [
            {"slot": slot, "channel": channel, "role": LOGICAL_CHANNEL_ROLES[slot]}
            for slot, channel in enumerate(served)
        ],
    }


def allocate_logical_channels(card, count):
    """Allocate up to `count` unique UICC channels; at least one is required.

    A card that runs out of channels part way is served with what it granted (see
    slot_channel_map) rather than refused outright. Nothing is left open on failure.
    """
    channels = []
    try:
        for _slot in range(int(count)):
            try:
                channel = card.open_channel()
                for attempt in range(2, CHANNEL_OPEN_ATTEMPTS + 1):
                    if channel not in channels:
                        break
                    print("[bridge] MANAGE CHANNEL OPEN returned channel %d again; settling "
                          "the AT port and retrying (attempt %d/%d)" %
                          (channel, attempt, CHANNEL_OPEN_ATTEMPTS), flush=True)
                    card.settle()
                    channel = card.open_channel()
                if channel in channels:
                    raise ModemError("duplicate logical channel allocated: %d" % channel)
            except ModemError as exc:
                if not channels:
                    raise
                print("[bridge] SIM granted %d of %d logical channels (%s); slots will share"
                      % (len(channels), int(count), exc), flush=True)
                break
            channels.append(channel)
        return channels
    except Exception as exc:
        for channel in channels:
            card.close_channel(channel)
        raise ModemError(
            "SIM logical channel allocation failed (%d/%d allocated): %s" %
            (len(channels), LOGICAL_CHANNEL_CAPACITY, exc)) from exc


def slot_channel_map(channels, slots):
    """Assign every slot a channel, sharing when the card granted fewer than `slots`.

    With two channels the IMS slot keeps one to itself: Asterisk abandons an AKA request
    after a fixed timeout, while PIN checks and the SWu handshake are not that sensitive
    and do not run at the same moment as an IMS registration challenge.
    """
    channels = list(channels)
    if len(channels) >= slots:
        return channels[:slots]
    if len(channels) == 1:
        return channels * slots
    # Two channels, three slots: pin and swu share, ims ("ims" is the last role) is alone.
    return [channels[0]] * (slots - 1) + [channels[1]]


def _is_select(apdu):
    return len(apdu) >= 4 and apdu[1] == 0xA4


def _is_absolute_select(apdu):
    """True when a SELECT fixes the whole file context by itself, independent of history."""
    p1 = apdu[2]
    if p1 in (0x04, 0x08):  # by DF name (AID), by path from MF
        return True
    if p1 == 0x00:
        data = apdu[5:5 + apdu[4]] if len(apdu) > 5 else b""
        return data in (b"", b"\x3F\x00")
    return False


def _select_succeeded(response):
    return len(response) >= 2 and response[-2] in (0x90, 0x91, 0x61, 0x9F, 0x62, 0x63)


class ChannelArbiter:
    """Serialize the slots mapped to one UICC logical channel.

    A slot that is alone on its channel pays nothing: the owner never changes, so no SELECT
    is replayed. The selected-file history is still kept, so the same object serves both
    the one-channel-per-slot and the shared layouts.
    """

    def __init__(self, card, channel):
        self.card = card
        self.channel = channel
        self.cond = threading.Condition()
        self.owner = None
        self.pending = None
        self.pending_deadline = 0.0
        self.selects = {}
        self.overflowed = set()

    def _restore(self, slot):
        history = self.selects.get(slot) or [SELECT_MF]
        for apdu in history:
            try:
                self.card.transmit(apdu, self.channel)
            except ModemError as exc:
                print("[bridge] slot %d could not restore its file context on channel %d: %s"
                      % (slot, self.channel, exc), flush=True)
                return

    def _claim(self, slot):
        """Wait out another slot's owed GET RESPONSE, then make `slot` the owner."""
        while self.pending not in (None, slot):
            remaining = self.pending_deadline - time.monotonic()
            if remaining <= 0:
                self.pending = None
                break
            self.cond.wait(remaining)
        if self.owner is not None and self.owner != slot:
            self._restore(slot)
        self.owner = slot

    def _record(self, slot, apdu, response):
        if not _is_select(apdu) or not _select_succeeded(response):
            return
        if _is_absolute_select(apdu):
            self.selects[slot] = [apdu]
            self.overflowed.discard(slot)
            return
        history = self.selects.setdefault(slot, [])
        history.append(apdu)
        if len(history) > MAX_SELECT_HISTORY:
            del history[0]
            if slot not in self.overflowed:
                self.overflowed.add(slot)
                print("[bridge] slot %d relative SELECT chain exceeds %d; a shared-channel "
                      "restore may land in the wrong DF" % (slot, MAX_SELECT_HISTORY),
                      flush=True)

    def transmit(self, slot, apdu):
        with self.cond:
            self._claim(slot)
            response = self.card.transmit(apdu, self.channel)
            if self.card.closes_logical_channel(apdu, self.channel):
                # ModemCard.transmit put the channel back on MF for the next client.
                self.selects[slot] = [SELECT_MF]
            else:
                self._record(slot, apdu, response)
            if len(response) >= 2 and response[-2] in (0x61, 0x6C):
                self.pending = slot
                self.pending_deadline = time.monotonic() + PENDING_RESPONSE_HOLD_SECONDS
            elif self.pending == slot:
                self.pending = None
            self.cond.notify_all()
            return response

    def reset(self, slot):
        with self.cond:
            self._claim(slot)
            if self.pending == slot:
                self.pending = None
            self.selects[slot] = [SELECT_MF]
            try:
                self.card.select_mf(self.channel)
            finally:
                self.cond.notify_all()


class ATSerial(serial.Serial if serial else object):
    """A Serial that tolerates absent modem control lines.

    pyserial raises DTR and RTS as part of open() and offers no way to skip that
    (pyserial#729 — setting .dtr first only swaps which ioctl runs). On virtualised USB
    passthrough the underlying control transfer can fail with EPROTO, and a port with no
    modem-control support at all answers ENOTTY. An AT command channel uses neither line,
    so either failure must not cost the port. Verified on the PVE guest that hit it:
    with these overrides the same port opens and the bridge proceeds.
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


class ModemCard:
    def __init__(self, port, baud=115200, timeout=10.0, debug=False):
        if serial is None:
            raise ModemError("pyserial is required for direct modem access")
        self.lock = threading.RLock()
        self.debug = debug
        self.timeout = timeout
        self.ser = ATSerial(
            port, baud, timeout=0.25, write_timeout=2, exclusive=True
        )
        try:
            self._drain()
            self._at("ATE0")
            self._at("AT+CMEE=2")
        except Exception:
            # A port that is not an AT port (the modem probe tries several) must not stay
            # locked until garbage collection gets to it.
            self.close()
            raise

    def _drain(self):
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if not self.ser.read(4096):
                break

    def settle(self):
        """Discard whatever the modem is still sending, so the next reply is the next reply.

        Subclasses that do not own a serial port inherit the pause without the read.
        """
        time.sleep(CHANNEL_SETTLE_SECONDS)
        if getattr(self, "ser", None) is not None:
            with self.lock:
                self._drain()

    def _at(self, command):
        with self.lock:
            self.ser.reset_input_buffer()
            self.ser.write((command + "\r").encode("ascii"))
            self.ser.flush()
            buf = bytearray()
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                chunk = self.ser.read(1024)
                if chunk:
                    buf.extend(chunk)
                    lines = bytes(buf).replace(b"\r", b"\n").splitlines()
                    if any(line.strip() == b"OK" for line in lines):
                        return bytes(buf)
                    if any(
                        line.strip() == b"ERROR"
                        or line.strip().startswith(b"+CME ERROR:")
                        for line in lines
                    ):
                        raise ModemError(bytes(buf).decode("ascii", "replace"))
            raise ModemError("timeout waiting for %s: %r" % (command, bytes(buf[-200:])))

    def csim(self, apdu):
        hex_apdu = apdu.hex().upper()
        if self.debug:
            print("[bridge] APDU --> header=%s bytes=%d" %
                  (apdu[:4].hex().upper(), len(apdu)), flush=True)
        health = getattr(self, "access_health", None)
        channel = apdu[0] & 3
        try:
            raw = self._at('AT+CSIM=%d,"%s"' % (len(hex_apdu), hex_apdu))
            match = CSIM_RE.search(raw)
            if not match:
                raise ModemError("missing +CSIM response: %r" % raw[-200:])
            try:
                response = bytes.fromhex(match.group(2).decode("ascii"))
            except ValueError as exc:
                raise ModemError("malformed +CSIM response") from exc
            if len(response) < 2:
                raise ModemError("truncated +CSIM response")
        except ModemError:
            if health and channel:
                health.observe(channel, True)
            raise
        if health and channel:
            health.observe(channel, response[-2:] == bytes.fromhex("6881"))
        if self.debug:
            status = response[-2:].hex().upper() if len(response) >= 2 else "unknown"
            print("[bridge] APDU <-- bytes=%d sw=%s" %
                  (max(0, len(response) - 2), status), flush=True)
        return response

    def _iccid_from_card(self):
        """Read EF_ICCID over AT+CSIM — the card's own answer on the basic channel.

        AT+CCID reports the baseband's cached SIM view, and an eSIM profile switch happens
        over AT+CSIM behind the baseband's back: its REFRESH goes unserved in passthrough,
        so the cache keeps the previous profile's ICCID indefinitely. The orchestrator
        verifies a post-switch bridge rebuild against this identity, so publishing the
        cache made every switch look stuck until the 45 s timeout (issue #26). Selecting
        MF afterwards restores the basic-channel file state the baseband expects.
        """
        try:
            for select in ("00A4080C022FE2", "00A40804022FE2"):
                response = self.csim(bytes.fromhex(select))
                if len(response) >= 2 and response[-2] in (0x90, 0x61):
                    break
            else:
                return ""
            response = self.csim(bytes.fromhex("00B000000A"))
            if len(response) != 12 or response[-2:] != b"\x90\x00":
                return ""
            return decode_bcd_iccid(response[:10])
        except ModemError:
            return ""
        finally:
            try:
                self.select_mf(0)
            except ModemError:
                pass

    def identity(self):
        """Read hardware IMEI and the currently inserted SIM ICCID while we own the AT port.

        The bridge is the only safe place to do this: the serial port is opened exclusively for
        its whole lifetime, so the control plane must consume published metadata instead of
        racing another AT client against APDU traffic.
        """
        imei = ""
        for command in ("AT+CGSN", "AT+GSN"):
            try:
                match = IMEI_RE.search(self._at(command))
                if match:
                    imei = match.group(1).decode("ascii")
                    break
            except ModemError:
                pass
        iccid = self._iccid_from_card()
        if not iccid:
            for command in ("AT+CCID", "AT+ICCID"):
                try:
                    match = ICCID_RE.search(self._at(command))
                    if match:
                        iccid = match.group(1).decode("ascii")
                        break
                except ModemError:
                    pass
        return {"imei": imei, "iccid": iccid}

    def close_channel(self, channel):
        try:
            # EC25's AT+CSIM rejects the four-byte Case-1 form as a bare AT ERROR. Include
            # Le=00 so MANAGE CHANNEL CLOSE is accepted and an interrupted bridge does not
            # leak channels until the modem is reset.
            self.csim(bytes((0x00, 0x70, 0x80, channel, 0x00)))
        except ModemError:
            pass

    def open_channel(self):
        response = self.csim(bytes.fromhex("0070000001"))
        if len(response) != 3 or response[-2:] != b"\x90\x00":
            raise ModemError("MANAGE CHANNEL OPEN failed: %s" % response.hex())
        channel = response[0]
        if channel not in (1, 2, 3):
            raise ModemError("unsupported logical channel allocated: %d" % channel)
        return channel

    @staticmethod
    def on_channel(apdu, channel):
        if len(apdu) < 2:
            return None, bytes.fromhex("6700")
        if apdu[0] == 0xFF:
            return None, bytes.fromhex("6D00")
        if apdu[1] == 0x70:
            # MANAGE CHANNEL. Each VPCD slot already owns one physical UICC logical channel,
            # opened when the bridge starts. PC/SC eUICC clients such as lpac still perform
            # the standard handshake, but forwarding it would allocate a different channel
            # or close the bridge-owned one behind its back. Emulate only the exact OPEN and
            # CLOSE forms lpac uses: OPEN reports the channel this slot already holds, while
            # CLOSE succeeds without releasing it. Reject every malformed/unsupported variant
            # with Incorrect parameters (6A86) so no ambiguous command reaches the modem.
            if apdu == bytes.fromhex("0070000001"):
                return None, bytes((channel, 0x90, 0x00))
            if apdu == bytes((0x00, 0x70, 0x80, channel, 0x00)):
                return None, bytes.fromhex("9000")
            return None, bytes.fromhex("6A86")
        # 0xA0 is the legacy GSM class and has no logical-channel encoding.
        if apdu[0] == 0xA0:
            return None, bytes.fromhex("6881")
        rewritten = bytes(((apdu[0] & 0xFC) | channel,)) + apdu[1:]
        return rewritten, None

    @staticmethod
    def closes_logical_channel(apdu, channel) -> bool:
        return apdu == bytes((0x00, 0x70, 0x80, channel, 0x00))

    def transmit(self, apdu, channel):
        rewritten, local_response = self.on_channel(apdu, channel)
        if local_response is not None:
            if self.closes_logical_channel(apdu, channel):
                # An LPA leaves the ISD-R selected here when it closes its channel, but the
                # slot is shared: pin_keeper and the engine come back to the same real UICC
                # channel expecting the plain USIM view, and their ADF.USIM select fails
                # against an eUICC application. pcscd only power-cycles the card when every
                # client is gone, so restore the file system now instead of waiting for it.
                try:
                    self.select_mf(channel)
                except ModemError as exc:
                    print("[bridge] slot channel %d could not restore MF after an LPA "
                          "session: %s" % (channel, exc), flush=True)
            return local_response
        try:
            return self.csim(rewritten)
        except ModemError as exc:
            print("[bridge] slot channel %d modem error: %s" % (channel, exc), flush=True)
            return bytes.fromhex("6F00")

    def select_mf(self, channel):
        apdu, _ = self.on_channel(bytes.fromhex("00A40004023F00"), channel)
        return self.csim(apdu)

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


class ModemManagerCard(ModemCard):
    """Send AT commands through ModemManager instead of racing its serial port.

    ModemManager remains the sole tty owner and serializes these commands with its QMI/AT
    activity.  This permits the VPCD APDU bridge to coexist experimentally with an active
    cellular bearer. ModemManager must be running with command support enabled (--debug).
    """

    def __init__(self, modem, timeout=10.0, debug=False):
        self.lock = threading.RLock()
        self.debug = debug
        self.timeout = timeout
        self.modem = str(modem)
        # Fail early with a harmless command and a clear error if command passthrough is off.
        self._at("AT")

    def _at(self, command):
        with self.lock:
            try:
                result = subprocess.run(
                    ["mmcli", "-m", self.modem, "--command=" + command],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self.timeout,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ModemError("ModemManager command failed: %s" % exc) from exc
            raw = result.stdout + result.stderr
            if result.returncode:
                raise ModemError(raw.decode("utf-8", "replace").strip())
            return raw

    def close(self):
        pass


def recv_exact(sock, size):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            return None
        chunks.extend(chunk)
    return bytes(chunks)


def serve_slot(arbiter, host, port, slot, atr, debug):
    channel = arbiter.channel
    delay = SLOT_RETRY_SECONDS
    reported = ""
    while True:
        sock = None
        try:
            sock = socket.create_connection((host, port), timeout=10)
            sock.settimeout(None)
            delay, reported = SLOT_RETRY_SECONDS, ""
            print(
                "[bridge] slot %d connected to %s:%d on channel %d"
                % (slot, host, port, channel),
                flush=True,
            )
            while True:
                header = recv_exact(sock, 2)
                if header is None:
                    break
                (length,) = struct.unpack(">H", header)
                if length == 0:
                    continue
                payload = recv_exact(sock, length)
                if payload is None:
                    break
                if length == 1:
                    control = payload[0]
                    if control == 0x04:
                        sock.sendall(struct.pack(">H", len(atr)) + atr)
                    elif control in (0x01, 0x02):
                        try:
                            arbiter.reset(slot)
                        except ModemError as exc:
                            print(
                                "[bridge] slot %d reset/select failed: %s"
                                % (slot, exc),
                                flush=True,
                            )
                    if debug:
                        print(
                            "[bridge] slot %d control 0x%02X" % (slot, control),
                            flush=True,
                        )
                    continue
                response = arbiter.transmit(slot, payload)
                sock.sendall(struct.pack(">H", len(response)) + response)
        except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
            # Repeating an unchanged reason says nothing the first line did not, so only a
            # new one is printed. Debug mode keeps every attempt for a live investigation.
            if debug or str(exc) != reported:
                reported = str(exc)
                print("[bridge] slot %d disconnected: %s" % (slot, exc), flush=True)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        time.sleep(delay)
        delay = min(delay * 2, SLOT_RETRY_CEILING_SECONDS)


def write_metadata(path, data):
    """Atomically publish modem identity for the control plane's /data bind mount."""
    if not path:
        return
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, sort_keys=True)
        handle.write("\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def retain_hardware_identity(previous, observed):
    """Keep a verified IMEI when a later AT refresh is temporarily incomplete.

    ICCID is deliberately *not* retained: an empty ICCID can be real card removal and has to
    reach the control plane.  The modem's hardware IMEI cannot change while this bridge owns
    the same physical device, so replacing it with an empty/garbled read only destroys the
    evidence needed by an automatic line rebuild.
    """
    result = dict(observed or {})
    imei = str(result.get("imei") or "")
    retained = str((previous or {}).get("imei") or "")
    if not re.fullmatch(r"\d{15}", imei) and re.fullmatch(r"\d{15}", retained):
        result["imei"] = retained
    return result


def refresh_metadata(card, path, static, interval, initial_identity=None,
                     stopping=None, metadata_lock=None):
    """Refresh ICCID after a hot SIM swap without competing for the exclusive AT port."""
    identity = dict(initial_identity or {})
    stopping = stopping or threading.Event()
    metadata_lock = metadata_lock or threading.Lock()
    while not stopping.wait(interval):
        try:
            identity = retain_hardware_identity(identity, card.identity())
            with metadata_lock:
                if not stopping.is_set():
                    write_metadata(path, {**static, **identity, "updated_at": int(time.time())})
        except Exception as exc:
            print("[bridge] identity refresh failed: %s" % exc, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-m", "--modem", default="/dev/ttyUSB2")
    parser.add_argument("-H", "--host", default="127.0.0.1")
    parser.add_argument("-p", "--base-port", type=int, default=0x8C7B)
    parser.add_argument("-n", "--slots", type=int, default=3, choices=(1, 2, 3))
    parser.add_argument("-a", "--atr", default=DEFAULT_ATR.hex())
    parser.add_argument("--metadata-file", default="",
                        help="publish modem IMEI/ICCID JSON for the control plane")
    parser.add_argument("--hardware-id", default="",
                        help="stable physical modem id assigned by the host orchestrator")
    parser.add_argument("--modemmanager", default="",
                        help="route AT commands through this ModemManager modem index/path")
    parser.add_argument("--identity-refresh", type=int, default=15,
                        help="seconds between IMEI/ICCID metadata refreshes (0 disables)")
    parser.add_argument("-d", "--debug", action="store_true")
    args = parser.parse_args()

    card = (ModemManagerCard(args.modemmanager, debug=args.debug)
            if args.modemmanager else ModemCard(args.modem, debug=args.debug))
    identity = card.identity()
    static_metadata = {
        "version": 1,
        # Lets the orchestrator prove that a ready metadata document came from the
        # replacement bridge, rather than accepting the previous process's last write.
        "bridge_pid": os.getpid(),
        "modem": os.path.realpath(args.modem),
        "base_port": args.base_port,
        "slots": args.slots,
        "hardware_id": args.hardware_id,
        **logical_channel_metadata([], args.slots, "allocating"),
    }
    write_metadata(args.metadata_file, {**static_metadata, **identity,
                                        "updated_at": int(time.time())})
    print("[bridge] modem identity refreshed (imei=%s iccid=%s)" %
          ("available" if identity["imei"] else "unavailable",
           "available" if identity["iccid"] else "unavailable"), flush=True)
    # Clear channels left behind by an interrupted prior bridge.
    for channel in (1, 2, 3):
        card.close_channel(channel)

    try:
        channels = allocate_logical_channels(card, args.slots)
    except ModemError as exc:
        static_metadata.update(logical_channel_metadata([], args.slots, "error", str(exc)))
        write_metadata(args.metadata_file, {**static_metadata, **identity,
                                            "updated_at": int(time.time())})
        print("[bridge] %s" % exc, flush=True)
        card.close()
        raise
    slot_channels = slot_channel_map(channels, args.slots)
    static_metadata.update(logical_channel_metadata(channels, args.slots, "ready",
                                                    slot_channels=slot_channels))
    write_metadata(args.metadata_file, {**static_metadata, **identity,
                                        "updated_at": int(time.time())})
    print("[bridge] allocated logical channels %r" % channels, flush=True)
    if len(set(slot_channels)) < len(slot_channels):
        print("[bridge] slot channel map %r (shared)" % slot_channels, flush=True)

    # Start monitoring only after allocation. Basic-channel identity refreshes must not
    # erase the failures of a dead supplementary channel.
    card.access_health = SIMAccessHealth()

    atr = bytes.fromhex(args.atr)
    arbiters = {channel: ChannelArbiter(card, channel) for channel in channels}
    threads = []
    for slot, channel in enumerate(slot_channels):
        thread = threading.Thread(
            target=serve_slot,
            args=(
                arbiters[channel],
                args.host,
                args.base_port + slot,
                slot,
                atr,
                args.debug,
            ),
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    stopping = threading.Event()
    metadata_lock = threading.Lock()
    if args.metadata_file and args.identity_refresh > 0:
        threading.Thread(target=refresh_metadata,
                         args=(card, args.metadata_file, static_metadata,
                               args.identity_refresh, identity, stopping, metadata_lock),
                         daemon=True).start()

    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    try:
        while (not stopping.is_set() and not card.access_health.failed.is_set()
               and all(thread.is_alive() for thread in threads)):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stopping.set()
        # Release the UICC logical channels before handing the modem to ModemManager.
        # A raw SIGTERM used to leak all three channels until the next modem reset.
        for channel in channels:
            card.close_channel(channel)
        failed = card.access_health.failed.is_set()
        error = "SIM access failed repeatedly; rebuilding logical channels" if failed else ""
        static_metadata.update(logical_channel_metadata(
            [], args.slots, "error" if failed else "stopped", error))
        with metadata_lock:
            write_metadata(args.metadata_file, {**static_metadata, **identity,
                                                "updated_at": int(time.time())})
        card.close()
    if failed:
        print("[bridge] %s" % error, flush=True)
        return SIM_ACCESS_RECOVERY_EXIT
    return 0


if __name__ == "__main__":
    sys.exit(main())
