import asyncio
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import zipfile
from io import BytesIO
from unittest.mock import patch

from control.app import config, main, operations, vowifi_trace
from control.app import authz, gate
from engine import vowifi_trace as worker

SALT = 'a' * 64
SESSION = 'tr-abcdefabcdefabcd'


def request(now):
    return {'session': SESSION, 'salt': SALT, 'created_at': now, 'expires_at': now + 60}


class TraceParsingTests(unittest.TestCase):
    def test_addresses_are_canonical_and_only_correlate_within_one_session(self):
        first = worker.label('2001:db8::1', SALT)
        self.assertEqual(first, worker.label('2001:0db8:0000:0000:0000:0000:0000:0001', SALT))
        self.assertNotEqual(first, worker.label('2001:db8::1', 'b' * 64))
        self.assertRegex(first, r'^ip-[a-p]{16}$')
        self.assertIsNone(worker.label('private.example.invalid', SALT))

    def test_registration_does_not_keep_the_subscriber_uri(self):
        result = worker.registration('volte_ims/sip:private-user@example.invalid volte_ims Registered (exp. 1551s)')
        self.assertEqual(result, {'registration': 'Registered', 'next_refresh': 1551})
        self.assertEqual(worker.registration('invalid'), {'registration': 'unknown'})

    def test_tcp_sockets_keep_ports_without_addresses_or_process_arguments(self):
        result = worker.sockets('ESTAB 0 0 [2001:db8::1]:51723 [2001:db8::2]:65385\n'
                                'LISTEN 0 10 [::]:52001 [::]:*\n', SALT)
        self.assertEqual(result[0]['local']['port'], 51723)
        self.assertEqual(result[0]['peer']['port'], 65385)
        self.assertEqual(result[1]['local']['port'], 52001)
        self.assertNotIn('2001:db8', json.dumps(result))

    def test_xfrm_retains_counters_but_never_algorithm_keys(self):
        result = worker.xfrm_states('src 2001:db8::2 dst 2001:db8::1\n'
            ' proto esp spi 0x00001234 reqid 2342 mode transport\n'
            ' auth-trunc hmac(md5) private-auth-key 96\n'
            ' enc cbc(aes) private-encryption-key\n'
            ' lifetime current:\n 1000(bytes), 3(packets)\n'
            ' stats:\n replay-window 0 replay 2 failed 1\n', SALT)
        self.assertEqual(result[0]['spi'], 4660)
        self.assertEqual(result[0]['packets'], 3)
        self.assertEqual(result[0]['failed'], 1)
        self.assertNotIn('private-', json.dumps(result))

    def test_packets_retain_handshake_flags_and_esp_spi_without_payload(self):
        tcp = worker.packet('1794124000.000001 IP6 2001:db8::2.65386 > 2001:db8::1.52001: Flags [S], seq 1, length 0', SALT)
        self.assertEqual((tcp['proto'], tcp['source']['port'], tcp['destination']['port'], tcp['flags']),
                         ('tcp', 65386, 52001, 'S'))
        esp = worker.packet('1794124000.000002 IP6 2001:db8::2 > 2001:db8::1: ESP(spi=0x00001234,seq=0x2), length 300', SALT)
        self.assertEqual((esp['spi'], esp['sequence']), (4660, 2))
        self.assertIsNone(worker.packet('tcpdump: private-error', SALT))
        self.assertIsNone(worker.packet('1794124000.0 IP 192.0.2.1.100 > 192.0.2.2.200: UDP, length 4', SALT))

    def test_expiry_is_bounded_and_not_extended_by_bad_request_data(self):
        self.assertIsNotNone(worker.active_request(request(100), 100))
        self.assertIsNone(worker.active_request(request(100), 160))
        self.assertIsNone(worker.active_request({**request(100), 'expires_at': 3701}, 101))
        self.assertIsNone(worker.active_request({**request(100), 'salt': 'private'}, 101))
        self.assertIsNone(worker.active_request({**request(100), 'session': []}, 101))

    def test_export_has_a_second_closed_schema_privacy_boundary(self):
        source = {'kind': 'sip', 'session': SESSION, 'ts': 100, 'method': 'INVITE',
                  'code': 0, 'peer': {'address': 'ip-abcdefghijklmnop', 'port': 65386},
                  'contacts': [{'address': 'private.example.invalid', 'user': 'private-user', 'port': 52001}],
                  'Authorization': 'private-auth', 'body': 'private-message', 'salt': SALT,
                  'unknown': {'address': 'ip-abcdefghijklmnop'}}
        exported = vowifi_trace.export_line(json.dumps(source))
        value = json.loads(exported)
        self.assertEqual(value['peer']['port'], 65386)
        self.assertEqual(value['contacts'], [{'port': 52001}])
        for secret in ('private-', 'unknown', SALT, 'Authorization'):
            self.assertNotIn(secret, exported)
        for invalid in ('broken', '[]', '{"kind": []}', json.dumps({**source, 'session': 'private'})):
            self.assertIsNone(vowifi_trace.export_line(invalid))


