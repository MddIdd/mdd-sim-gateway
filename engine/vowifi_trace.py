#!/usr/bin/env python3
"""Expiring, metadata-only VoWiFi flight recorder. Never enable raw SIP logging."""
from __future__ import annotations

import concurrent.futures
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time

MAX_SECONDS = 3600
MAX_FILE_BYTES = 512 * 1024
LABEL_ALPHABET = str.maketrans('0123456789abcdef', 'abcdefghijklmnop')
SESSION = re.compile(r'^tr-[a-f0-9]{16}$')
SALT = re.compile(r'^[a-f0-9]{64}$')
REGISTRATIONS = {'Registered', 'Rejected', 'Unregistered', 'Stopped'}
TCP_STATES = {'ESTAB', 'LISTEN', 'SYN-SENT', 'SYN-RECV', 'FIN-WAIT-1', 'FIN-WAIT-2',
              'TIME-WAIT', 'CLOSE', 'CLOSE-WAIT', 'LAST-ACK', 'CLOSING'}


def label(value, salt, prefix='ip'):
    value = str(value)
    if prefix == 'ip':
        try:
            value = str(ipaddress.ip_address(value.split('%', 1)[0]))
        except ValueError:
            return None
    digest = hashlib.sha1((salt + '/' + value).encode()).hexdigest()[:16]
    return prefix + '-' + digest.translate(LABEL_ALPHABET)


def read_json(path):
    try:
        if path.stat().st_size > 8192:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def active_request(value, now=None):
    now = time.time() if now is None else now
    try:
        created, expires = int(value['created_at']), int(value['expires_at'])
        if not SESSION.fullmatch(value['session']) or not SALT.fullmatch(value['salt']):
            return None
        if not 0 < expires - created <= MAX_SECONDS or not created - 5 <= now < expires:
            return None
        return {key: value[key] for key in ('session', 'salt', 'created_at', 'expires_at')}
    except (KeyError, TypeError, ValueError):
        return None


def write_private(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(value if isinstance(value, str) else json.dumps(value))
    os.replace(temporary, path)


def append_record(path, record):
    line = json.dumps(record, separators=(',', ':')) + '\n'
    try:
        if path.stat().st_size + len(line.encode()) > MAX_FILE_BYTES:
            os.replace(path, path.with_name(path.name + '.1'))
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'a') as handle:
        handle.write(line)


def endpoint(value, salt):
    value = value.strip('[]')
    direct = label(value, salt)
    if direct:
        return {'address': direct, 'port': 0}
    if value.startswith('[') and ']:' in value:
        address, port = value[1:].rsplit(']:', 1)
    elif ':' in value:
        address, port = value.rsplit(':', 1)
    elif '.' in value:
        address, port = value.rsplit('.', 1)
    else:
        return None
    # tcpdump uses a dot before an IPv6 TCP port; ss uses a colon.
    if not port.isdigit() and '.' in value:
        address, port = value.rsplit('.', 1)
    if value.count(':') > 1 and '.' in value:
        address, port = value.rsplit('.', 1)
    address = label(address.strip('[]'), salt)
    if address and port.isdigit() and 0 <= int(port) <= 65535:
        return {'address': address, 'port': int(port)}
    return None


def registration(text):
    for line in text.splitlines():
        if not line.lstrip().startswith('volte_ims/'):
            continue
        match = re.search(r'\b(Registered|Rejected|Unregistered|Stopped)\b', line)
        if match:
            result = {'registration': match[1]}
            expiry = re.search(r'\(exp\.\s+(\d+)s( ago)?\)', line)
            if expiry:
                result['next_refresh'] = min(604800, int(expiry[1])) * (-1 if expiry[2] else 1)
            return result
    return {'registration': 'unknown'}


def sockets(text, salt):
    result = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] not in TCP_STATES:
            continue
        local, peer = endpoint(parts[3], salt), endpoint(parts[4], salt)
        if local:
            row = {'state': parts[0], 'local': local}
            if peer:
                row['peer'] = peer
            result.append(row)
    return result[:64]


