"""Administrator-controlled, expiring VoWiFi evidence and closed-schema bundle export."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import threading
import time

from . import config as cfg

MAX_SECONDS = 3600
KINDS = {'sip', 'snapshot', 'packet', 'mark'}
RESULTS = {'incoming_failed', 'incoming_rang', 'incoming_answered', 'outgoing_failed', 'outgoing_answered'}
LABEL = re.compile(r'^(?:ip|host|call|user)-[a-p]{16}$')
SESSION = re.compile(r'^tr-[0-9a-f]{16}$')
ENUMS = KINDS | RESULTS | {
    'rx', 'tx', 'tcp', 'udp', 'ws', 'tls', 'other', 'esp', 'ah', 'transport', 'tunnel', 'beet',
    'REGISTER', 'INVITE', 'ACK', 'CANCEL', 'BYE', 'MESSAGE', 'OPTIONS', 'SUBSCRIBE',
    'NOTIFY', 'PRACK', 'UPDATE', 'INFO', 'REFER', 'PUBLISH', 'OTHER',
    'CONNECTED', 'CONNECTING', 'DOWN', 'unknown', 'Registered', 'Rejected', 'Unregistered', 'Stopped',
    'ESTAB', 'LISTEN', 'SYN-SENT', 'SYN-RECV', 'FIN-WAIT-1', 'FIN-WAIT-2', 'TIME-WAIT',
    'CLOSE', 'CLOSE-WAIT', 'LAST-ACK', 'CLOSING', 'registration', 'sockets', 'xfrm', 'addresses', 'xfrm_errors',
}
KEYS = {'kind', 'session', 'ts', 'direction', 'method', 'code', 'cseq', 'transport', 'local', 'peer',
        'address', 'port', 'call', 'uri', 'user', 'contacts', 'expires', 'security_client',
        'security_server', 'security_verify', 'port_c', 'port_s', 'spi_c', 'spi_s', 'tunnel',
        'registration', 'next_refresh', 'sockets', 'xfrm', 'unavailable', 'inner_ip', 'pcscf',
        'addresses', 'prefix', 'xfrm_errors', 'state', 'source', 'destination', 'proto', 'spi',
        'reqid', 'mode', 'bytes', 'packets', 'replay_window', 'replay', 'failed', 'sequence', 'flags', 'result'}
XFRM_COUNTERS = {'Xfrm' + name for name in (
    'InError', 'InBufferError', 'InHdrError', 'InNoStates', 'InStateProtoError', 'InStateModeError',
    'InStateSeqError', 'InStateExpired', 'InStateMismatch', 'InStateInvalid', 'InTmplMismatch',
    'InNoPols', 'InPolBlock', 'InPolError', 'OutError', 'OutBundleGenError', 'OutBundleCheckError',
    'OutNoStates', 'OutStateProtoError', 'OutStateModeError', 'OutStateSeqError', 'OutStateExpired',
    'OutPolBlock', 'OutPolDead', 'OutPolError', 'FwdHdrError', 'OutStateInvalid', 'AcquireError')}
_LOCK = threading.RLock()


def directory(iid):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', str(iid)):
        raise ValueError('Invalid line')
    return Path(cfg.DATA_DIR) / 'instances' / str(iid) / 'logs' / 'vowifi-trace'


def read_json(path):
    try:
        if path.stat().st_size > 8192:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle)
    os.replace(temporary, path)


def status(iid):
    root = directory(iid)
    request, worker = read_json(root / 'request.json'), read_json(root / 'status.json')
    now = int(time.time())
    session = str(request.get('session') or '')
    valid = bool(SESSION.fullmatch(session))
    try:
        created, expires = int(request.get('created_at', 0)), int(request.get('expires_at', 0))
    except (TypeError, ValueError):
        created = expires = 0
    active = valid and 0 < expires - created <= MAX_SECONDS and created - 5 <= now < expires
    same = valid and worker.get('session') == session
    def number(key):
        try:
            return max(0, int(worker.get(key) or 0)) if same else 0
        except (TypeError, ValueError):
            return 0
    updated = number('updated_at')
    return {'active': bool(active), 'session': session if valid else '', 'created_at': created,
            'expires_at': expires, 'remaining_seconds': max(0, expires - now) if active else 0,
            'recording': bool(active and same and worker.get('active') is True and 0 <= now - updated <= 20),
            'last_sample': number('last_sample'), 'packets': number('packets'),
            'packet_dropped': number('packet_dropped'),
            'packet_capture': worker.get('packet_capture') if same and worker.get('packet_capture') in {'running', 'waiting'} else 'waiting',
            'unavailable': [name for name in worker.get('unavailable', []) if name in {'registration', 'sockets', 'xfrm', 'addresses', 'xfrm_errors'}] if same and isinstance(worker.get('unavailable'), list) else [],
            'has_records': any(root.glob('*.jsonl*'))}


def start(iid, minutes=60):
    if type(minutes) is not int or minutes not in (15, 30, 60):
        raise ValueError('Choose 15, 30 or 60 minutes')
    with _LOCK:
        if status(iid)['active']:
            raise ValueError('A recording is already active')
        root = directory(iid)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ('sip', 'snapshots', 'packets', 'marks'):
            for suffix in ('.jsonl', '.jsonl.1'):
                (root / (name + suffix)).unlink(missing_ok=True)
        (root / 'status.json').unlink(missing_ok=True)
        now = int(time.time())
        write_private(root / 'request.json', {'session': 'tr-' + secrets.token_hex(8),
                      'salt': secrets.token_hex(32), 'created_at': now, 'expires_at': now + minutes * 60})
        return status(iid)


def stop(iid):
    with _LOCK:
        root = directory(iid)
        request = read_json(root / 'request.json')
        if request:
            request['expires_at'] = min(int(time.time()), int(request.get('expires_at') or 0))
            write_private(root / 'request.json', request)
        # Stop native SIP summaries immediately; the worker observes the request on its next tick.
        (root.parent.parent / 'run' / 'vowifi-trace.enabled').unlink(missing_ok=True)
        return status(iid)


def mark(iid, result):
    if not isinstance(result, str) or result not in RESULTS:
        raise ValueError('Choose a test result')
    with _LOCK:
        current = status(iid)
        if not current['active']:
            raise ValueError('Start a recording first')
        path = directory(iid) / 'marks.jsonl'
        record = {'kind': 'mark', 'session': current['session'], 'ts': int(time.time()), 'result': result}
        if path.exists() and path.stat().st_size > 65536:
            raise ValueError('Too many test markers')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a') as handle:
            handle.write(json.dumps(record) + '\n')
        return record


def _clean(value, key='', depth=0):
    if depth > 7:
        return None
    if isinstance(value, dict):
        return {name: cleaned for name, item in value.items()
                if name in KEYS | XFRM_COUNTERS
                and (cleaned := _clean(item, name, depth + 1)) is not None}
    if isinstance(value, list):
        return [cleaned for item in value[:64] if (cleaned := _clean(item, key, depth + 1)) is not None]
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not -604800 <= value <= 2**63 - 1:
            return None
        limits = {'port': 65535, 'port_c': 65535, 'port_s': 65535, 'code': 699,
                  'prefix': 128, 'expires': 604800, 'next_refresh': 604800,
                  'cseq': 0x7fffffff, 'spi': 0xffffffff, 'spi_c': 0xffffffff, 'spi_s': 0xffffffff}
        return value if value <= limits.get(key, 2**63 - 1) and (value >= 0 or key in {'expires', 'next_refresh'}) else None
    if isinstance(value, str):
        if value in ENUMS or LABEL.fullmatch(value):
            return value
        if key == 'session' and SESSION.fullmatch(value):
            return value
        if key == 'flags' and re.fullmatch(r'[.SFPRUAECW]{1,9}', value):
            return value
    return None


def export_line(line):
    """Reject arbitrary strings even if a corrupt recorder/manual file contains them."""
    try:
        record = json.loads(line)
    except (TypeError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get('kind'), str) or record['kind'] not in KINDS:
        return None
    if not isinstance(record.get('session'), str) or not SESSION.fullmatch(record['session']):
        return None
    if not isinstance(record.get('ts'), (int, float)) or isinstance(record['ts'], bool):
        return None
    return json.dumps(_clean(record), ensure_ascii=False, sort_keys=True)