class TraceLifecycleTests(unittest.TestCase):
    def test_start_stop_and_markers_preserve_boundaries_and_private_salt(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, 'DATA_DIR', temp), patch.object(vowifi_trace.time, 'time', return_value=100):
            current = vowifi_trace.start('2', 60)
            self.assertTrue(current['active'])
            self.assertFalse(current['recording'])
            self.assertNotIn('salt', current)
            self.assertEqual(current['remaining_seconds'], 3600)
            root = vowifi_trace.directory('2')
            self.assertEqual((root/'request.json').stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError): vowifi_trace.start('2')
            mark = vowifi_trace.mark('2', 'incoming_failed')
            self.assertEqual(mark['result'], 'incoming_failed')
            self.assertTrue((root/'marks.jsonl').exists())
            self.assertFalse(vowifi_trace.stop('2')['active'])
            with self.assertRaises(ValueError): vowifi_trace.mark('2', 'incoming_rang')
            for minutes in (True, 0, 61, 60.0, '60'):
                with self.assertRaises(ValueError): vowifi_trace.start('2', minutes)
            for identifier in ('../other', '2/../../../other', ''):
                with self.assertRaises(ValueError): vowifi_trace.directory(identifier)

    def test_completed_worker_can_resume_same_session_after_a_container_rebuild(self):
        with tempfile.TemporaryDirectory() as temp:
            root, run = Path(temp)/'logs', Path(temp)/'run'
            root.mkdir();run.mkdir()
            worker.write_private(root/'request.json', request(int(time.time())))
            for _ in range(2):
                recorder = worker.Recorder(root, run)
                def sample():
                    recorder.emit('snapshots', {'kind': 'snapshot', 'ts': int(time.time()), 'registration': 'Registered'})
                    recorder.stop.set()
                with patch.object(recorder, 'sample', side_effect=sample), patch.object(recorder, 'ensure_capture'):
                    recorder.run()
                self.assertFalse(recorder.marker.exists())
            records=[json.loads(line) for line in (root/'snapshots.jsonl').read_text().splitlines()]
            self.assertEqual(len(records), 2)
            self.assertEqual({row['session'] for row in records}, {SESSION})
            self.assertFalse(json.loads((root/'status.json').read_text())['active'])

    def test_a_monotonically_expired_session_does_not_reopen_after_clock_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            root, run = Path(temp)/'logs', Path(temp)/'run'
            root.mkdir();run.mkdir()
            worker.write_private(root/'request.json', request(int(time.time())))
            recorder=worker.Recorder(root,run)
            recorder.completed_session=SESSION
            with patch.object(recorder,'sample') as sample, patch.object(recorder.stop,'wait',side_effect=lambda _: recorder.stop.set()):
                recorder.run()
            sample.assert_not_called()

    def test_sampling_never_requests_authentication_keys_or_changes_registration(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            recorder=worker.Recorder(root,root)
            recorder.request=request(int(time.time()))
            commands=[]
            def execute(command):
                commands.append(command[1]);return command[0], ''
            with patch.object(recorder, 'command', side_effect=execute):
                recorder.sample()
            recorder.pool.shutdown()
            self.assertIn(['ip','-s','xfrm','state','list','nokeys'],commands)
            self.assertFalse(any('send register' in ' '.join(command) or 'logger on' in ' '.join(command) for command in commands))
            text=(root/'snapshots.jsonl').read_text()
            self.assertNotIn(SALT,text)

    def test_support_bundle_contains_only_exported_evidence_not_request_or_raw_files(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config,'DATA_DIR',temp), patch.object(config,'get_settings',return_value={}):
            root=vowifi_trace.directory('2');root.mkdir(parents=True)
            (root/'request.json').write_text(json.dumps(request(100)))
            (root/'raw.pcap').write_text('private-packet')
            (root/'sip.jsonl').write_text(json.dumps({'kind':'sip','session':SESSION,'ts':100,'method':'INVITE','peer':{'port':65386,'address':'ip-abcdefghijklmnop'},'body':'private-body'})+'\n')
            (root/'marks.jsonl').write_text(json.dumps({'kind':'mark','session':SESSION,'ts':101,'result':'incoming_failed'})+'\n')
            content=operations.support_bundle({})
            with zipfile.ZipFile(BytesIO(content)) as archive:
                names=archive.namelist()
                self.assertIn('logs/2-vowifi-trace-sip.jsonl',names)
                self.assertIn('logs/2-vowifi-trace-marks.jsonl',names)
                combined='\n'.join(archive.read(name).decode() for name in names)
            for secret in (SALT,'private-body','private-packet','request.json','raw.pcap'):
                self.assertNotIn(secret,combined)

    def test_api_requires_a_running_known_line_and_is_administrator_only(self):
        client=gate.Principal('client',client_id=1,credential='client:1')
        for method,suffix in (('GET',''),('POST',''),('DELETE',''),('POST','/markers')):
            self.assertFalse(authz.allowed(client,method,'/api/instances/2/diagnostics/vowifi-trace'+suffix))
        with patch.object(config,'get_instance',return_value=None):
            with self.assertRaises(main.HTTPException) as error:
                asyncio.run(main.api_vowifi_trace_status('2'))
            self.assertEqual(error.exception.status_code,404)
        with patch.object(config,'get_instance',return_value={'id':'2'}), patch.object(main.engine,'is_running',return_value=False):
            with self.assertRaises(main.HTTPException) as error:
                asyncio.run(main.api_vowifi_trace_start('2',{}))
            self.assertEqual(error.exception.status_code,409)


if __name__ == '__main__':
    unittest.main()