def xfrm_states(text, salt):
    result, current = [], None
    for line in text.splitlines():
        match = re.match(r'^src (\S+) dst (\S+)', line)
        if match:
            source, destination = label(match[1], salt), label(match[2], salt)
            current = {'source': source, 'destination': destination} if source and destination else None
            if current is not None:
                result.append(current)
            continue
        if current is None:
            continue
        match = re.search(r'\bproto (esp|ah) spi (0x[0-9a-fA-F]{1,8}) reqid (\d+) mode (transport|tunnel|beet)', line)
        if match:
            current.update(proto=match[1], spi=int(match[2], 16),
                           reqid=min(int(match[3]), 0xffffffff), mode=match[4])
        match = re.search(r'(\d+)\(bytes\),\s*(\d+)\(packets\)', line)
        if match:
            current.update(bytes=int(match[1]), packets=int(match[2]))
        match = re.search(r'replay-window (\d+) replay (\d+) failed (\d+)', line)
        if match:
            current.update(replay_window=int(match[1]), replay=int(match[2]), failed=int(match[3]))
        # Authentication/encryption lines, keys and every unknown line are ignored.
    return result[:32]


def packet(text, salt):
    match = re.match(r'^(\d+\.\d+)\s+IP6?\s+(.*?) > (.*?):\s+(.*)$', text)
    if not match:
        return None
    source, target = match[2], match[3]
    def tcp_endpoint(value):
        if '.' in value:
            address, port = value.rsplit('.', 1)
            if label(address, salt) and port.isdigit():
                return {'address': label(address, salt), 'port': min(int(port), 65535)}
        return endpoint(value, salt)
    source, target = tcp_endpoint(source), tcp_endpoint(target)
    if not source or not target:
        return None
    detail = match[4]
    result = {'kind': 'packet', 'ts': float(match[1]), 'source': source, 'destination': target}
    esp = re.search(r'ESP\(spi=(0x[0-9a-fA-F]{1,8}),seq=(0x[0-9a-fA-F]+|\d+)\)', detail)
    flags = re.search(r'Flags \[([.SFPRUAECW]{1,9})\]', detail)
    if esp:
        result.update(proto='esp', spi=int(esp[1], 16), sequence=int(esp[2], 16 if esp[2].startswith('0x') else 10))
    elif flags:
        result.update(proto='tcp', flags=flags[1])
    else:
        return None
    length = re.search(r'\blength (\d+)', detail)
    if length:
        result['bytes'] = min(int(length[1]), 65535)
    return result


