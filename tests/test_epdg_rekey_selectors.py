"""Offline checks for the opt-in, peer-initiated ESP rekey selector decision."""
import ast
import ipaddress
import os
import socket
import struct
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "engine" / "swu_ike.py"
tree = ast.parse(SOURCE.read_text())
wanted = {"_handle_epdg_esp_rekey", "_accept_epdg_esp_rekey", "_rekey_ts_covers_scope"}
encoding = {"answer_CREATE_CHILD_SA_rekey", "encode_payload_type_ts"}
methods = [node for node in ast.walk(tree)
           if isinstance(node, ast.FunctionDef) and node.name in wanted | encoding]
assert len(methods) == len(wanted | encoding)
ns = {"os": os, "time": time, "ipaddress": ipaddress, "socket": socket,
      "struct": struct, "swu_log": lambda *_: None}
for node in tree.body:
    if isinstance(node, ast.Assign):
        try:
            value = ast.literal_eval(node.value)
        except (TypeError, ValueError):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                ns[target.id] = value
exec(compile(ast.Module(body=methods, type_ignores=[]), str(SOURCE), "exec"), ns)


def ts(address, end=None, family=7):
    return (family, 0, 0, 65535, address, end or address)


class Pipe:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


class Tunnel:
    _rekey_ts_covers_scope = staticmethod(ns["_rekey_ts_covers_scope"])

    def __init__(self):
        for name in wanted - {"_rekey_ts_covers_scope"}:
            setattr(self, name, types.MethodType(ns[name], self))
        self.spi_init_child = b"A" * 4
        self.spi_resp_child = b"B" * 4
        self.sa_list_negotiated_child = [[]]
        self.negotiated_encryption_algorithm_child = 1
        self.negotiated_integrity_algorithm_child = 1
        self.ike_to_ipsec_encoder = Pipe()
        self.ike_to_ipsec_decoder = Pipe()
        self.sent = []
        self.responses = []
        # These were negotiated in a UE-initiated exchange; the ePDG's
        # rekey TSi/TSr must have the opposite roles.
        self._negotiated_child_tsi = [ts("192.0.2.10")]
        self._negotiated_child_tsr = [ts("198.51.100.0", "198.51.100.255")]

    def answer_CREATE_CHILD_SA_error(self, code):
        return ("error", code)

    def answer_CREATE_CHILD_SA_rekey(self, sa_list, with_ke, tsi, tsr):
        self.responses.append((tsi, tsr))
        self.sa_spi_list = [b"C" * 4]
        return b"response"

    def generate_keying_material_child_responder(self, _):
        self.SK_IPSEC_EI = self.SK_IPSEC_ER = b"key"
        self.SK_IPSEC_AI = self.SK_IPSEC_AR = b"auth"

    def encode_inter_process_protocol(self, message):
        return message

    def send_data(self, packet):
        self.sent.append(packet)


def request(tunnel, tsi=None, tsr=None):
    payloads = [
        [ns["SA"], [1, ns["ESP"], b"D" * 4]],
        [ns["NINR"], [b"N" * 16]],
        [ns["N"], [ns["ESP"], ns["REKEY_SA"], tunnel.spi_resp_child, b""]],
    ]
    if tsi is not None:
        payloads.append([ns["TSI"], [len(tsi), tsi]])
    if tsr is not None:
        payloads.append([ns["TSR"], [len(tsr), tsr]])
    return payloads


class RekeySelectorTests(unittest.TestCase):
    def test_response_encoder_uses_selected_lists_not_wildcard_offer(self):
        class Encoder:
            answer_CREATE_CHILD_SA_rekey = ns["answer_CREATE_CHILD_SA_rekey"]
            encode_payload_type_ts = ns["encode_payload_type_ts"]
            ike_spi_initiator = b"I" * 8
            ike_spi_responder = b"R" * 8
            ike_decoded_header = {"message_id": 7}
            ts_list_initiator = [ts("0.0.0.0", "255.255.255.255")]
            ts_list_responder = ts_list_initiator

            def encode_header(self, *_):
                return b"header"

            def encode_generic_payload_header(self, _, __, payload):
                return payload

            def encode_payload_type_sa(self, _):
                return b"sa"

            def encode_payload_type_ninr(self):
                return b"nonce"

            def set_ike_packet_length(self, packet):
                return packet

            def encode_payload_type_sk(self, packet):
                return packet

        peer = [ts("198.51.100.0", "198.51.100.255")]
        local = [ts("192.0.2.10")]
        packet = Encoder().answer_CREATE_CHILD_SA_rekey([[]], False, peer, local)
        self.assertIn(socket.inet_pton(socket.AF_INET, "198.51.100.0"), packet)
        self.assertIn(socket.inet_pton(socket.AF_INET, "192.0.2.10"), packet)
        self.assertNotIn(socket.inet_pton(socket.AF_INET, "255.255.255.255"), packet)

    def test_response_preserves_peer_roles_and_old_scope(self):
        tunnel = Tunnel()
        wide_tsi = [ts("0.0.0.0", "255.255.255.255")]
        wide_tsr = [ts("0.0.0.0", "255.255.255.255")]
        with patch.dict(os.environ, {"SWU_ACCEPT_EPDG_ESP_REKEY": "1"}):
            tunnel._handle_epdg_esp_rekey(request(tunnel, wide_tsi, wide_tsr))
        self.assertEqual(tunnel.responses, [(tunnel._negotiated_child_tsr,
                                             tunnel._negotiated_child_tsi)])
        self.assertEqual(tunnel.sent, [b"response"])
        self.assertEqual(len(tunnel.ike_to_ipsec_encoder.messages), 1)

    def test_narrow_or_missing_request_does_not_switch_workers(self):
        for tsi, tsr in (([ts("198.51.100.1")], [ts("192.0.2.10")]),
                         ([ts("198.51.100.0", "198.51.100.255")], None)):
            with self.subTest(tsi=tsi, tsr=tsr):
                tunnel = Tunnel()
                with patch.dict(os.environ, {"SWU_ACCEPT_EPDG_ESP_REKEY": "1"}):
                    tunnel._handle_epdg_esp_rekey(request(tunnel, tsi, tsr))
                self.assertEqual(tunnel.sent, [("error", ns["NO_PROPOSAL_CHOSEN"])])
                self.assertFalse(tunnel.ike_to_ipsec_encoder.messages)
                self.assertFalse(tunnel.ike_to_ipsec_decoder.messages)
                self.assertEqual(tunnel.spi_resp_child, b"B" * 4)

    def test_ipv6_and_protocol_port_scope(self):
        covers = Tunnel._rekey_ts_covers_scope
        self.assertTrue(covers([ts("2001:db8::", "2001:db8::ffff", 8)],
                               [ts("2001:db8::10", family=8)]))
        self.assertFalse(covers([ts("2001:db8::11", family=8)],
                                [ts("2001:db8::10", family=8)]))
        self.assertFalse(covers([(7, 17, 0, 65535, "192.0.2.10", "192.0.2.10")],
                                [ts("192.0.2.10")]))


if __name__ == "__main__":
    unittest.main()
