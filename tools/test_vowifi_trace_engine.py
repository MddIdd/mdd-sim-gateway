#!/usr/bin/env python3
"""Exercise the compiled metadata recorder inside an isolated Engine image (no SIM/network)."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time


def inside():
    with tempfile.TemporaryDirectory(prefix='mdd-trace-smoke-') as temporary:
        root = Path(temporary)
        for name in ('run', 'log', 'spool'):
            (root / name).mkdir()
        output = Path('/logs/vowifi-trace')
        output.mkdir(parents=True, mode=0o700)
        salt = 'a' * 64
        marker = root / 'run/vowifi-trace.enabled'
        marker.write_text(f'{int(time.time()) + 120} {salt} tr-abcdefabcdefabcd\n')
        (root / 'asterisk.conf').write_text(f'''[directories]
astetcdir = {root}
astmoddir = /usr/lib/asterisk/modules
astvarlibdir = /var/lib/asterisk
astdbdir = {root}
astkeydir = {root}
astdatadir = /var/lib/asterisk
astagidir = /var/lib/asterisk/agi-bin
astspooldir = {root}/spool
astrundir = {root}/run
astlogdir = {root}/log
''')
        (root / 'modules.conf').write_text('[modules]\nautoload=yes\n')
        (root / 'logger.conf').write_text('[logfiles]\nconsole=error\n')
        (root / 'pjsip.conf').write_text('[trace-test]\ntype=transport\nprotocol=tcp\nbind=127.0.0.1:15060\n')
        config = str(root/'asterisk.conf')
        startup = root/'startup.log'
        with startup.open('w') as log:
            process = subprocess.Popen(['asterisk', '-f', '-C', config],
                                       stdout=log, stderr=subprocess.STDOUT,
                                       env={**os.environ, 'MDD_RUNDIR': str(root/'run')})
        try:
            deadline = time.monotonic() + 25
            while True:
                try:
                    # The transport binds while res_pjsip is loading, before the logger
                    # module necessarily registers its callbacks. Wait for the recorder.
                    ready = subprocess.run(['asterisk', '-C', config, '-rx',
                                            'module show like res_pjsip_logger.so'],
                                           capture_output=True, text=True, timeout=1)
                    if 'res_pjsip_logger.so' not in ready.stdout or 'Running' not in ready.stdout:
                        raise OSError('Recorder module is not running yet')
                    connection = socket.create_connection(('127.0.0.1', 15060), timeout=1)
                    break
                except (OSError, subprocess.TimeoutExpired):
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise AssertionError('Asterisk test transport did not start')
                    time.sleep(0.2)
            connection.settimeout(3)
            def send(sequence):
                body = 'never-export-this-body'
                message = (f'OPTIONS sip:private-receiver@example.invalid SIP/2.0\r\n'
                           f'Via: SIP/2.0/TCP 127.0.0.1:15061;branch=z9hG4bKtrace{sequence};rport\r\n'
                           'From: <sip:private-caller@example.invalid>;tag=private-tag\r\n'
                           'To: <sip:private-receiver@example.invalid>\r\n'
                           'Call-ID: never-export-this-call-id@example.invalid\r\n'
                           f'CSeq: {sequence} OPTIONS\r\n'
                           'Max-Forwards: 10\r\n'
                           'Contact: <sip:private-contact@127.0.0.1:15061>;expires=42\r\n'
                           'Authorization: Digest username="private-auth", realm="private-realm", nonce="private-nonce", uri="sip:private-receiver@example.invalid", response="private-response"\r\n'
                           'Security-Verify: ipsec-3gpp;port-c=42001;port-s=52001;spi-c=4660;spi-s=22136\r\n'
                           'Content-Type: text/plain\r\n'
                           f'Content-Length: {len(body)}\r\n\r\n{body}')
                connection.sendall(message.encode())
                try:
                    connection.recv(65536)
                except socket.timeout:
                    pass
            send(1)
            deadline = time.monotonic() + 5
            path = output/'sip.jsonl'
            def has_tx():
                try:
                    return any(json.loads(line).get('direction') == 'tx' for line in path.read_text().splitlines())
                except (OSError, ValueError):
                    return False
            while not has_tx() and time.monotonic() < deadline:
                time.sleep(0.1)
            assert path.exists(), 'Compiled SIP recorder produced no metadata'
            text = path.read_text()
            records = [json.loads(line) for line in text.splitlines()]
            incoming = next(record for record in records if record['direction']=='rx' and record['cseq']==1)
            assert incoming['method']=='OPTIONS' and incoming['transport']=='tcp', incoming
            assert incoming['contacts'][0]['expires']==42, incoming
            assert incoming['security_verify']=={'port_c':42001,'port_s':52001,'spi_c':4660,'spi_s':22136}, incoming
            digest=hashlib.sha1((salt+'/127.0.0.1').encode()).hexdigest()[:16]
            expected='ip-'+digest.translate(str.maketrans('0123456789abcdef','abcdefghijklmnop'))
            assert incoming['peer']['address']==expected, incoming
            assert any(record['direction']=='tx' for record in records), records
            for secret in ('private-', 'never-export', 'Authorization', '127.0.0.1', salt):
                assert secret not in text, secret
            marker.write_text(f'{int(time.time())-1} {salt} tr-abcdefabcdefabcd\n')
            count=len(records)
            send(2);time.sleep(0.3)
            assert len(path.read_text().splitlines())==count, 'Expired marker continued recording'
            connection.close()
            print('Native recorder: TCP receive/transmit, correlation, Contact expiry, security ports/SPIs, source anonymization and expiry passed.')
        except BaseException:
            print('Asterisk process status:', process.poll(), flush=True)
            print(startup.read_text(errors='replace')[-4000:], flush=True)
            raise
        finally:
            process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=2)


def main():
    if len(sys.argv)==2 and sys.argv[1]=='--inside':
        inside()
        return 0
    if len(sys.argv)!=2:
        raise SystemExit('Usage: python tools/test_vowifi_trace_engine.py IMAGE')
    subprocess.run(['docker','run','--rm','--network','none','-i','--entrypoint','python3',
                    sys.argv[1],'-','--inside'],input=Path(__file__).read_bytes(),check=True,timeout=45)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