class Recorder:
    def __init__(self, root=Path('/logs/vowifi-trace'), run=None):
        self.root = Path(root)
        self.run_dir = Path(run or os.environ.get('MDD_RUNDIR', '/run/mdd-sim-gateway'))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.marker = self.run_dir / 'vowifi-trace.enabled'
        self.stop = threading.Event()
        self.request = None
        self.monotonic_end = 0
        self.completed_session = None
        self.capture = None
        self.capture_thread = None
        self.capture_index = None
        self.packet_count = 0
        self.packet_dropped = 0
        self.packet_window = (0, 0)
        self.last_sample = 0
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self.log_lock = threading.Lock()

    def emit(self, name, value):
        with self.log_lock:
            append_record(self.root / (name + '.jsonl'), {'session': self.request['session'], **value})

    def command(self, item):
        name, args = item
        try:
            response = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      text=True, errors='replace', timeout=1.5)
            return name, response.stdout[:65536] if response.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return name, None

    def sample(self):
        values = dict(self.pool.map(self.command, [
            ('registration', ['asterisk', '-rx', 'pjsip show registrations']),
            ('sockets', ['ss', '-H', '-nta']),
            ('xfrm', ['ip', '-s', 'xfrm', 'state', 'list', 'nokeys']),
            ('addresses', ['ip', '-6', '-o', 'addr', 'show', 'dev', 'ipsec0']),
        ]))
        salt = self.request['salt']
        runtime = read_json(self.run_dir / 'swu_status.json')
        state = runtime.get('state')
        sample = {'kind': 'snapshot', 'ts': int(time.time()),
                  'tunnel': state if state in {'CONNECTED', 'CONNECTING', 'DOWN'} else 'unknown',
                  **registration(values['registration'] or ''),
                  'sockets': sockets(values['sockets'] or '', salt),
                  'xfrm': xfrm_states(values['xfrm'] or '', salt),
                  'unavailable': [name for name, value in values.items() if value is None]}
        for key in ('inner_ip', 'pcscf'):
            address = label(runtime.get(key, ''), salt)
            if address:
                sample[key] = address
        addresses = []
        for value in re.findall(r'\binet6 (\S+)', values['addresses'] or ''):
            try:
                interface = ipaddress.ip_interface(value)
                addresses.append({'address': label(str(interface.ip), salt), 'prefix': interface.network.prefixlen})
            except ValueError:
                pass
        sample['addresses'] = addresses[:16]
        try:
            counters = {}
            for line in Path('/proc/net/xfrm_stat').read_text().splitlines():
                key, value = line.split()
                if re.fullmatch(r'Xfrm[A-Za-z]{1,40}', key) and value.isdigit():
                    counters[key] = int(value)
            sample['xfrm_errors'] = counters
        except (OSError, ValueError):
            sample['unavailable'].append('xfrm_errors')
        self.emit('snapshots', sample)
        self.last_sample = sample['ts']
        self.status(True, sample['unavailable'])

    def status(self, active, unavailable=()):
        write_private(self.root / 'status.json', {
            'session': self.request['session'], 'active': active, 'updated_at': int(time.time()),
            'last_sample': self.last_sample, 'packets': self.packet_count,
            'packet_dropped': self.packet_dropped, 'unavailable': list(unavailable),
            'packet_capture': 'running' if self.capture and self.capture.poll() is None else 'waiting',
        })

    def stop_capture(self):
        if self.capture:
            if self.capture.poll() is None:
                self.capture.terminate()
                try:
                    self.capture.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.capture.kill()
                    self.capture.wait(timeout=1)
            if self.capture_thread:
                self.capture_thread.join(timeout=1)
            self.capture = None
            self.capture_thread = None

    def ensure_capture(self):
        try:
            index = Path('/sys/class/net/ipsec0/ifindex').read_text().strip()
        except OSError:
            index = None
        if index != self.capture_index:
            self.stop_capture()
            self.capture_index = index
        if index is None or (self.capture and self.capture.poll() is None):
            return
        self.stop_capture()
        try:
            self.capture = subprocess.Popen(
                ['tcpdump', '-p', '-nn', '-tt', '-l', '-i', 'ipsec0',
                 'tcp or ip proto 50 or ip6 proto 50'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors='replace')
        except OSError:
            return
        process = self.capture
        salt = self.request['salt']
        def consume():
            try:
                for line in process.stdout:
                    value = packet(line, salt)
                    if value is None:
                        continue
                    second, count = self.packet_window
                    now = int(time.monotonic())
                    count = count + 1 if second == now else 1
                    self.packet_window = now, count
                    if count > 50:
                        self.packet_dropped += 1
                        continue
                    self.emit('packets', value)
                    self.packet_count += 1
            finally:
                process.stdout.close()
        self.capture_thread = threading.Thread(target=consume, daemon=True)
        self.capture_thread.start()

    def finish(self):
        self.marker.unlink(missing_ok=True)
        self.stop_capture()
        if self.request:
            self.status(False)
            self.request = None

    def run(self):
        os.umask(0o077)
        lock = (self.root / '.lock').open('a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        next_sample = 0
        try:
            while not self.stop.is_set():
                request = active_request(read_json(self.root / 'request.json'))
                if request is None or request['session'] == self.completed_session:
                    self.finish()
                else:
                    if not self.request or request['session'] != self.request['session']:
                        self.finish()
                        self.request = request
                        self.packet_count = self.packet_dropped = self.last_sample = 0
                        self.monotonic_end = time.monotonic() + min(MAX_SECONDS, request['expires_at'] - time.time())
                        write_private(self.marker, f"{request['expires_at']} {request['salt']} {request['session']}\n")
                        next_sample = 0
                    if time.monotonic() >= self.monotonic_end:
                        self.completed_session = self.request['session']
                        self.finish()
                        # A backwards clock must not reopen the same expired session.
                        self.stop.wait(1)
                        continue
                    self.ensure_capture()
                    if time.monotonic() >= next_sample:
                        self.sample()
                        next_sample = time.monotonic() + 5
                self.stop.wait(0.5)
        finally:
            self.finish()
            self.pool.shutdown(wait=True)
            lock.close()


def main():
    recorder = Recorder()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: recorder.stop.set())
    try:
        recorder.run()
    except BlockingIOError:
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
