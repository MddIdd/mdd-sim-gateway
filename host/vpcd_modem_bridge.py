#!/usr/bin/env python3
"""Expose modem AT+CSIM as three independent VPCD slots.

Each VPCD slot is mapped to one ISO 7816 logical channel.  The modem has one
physical AT port, so all AT commands are serialized while the selected-file
state remains isolated by the UICC logical channels.
"""

import argparse
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


class ModemError(RuntimeError):
    pass


def logical_channel_metadata(channels, requested=3, status="ready", error=""):
    """Return stable, UI-safe resource metadata for one physical UICC."""
    values = [int(channel) for channel in channels]
    return {
        "channel_capacity": LOGICAL_CHANNEL_CAPACITY,
        "channel_requested": int(requested),
        "channel_allocated": len(values),
        "channel_status": str(status),
        "channel_error": str(error),
        "logical_channels": [
            {"slot": slot, "channel": channel, "role": LOGICAL_CHANNEL_ROLES[slot]}
            for slot, channel in enumerate(values)
        ],
    }


def allocate_logical_channels(card, count):
    """Allocate unique UICC channels and clean up partial allocations on failure."""
    channels = []
    try:
        for _slot in range(int(count)):
            channel = card.open_channel()
            if channel in channels:
                raise ModemError("duplicate logical channel allocated: %d" % channel)
            channels.append(channel)
        return channels
    except Exception as exc:
        for channel in channels:
            card.close_channel(channel)
        raise ModemError(
            "SIM logical channel allocation failed (%d/%d allocated): %s" %
            (len(channels), LOGICAL_CHANNEL_CAPACITY, exc)) from exc


class ModemCard:
    def __init__(self, port, baud=115200, timeout=10.0, debug=False):
        if serial is None:
            raise ModemError("pyserial is required for direct modem access")
        self.lock = threading.RLock()
        self.debug = debug
        self.timeout = timeout
        self.ser = serial.Serial(
            port, baud, timeout=0.25, write_timeout=2, exclusive=True
        )
        self._drain()
        self._at("ATE0")
        self._at("AT+CMEE=2")

    def _drain(self):
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if not self.ser.read(4096):
                break

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
        raw = self._at('AT+CSIM=%d,"%s"' % (len(hex_apdu), hex_apdu))
        match = CSIM_RE.search(raw)
        if not match:
            raise ModemError("missing +CSIM response: %r" % raw[-200:])
        response = bytes.fromhex(match.group(2).decode("ascii"))
        if self.debug:
            status = response[-2:].hex().upper() if len(response) >= 2 else "unknown"
            print("[bridge] APDU <-- bytes=%d sw=%s" %
                  (max(0, len(response) - 2), status), flush=True)
        return response

    def identity(self):
        """Read hardware IMEI and the currently inserted SIM ICCID while we own the AT port.

        The bridge is the only safe place to do this: the serial port is opened exclusively for
        its whole lifetime, so the control plane must consume published metadata instead of
        racing another AT client against APDU traffic.
        """
        imei = ""
        iccid = ""
        for command in ("AT+CGSN", "AT+GSN"):
            try:
                match = IMEI_RE.search(self._at(command))
                if match:
                    imei = match.group(1).decode("ascii")
                    break
            except ModemError:
                pass
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
            return None, bytes.fromhex("6881")
        # 0xA0 is the legacy GSM class and has no logical-channel encoding.
        if apdu[0] == 0xA0:
            return None, bytes.fromhex("6881")
        rewritten = bytes(((apdu[0] & 0xFC) | channel,)) + apdu[1:]
        return rewritten, None

    def transmit(self, apdu, channel):
        rewritten, local_response = self.on_channel(apdu, channel)
        if local_response is not None:
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


def serve_slot(card, host, port, slot, channel, atr, debug):
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
                            card.select_mf(channel)
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
                response = card.transmit(payload, channel)
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


def refresh_metadata(card, path, static, interval):
    """Refresh ICCID after a hot SIM swap without competing for the exclusive AT port."""
    while True:
        time.sleep(interval)
        try:
            write_metadata(path, {**static, **card.identity(), "updated_at": int(time.time())})
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
    static_metadata.update(logical_channel_metadata(channels, args.slots, "ready"))
    write_metadata(args.metadata_file, {**static_metadata, **identity,
                                        "updated_at": int(time.time())})
    print("[bridge] allocated logical channels %r" % channels, flush=True)

    atr = bytes.fromhex(args.atr)
    threads = []
    for slot, channel in enumerate(channels):
        thread = threading.Thread(
            target=serve_slot,
            args=(
                card,
                args.host,
                args.base_port + slot,
                slot,
                channel,
                atr,
                args.debug,
            ),
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    if args.metadata_file and args.identity_refresh > 0:
        threading.Thread(target=refresh_metadata,
                         args=(card, args.metadata_file, static_metadata,
                               args.identity_refresh), daemon=True).start()

    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    try:
        while not stopping.is_set() and all(thread.is_alive() for thread in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        # Release the UICC logical channels before handing the modem to ModemManager.
        # A raw SIGTERM used to leak all three channels until the next modem reset.
        for channel in channels:
            card.close_channel(channel)
        static_metadata.update(logical_channel_metadata([], args.slots, "stopped"))
        write_metadata(args.metadata_file, {**static_metadata, **identity,
                                            "updated_at": int(time.time())})
        card.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
