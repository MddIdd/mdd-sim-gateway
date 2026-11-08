"""Replay malformed IKE envelopes without importing SIM or opening network devices."""
import ast
import contextlib
import io
import ipaddress
from pathlib import Path
import struct
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'engine/swu_ike.py'


def engine_class():
    tree = ast.parse(SOURCE.read_text())
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'swu')
    wanted = {'return_flags', 'decode_header', 'decode_generic_payload_header', 'decode_payload', 'decode_ike', 'set_routes'}
    original.body = [node for node in original.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {'struct': struct, 'ipaddress': ipaddress, 'SK': 46, 'SKF': 53,
                 'SWU_ASSIGN_IPV6_GLOBAL': True, 'SWU_WRITE_RESOLV': False}
    exec(compile(ast.Module(body=[original], type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return namespace['swu']


def decoder():
    instance = engine_class()()
    instance.ike_decoded_header = {}
    instance.ike_spi_initiator = b'a' * 8
    instance.ike_spi_responder = b'b' * 8
    instance.ike_spi_initiator_old = instance.ike_spi_responder_old = b'c' * 8
    instance.decodable_payloads = [46]
    # Crypto validation has its own tests; represent successfully decrypted empty DPD here.
    instance.decode_payload_type = lambda *_: []
    return instance


def message(payload=b'', next_payload=46):
    return struct.pack('!8s8sBBBBII', b'a' * 8, b'b' * 8, next_payload, 0x20, 37, 0x20,
                       1, 28 + len(payload)) + payload


class DecoderBoundaryTests(unittest.TestCase):
    def test_header_only_packet_cannot_reach_connected_state_as_valid(self):
        instance = decoder()
        with contextlib.redirect_stdout(io.StringIO()):
            instance.decode_ike(message())
        self.assertFalse(instance.ike_decoded_ok)
        self.assertEqual(instance.decoded_payload, [])

    def test_zero_length_and_truncated_payloads_are_rejected(self):
        for data in (b'\x00', b'\x00\x00\x00\x00', b'\x00\x00\x00\x03', b'\x00\x00\x00\x08'):
            with self.subTest(data=data), contextlib.redirect_stdout(io.StringIO()):
                instance = decoder()
                instance.decode_ike(message(data))
                self.assertFalse(instance.ike_decoded_ok)

    def test_valid_empty_encrypted_dpd_remains_accepted(self):
        instance = decoder()
        with contextlib.redirect_stdout(io.StringIO()):
            instance.decode_ike(message(b'\x00\x00\x00\x04'))
        self.assertTrue(instance.ike_decoded_ok)
        self.assertEqual(instance.decoded_payload, [[46, []]])

    def test_failed_decryption_is_not_treated_as_an_empty_dpd(self):
        instance = decoder()
        instance.decode_payload_type = lambda *_: None
        with contextlib.redirect_stdout(io.StringIO()):
            instance.decode_ike(message(b'\x00\x00\x00\x04'))
        self.assertFalse(instance.ike_decoded_ok)

    def test_compressed_ipv6_keeps_all_interface_identifier_bits(self):
        for address in ('2001:db8:1:2::1234', '2001:db8::1', '2001:db8:1:2:abcd:ef01:2345:6789'):
            with self.subTest(address=address):
                instance = decoder()
                commands = []
                instance.open_tun = lambda _: None
                instance.exec_in_netns = commands.append
                instance._compute_and_apply_tun_mtu = lambda: None
                instance.tun_device = 'ipsec0'
                instance.netns_name = None
                instance.ip_address_list = instance.dns_address_list = instance.dnsv6_address_list = []
                instance.ipv6_address_list = [address]
                instance.set_routes()
                assigned = [command.split()[4].split('/')[0] for command in commands if command.startswith('ip -6 addr add ')]
                local = ipaddress.IPv6Address(assigned[0])
                self.assertTrue(local.is_link_local)
                self.assertEqual(int(local) & ((1 << 64) - 1), int(ipaddress.IPv6Address(address)) & ((1 << 64) - 1))
                self.assertEqual(assigned[1], address)


if __name__ == '__main__':
    unittest.main()
