# SPDX-License-Identifier: GPL-3.0-only
# Derived from fasferraz/SWu-IKEv2 (GPL-3.0); modified for MDD Sim Gateway.
import serial
import struct
import threading
import socket
import random
import time
import select
import sys
import os
import errno
import fcntl
import subprocess
import multiprocessing
import ctypes
import signal
import requests
import hashlib
import ipaddress

# Python 3.14 changed POSIX multiprocessing's default from fork to forkserver.  The SWu data
# plane workers intentionally inherit the live IKE/crypto state; serialising that state is both
# unnecessary and impossible (cryptography's DHParameterNumbers is not picklable).  Keep the
# Linux behaviour this engine was designed and tested with, explicitly, across Python upgrades.
if sys.platform.startswith("linux"):
    multiprocessing.set_start_method("fork", force=True)

from optparse import OptionParser
from binascii import hexlify, unhexlify

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import dh
from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from Crypto.Cipher import AES
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from smartcard.System import readers
from smartcard.util import toHexString,toBytes
try:
    from smartcard.scard import SCardBeginTransaction, SCardEndTransaction, SCARD_LEAVE_CARD
except Exception:                     # pragma: no cover
    SCardBeginTransaction = SCardEndTransaction = None
    SCARD_LEAVE_CARD = 0

from card.USIM import *

requests.packages.urllib3.disable_warnings()

_WORKER_LOG_PATH = None
WORKER_LOG_MAX_BYTES = 512 * 1024


def prepare_ipsec_worker(parent_pid, role):
    """Detach an ESP worker from the parent log pipe and bind it to its parent process."""
    global _WORKER_LOG_PATH
    # fork inherits main()'s graceful tunnel teardown handler. PDEATHSIG must terminate only the
    # worker: running the parent's handler here would overwrite its classified DOWN status, send
    # duplicate notifications and transmit DELETE with a stale fork-time message id.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(1, signal.SIGTERM, 0, 0, 0)  # PR_SET_PDEATHSIG
    except Exception:
        pass
    if os.getppid() != int(parent_pid):
        os._exit(1)
    # Raw worker stdout can be extremely noisy and must not retain entrypoint's pipe. Preserve
    # only explicit swu_log diagnostics in one bounded file per role; raw packet dumps go away.
    try:
        os.makedirs(SWU_RUNDIR, exist_ok=True)
        _WORKER_LOG_PATH = os.path.join(SWU_RUNDIR, "esp-%s.log" % role)
        with open(_WORKER_LOG_PATH, "w"):
            pass
        os.chmod(_WORKER_LOG_PATH, 0o600)
    except OSError:
        _WORKER_LOG_PATH = None
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
        finally:
            if devnull > 2:
                os.close(devnull)
    except OSError:
        pass

# --- APDU-level tracing (diagnostic, SWU_APDU_DEBUG, default OFF) ------------------------------
# Wrap pyscard's CardConnection.transmit so every APDU issued in THIS process is logged with the
# reader, the command hex, the response SW + length, and the elapsed time. The mitshell card.USIM
# path (return_res_ck_ik -> USIM().authenticate()) and swu_ike's own PIN path both go through
# CardConnection.transmit, so this pinpoints exactly which APDU blocks/errs during EAP-AKA SIM
# authentication. Only swu_ike's process is patched (pin_keeper / ami_usim are separate processes),
# and swu_ike only issues card APDUs during auth, so the log stays targeted. Enable: SWU_APDU_DEBUG=1.
if os.environ.get("SWU_APDU_DEBUG", "0") not in ("0", "", "no"):
    try:
        from smartcard.CardConnection import CardConnection as _CC

        def _make_traced(_orig):
            def _traced_transmit(self, apdu, *a, **k):
                try:
                    _rd = str(self.getReader())
                except Exception:
                    _rd = "?"
                # APDUs can contain PINs, identities, AKA challenges, and key material. Log only
                # the command header and size, never request/response bodies.
                _hdr = " ".join("%02X" % b for b in apdu[:4])
                print("[apdu] %s --> header=%s bytes=%d" % (_rd, _hdr, len(apdu)), flush=True)
                _t0 = time.time()
                try:
                    res = _orig(self, apdu, *a, **k)
                except Exception as _e:
                    print("[apdu] %s <!! EXC after %.3fs: %r" % (_rd, time.time() - _t0, _e), flush=True)
                    raise
                try:
                    data, sw1, sw2 = res
                    print("[apdu] %s <-- sw=%02X%02X %dB (%.3fs)" %
                          (_rd, sw1, sw2, len(data), time.time() - _t0), flush=True)
                except Exception:
                    # Diagnostics must never turn an unusual return value into a new card error,
                    # and repr(res) could expose APDU response bodies. Record only its type.
                    print("[apdu] %s <-- unexpected response type=%s (%.3fs)" %
                          (_rd, type(res).__name__, time.time() - _t0), flush=True)
                return res
            return _traced_transmit

        _CC.transmit = _make_traced(_CC.transmit)
        # PCSCCardConnection may override transmit(); patch it too if so, so nothing is missed.
        try:
            from smartcard.pcsc.PCSCCardConnection import PCSCCardConnection as _PCC
            if "transmit" in _PCC.__dict__:
                _PCC.transmit = _make_traced(_PCC.__dict__["transmit"])
        except Exception:
            pass
        print("[apdu] APDU tracing installed (SWU_APDU_DEBUG)", flush=True)
    except Exception as _e:
        print("[apdu] trace install failed: %r" % _e, flush=True)

# =============================================================================================
# VoWiFi engine integration (headless daemon glue). These helpers let swu_ike.py run as the
# engine's tunnel process in place of strongSwan: emit tunnel state + P-CSCF to the run dir,
# notify the manager, and keep a readable IKE log the control plane can classify.
# =============================================================================================
import json as _json
import signal as _signal

SWU_RUNDIR = os.environ.get("MDD_RUNDIR", "/run/mdd-sim-gateway")
SWU_IFACE = os.environ.get("SWU_IFACE", "ipsec0")          # tun device name (pjsip binds this)
SWU_NOTIFY = os.environ.get("SWU_NOTIFY", "/usr/local/bin/notify.py")
SWU_ASSIGN_IPV6_GLOBAL = os.environ.get("SWU_ASSIGN_IPV6_GLOBAL", "1") not in ("0", "", "no")
SWU_WRITE_RESOLV = os.environ.get("SWU_WRITE_RESOLV", "0") not in ("0", "", "no")

# --- Data-plane MTU / fragmentation handling -------------------------------------------------
# The userspace ESP dataplane reads inner IP packets off the tun (ipsec0), wraps each in
# ESP(+UDP for NAT-T)+outer-IPv4, and sends it out toward the ePDG. Design goals:
#   * The tun (ipsec0) MTU is FIXED and stable so the IMS/pjsip layer is unaware of any path
#     constraint (SWU_TUN_MTU, default 1400 -> a 1400 inner + ~81 ESP overhead = ~1481 outer,
#     which still fits the common 1500/1480 uplinks with no fragmentation at all).
#   * Fragmentation is done ENTIRELY by the tunnel client, transparently: when the real outer
#     path MTU cannot carry inner+overhead, swu splits the inner packet (RFC 791 for IPv4, an
#     RFC 8200 Fragment header for IPv6) so every ESP datagram on the wire is COMPLETE and never
#     IP-fragmented (survives carriers/paths that drop IP fragments).
#   * The real outer MTU is SENSED beyond the container's own NIC: `ip route get <epdg>` exposes
#     the route-cached PMTU, so a WireGuard/1280 (or any constrained) hop on the HOST outside the
#     container — common when bypassing carrier IP restrictions — is picked up via PMTU discovery.
# Env knobs:
#   SWU_TUN_MTU     fixed ipsec0 MTU (default 1400)
#   SWU_OUTER_MTU   override the sensed outer path MTU (0 = autodetect via route + egress NIC)
#   SWU_MTU_MARGIN  extra bytes shaved off the fragmentation threshold (default 0)
#   SWU_INNER_FRAG  1 = swu fragments oversized inner packets (default 1)
#   SWU_ICMP_PTB    1 = also signal the local source with an ICMP PTB (default 0 — keep the IMS
#                       layer unaware; inner fragmentation already makes everything fit)
SWU_TUN_MTU_ENV   = int(os.environ.get("SWU_TUN_MTU", "1400") or 1400)
SWU_OUTER_MTU_ENV = int(os.environ.get("SWU_OUTER_MTU", "0") or 0)
SWU_MTU_MARGIN    = int(os.environ.get("SWU_MTU_MARGIN", "0") or 0)
SWU_INNER_FRAG    = os.environ.get("SWU_INNER_FRAG", "1") not in ("0", "", "no")
SWU_ICMP_PTB      = os.environ.get("SWU_ICMP_PTB", "0") not in ("0", "", "no")
SWU_MTU_REFRESH   = float(os.environ.get("SWU_MTU_REFRESH", "5") or 5)  # secs between re-sensing
IPV6_MIN_MTU      = 1280       # RFC 8200: an IPv6 link/interface must carry at least 1280 bytes


def swu_log(msg):
    """Emit a tagged status line. swu_ike's stdout is redirected to run/charon.log by the
    entrypoint, so a plain print() lands in the IKE log (which control-plane classify_ike /
    charon_log read) without intermixing into Asterisk's console. Falls back to appending
    directly only if stdout isn't already the IKE log (e.g. the netns probe run).

    The entrypoint timestamps the complete stdout/stderr stream, including raw STATE output and
    tracebacks, so this helper only supplies the stable tag consumed by diagnostics."""
    line = "[swu_ike] %s" % msg
    worker_path = globals().get("_WORKER_LOG_PATH")
    if worker_path:
        try:
            if os.path.getsize(worker_path) >= WORKER_LOG_MAX_BYTES:
                with open(worker_path, "w"):
                    pass
            with open(worker_path, "a") as handle:
                handle.write("[%s] %s\n" %
                             (time.strftime("%Y-%m-%d %H:%M:%S%z"), line))
            return
        except OSError:
            pass
    print(line, flush=True)


def swu_write_status(state, **extra):
    try:
        os.makedirs(SWU_RUNDIR, exist_ok=True)
        data = {"state": state, "ts": int(time.time())}
        data.update(extra)
        tmp = os.path.join(SWU_RUNDIR, "swu_status.json.tmp")
        with open(tmp, "w") as f:
            _json.dump(data, f)
        os.replace(tmp, os.path.join(SWU_RUNDIR, "swu_status.json"))
    except Exception as e:
        print("[swu_ike] status write failed: %r" % e, flush=True)


def swu_write_pcscf(addr):
    try:
        os.makedirs(SWU_RUNDIR, exist_ok=True)
        with open(os.path.join(SWU_RUNDIR, "pcscf"), "w") as f:
            f.write(addr or "")
    except Exception:
        pass


def swu_notify(event, arg=None):
    try:
        cmd = ["python3", SWU_NOTIFY, event]
        if arg:
            cmd.append(arg)
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def swu_apply_pcscf(addr):
    """Re-render pjsip.conf for a (possibly new) P-CSCF and reload Asterisk, but only when the
    P-CSCF actually changed. The ePDG can hand out a DIFFERENT P-CSCF on every (re)connect /
    reauth; pjsip's type=identify/type=resolve are pinned to the P-CSCF IP, so a stale value
    means inbound INVITEs from the new P-CSCF don't match (calls/SMS fail) and outbound routing
    is wrong. This keeps them in sync on every reconnect, not just the first bring-up."""
    if not addr:
        return
    last = None
    try:
        with open(os.path.join(SWU_RUNDIR, "pcscf.applied")) as f:
            last = f.read().strip()
    except Exception:
        last = None
    if last == addr:
        return
    render = os.environ.get("SWU_RENDER", "/usr/local/bin/render.py")
    if not os.path.exists(render):
        return
    try:
        swu_log("P-CSCF changed (%s -> %s); re-rendering pjsip + reloading Asterisk" % (last, addr))
        subprocess.call(["python3", render], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Reload just the parts affected by the P-CSCF change. res_pjsip reload re-reads
        # pjsip.conf (identify/resolve/registration/endpoint) without dropping the tunnel.
        subprocess.call(["asterisk", "-rx", "module reload res_pjsip.so"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.call(["asterisk", "-rx", "pjsip send register volte_ims"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(os.path.join(SWU_RUNDIR, "pcscf.applied"), "w") as f:
            f.write(addr)
    except Exception as e:
        swu_log("pcscf apply failed: %r" % e)


'''

Ike Process

IPsec_encoder (receives data from tunnel interface -> encrypts and sends it towards the server/epdg)

IPsec_decoder (receives encrypted data from server/epdg -> decypts it and sends it to the tunnel interface)

'''

INTER_PROCESS_CREATE_SA = 1
INTER_PROCESS_UPDATE_SA = 2
INTER_PROCESS_DELETE_SA = 3
INTER_PROCESS_IKE       = 4
INTER_PROCESS_LIVENESS_RX = 8   # P2-3: ESP-decap worker -> IKE loop: a protected IPsec packet
                                # arrived, so the SA is alive (refresh the DPD/liveness clock).

INTER_PROCESS_IE_ENCR_ALG    = 1
INTER_PROCESS_IE_INTEG_ALG   = 2
INTER_PROCESS_IE_ENCR_KEY    = 3
INTER_PROCESS_IE_INTEG_KEY   = 4
INTER_PROCESS_IE_SPI_INIT    = 5
INTER_PROCESS_IE_SPI_RESP    = 6
INTER_PROCESS_IE_IKE_MESSAGE = 7


#DEFAULTs

DEFAULT_IKE_PORT = 500
DEFAULT_IKE_NAT_TRAVERSAL_PORT = 4500

DEFAULT_SERVER = '1.2.3.4'

DEFAULT_COM = '/dev/ttyUSB2'
DEFAULT_IMSI = '123456012345678'
DEFAULT_MCC = '123'
DEFAULT_MNC = '456'
DEFAULT_APN = 'internet'
DEFAULT_TIMEOUT_UDP = 2
#DEFAULT_TIMEOUT_UDP_NAT_TRANSVERSAL = 2

DEFAULT_CK = '0123456789ABCDEF0123456789ABCDEF'
DEFAULT_IK = '0123456789ABCDEF0123456789ABCDEF'
DEFAULT_RES = '0123456789ABCDEF'


# IMEI (15 digits) and IMEISV (16 digits) - used in DEVICE_IDENTITY notify response
IMEI                                    = '123456789012347'   # 15 digits. The last digit (7) is the checksum digit.
IMEISV                                  = '1234567890123456'  # 16 digits


NONE = 0

#IKEv2 Payload Types
SA =      33
KE =      34
IDI =     35
IDR =     36 
CERT =    37
CERTREQ = 38
AUTH =    39
NINR =    40
N =       41
D =       42
V =       43
TSI =     44
TSR =     45
SK =      46
CP =      47
EAP =     48
SKF =     53                                 # RFC 7383 Encrypted-and-Authenticated-Fragment payload

#IKEv2 Exchange Types
IKE_SA_INIT =     34
IKE_AUTH =        35
CREATE_CHILD_SA = 36
INFORMATIONAL =   37

RESERVED = 0
IKE = 1
AH =  2
ESP = 3   

#Transform Type Values
ENCR = 1
PRF = 2
INTEG = 3
D_H = 4
ESN = 5


#Transform Type 1 - Encryption Algorithm Transform IDs
ENCR_DES_IV64 =    1
ENCR_DES=          2
ENCR_3DES =        3
ENCR_RC5 =         4
ENCR_IDEA =        5
ENCR_CAST =        6
ENCR_BLOWFISH =    7
ENCR_3IDEA =       8
ENCR_DES_IV32 =    9
ENCR_NULL =       11 #Not allowed
ENCR_AES_CBC =    12
ENCR_AES_CTR =    13
ENCR_AES_CCM_8 =  14
ENCR_AES_CCM_12 = 15
ENCR_AES_CCM_16 = 16
ENCR_AES_GCM_8 =  18
ENCR_AES_GCM_12 = 19
ENCR_AES_GCM_16 = 20

#Transform Type 2 - Pseudorandom Function Transform IDs
PRF_HMAC_MD5 =          1
PRF_HMAC_SHA1 =         2
PRF_HMAC_TIGER =        3
PRF_AES128_XCBC =       4
PRF_HMAC_SHA2_256 =     5
PRF_HMAC_SHA2_384 =     6
PRF_HMAC_SHA2_512 =     7
PRF_AES128_CMAC =       8

#Transform Type 3 - Integrity Algorithm Transform IDs
NONE =                      0
AUTH_HMAC_MD5_96 =	        1
AUTH_HMAC_SHA1_96 =         2
AUTH_DES_MAC =	            3
AUTH_KPDK_MD5 =             4
AUTH_AES_XCBC_96 =          5
AUTH_HMAC_MD5_128 =         6
AUTH_HMAC_SHA1_160 =        7
AUTH_AES_CMAC_96 =          8
AUTH_AES_128_GMAC =         9
AUTH_AES_192_GMAC =        10
AUTH_AES_256_GMAC =        11
AUTH_HMAC_SHA2_256_128 =   12
AUTH_HMAC_SHA2_384_192 =   13
AUTH_HMAC_SHA2_512_256 =   14

#Transform Type 4 - Diffie-Hellman Group Transform IDs
MODP_768_bit =          1
MODP_1024_bit =         2
MODP_1536_bit =         5
MODP_2048_bit =        14
MODP_3072_bit =        15
MODP_4096_bit =        16
MODP_6144_bit =        17
MODP_8192_bit =        18


ESN_NO_ESN = 0
ESN_ESN =    1

TLV = 0
TV =  1

#IKEv2 Transform Attribute Types
KEY_LENGTH = (14, TV)


def ike_proposals_for_plmn(mcc, mnc):
    """Return IKE proposals for the home PLMN.

    DITO Telecommunity's ePDG (515-66) advertises only the legacy 3GPP suite
    AES-CBC-128 / HMAC-SHA1 / MODP-1024.  Keep the stronger, proven upstream
    proposal set for every other carrier instead of weakening negotiation globally.
    """
    plmn = (str(mcc or "").zfill(3), str(mnc or "").zfill(3))
    if plmn == ("515", "066"):
        return [[
            [IKE, 0],
            [ENCR, ENCR_AES_CBC, [KEY_LENGTH, 128]],
            [PRF, PRF_HMAC_SHA1],
            [INTEG, AUTH_HMAC_SHA1_96],
            [D_H, MODP_1024_bit],
        ]]
    return [
        [
            [IKE, 0],
            [ENCR, ENCR_AES_CBC, [KEY_LENGTH, 256]],
            [PRF, PRF_HMAC_SHA2_256],
            [INTEG, AUTH_HMAC_SHA2_256_128],
            [D_H, MODP_2048_bit],
        ],
        [
            [IKE, 0],
            [ENCR, ENCR_AES_CBC, [KEY_LENGTH, 128]],
            [PRF, PRF_HMAC_SHA2_256],
            [INTEG, AUTH_HMAC_SHA2_256_128],
            [D_H, MODP_2048_bit],
        ],
        [
            [IKE, 0],
            [ENCR, ENCR_AES_CBC, [KEY_LENGTH, 256]],
            [PRF, PRF_HMAC_SHA1],
            [INTEG, AUTH_HMAC_SHA1_96],
            [D_H, MODP_2048_bit],
        ],
        [
            [IKE, 0],
            [ENCR, ENCR_AES_CBC, [KEY_LENGTH, 128]],
            [PRF, PRF_HMAC_SHA1],
            [INTEG, AUTH_HMAC_SHA1_96],
            [D_H, MODP_2048_bit],
        ],
    ]


def requests_permanent_eap_identity(attributes):
    """Whether EAP-AKA asks the UE for a permanent/full-auth identity."""
    if not attributes:
        return False
    return attributes[0][0] in (
        AT_PERMANENT_ID_REQ,
        AT_ANY_ID_REQ,
        AT_FULLAUTH_ID_REQ,
        AT_IDENTITY,
    )


def build_eap_identity_response(identifier, identity):
    """EAP-Response/Identity (RFC 3748 §5.1): the Type-Data is the bare NAI,
    with none of EAP-AKA's attribute framing."""
    identity_bytes = identity.encode()
    return (bytes([EAP_RESPONSE, identifier])
            + struct.pack('!H', 5 + len(identity_bytes))
            + bytes([EAP_IDENTITY])
            + identity_bytes)


#states
OK =                            0
TIMEOUT =                       1
REPEAT_STATE =                  2
DECODING_ERROR =                3
MANDATORY_INFORMATION_MISSING = 4
OTHER_ERROR =                   5
REPEAT_STATE_COOKIE =           6


#IKEv2 Notify Message Types - Error Types
UNSUPPORTED_CRITICAL_PAYLOAD            =     1
INVALID_IKE_SPI                         =     4
INVALID_MAJOR_VERSION                   =     5
INVALID_SYNTAX                          =     7
INVALID_MESSAGE_ID                      =     9
INVALID_SPI                             =    11
NO_PROPOSAL_CHOSEN                      =    14
INVALID_KE_PAYLOAD                      =    17
AUTHENTICATION_FAILED                   =    24
SINGLE_PAIR_REQUIRED                    =    34
NO_ADDITIONAL_SAS                       =    35
INTERNAL_ADDRESS_FAILURE                =    36
FAILED_CP_REQUIRED                      =    37
TS_UNACCEPTABLE                         =    38
INVALID_SELECTORS                       =    39
TEMPORARY_FAILURE                       =    43
CHILD_SA_NOT_FOUND                      =    44
# from 24.302                                        
PDN_CONNECTION_REJECTION                =  8192
MAX_CONNECTION_REACHED                  =  8193
SEMANTIC_ERROR_IN_THE_TFT_OPERATION     =  8241
SYNTACTICAL_ERROR_IN_THE_TFT_OPERATION  =  8242
SEMANTIC_ERRORS_IN_PACKET_FILTERS       =  8244
SYNTACTICAL_ERRORS_IN_PACKET_FILTERS    =  8245
NON_3GPP_ACCESS_TO_EPC_NOT_ALLOWED      =  9000
USER_UNKNOWN                            =  9001
NO_APN_SUBSCRIPTION                     =  9002
AUTHORIZATION_REJECTED                  =  9003
ILLEGAL_ME                              =  9006
NETWORK_FAILURE                         = 10500
RAT_TYPE_NOT_ALLOWED                    = 11001
IMEI_NOT_ACCEPTED                       = 11005
PLMN_NOT_ALLOWED                        = 11011
UNAUTHENTICATED_EMERGENCY_NOT_SUPPORTED = 11055

#IKEv2 Notify Message Types - Status Types
INITIAL_CONTACT                         = 16384
SET_WINDOW_SIZE                         = 16385
ADDITIONAL_TS_POSSIBLE                  = 16386
IPCOMP_SUPPORTED                        = 16387      
NAT_DETECTION_SOURCE_IP                 = 16388      
NAT_DETECTION_DESTINATION_IP            = 16389
COOKIE                                  = 16390
USE_TRANSPORT_MODE                      = 16391
HTTP_CERT_LOOKUP_SUPPORTED              = 16392
REKEY_SA                                = 16393
ESP_TFC_PADDING_NOT_SUPPORTED           = 16394
NON_FIRST_FRAGMENTS_ALSO                = 16395

IKEV2_FRAGMENTATION_SUPPORTED           = 16430      # RFC 7383 status notify (IKE fragmentation)

EAP_ONLY_AUTHENTICATION                 = 16417
# from 24.302                                        
REACTIVATION_REQUESTED_CAUSE            = 40961
BACKOFF_TIMER                           = 41041
PDN_TYPE_IPv4_ONLY_ALLOWED              = 41050
PDN_TYPE_IPv6_ONLY_ALLOWED              = 41051
DEVICE_IDENTITY                         = 41101
EMERGENCY_SUPPORT                       = 41112
EMERGENCY_CALL_NUMBERS                  = 41134
NBIFOM_GENERIC_CONTAINER                = 41288
P_CSCF_RESELECTION_SUPPORT              = 41304
PTI                                     = 41501
IKEV2_MULTIPLE_BEARER_PDN_CONNECTIVITY  = 42011
EPS_QOS                                 = 42014
EXTENDED_EPS_QOS                        = 42015
TFT                                     = 42017
MODIFIED_BEARER                         = 42020
APN_AMBR                                = 42094
EXTENDED_APN_AMBR                       = 42095
N1_MODE_CAPABILITY                      = 51015
N1_MODE_INFORMATION                     = 51115
N1_MODE_S_NSSAI_PLMN_ID                 = 52216
DNS_SRV_SEC_INFO_IND                    = 52301
DNS_SRV_SEC_INFO                        = 52302
ATSSS_REQUEST                           = 52331
ATSSS_RESPONSE                          = 52332
HPA_INFO                                = 55911

# Reverse lookup: IKEv2 Notify Message Type value -> name, for decode/logging of received
# Notify payloads and for responding correctly to ePDG requests (3GPP TS 24.302 clause 8.1.2).
# Covers RFC 7296 status/error notifies plus the FULL 3GPP private ranges:
#   Table 8.1.2.2-1 (Private Error Types)  and  Table 8.1.2.3-1 (Private Status Types).
# Only genuine Notify Message Types are listed (so protocol-ID and transform constants that
# happen to share integer values are never confused for notifies).
NOTIFY_TYPE_NAMES = {
    # ---- RFC 7296 error types (< 16384) ----
    1: "UNSUPPORTED_CRITICAL_PAYLOAD", 4: "INVALID_IKE_SPI", 5: "INVALID_MAJOR_VERSION",
    7: "INVALID_SYNTAX", 9: "INVALID_MESSAGE_ID", 11: "INVALID_SPI",
    14: "NO_PROPOSAL_CHOSEN", 17: "INVALID_KE_PAYLOAD", 24: "AUTHENTICATION_FAILED",
    34: "SINGLE_PAIR_REQUIRED", 35: "NO_ADDITIONAL_SAS", 36: "INTERNAL_ADDRESS_FAILURE",
    37: "FAILED_CP_REQUIRED", 38: "TS_UNACCEPTABLE", 39: "INVALID_SELECTORS",
    40: "UNACCEPTABLE_ADDRESSES", 41: "UNEXPECTED_NAT_DETECTED", 43: "TEMPORARY_FAILURE",
    44: "CHILD_SA_NOT_FOUND",
    # ---- 3GPP TS 24.302 Table 8.1.2.2-1: Private Error Types ----
    8192: "PDN_CONNECTION_REJECTION", 8193: "MAX_CONNECTION_REACHED",
    8241: "SEMANTIC_ERROR_IN_THE_TFT_OPERATION",
    8242: "SYNTACTICAL_ERROR_IN_THE_TFT_OPERATION",
    8244: "SEMANTIC_ERRORS_IN_PACKET_FILTERS",
    8245: "SYNTACTICAL_ERRORS_IN_PACKET_FILTERS",
    9000: "NON_3GPP_ACCESS_TO_EPC_NOT_ALLOWED", 9001: "USER_UNKNOWN",
    9002: "NO_APN_SUBSCRIPTION", 9003: "AUTHORIZATION_REJECTED", 9006: "ILLEGAL_ME",
    10500: "NETWORK_FAILURE", 11001: "RAT_TYPE_NOT_ALLOWED", 11005: "IMEI_NOT_ACCEPTED",
    11011: "PLMN_NOT_ALLOWED", 11055: "UNAUTHENTICATED_EMERGENCY_NOT_SUPPORTED",
    # ---- RFC 7296 status types (>= 16384) ----
    16384: "INITIAL_CONTACT", 16385: "SET_WINDOW_SIZE", 16386: "ADDITIONAL_TS_POSSIBLE",
    16387: "IPCOMP_SUPPORTED", 16388: "NAT_DETECTION_SOURCE_IP",
    16389: "NAT_DETECTION_DESTINATION_IP", 16390: "COOKIE", 16391: "USE_TRANSPORT_MODE",
    16392: "HTTP_CERT_LOOKUP_SUPPORTED", 16393: "REKEY_SA",
    16394: "ESP_TFC_PADDING_NOT_SUPPORTED", 16395: "NON_FIRST_FRAGMENTS_ALSO",
    16417: "EAP_ONLY_AUTHENTICATION",
    16430: "IKEV2_FRAGMENTATION_SUPPORTED",
    # ---- 3GPP TS 24.302 Table 8.1.2.3-1: Private Status Types ----
    40961: "REACTIVATION_REQUESTED_CAUSE", 41041: "BACKOFF_TIMER",
    41050: "PDN_TYPE_IPv4_ONLY_ALLOWED", 41051: "PDN_TYPE_IPv6_ONLY_ALLOWED",
    41101: "DEVICE_IDENTITY", 41112: "EMERGENCY_SUPPORT",
    41134: "EMERGENCY_CALL_NUMBERS", 41288: "NBIFOM_GENERIC_CONTAINER",
    41304: "P_CSCF_RESELECTION_SUPPORT", 41501: "PTI",
    42011: "IKEV2_MULTIPLE_BEARER_PDN_CONNECTIVITY", 42014: "EPS_QOS",
    42015: "EXTENDED_EPS_QOS", 42017: "TFT", 42020: "MODIFIED_BEARER",
    42094: "APN_AMBR", 42095: "EXTENDED_APN_AMBR", 51015: "N1_MODE_CAPABILITY",
    51115: "N1_MODE_INFORMATION", 52216: "N1_MODE_S_NSSAI_PLMN_ID",
    52301: "DNS_SRV_SEC_INFO_IND", 52302: "DNS_SRV_SEC_INFO",
    52331: "ATSSS_REQUEST", 52332: "ATSSS_RESPONSE", 55911: "HPA_INFO",
}

# Friendly, human-readable descriptions for the 3GPP TS 24.302 private Notify types (Tables
# 8.1.2.2-1 and 8.1.2.3-1). Used to render errors/status in the IKE log so operators do not
# have to look up the numeric code. Keyed by Notify Message Type value.
NOTIFY_DESCRIPTIONS = {
    # ---- Private Error Types (Table 8.1.2.2-1) ----
    8192: "PDN connection (for the IP address in the notify data) was rejected by the network",
    8193: "Max PDN connections for this APN reached; no more can be established (first-conn "
          "rejection means the APN is not allowed for this UE)",
    8241: "Requested service rejected: semantic error in the TFT operation in the request",
    8242: "Requested service rejected: syntactical error in the TFT operation in the request",
    8244: "Requested service rejected: semantic error in the packet filter(s) in the request",
    8245: "Requested service rejected: syntactical error in the packet filter(s) in the request",
    9000: "Non-3GPP access to EPC not allowed (no non-3GPP subscription / policy) "
          "[DIAMETER_ERROR_USER_NO_NON_3GPP_SUBSCRIPTION]",
    9001: "User unknown - the IMSI is not known to the network [DIAMETER_ERROR_USER_UNKNOWN]",
    9002: "No APN subscription - requested APN not in the user profile "
          "[DIAMETER_ERROR_USER_NO_APN_SUBSCRIPTION]",
    9003: "Authorization rejected - user barred from non-3GPP access or this APN "
          "[DIAMETER_AUTHORIZATION_REJECTED]",
    9006: "Illegal ME - the mobile equipment is not accepted by the network "
          "[DIAMETER_ERROR_ILLEGAL_EQUIPMENT]",
    10500: "Network failure - the requested procedure could not be completed "
           "[DIAMETER_ERROR_UNABLE_TO_COMPLY]",
    11001: "RAT type not allowed - access type is restricted for this user "
           "[DIAMETER_RAT_TYPE_NOT_ALLOWED]",
    11005: "IMEI not accepted - emergency request using an IMEI was rejected",
    11011: "PLMN not allowed - roaming/PLMN filtering rejected the request "
           "[DIAMETER_ERROR_ROAMING_NOT_ALLOWED]",
    11055: "Unauthenticated emergency not supported - emergency with unauthenticated IMSI "
           "rejected (auth failed / cannot proceed at AAA)",
    # ---- Private Status Types (Table 8.1.2.3-1) ----
    40961: "Reactivation requested - the IPsec tunnel was released; UE should re-establish it "
           "for the same PDN connection",
    41041: "Backoff timer - network-supplied backoff timer value (GPRS timer 3)",
    41050: "Only PDN type IPv4 is allowed for the requested PDN connectivity",
    41051: "Only PDN type IPv6 is allowed for the requested PDN connectivity",
    41101: "Device identity - the ePDG requests (or the UE supplies) the IMEI/IMEISV",
    41112: "Emergency support - the ePDG supports emergency service",
    41134: "Emergency call numbers - local emergency numbers provided by the ePDG",
    41288: "NBIFOM generic container",
    41304: "P-CSCF reselection support (P-CSCF restoration extension for untrusted WLAN)",
    41501: "PTI - procedure transaction identity for an ePDG-initiated modification",
    42011: "IKEv2 multiple bearer PDN connectivity support",
    42014: "EPS QoS",
    42015: "Extended EPS QoS",
    42017: "TFT (traffic flow template)",
    42020: "Modified bearer - sender's ESP SPI",
    42094: "APN-AMBR",
    42095: "Extended APN-AMBR",
    51015: "N1 mode capability / PDU session ID",
    51115: "N1 mode information (S-NSSAI)",
    52216: "N1 mode S-NSSAI PLMN ID",
    52301: "DNS server security info indication",
    52302: "DNS server security info",
    52331: "ATSSS request parameters",
    52332: "ATSSS response parameters",
    55911: "High priority access info",
}

def notify_name(value):
    return NOTIFY_TYPE_NAMES.get(value, "UNKNOWN")

def notify_describe(value):
    """Return a human-friendly one-line description for a Notify Message Type, or '' if none."""
    return NOTIFY_DESCRIPTIONS.get(value, "")

# Notify Message Types for which swu_ike has an explicit code path (decode + act on). Any received
# Notify NOT in this set is "unhandled": still valid, but we take no specific action, so it gets an
# extra verbose log line (full hex + class + hint) — invaluable when diagnosing an unexpected
# tunnel drop, since the ePDG often signals the cause via a Notify we don't yet act on. Keep this
# in sync with the actual `== <CONST>` / `in (...)` handling sites in state_1..4 + handle_INFORMATIONAL.
HANDLED_NOTIFY_TYPES = frozenset({
    # IKE_SA_INIT (state_1)
    INVALID_KE_PAYLOAD, COOKIE, NAT_DETECTION_SOURCE_IP, NAT_DETECTION_DESTINATION_IP,
    IKEV2_FRAGMENTATION_SUPPORTED,
    # IKE_AUTH (state_2/3/4): DEVICE_IDENTITY request/answer, PDN type, backoff, and every
    # error notify (< 16384) is uniformly surfaced via log_notify_error before aborting.
    DEVICE_IDENTITY, PDN_TYPE_IPv4_ONLY_ALLOWED, PDN_TYPE_IPv6_ONLY_ALLOWED, BACKOFF_TIMER,
    # INFORMATIONAL (handle_INFORMATIONAL_request / state_delete)
    REACTIVATION_REQUESTED_CAUSE,
})

def notify_is_handled(value):
    """True if swu_ike has a specific action for this Notify type. All error types (< 16384) are
    considered handled because the auth flow uniformly logs + aborts on them."""
    return value < 16384 or value in HANDLED_NOTIFY_TYPES

# --- 3GPP TS 24.302 clause 7.2.2.2 reject-Notify retry policy (Table 8.1.2.2-1) --------------------
# When the ePDG rejects the attach with a Private Error Notify, blindly re-attaching in a tight loop
# (a) hammers the network and (b) is forbidden for some causes until the USIM/PLMN changes. Classify
# each reject so start_ike can stop spinning and so the manager gets a machine-readable reason.
#   no_retry  : same-PLMN retry is pointless until the SIM is swapped (subscription/equipment/PLMN
#               level rejections). swu_ike stops looping and exits; durable back-off is the manager's.
#   backoff   : retry is allowed later; honour an attached BACKOFF_TIMER (Tw3) if present, else an
#               implementation back-off (the supervised restart cadence spaces attempts out).
#   transient : generic RFC 7296 / unclassified errors -> the existing bounded retry loop as today.
REJECT_NO_RETRY_SAME_PLMN = frozenset({9000, 9001, 9003, 9006, 11001, 11011})
REJECT_BACKOFF_THEN_RETRY = frozenset({10500, 9002})

def reject_policy(code):
    if code in REJECT_NO_RETRY_SAME_PLMN:
        return "no_retry"
    if code in REJECT_BACKOFF_THEN_RETRY:
        return "backoff"
    return "transient"     # generic / RFC 7296 errors: bounded retry as today


#IKEv2 Authenticaton Method
RSA_DIGITAL_SIGNATURE             = 1
SHARED_KEY_MESSAGE_INTEGRITY_CODE = 2
DSS_DIGITAL_SIGNATURE             = 3

#IKEv2 Traffic Selector Types
TS_IPV4_ADDR_RANGE = 7
TS_IPV6_ADDR_RANGE = 8

#IP protocol_id
ANY =   0
TCP =   6
UDP =  17
ICMP =  1
ESP_PROTOCOL = 50

NAT_TRAVERSAL = 4500

#IKEv2 Configuration Payload CFG Types
CFG_REQUEST =       1
CFG_REPLY =         2
CFG_SET =           3
CFG_ACK =           4

# IKEv2 Configuration Payload Attribute Types (num, length) None = more
INTERNAL_IP4_ADDRESS	           = 1
INTERNAL_IP4_NETMASK	           = 2
INTERNAL_IP4_DNS	               = 3
INTERNAL_IP4_NBNS	               = 4
INTERNAL_IP4_DHCP		           = 6
APPLICATION_VERSION		           = 7
INTERNAL_IP6_ADDRESS	           = 8
INTERNAL_IP6_DNS	               = 10
INTERNAL_IP6_DHCP	               = 12
INTERNAL_IP4_SUBNET	               = 13
SUPPORTED_ATTRIBUTES	           = 14
INTERNAL_IP6_SUBNET	               = 15
MIP6_HOME_PREFIX	               = 16
INTERNAL_IP6_LINK	               = 17
INTERNAL_IP6_PREFIX	               = 18
HOME_AGENT_ADDRESS	               = 19
P_CSCF_IP4_ADDRESS	               = 20
P_CSCF_IP6_ADDRESS	               = 21
FTT_KAT		                       = 22
EXTERNAL_SOURCE_IP4_NAT_INFO       = 23
TIMEOUT_PERIOD_FOR_LIVENESS_CHECK  = 24
INTERNAL_DNS_DOMAIN	               = 25
INTERNAL_DNSSEC_TA                 = 26

#IKEv2 Identification Payload ID Types
ID_IPV4_ADDR     = 1
ID_FQDN	         = 2
ID_RFC822_ADDR	 = 3
ID_IPV6_ADDR	 = 5
ID_DER_ASN1_DN	 = 9
ID_DER_ASN1_GN	 = 10
ID_KEY_ID	     = 11
ID_FC_NAME	     = 12
ID_NULL	         = 13




#EAP COde type
EAP_REQUEST  = 1
EAP_RESPONSE = 2
EAP_SUCCESS  = 3
EAP_FAILURE  = 4

#IANA EAP Type
EAP_IDENTITY = 1
EAP_AKA = 23

#EAP-AKA/EAP-SIM Subtypes:
AKA_Challenge = 1
AKA_Authentication_Reject = 2
AKA_Synchronization_Failure = 4
AKA_Identity = 5
SIM_Start = 10
SIM_Challenge = 11
AKA_Notification = 12
SIM_Notification = 12
AKA_Reauthentication = 13
SIM_Reauthentication = 13
AKA_Client_Error = 14
SIM_Client_Error = 14

#EAP-AKA/EAP-SIM Atrributes:
AT_RAND = 1
AT_AUTN = 2
AT_RES = 3
AT_AUTS = 4
AT_PADDING = 6
AT_NONCE_MT = 7
AT_PERMANENT_ID_REQ = 10
AT_MAC = 11
AT_NOTIFICATION = 12
AT_ANY_ID_REQ = 13
AT_IDENTITY = 14
AT_VERSION_LIST = 15
AT_SELECTED_VERSION = 16
AT_FULLAUTH_ID_REQ = 17
AT_COUNTER = 19
AT_COUNTER_TOO_SMALL = 20
AT_NONCE_S = 21
AT_CLIENT_ERROR_CODE = 22
AT_IV = 129
AT_ENCR_DATA = 130
AT_NEXT_PSEUDONYM = 132
AT_NEXT_REAUTH_ID = 133
AT_CHECKCODE = 134
AT_RESULT_IND = 135


# Role
ROLE_INITIATOR = 1
ROLE_RESPONDER = 0


class swu():

    def __init__(self, source_address,epdg_address,apn,modem,default_gateway,mcc,mnc,imsi,netns):
        self.source_address = source_address
        self.epdg_address = epdg_address
        self.apn = apn
        self.com_port = modem
        self.default_gateway = default_gateway
        # 3GPP TS 23.003: in the NAI realm and every EPC FQDN (mnc<MNC>.mcc<MCC>.3gppnetwork.org,
        # and the APN-FQDN IDr) the MCC is 3 digits and the MNC MUST be zero-padded to 3 digits.
        # Normalise once here so all NAI/IDr builders below use the padded form — passing "15"
        # instead of "015" would otherwise construct a wrong "mnc15" realm and be rejected.
        self.mcc = str(mcc).zfill(3) if mcc else mcc
        self.mnc = str(mnc).zfill(3) if mnc else mnc
        self.imsi = imsi
        
        self.netns_name = netns
        
        self.set_variables()
        self.set_udp() # default
        self.create_socket(self.client_address)
        self.create_socket_nat(self.client_address_nat)
        self.create_socket_esp(self.client_address_esp)        
        self.userplane_mode = ESP_PROTOCOL
        
        self.sk_ENCR_NULL_pad_length = 0 #[0 or 1 byte] SK payload is not definied in RFC for IKEv2. Some vendors don't use pad length byte, others use.

        
    def set_variables(self):
        self.port = DEFAULT_IKE_PORT
        self.port_nat = DEFAULT_IKE_NAT_TRAVERSAL_PORT
        self.client_address = (self.source_address,self.port)
        self.client_address_nat = (self.source_address,self.port_nat) 
        self.client_address_esp = (self.source_address,0)         
        self.timeout = DEFAULT_TIMEOUT_UDP
        self.state = 0
        self.server_address = (self.epdg_address, self.port)
        self.server_address_nat = (self.epdg_address, self.port_nat)
        self.server_address_esp = (self.epdg_address, 0)        
        self.message_id_request = 0
        self.message_id_responses = 0

        self.role = ROLE_INITIATOR
        self.old_ike_message_received = False        
        self.ike_spi_initiator_old = None
        self.ike_spi_responder_old = None
        self.next_reauth_id = None
        
        self.check_nat = True
        self.device_identity_requested = False
        self.device_identity_type = None
        # Per-instance IMEI/IMEISV for the ePDG DEVICE_IDENTITY response. Default to the module
        # constants (upstream behaviour) until set_device_identity() overrides them.
        self.imei = IMEI
        self.imeisv = IMEISV

        # G2: reject-Notify classification carried out of the auth flow to start_ike / status.
        # reject_reason_code = the friendly Notify name of the last hard reject (or None);
        # reject_reason_policy = reject_policy() of it; reject_backoff_seconds = decoded Tw3 if the
        # ePDG attached a BACKOFF_TIMER to the reject (else None). Reset each attach.
        self.reject_reason_code = None
        self.reject_reason_policy = None
        self.reject_backoff_seconds = None

        # G4: initiator liveness / Dead-Peer-Detection (TS 24.302 7.2.2A). liveness_period = idle
        # interval before we send an empty INFORMATIONAL request probe; after SWU_LIVENESS_RETRIES
        # consecutive unanswered probes we declare the SA dead and tear down (the supervisor
        # re-establishes). 0 disables. Kept separate from the RFC 3948 NAT-T keepalive (that holds
        # the UDP mapping open but is unacknowledged, so it is not a liveness proof).
        self.liveness_period = float(os.environ.get("SWU_LIVENESS_PERIOD", "20") or 20)
        self.liveness_retries = int(os.environ.get("SWU_LIVENESS_RETRIES", "4") or 4)
        self._last_rx = None                 # time.monotonic() of last protected IKE msg in-loop
        self._liveness_outstanding = 0       # consecutive probes sent without a response
        self._liveness_probe_mid = None      # message-id of the in-flight probe (for response match)

        # Proactive CHILD-SA rekey (TS 24.302 7.2.2C / RFC 7296). IKEv2 does NOT carry SA lifetime
        # on the wire, so rekey timing is local policy: we rekey the ESP (CHILD) SA every
        # child_rekey_period seconds measured from its establishment, before it silently ages out
        # on the ePDG (which otherwise stops accepting our ESP => a periodic idle drop). 0 disables
        # (passive only). SWU_CHILD_REKEY_MINUTES is set by render.py from settings.rekey.minutes
        # (default 30 min). We use a UE-initiated make-before-break CREATE_CHILD_SA with PFS
        # (Telus rejects a no-PFS child rekey with NO_PROPOSAL_CHOSEN); on reject/timeout we fall
        # back to a supervised re-establish (state_delete + exit -> entrypoint restarts with PIN
        # verify), i.e. never worse than the pre-rekey behaviour at the same expiry point.
        _rk_min = float(os.environ.get("SWU_CHILD_REKEY_MINUTES", "30") or 30)
        self.child_rekey_period = _rk_min * 60.0 if _rk_min > 0 else 0.0
        self._child_sa_time = None           # time.monotonic() the current CHILD SA was installed
        self._rekey_outstanding = False      # a UE-initiated CHILD rekey request is in flight
        self._rekey_sent_at = None           # time.monotonic() the rekey request was sent (timeout)
        self._rekey_packet = None            # exact bytes in flight, for verbatim retransmission
        self._rekey_tries = 0                # transmissions of the current request (1 = original)
        self._rekey_retry_at = None          # retry time after an explicit peer rejection
        self.rekey_response_timeout = float(os.environ.get("SWU_REKEY_TIMEOUT", "10") or 10)
        # RFC 7296 2.1 makes retransmitting an unanswered request the initiator's job. Sending
        # once and giving up treated a single lost packet as a dead peer: measured on this
        # gateway, one unanswered rekey tore down a tunnel that had been carrying IMS for
        # fifty minutes, and the rebuild cost five further minutes of failed registration.
        self.rekey_retransmits = int(os.environ.get("SWU_REKEY_RETRANSMITS", "3") or 3)
        # How long to wait before trying again after an explicit rejection. The old CHILD SA is
        # untouched and still carrying traffic, so this is a retry, not a recovery.
        self.rekey_retry_interval = float(os.environ.get("SWU_REKEY_RETRY_INTERVAL", "300") or 300)
        # Consume exactly one response for each CREATE_CHILD_SA request. A peer retransmits the
        # same response when our request was retransmitted; applying it twice deletes the new SA.
        self._create_child_request_id = None
        self._create_child_response_handled = False
        # Which of our CREATE_CHILD_SA request kinds is in flight ("ike" or "child"), so the
        # shared response handler credits an error notify against the right rekey driver.
        self._create_child_kind = None

        # Proactive IKE SA rekey (RFC 7296 2.18, UE-initiated make-before-break). Carriers
        # bound the IKE SA / SWu session by a local clock we cannot see: EE rekeys it at
        # ~12 h, and giffgaff/O2 UK silently invalidates the session at ~2h50m without any
        # IKE message at all (issue #33) — IMS then rejects re-REGISTER until a fresh
        # session exists. We do not implement the responder side of an ePDG-initiated rekey
        # (state_epdg_create_sa refuses it and the ePDG then deletes the SA => ~1 min
        # outage), so this period must stay comfortably below the carrier's clock; render.py
        # supplies it from settings (default 150 min). Rekeying FIRST keeps us the exchange
        # initiator, so the SA
        # roles, key-selection and header-flag assumptions baked into this file stay valid. The
        # machinery (state_ue_create_sa + the IKE branch of state_epdg_create_sa_response:
        # SKEYSEED' = prf(SK_d, g^ir | Ni | Nr), old-SA answer paths, DELETE old) predates this
        # timer; the timer just fires it before the ePDG's own clock does. On explicit
        # rejection the established IKE SA is untouched (retry later); on no-response we
        # re-establish, which is no worse than the ePDG-initiated teardown at the same age.
        _ike_rk_min = float(os.environ.get("SWU_IKE_REKEY_MINUTES", "600") or 600)
        self.ike_rekey_period = _ike_rk_min * 60.0 if _ike_rk_min > 0 else 0.0
        self.ike_rekey_retry_interval = float(os.environ.get("SWU_IKE_REKEY_RETRY_INTERVAL", "3600") or 3600)
        self._ike_sa_time = None             # time.monotonic() the current IKE SA was established
        self._ike_rekey_outstanding = False  # a UE-initiated IKE rekey request is in flight
        self._ike_rekey_sent_at = None
        self._ike_rekey_packet = None        # exact bytes in flight, for verbatim retransmission
        self._ike_rekey_tries = 0
        self._ike_rekey_retry_at = None
        self._ike_rekey_failed = False

        # G1: IKEv2 fragmentation (RFC 7383). fragmentation_enabled advertises
        # IKEV2_FRAGMENTATION_SUPPORTED in IKE_SA_INIT (additive/optional; disable via
        # SWU_FRAGMENTATION=0 if a carrier chokes). peer_supports_fragmentation is set when the
        # ePDG echoes the notify. _frag_buf reassembles inbound SKF fragments keyed by message-id.
        self.fragmentation_enabled = (os.environ.get("SWU_FRAGMENTATION", "1") != "0")
        self.peer_supports_fragmentation = False
        self._frag_buf = {}
        # P2-4: outbound RFC 7383 fragmentation. Split a protected IKE message into SKF fragments
        # only when BOTH sides advertised support AND the plaintext body exceeds fragment_size.
        # Default 1000 keeps each fragment well under UDP/IPsec/NAT-T path MTU; do not exceed 1200
        # without interop testing. Telus messages are small so this never triggers there.
        self.fragment_size = int(os.environ.get("SWU_FRAGMENT_SIZE", "1000") or 1000)

        # P0-1: ePDG may assign the inner IPv6 by prefix (INTERNAL_IP6_SUBNET) instead of a full
        # INTERNAL_IP6_ADDRESS (TS 24.302 7.4.1.1). Init empty so state_4/set_routes can test it
        # before any CFG_REPLY is parsed. Entries are (prefix_string, prefix_len_int).
        self.ipv6_subnet_list = []

        # P2-5: responder-side duplicate-request response cache. ePDG INFORMATIONAL/CREATE_CHILD_SA
        # requests may be retransmitted; keyed by (exchange_type, message_id) -> response bytes (or
        # list of fragment bytes) so a duplicate re-sends the SAME response WITHOUT re-running the
        # side effects (delete SA, switch SPI, re-register). Bounded to the most-recent few ids.
        self._response_cache = {}
        self._response_cache_order = []
        self._response_cache_max = 16
        self._capture_buf = None      # when a list, _send_one appends every packet it sends here

        # P2-5: initiator request retransmission. On timeout we resend the SAME bytes (never
        # regenerate nonce/SPI/DH/IV) following this per-attempt seconds schedule. The FIRST
        # attempt's 2s timeout equals the original single socket timeout, so a prompt ePDG (Telus
        # responds in <1s) succeeds on the first try with byte-for-byte identical timing; the extra
        # attempts only help a lossy link. Bounded total (2+4+8=14s) keeps failure detection within
        # the entrypoint's 90s establishment budget.
        _rt = os.environ.get("SWU_IKE_RETRANS_TIMEOUTS", "2,4,8")
        try:
            self._retrans_timeouts = [float(x) for x in _rt.split(",") if x.strip()] or [self.timeout]
        except Exception:
            self._retrans_timeouts = [self.timeout]

        # P2-3: throttle the ESP-activity liveness IPC so a busy tunnel does not flood the pipe.
        self._esp_liveness_last_tx = 0.0
        self._esp_liveness_min_interval = float(os.environ.get("SWU_ESP_LIVENESS_INTERVAL", "5") or 5)

        self.set_identification(IDI,ID_RFC822_ADDR,'0' + self.imsi + '@nai.epc.mnc' + self.mnc + '.mcc' + self.mcc + '.3gppnetwork.org')
        # IDr (the identity of the ePDG we want to reach) is, per 3GPP TS 24.302 clause 7.2.2.1,
        # the APN encoded as an ID_FQDN. Two encodings are seen in the wild:
        #   "apn"  -> the bare APN (e.g. "ims"). This is the default and stays the default because
        #            it is the empirically-proven form most carriers' ePDGs accept; do NOT change
        #            it lightly.
        #   "fqdn" -> the operator-identified APN-FQDN a real UE builds:
        #            "<apn>.apn.epc.mnc<MNC3>.mcc<MCC3>.pub.3gppnetwork.org" (23.003). self.mcc /
        #            self.mnc are already zero-padded to 3 digits in __init__.
        # Selected by env SWU_IDR_MODE so a carrier that needs the full FQDN can opt in without a
        # code change; falls through to the bare-APN behaviour when unset/invalid.
        idr_mode = os.environ.get("SWU_IDR_MODE", "apn")
        if idr_mode == "fqdn" and self.mcc and self.mnc:
            idr_value = "%s.apn.epc.mnc%s.mcc%s.pub.3gppnetwork.org" % (self.apn, self.mnc, self.mcc)
        else:
            idr_value = self.apn
        self.set_identification(IDR, ID_FQDN, idr_value)
        
        self.ike_decoded_header = {}
        self.decodable_payloads = [
            SA,
            KE,
            IDI,
            IDR,
            CERT,
            CERTREQ,
            AUTH,
            NINR,
            N,
            D,
            V,
            TSI,
            TSR,
            SK,
            CP,
            EAP
        ]
      
        self.iana_diffie_hellman = {
            MODP_768_bit:   768,
            MODP_1024_bit: 1024,
            MODP_1536_bit: 1536,
            MODP_2048_bit: 2048,
            MODP_3072_bit: 3072,
            MODP_4096_bit: 4096,
            MODP_6144_bit: 6144,
            MODP_8192_bit: 8192 
        }
        self.prf_function = {
            PRF_HMAC_MD5 :        hashes.MD5(),
            PRF_HMAC_SHA1 :       hashes.SHA1(),    
            #PRF_HMAC_TIGER :        3
            #PRF_AES128_XCBC :       4
            PRF_HMAC_SHA2_256 :   hashes.SHA256(),
            PRF_HMAC_SHA2_384 :   hashes.SHA384(),
            PRF_HMAC_SHA2_512 :   hashes.SHA512()
            #PRF_AES128_CMAC :       8 
        }
        self.prf_key_len_bytes = {
            PRF_HMAC_MD5 :          16,
            PRF_HMAC_SHA1 :         20,
            #PRF_HMAC_TIGER :        -,
            PRF_AES128_XCBC :       16,
            PRF_HMAC_SHA2_256 :     32,
            PRF_HMAC_SHA2_384 :     48,
            PRF_HMAC_SHA2_512 :     64,
            PRF_AES128_CMAC :       16,
            
        }
        self.integ_function = {        
            NONE :                      None,
            AUTH_HMAC_MD5_96 :	        hashes.MD5(),
            AUTH_HMAC_SHA1_96 :         hashes.SHA1(),
            #AUTH_DES_MAC :	            -,
            #AUTH_KPDK_MD5 :             -,
            #AUTH_AES_XCBC_96 :          16,
            #AUTH_HMAC_MD5_128 :         -,
            #AUTH_HMAC_SHA1_160 :        -,
            #AUTH_AES_CMAC_96 :          -,
            #AUTH_AES_128_GMAC :         16,
            #AUTH_AES_192_GMAC :        24,
            #AUTH_AES_256_GMAC :        32,
            AUTH_HMAC_SHA2_256_128 :   hashes.SHA256(),
            AUTH_HMAC_SHA2_384_192 :   hashes.SHA384(),
            AUTH_HMAC_SHA2_512_256 :   hashes.SHA512()            
        }        
        self.integ_key_len_bytes = {        
            NONE :                      0,
            AUTH_HMAC_MD5_96 :	        16,
            AUTH_HMAC_SHA1_96 :         20,
            #AUTH_DES_MAC :	            -,
            #AUTH_KPDK_MD5 :             -,
            #AUTH_AES_XCBC_96 :          16,
            #AUTH_HMAC_MD5_128 :         -,
            #AUTH_HMAC_SHA1_160 :        -,
            #AUTH_AES_CMAC_96 :          -,
            #AUTH_AES_128_GMAC :         16,
            #AUTH_AES_192_GMAC :        24,
            #AUTH_AES_256_GMAC :        32,
            AUTH_HMAC_SHA2_256_128 :   32,
            AUTH_HMAC_SHA2_384_192 :   48,
            AUTH_HMAC_SHA2_512_256 :   64        
        }
        self.integ_key_truncated_len_bytes = {        
            NONE :                      0,
            AUTH_HMAC_MD5_96 :	        12,
            AUTH_HMAC_SHA1_96 :         12,
            #AUTH_DES_MAC :	            -,
            #AUTH_KPDK_MD5 :             -,
            #AUTH_AES_XCBC_96 :          12,
            #AUTH_HMAC_MD5_128 :         -,
            #AUTH_HMAC_SHA1_160 :        -,
            #AUTH_AES_CMAC_96 :          -,
            #AUTH_AES_128_GMAC :         16?,
            #AUTH_AES_192_GMAC :        24?,
            #AUTH_AES_256_GMAC :        32?,
            AUTH_HMAC_SHA2_256_128 :   16,
            AUTH_HMAC_SHA2_384_192 :   24,
            AUTH_HMAC_SHA2_512_256 :   32        
        }        
        self.configuration_payload_len_bytes = {
        
            INTERNAL_IP4_ADDRESS	                : 4,
            INTERNAL_IP4_NETMASK	                : 4,
            INTERNAL_IP4_DNS	                    : 4,
            INTERNAL_IP4_NBNS	                    : 4,
            INTERNAL_IP4_DHCP		                : 4,
            APPLICATION_VERSION		                : None,
            INTERNAL_IP6_ADDRESS	                : 16,
            INTERNAL_IP6_DNS	                    : 16,
            INTERNAL_IP6_DHCP	                    : 16,
            INTERNAL_IP4_SUBNET	                    : 8,
            SUPPORTED_ATTRIBUTES	                : None,
            INTERNAL_IP6_SUBNET	                    : 17,
            MIP6_HOME_PREFIX	                    : 21,
            INTERNAL_IP6_LINK	                    : None,
            INTERNAL_IP6_PREFIX	                    : 17,
            HOME_AGENT_ADDRESS	                    : None, #16 or 20
            P_CSCF_IP4_ADDRESS	                    : 4,
            P_CSCF_IP6_ADDRESS	                    : 16,
            FTT_KAT		                            : 2,
            EXTERNAL_SOURCE_IP4_NAT_INFO            : 6,
            TIMEOUT_PERIOD_FOR_LIVENESS_CHECK	    : 4,
            INTERNAL_DNS_DOMAIN	                    : None,
            INTERNAL_DNSSEC_TA                      : None      
        }
        self.errors = {
            OK :                            'OK',
            TIMEOUT :                       'TIMEOUT',
            REPEAT_STATE :                  'REPEAT_STATE',
            DECODING_ERROR :                'DECODING_ERROR',
            MANDATORY_INFORMATION_MISSING : 'MANDATORY_INFORMATION_MISSING',
            OTHER_ERROR :                   'OTHER_ERROR'    
        }


    def return_integrity_algorithm_name(self):        
        integ_alg = {
            AUTH_HMAC_MD5_96 : "HMAC_MD5_96 [RFC2403]",
            AUTH_HMAC_SHA1_96 : "HMAC_SHA1_96 [RFC2404]",
            AUTH_HMAC_SHA2_256_128 : "HMAC_SHA2_256_128 [RFC4868]", 
            AUTH_HMAC_SHA2_384_192 : "HMAC_SHA2_384_192 [RFC4868]",
            AUTH_HMAC_SHA2_512_256 : "HMAC_SHA2_512_256 [RFC4868]",
            NONE : "NONE [RFC4306]"
        }
        return integ_alg.get(self.negotiated_integrity_algorithm,'UNKNOWN')
    
    def return_encryption_algorithm_name(self):
        encr_alg = ''
        key_size = self.negotiated_encryption_algorithm_key_size
        if key_size == 128 and self.negotiated_encryption_algorithm == ENCR_AES_CBC:
            encr_alg = "AES-CBC-128 [RFC3602]"
        elif key_size == 256 and self.negotiated_encryption_algorithm == ENCR_AES_CBC:
            encr_alg = "AES-CBC-256 [RFC3602]"
        elif self.negotiated_encryption_algorithm == ENCR_NULL:
            encr_alg = "NULL [RFC2410]"
        return encr_alg
    
    def return_integrity_algorithm_child_name(self):
        integ_alg = {
            AUTH_HMAC_MD5_96 : "HMAC-MD5-96 [RFC2403]",
            AUTH_HMAC_SHA1_96 : "HMAC-SHA-1-96 [RFC2404]",
            AUTH_HMAC_SHA2_256_128 : "HMAC-SHA-256-128 [RFC4868]", 
            AUTH_HMAC_SHA2_384_192 : "HMAC-SHA-384-192 [RFC4868]",
            AUTH_HMAC_SHA2_512_256 : "HMAC-SHA-512-256 [RFC4868]",
            NONE : "NULL"
        }
        return integ_alg.get(self.negotiated_integrity_algorithm_child,'UNKNOWN')        
    
    def return_encryption_algorithm_child_name(self):
        encr_alg = ''
        if self.negotiated_encryption_algorithm_child == ENCR_AES_CBC:
            encr_alg = "AES-CBC [RFC3602]"
        elif self.negotiated_encryption_algorithm_child == ENCR_AES_GCM_8:
            encr_alg = "AES-GCM [RFC4106]"
        elif self.negotiated_encryption_algorithm_child == ENCR_AES_GCM_12:
            encr_alg = "AES-GCM [RFC4106]"
        elif self.negotiated_encryption_algorithm_child == ENCR_AES_GCM_16:
            encr_alg = "AES-GCM [RFC4106]"            
        elif self.negotiated_encryption_algorithm_child == ENCR_NULL:
            encr_alg = "NULL"
        return encr_alg    
    
    def print_ikev2_decryption_table(self):
        # Never persist traffic-decryption keys. Support bundles include this process's log.
        return
    def print_esp_sa(self):
        # Never persist traffic-decryption keys. Support bundles include this process's log.
        return


       
    def set_timeout(self,value):
        self.timeout = value
        
    def set_udp(self):
        self.socket_type = UDP

    def create_socket(self,client_address):
        
        if self.socket_type == UDP:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        else:
            exit()
            
        self.socket.bind(client_address)                
        self.socket.settimeout(self.timeout)
        self._enable_outer_pmtud(self.socket)


    def create_socket_nat(self,client_address):
        
        if self.socket_type == UDP:
            self.socket_nat = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        else:
            exit()
            
        self.socket_nat.bind(client_address)                
        self.socket_nat.settimeout(self.timeout)
        self._enable_outer_pmtud(self.socket_nat)

    def create_socket_esp(self,client_address):
        self.socket_esp = socket.socket(socket.AF_INET, socket.SOCK_RAW, ESP_PROTOCOL)
        self.socket_esp.bind(client_address)    
        self._enable_outer_pmtud(self.socket_esp)

    def _enable_outer_pmtud(self, sock):
        """Set IP_PMTUDISC_DO on an outer (ePDG-facing) socket: the kernel sets DF, discovers the
        path MTU (from ICMP frag-needed, incl. a WireGuard/constrained hop on the HOST outside the
        container) and caches it on the route, and NEVER IP-fragments our datagram. We never want
        the kernel to IP-fragment the ESP-in-UDP: many carrier/ePDG paths drop IP fragments (EE
        does). Instead swu splits the INNER packet so every ESP datagram is complete and fits the
        sensed PMTU. The default IP_PMTUDISC_WANT would instead silently EMSGSIZE-drop oversized
        sends; PMTUDISC_DO is the same DF behaviour but keeps the route PMTU query (`ip route get`)
        populated for _sense_outer_mtu(). Best-effort: never abort socket setup on an odd kernel."""
        try:
            ip_mtu_discover = getattr(socket, "IP_MTU_DISCOVER", 10)
            ip_pmtudisc_do = getattr(socket, "IP_PMTUDISC_DO", 2)
            sock.setsockopt(socket.IPPROTO_IP, ip_mtu_discover, ip_pmtudisc_do)
        except Exception as e:
            swu_log("could not set IP_PMTUDISC_DO on outer socket: %r" % e)
        
    def set_server(self,address):
        self.server_address = (address,self.port)

    def set_server_nat(self,address):
        self.server_address_nat = (address,self.port_nat)

    def set_server_esp(self,address):
        self.server_address_esp = (address,0)

    def send_data(self, data):
        # P2-4: encode_payload_type_sk may return a list of SKF fragment packets; send each.
        if isinstance(data, (list, tuple)):
            for d in data:
                self._send_one(d)
            return
        self._send_one(data)

    def _send_one(self, data):
        # P2-5: when capturing a responder reply (see _dispatch_epdg_request), record the exact
        # bytes so a duplicate request can be answered from cache without re-running side effects.
        if self._capture_buf is not None:
            self._capture_buf.append(data)
        if self.userplane_mode == ESP_PROTOCOL:
            self.socket.sendto(data, self.server_address)
        else:
            self.socket_nat.sendto(b'\x00'*4 + data, self.server_address_nat)

    def _send_request_await_response(self, packet):
        """P2-5: send an initiator IKE request and wait for a decodable response, RETRANSMITTING
        THE SAME bytes on timeout (per SWU_IKE_RETRANS_TIMEOUTS). Never regenerates the packet, so
        nonce/SPI/DH/IV are preserved (mandatory for IKE_SA_INIT / IKE_AUTH / CREATE_CHILD_SA
        retransmission). Returns True once a message decoded (self.ike_decoded_ok True) — leaving
        self.decoded_payload set for the caller — or False after the schedule is exhausted. On the
        prompt-response path this returns on the very first recv, identical to the old behaviour."""
        esp_mode = (self.userplane_mode == ESP_PROTOCOL)
        sock = self.socket if esp_mode else self.socket_nat
        schedule = self._retrans_timeouts or [self.timeout]
        try:
            for attempt, per_to in enumerate(schedule):
                if attempt > 0:
                    swu_log("IKE request retransmit %d/%d (message_id=%d, same bytes)" %
                            (attempt + 1, len(schedule), self.message_id_request))
                self.send_data(packet)
                deadline = time.monotonic() + per_to
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        sock.settimeout(remaining)
                        data, address = sock.recvfrom(2000)
                    except socket.timeout:
                        break
                    except Exception:
                        break
                    if esp_mode:
                        self.decode_ike(data)
                    else:
                        self.decode_ike(data[4:])
                    if self.ike_decoded_ok == True:
                        return True
            return False
        finally:
            try:
                sock.settimeout(self.timeout)
            except Exception:
                pass

    def _cache_response(self, key, response):
        """P2-5: store a responder reply (bytes or list[bytes]) under (exchange_type, message_id),
        evicting the oldest when over the bound."""
        if key not in self._response_cache:
            self._response_cache_order.append(key)
            while len(self._response_cache_order) > self._response_cache_max:
                old = self._response_cache_order.pop(0)
                self._response_cache.pop(old, None)
        self._response_cache[key] = response

    def _dispatch_epdg_request(self, handler):
        """P2-5: dispatch an ePDG-initiated request (INFORMATIONAL/CREATE_CHILD_SA, flags[0]==0)
        through the duplicate-request cache. On a first-seen (exchange_type, message_id) we run the
        handler (which sends its response via send_data) while capturing the exact response bytes,
        then cache them. On a retransmitted request with the same key we resend the cached response
        and DO NOT re-run the handler — so the side effects (SA delete, SPI switch, P-CSCF
        re-register) happen exactly once (RFC 7296 2.1 responder retransmission rules)."""
        key = (self.ike_decoded_header['exchange_type'], self.ike_decoded_header['message_id'])
        if key in self._response_cache:
            swu_log("duplicate ePDG request exch=%d msgid=%d; resending cached response "
                    "(side effects not re-run)" % key)
            self.send_data(self._response_cache[key])
            return
        self._capture_buf = []
        try:
            handler()
        finally:
            captured = self._capture_buf
            self._capture_buf = None
        if captured:
            self._cache_response(key, captured if len(captured) > 1 else captured[0])

    def return_random_bytes(self,size):
        if size == 0: return b''
        if size == 4: return struct.pack('!I', random.randrange(pow(2,32)-1))
        if size == 8: return struct.pack('!Q', random.randrange(pow(2,64)-1))
        if size == 16: return struct.pack('!Q', random.randrange(pow(2,64)-1)) + struct.pack('!Q', random.randrange(pow(2,64)-1))

    def return_random_int(self,size):
        if size == 4: return random.randrange(pow(2,32)-1)
        if size == 8: return random.randrange(pow(2,64)-1)
        if size == 16: return random.randrange(pow(2,128)-1)



    def return_flags(self,value): #works with value or tuple
        
        if type(value) is int:
            rvi = (value//8)%8
            return (rvi // 4, (rvi//2)%2, rvi%2)
        else: #is a tuple with (r,v,i)
            return 32*value[0]+16*value[1]+8*value[2]
            
            
            
    def dh_create_private_key_and_public_bytes(self,key_size):
        prime = {
             768: 0xFFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A63A3620FFFFFFFFFFFFFFFF,
            1024: 0xFFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7EDEE386BFB5A899FA5AE9F24117C4B1FE649286651ECE65381FFFFFFFFFFFFFFFF,                 
            1536: 0xffffffffffffffffc90fdaa22168c234c4c6628b80dc1cd129024e088a67cc74020bbea63b139b22514a08798e3404ddef9519b3cd3a431b302b0a6df25f14374fe1356d6d51c245e485b576625e7ec6f44c42e9a637ed6b0bff5cb6f406b7edee386bfb5a899fa5ae9f24117c4b1fe649286651ece45b3dc2007cb8a163bf0598da48361c55d39a69163fa8fd24cf5f83655d23dca3ad961c62f356208552bb9ed529077096966d670c354e4abc9804f1746c08ca237327ffffffffffffffff,
            2048: 0xffffffffffffffffc90fdaa22168c234c4c6628b80dc1cd129024e088a67cc74020bbea63b139b22514a08798e3404ddef9519b3cd3a431b302b0a6df25f14374fe1356d6d51c245e485b576625e7ec6f44c42e9a637ed6b0bff5cb6f406b7edee386bfb5a899fa5ae9f24117c4b1fe649286651ece45b3dc2007cb8a163bf0598da48361c55d39a69163fa8fd24cf5f83655d23dca3ad961c62f356208552bb9ed529077096966d670c354e4abc9804f1746c08ca18217c32905e462e36ce3be39e772c180e86039b2783a2ec07a28fb5c55df06f4c52c9de2bcbf6955817183995497cea956ae515d2261898fa051015728e5a8aacaa68ffffffffffffffff,
            3072: 0xffffffffffffffffc90fdaa22168c234c4c6628b80dc1cd129024e088a67cc74020bbea63b139b22514a08798e3404ddef9519b3cd3a431b302b0a6df25f14374fe1356d6d51c245e485b576625e7ec6f44c42e9a637ed6b0bff5cb6f406b7edee386bfb5a899fa5ae9f24117c4b1fe649286651ece45b3dc2007cb8a163bf0598da48361c55d39a69163fa8fd24cf5f83655d23dca3ad961c62f356208552bb9ed529077096966d670c354e4abc9804f1746c08ca18217c32905e462e36ce3be39e772c180e86039b2783a2ec07a28fb5c55df06f4c52c9de2bcbf6955817183995497cea956ae515d2261898fa051015728e5a8aaac42dad33170d04507a33a85521abdf1cba64ecfb850458dbef0a8aea71575d060c7db3970f85a6e1e4c7abf5ae8cdb0933d71e8c94e04a25619dcee3d2261ad2ee6bf12ffa06d98a0864d87602733ec86a64521f2b18177b200cbbe117577a615d6c770988c0bad946e208e24fa074e5ab3143db5bfce0fd108e4b82d120a93ad2caffffffffffffffff,
            4096: 0xffffffffffffffffc90fdaa22168c234c4c6628b80dc1cd129024e088a67cc74020bbea63b139b22514a08798e3404ddef9519b3cd3a431b302b0a6df25f14374fe1356d6d51c245e485b576625e7ec6f44c42e9a637ed6b0bff5cb6f406b7edee386bfb5a899fa5ae9f24117c4b1fe649286651ece45b3dc2007cb8a163bf0598da48361c55d39a69163fa8fd24cf5f83655d23dca3ad961c62f356208552bb9ed529077096966d670c354e4abc9804f1746c08ca18217c32905e462e36ce3be39e772c180e86039b2783a2ec07a28fb5c55df06f4c52c9de2bcbf6955817183995497cea956ae515d2261898fa051015728e5a8aaac42dad33170d04507a33a85521abdf1cba64ecfb850458dbef0a8aea71575d060c7db3970f85a6e1e4c7abf5ae8cdb0933d71e8c94e04a25619dcee3d2261ad2ee6bf12ffa06d98a0864d87602733ec86a64521f2b18177b200cbbe117577a615d6c770988c0bad946e208e24fa074e5ab3143db5bfce0fd108e4b82d120a92108011a723c12a787e6d788719a10bdba5b2699c327186af4e23c1a946834b6150bda2583e9ca2ad44ce8dbbbc2db04de8ef92e8efc141fbecaa6287c59474e6bc05d99b2964fa090c3a2233ba186515be7ed1f612970cee2d7afb81bdd762170481cd0069127d5b05aa993b4ea988d8fddc186ffb7dc90a6c08f4df435c934063199ffffffffffffffff,
            6144: 0xffffffffffffffffc90fdaa22168c234c4c6628b80dc1cd129024e088a67cc74020bbea63b139b22514a08798e3404ddef9519b3cd3a431b302b0a6df25f14374fe1356d6d51c245e485b576625e7ec6f44c42e9a637ed6b0bff5cb6f406b7edee386bfb5a899fa5ae9f24117c4b1fe649286651ece45b3dc2007cb8a163bf0598da48361c55d39a69163fa8fd24cf5f83655d23dca3ad961c62f356208552bb9ed529077096966d670c354e4abc9804f1746c08ca18217c32905e462e36ce3be39e772c180e86039b2783a2ec07a28fb5c55df06f4c52c9de2bcbf6955817183995497cea956ae515d2261898fa051015728e5a8aaac42dad33170d04507a33a85521abdf1cba64ecfb850458dbef0a8aea71575d060c7db3970f85a6e1e4c7abf5ae8cdb0933d71e8c94e04a25619dcee3d2261ad2ee6bf12ffa06d98a0864d87602733ec86a64521f2b18177b200cbbe117577a615d6c770988c0bad946e208e24fa074e5ab3143db5bfce0fd108e4b82d120a92108011a723c12a787e6d788719a10bdba5b2699c327186af4e23c1a946834b6150bda2583e9ca2ad44ce8dbbbc2db04de8ef92e8efc141fbecaa6287c59474e6bc05d99b2964fa090c3a2233ba186515be7ed1f612970cee2d7afb81bdd762170481cd0069127d5b05aa993b4ea988d8fddc186ffb7dc90a6c08f4df435c93402849236c3fab4d27c7026c1d4dcb2602646dec9751e763dba37bdf8ff9406ad9e530ee5db382f413001aeb06a53ed9027d831179727b0865a8918da3edbebcf9b14ed44ce6cbaced4bb1bdb7f1447e6cc254b332051512bd7af426fb8f401378cd2bf5983ca01c64b92ecf032ea15d1721d03f482d7ce6e74fef6d55e702f46980c82b5a84031900b1c9e59e7c97fbec7e8f323a97a7e36cc88be0f1d45b7ff585ac54bd407b22b4154aacc8f6d7ebf48e1d814cc5ed20f8037e0a79715eef29be32806a1d58bb7c5da76f550aa3d8a1fbff0eb19ccb1a313d55cda56c9ec2ef29632387fe8d76e3c0468043e8f663f4860ee12bf2d5b0b7474d6e694f91e6dcc4024ffffffffffffffff,
            8192: 0xffffffffffffffffc90fdaa22168c234c4c6628b80dc1cd129024e088a67cc74020bbea63b139b22514a08798e3404ddef9519b3cd3a431b302b0a6df25f14374fe1356d6d51c245e485b576625e7ec6f44c42e9a637ed6b0bff5cb6f406b7edee386bfb5a899fa5ae9f24117c4b1fe649286651ece45b3dc2007cb8a163bf0598da48361c55d39a69163fa8fd24cf5f83655d23dca3ad961c62f356208552bb9ed529077096966d670c354e4abc9804f1746c08ca18217c32905e462e36ce3be39e772c180e86039b2783a2ec07a28fb5c55df06f4c52c9de2bcbf6955817183995497cea956ae515d2261898fa051015728e5a8aaac42dad33170d04507a33a85521abdf1cba64ecfb850458dbef0a8aea71575d060c7db3970f85a6e1e4c7abf5ae8cdb0933d71e8c94e04a25619dcee3d2261ad2ee6bf12ffa06d98a0864d87602733ec86a64521f2b18177b200cbbe117577a615d6c770988c0bad946e208e24fa074e5ab3143db5bfce0fd108e4b82d120a92108011a723c12a787e6d788719a10bdba5b2699c327186af4e23c1a946834b6150bda2583e9ca2ad44ce8dbbbc2db04de8ef92e8efc141fbecaa6287c59474e6bc05d99b2964fa090c3a2233ba186515be7ed1f612970cee2d7afb81bdd762170481cd0069127d5b05aa993b4ea988d8fddc186ffb7dc90a6c08f4df435c93402849236c3fab4d27c7026c1d4dcb2602646dec9751e763dba37bdf8ff9406ad9e530ee5db382f413001aeb06a53ed9027d831179727b0865a8918da3edbebcf9b14ed44ce6cbaced4bb1bdb7f1447e6cc254b332051512bd7af426fb8f401378cd2bf5983ca01c64b92ecf032ea15d1721d03f482d7ce6e74fef6d55e702f46980c82b5a84031900b1c9e59e7c97fbec7e8f323a97a7e36cc88be0f1d45b7ff585ac54bd407b22b4154aacc8f6d7ebf48e1d814cc5ed20f8037e0a79715eef29be32806a1d58bb7c5da76f550aa3d8a1fbff0eb19ccb1a313d55cda56c9ec2ef29632387fe8d76e3c0468043e8f663f4860ee12bf2d5b0b7474d6e694f91e6dbe115974a3926f12fee5e438777cb6a932df8cd8bec4d073b931ba3bc832b68d9dd300741fa7bf8afc47ed2576f6936ba424663aab639c5ae4f5683423b4742bf1c978238f16cbe39d652de3fdb8befc848ad922222e04a4037c0713eb57a81a23f0c73473fc646cea306b4bcbc8862f8385ddfa9d4b7fa2c087e879683303ed5bdd3a062b3cf5b3a278a66d2a13f83f44f82ddf310ee074ab6a364597e899a0255dc164f31cc50846851df9ab48195ded7ea1b1d510bd7ee74d73faf36bc31ecfa268359046f4eb879f924009438b481c6cd7889a002ed5ee382bc9190da6fc026e479558e4475677e9aa9e3050e2765694dfc81f56e880b96e7160c980dd98edd3dfffffffffffffffff            
        }    
        g = 2        
        self.pn = dh.DHParameterNumbers(prime.get(key_size),g)
        parameters = self.pn.parameters()
        self.dh_private_key = parameters.generate_private_key()
        self.dh_public_key_bytes = self.dh_private_key.public_key().public_numbers().y.to_bytes(key_size//8,'big')
        
        
    def dh_calculate_shared_key(self,peer_public_key_bytes):
        peer_public_numbers = dh.DHPublicNumbers(int.from_bytes(peer_public_key_bytes, byteorder='big'), self.pn)
        peer_public_key = peer_public_numbers.public_key()
        self.dh_shared_key = self.dh_private_key.exchange(peer_public_key)
        
        
        
    def get_identity(self):
            imsi = return_imsi(self.com_port)
            self.imsi = imsi
            self.set_identification(IDI,ID_RFC822_ADDR,'0' + self.imsi + '@nai.epc.mnc' + self.mnc + '.mcc' + self.mcc + '.3gppnetwork.org')
    
    def encode_eap_at_identity(self, identity):
        """ Returns the EAP AT Identity as bytes """
        # 4 bytes -> header (type, at_len, identity_len)
        full_len = 4 + len(identity)
        at_len = int(full_len / 4)
        pad = 0
        if full_len % 4:
            pad = 4 - (full_len % 4)
            at_len += 1
        # 0e -> AT_IDENTITY
        eap_at_identity = (bytes([0x0e, at_len])
                + struct.pack('>H', len(identity))
                + identity.encode("utf-8")
                + pad * b'\x00')
        return eap_at_identity
        
    def eap_keys_calculation(self,ck, ik):
        identity = self.identification_initiator[1].encode('utf-8') #idi value
        digest = hashes.Hash(hashes.SHA1())
        digest.update(identity + ik + ck)
        MK = digest.finalize()
        
        result = b''
        xval = MK
        modulus = pow(2,160)
        
        for i in range(4):
            w0 = sha1_dss(xval)
            xval = ((int.from_bytes(xval,'big') + int.from_bytes(w0, 'big') + 1 ) % modulus).to_bytes(20,'big')
            w1 = sha1_dss(xval)
            xval = ((int.from_bytes(xval,'big') + int.from_bytes(w1, 'big') + 1 ) % modulus).to_bytes(20,'big')
            
            result += w0 + w1

        # return     
        return result[0:16],result[16:32],result[32:96],result[96:160],MK
        
        
    def eap_keys_calculation_fast_reauth(self,counter, nonce_s):
        identity = self.identification_initiator[1].encode('utf-8') #idi value
        digest = hashes.Hash(hashes.SHA1())
        digest.update(identity + struct.pack('!H',counter) + nonce_s + self.MK)
        XKEY = digest.finalize()
        
        result = b''
        xval = XKEY
        modulus = pow(2,160)
        
        for i in range(4):
            w0 = sha1_dss(xval)
            xval = ((int.from_bytes(xval,'big') + int.from_bytes(w0, 'big') + 1 ) % modulus).to_bytes(20,'big')
            w1 = sha1_dss(xval)
            xval = ((int.from_bytes(xval,'big') + int.from_bytes(w1, 'big') + 1 ) % modulus).to_bytes(20,'big')
            
            result += w0 + w1

        # return     
        return result[0:64],result[64:128],XKEY        
        
    def build_eap_aka_response(self, eap_identifier, res):
        """
        Build EAP-AKA Response payload with proper length calculation and padding.
        
        Args:
            eap_identifier: EAP packet identifier
            res: RES value (4-16 bytes according to 3GPP standard)
            
        Returns:
            Complete EAP-AKA response payload with proper length and padding
        """
        # Validate RES length (must be 4-16 bytes according to 3GPP TS 33.102)
        res_len = len(res)
        if res_len < 4 or res_len > 16:
            raise ValueError(f"RES length must be between 4-16 bytes, got {res_len}")
        
        # EAP-AKA fixed parts
        eap_code = bytes([2])  # Response
        eap_id = bytes([eap_identifier])
        eap_aka_header = fromHex('1701000003030040')  # EAP-AKA Challenge Response header
        eap_aka_header = fromHex('1701000003')  # EAP-AKA Challenge Response header
        eap_res_bit_len = struct.pack('!H', res_len * 8)
        at_mac_header = fromHex('0b050000')  # AT_MAC attribute header (16 bytes follow)
        
        # Calculate payload length before MAC
        # Structure: Code(1) + ID(1) + Length(2) + EAP-AKA Header(8) + RES(var) + AT_MAC(20)
        base_length = 1 + 1 + 2 + len(eap_aka_header) + 3 + res_len + len(at_mac_header) + 16
        
        # EAP-AKA payloads must be multiples of 4 bytes - add padding if needed
        padding_needed = (4 - (base_length % 4)) % 4
        eap_res_4bytes_len = struct.pack('!B', (res_len + padding_needed) // 4 + 1)
        padding = bytes(padding_needed)
        
        # Final length including padding
        total_length = base_length + padding_needed
        
        # Build length field (2 bytes, big endian)
        length_bytes = struct.pack('!H', total_length)
        
        # Construct payload without MAC (MAC placeholder is 16 zero bytes)
        eap_payload_response = (eap_code + 
                              eap_id + 
                              length_bytes + 
                              eap_aka_header + 
                              eap_res_4bytes_len + 
                              eap_res_bit_len + 
                              res + 
                              padding +  # Add padding after RES if needed
                              at_mac_header + 
                              bytes(16))  # MAC placeholder
        
        return eap_payload_response
        
#######################################################################################################################
#######################################################################################################################
################                            D E C O D E     F U N C T I O N S                          ################
#######################################################################################################################
#######################################################################################################################

        
    def decode_header(self, data):
        try:
        #if True:        
            self.ike_decoded_header['initiator_spi'] = data[0:8]
            self.ike_decoded_header['responder_spi'] = data[8:16]
            self.ike_decoded_header['next_payload'] = data[16]
            self.ike_decoded_header['major_version'] = data[17] // 16
            self.ike_decoded_header['minor_version'] = data[17] % 16        
            self.ike_decoded_header['exchange_type'] = data[18]
            self.ike_decoded_header['flags'] = self.return_flags(data[19])        
            self.ike_decoded_header['message_id'] = struct.unpack("!I",data[20:24])[0]
            self.ike_decoded_header['length'] =  struct.unpack("!I", data[24:28])[0]  #header + payloads
            
            
            if self.ike_spi_responder == (0).to_bytes(8,'big') and self.ike_spi_initiator == self.ike_decoded_header['initiator_spi'] :
                self.ike_spi_responder = self.ike_decoded_header['responder_spi']
                self.ike_decoded_header_ok = True
                self.old_ike_message_received = False
                return
                
            if self.ike_spi_initiator == self.ike_decoded_header['initiator_spi'] and self.ike_spi_responder == self.ike_decoded_header['responder_spi']:
                self.ike_decoded_header_ok = True
                self.old_ike_message_received = False
                return

            if self.ike_spi_initiator_old == self.ike_decoded_header['initiator_spi'] and self.ike_spi_responder_old == self.ike_decoded_header['responder_spi']:
                self.ike_decoded_header_ok = True
                self.old_ike_message_received = True
                return
                
            self.ike_decoded_header_ok = False
            return
        except:
            self.ike_decoded_header_ok = False


    def decode_generic_payload_header(self,data, position, payload_type):
        ike_decoded_payload_header = {}
        ike_decoded_payload_header['next_payload'] = data[position]
        ike_decoded_payload_header['C'] = data[position+1] // 128
        ike_decoded_payload_header['length'] =  struct.unpack("!H", data[position+2:position+4])[0]
        ike_decoded_payload_header['data'] = data[position+4:position+ike_decoded_payload_header['length']]      
        
        #to be used for SK decryption
        self.current_next_payload = ike_decoded_payload_header['next_payload']
        
        if payload_type in self.decodable_payloads:
            ike_decoded_payload_header['decoded'] = [payload_type, self.decode_payload_type(payload_type, ike_decoded_payload_header['data'])]
        else:
            ike_decoded_payload_header['decoded'] = [payload_type, None]
        
        position += ike_decoded_payload_header['length']
        return position, ike_decoded_payload_header['decoded'], ike_decoded_payload_header['next_payload'] 

    
    def decode_payload(self, data, next_payload, position=28): #by default it uses position 28 for normal
        
        decoded_payload = []
        while position < len(data):
            
            position, payload_decoded, next_payload = self.decode_generic_payload_header(data, position, next_payload)
            decoded_payload.append(payload_decoded)
        
        return (True, decoded_payload)
         
    def decode_ike(self, data):
        self.current_packet_received = data
        
        try:        
        #if True:
            self.decode_header(data)
            if self.ike_decoded_header_ok == False:
                self.ike_decoded_ok = False
            elif self.ike_decoded_header['next_payload'] == SKF:
                # G1 (RFC 7383): the message is fragmented — buffer/reassemble instead of the normal
                # single-message decode. _handle_skf sets ike_decoded_ok True only once every
                # fragment has arrived (the recv loops keep reading until then).
                self._handle_skf(data)
                if self.ike_decoded_ok == True:
                    print('received decoded message (reassembled from IKEv2 fragments)')
            else:

                (self.decoded_payload_ok, self.decoded_payload) = self.decode_payload(data, self.ike_decoded_header['next_payload'])
                if self.decoded_payload_ok == False:
                    self.ike_decoded_ok = False
                else:
                    self.ike_decoded_ok = True
                    print('received decoded message')
        except:
            self.ike_decoded_ok = False


    def decode_payload_type(self, type, data):
        payload_type = {
            SA:      self.decode_payload_type_sa  ,
            KE:      self.decode_payload_type_ke  ,
            IDI:     self.decode_payload_type_idi  ,
            IDR:     self.decode_payload_type_idr  ,
            CERT:    self.decode_payload_type_cert  ,
            CERTREQ: self.decode_payload_type_certreq  ,
            AUTH:    self.decode_payload_type_auth  ,
            NINR:    self.decode_payload_type_ninr  ,
            N:       self.decode_payload_type_n  ,
            D:       self.decode_payload_type_d  ,
            V:       self.decode_payload_type_v  ,
            TSI:     self.decode_payload_type_tsi_tsr  ,
            TSR:     self.decode_payload_type_tsi_tsr  ,
            SK:      self.decode_payload_type_sk  ,
            CP:      self.decode_payload_type_cp  ,
            EAP:     self.decode_payload_type_eap  
        }
        func = payload_type.get(type, self.unsupported_payload_type)     
        return func(data)


    def decode_payload_type_sa(self, data):
        spi = b''
        if data[5]!= 0:
            spi = data[8:8+data[6]]            
        return [data[4],data[5],spi] # proposal number, protocol_id, spi
        
    def decode_payload_type_ke(self, data):
        return [struct.unpack("!H", data[0:2])[0], data[4:]] # diffie-hellman group, key
        
        
    def decode_payload_type_idi(self, data):
        return [data[0],data[4:]]
        
    def decode_payload_type_idr(self, data):
        return [data[0],data[4:]]
        
    def decode_payload_type_cert(self, data):
        return [data[0],data[1:]]
        
    def decode_payload_type_certreq(self, data):
        return [data[0],data[1:]]
        
    def decode_payload_type_auth(self, data):
        return [data[0],data[4:]]
    
    def decode_payload_type_ninr(self, data):
        return [data]  # nounce_received
        
    def decode_payload_type_n(self, data):
        spi = b''
        notification_data = b''
        if data[1]!= 0: #spi present
            spi = data[4:4+data[1]]
        if len(data)>4 +data[1]: #notification data present
            notification_data = data[4+data[1]:]
        # Debug: log every received IKEv2 Notify (type name + code, protocol, and data hex).
        # Helps diagnose ePDG behaviour (errors, BACKOFF_TIMER, DEVICE_IDENTITY, P-CSCF-reselect,
        # …) and is groundwork for fuller 3GPP TS 24.302 handling. Goes to the IKE (charon) log.
        # ANY received Notify is recorded; those without an explicit swu_ike handler get an extra
        # verbose line (full hex, no truncation) so an operator can reconstruct the ePDG's intent
        # when chasing an unexpected tunnel drop.
        try:
            ntype = struct.unpack("!H", data[2:4])[0]
            proto = data[0]
            klass = "ERROR" if ntype < 16384 else "STATUS"
            extra = ""
            if notification_data:
                hexd = notification_data.hex()
                extra = " data=" + (hexd if len(hexd) <= 64 else hexd[:64] + "...")
            if spi:
                extra += " spi=" + spi.hex()
            desc = notify_describe(ntype)
            if desc:
                extra += "  # " + desc
            swu_log("received Notify: %s (%d, %s) protocol=%d%s" %
                    (notify_name(ntype), ntype, klass, proto, extra))
            # Unhandled STATUS notify (all errors are uniformly handled by the auth flow): emit a
            # louder, fully-detailed diagnostic so nothing the ePDG sends is silently ignored.
            if not notify_is_handled(ntype):
                known = notify_name(ntype) != "UNKNOWN"
                full_hex = notification_data.hex() if notification_data else "(none)"
                proto_name = {0: "IKE/none", IKE: "IKE", AH: "AH",
                              ESP: "ESP"}.get(proto, str(proto))
                swu_log("  UNHANDLED Notify %s(%d) proto=%s spi=%s data=%s%s "
                        "-- recorded, no action taken (retain for drop diagnosis)" % (
                            notify_name(ntype) if known else "type",
                            ntype, proto_name,
                            spi.hex() if spi else "(none)", full_hex,
                            (" | " + desc) if desc else
                            ("" if known else " | NOT in 3GPP TS 24.302 v18.5.0 tables — "
                             "vendor-private or newer release")))
        except Exception:
            pass
        return [data[0],struct.unpack("!H", data[2:4])[0],spi,notification_data] # protocol_id, notify_message_type, spi, notification_data
        
    def decode_payload_type_d(self, data):
        spi = b''
        spi_list = []
        num_of_spi = 0
        if data[1]!= 0: #spi present
            num_of_spi = struct.unpack("!H", data[2:4])[0]            
            for i in range(num_of_spi):
                spi_list.append(data[4+i*data[1]:4+(i+1)*data[1]])
            
        return [data[0],num_of_spi, spi_list] # [protocol_id, number of spi, [spi1, spi2, ... spi n]]
            
    def decode_payload_type_v(self, data):
        return [data]
        
    def decode_payload_type_tsi_tsr(self, data):
        num_of_ts = data[0]
        ts_list = []
        position = 4
        for i in range(num_of_ts):
            ts_type = data[position]
            protocol_id = data[position+1]
            start_port, end_port = struct.unpack("!H", data[position+4:position+6])[0], struct.unpack("!H", data[position+6:position+8])[0]
            if ts_type == TS_IPV4_ADDR_RANGE:
                starting_address = socket.inet_ntop(socket.AF_INET,data[position+8:position+12])
                ending_address = socket.inet_ntop(socket.AF_INET,data[position+12:position+16])
                position += 16                  
            elif ts_type == TS_IPV6_ADDR_RANGE:
                starting_address = socket.inet_ntop(socket.AF_INET6,data[position+8:position+24])
                ending_address = socket.inet_ntop(socket.AF_INET6,data[position+24:position+40])                
                position += 40
        
            ts_list.append((ts_type,protocol_id,start_port,end_port,starting_address,ending_address))     
        return [num_of_ts,ts_list]
       
       
       
       
    ######### CIPHERED PAYLOAD ######  
    ######### CIPHERED PAYLOAD ######  
    ######### CIPHERED PAYLOAD ######      
    def _decrypt_sk_body(self, data):
        """Decrypt + strip padding from the encrypted body of an SK (or an SKF fragment) payload,
        returning the plaintext IKE bytes. Factored out so RFC 7383 fragment reassembly reuses the
        exact same key selection + AES-CBC path as the normal SK decode. `data` begins with the IV
        (AES-CBC) or the ciphertext (NULL) and ends with the trailing ICV. Matches the existing
        behaviour: the ICV is stripped but NOT hard-verified (do not tighten this — it must remain
        byte-for-byte compatible with the working SK path)."""
        if self.negotiated_encryption_algorithm in (ENCR_AES_CBC,):
            vector = data[0:16]
            hash_size = self.integ_key_truncated_len_bytes.get(self.negotiated_integrity_algorithm)
            encrypted_data = data[16:len(data)-hash_size]
            if self.ike_decoded_header['flags'][2] == ROLE_RESPONDER:
                key = self.SK_ER_old if self.old_ike_message_received == True else self.SK_ER
            else:
                key = self.SK_EI_old if self.old_ike_message_received == True else self.SK_EI
            cipher = Cipher(algorithms.AES(key), modes.CBC(vector))
            decryptor = cipher.decryptor()
            uncipher_data = decryptor.update(encrypted_data) + decryptor.finalize()
            padding_length = uncipher_data[-1]
            return uncipher_data[0:-padding_length-1]
        elif self.negotiated_encryption_algorithm in (ENCR_NULL,):
            hash_size = self.integ_key_truncated_len_bytes.get(self.negotiated_integrity_algorithm)
            return data[0:len(data)-hash_size-self.sk_ENCR_NULL_pad_length]
        return None

    def decode_payload_type_sk(self, data):
        ike_payload = self._decrypt_sk_body(data)
        if ike_payload is None:
            return
        (result_ok, decoded_payload) = self.decode_payload(ike_payload, self.current_next_payload,0)
        if result_ok == True:
            return decoded_payload

    def _handle_skf(self, data):
        """G1 (RFC 7383): reassemble an inbound fragmented IKE message. An SKF payload replaces SK
        when the ePDG fragments a large message (e.g. an IKE_AUTH carrying a cert chain). Each SKF
        is individually encrypted+integrity-protected and prefixed (after the 4-byte generic payload
        header) with Fragment Number(2) + Total Fragments(2) [RFC 7383 §2.5]; only fragment #1
        carries the real next_payload. We decrypt each fragment, buffer plaintext by (message-id,
        fragment number), and once every fragment is present concatenate + decode the original
        message — presenting it to the state machine exactly as a normal SK message would be
        (decoded_payload = [[SK, <inner payloads>]]). Until complete, ike_decoded_ok stays False so
        the caller's recv loop keeps reading."""
        # SKF generic payload header at offset 28: next_payload(1) C+res(1) length(2)
        frag_next_payload = data[28]
        payload_len = struct.unpack("!H", data[30:32])[0]
        # fragment header immediately after the 4-byte generic payload header
        frag_num = struct.unpack("!H", data[32:34])[0]
        total = struct.unpack("!H", data[34:36])[0]
        body = data[36:28 + payload_len]           # IV + ciphertext + ICV for THIS fragment
        plaintext = self._decrypt_sk_body(body)
        if plaintext is None:
            plaintext = b''
        mid = self.ike_decoded_header['message_id']
        buf = self._frag_buf.setdefault(mid, {"total": total, "frags": {}, "first_np": None})
        buf["total"] = total
        buf["frags"][frag_num] = plaintext
        if frag_num == 1:
            buf["first_np"] = frag_next_payload
        swu_log("IKEv2 fragment %d/%d received (message_id=%d, %d plaintext bytes)" %
                (frag_num, total, mid, len(plaintext)))
        if len(buf["frags"]) >= total and all(k in buf["frags"] for k in range(1, total + 1)):
            reassembled = b''.join(buf["frags"][k] for k in range(1, total + 1))
            first_np = buf["first_np"] if buf["first_np"] is not None else 0
            (ok, decoded) = self.decode_payload(reassembled, first_np, 0)
            del self._frag_buf[mid]
            if ok:
                # Present it like a normal SK message so downstream state code (which checks
                # decoded_payload[0][0] == SK and iterates decoded_payload[0][1]) is unchanged.
                self.decoded_payload = [[SK, decoded]]
                self.decoded_payload_ok = True
                self.ike_decoded_ok = True
                swu_log("IKEv2 fragments reassembled (message_id=%d, %d fragments, %d plaintext bytes)" %
                        (mid, total, len(reassembled)))
            else:
                self.ike_decoded_ok = False
        else:
            self.ike_decoded_ok = False


    def decode_payload_type_cp(self, data):
        cfg_type = data[0]
        attribute_list = []
        position = 4
        while position + 4 <= len(data):
            # P0-1: mask the reserved high (R) bit of the attribute type (RFC 7296 3.15.1 — the
            # top bit is reserved and MUST be ignored on receipt; some ePDGs set it).
            attribute_type_raw = struct.unpack("!H", data[position:position+2])[0]
            attribute_type = attribute_type_raw & 0x7fff
            length = struct.unpack("!H", data[position+2:position+4])[0]
            # P0-1: never read past the buffer. A truncated/lying length must not throw an
            # uncaught exception (that would abort the whole IKE decode) — log and stop parsing
            # this CP, returning whatever was parsed so far.
            if position + 4 + length > len(data):
                swu_log("CP attribute type=%d claims length=%d but only %d bytes remain; "
                        "stopping CP parse" % (attribute_type, length, len(data) - position - 4))
                break
            attribute_value = b''
            if length > 0:
                att_len = self.configuration_payload_len_bytes.get(attribute_type)
                if attribute_type == TIMEOUT_PERIOD_FOR_LIVENESS_CHECK:
                    # P2-3: this attribute's value is a 4-byte network-order integer (seconds), NOT
                    # an IPv4 address, even though its length is 4. Keep the raw bytes so state_4's
                    # struct.unpack("!I", ...) reads the period correctly.
                    attribute_value = data[position+4:position+4+length]
                    attribute_list.append((attribute_type,attribute_value))
                elif att_len == 4: #ip
                    attribute_value = socket.inet_ntop(socket.AF_INET,data[position+4:position+8])
                    attribute_list.append((attribute_type,attribute_value))
                elif att_len == 8: #ip /netmask
                    attribute_value_1 = socket.inet_ntop(socket.AF_INET,data[position+4:position+8])
                    attribute_value_2 = socket.inet_ntop(socket.AF_INET,data[position+8:position+12])
                    attribute_list.append((attribute_type,attribute_value_1,attribute_value_2))
                elif att_len == 16: #ipv6
                    attribute_value = socket.inet_ntop(socket.AF_INET6,data[position+4:position+20])
                    attribute_list.append((attribute_type,attribute_value))
                elif att_len == 17: #ipv6 + prefix (16-byte prefix + 1-byte prefix length)
                    # P0-1 fix: the prefix length is the 17th value byte, i.e.
                    # data[position + 4 + 16] = data[position + 20]. The old code read
                    # data[position + 21], one byte past the attribute value.
                    attribute_value_1 = socket.inet_ntop(socket.AF_INET6,data[position+4:position+20])
                    attribute_value_2 = data[position+20]
                    attribute_list.append((attribute_type,attribute_value_1, attribute_value_2))
                else:
                    attribute_value = data[position+4:position+4+length]
                    attribute_list.append((attribute_type,attribute_value))
            else:
                attribute_list.append((attribute_type,attribute_value))
            position += length + 4
        return [cfg_type,attribute_list]
        
    def decode_payload_type_eap(self, data):
        code = data[0] #1- request, 2-response, 3-success, 4-failure
        identifier = data[1]
        if code in (EAP_SUCCESS,EAP_FAILURE): 
            return [code,identifier]
        elif code in (EAP_REQUEST,EAP_RESPONSE):
            if data[4] == EAP_AKA:
                return [code,identifier,data[4],data[5],self.decode_eap_attributes(data[8:])] #code, identifier, type, sub type, [attributes list]
            else:
                return [code,identifier,data[4],data[5:]]
        else:
            return []

    def unsupported_payload_type(self, data):
        return None
        

    def decode_eap_attributes(self, data):
        eap_aka_decoded = []
        position = 0
        while position < len(data):
            attribute = data[position]
            if attribute in (AT_PERMANENT_ID_REQ,AT_ANY_ID_REQ,AT_FULLAUTH_ID_REQ,AT_RESULT_IND,AT_COUNTER,AT_COUNTER_TOO_SMALL,AT_CLIENT_ERROR_CODE,AT_NOTIFICATION):
                eap_aka_decoded.append((attribute,struct.unpack("!H", data[position+2:position+4])[0]))
            elif attribute in (AT_IDENTITY,AT_RES,AT_NEXT_PSEUDONYM,AT_NEXT_REAUTH_ID):
                eap_aka_decoded.append((attribute,data[position+4:position+4+struct.unpack("!H", data[position+2:position+4])[0]]))
            elif attribute in (AT_RAND,AT_AUTN,AT_IV,AT_MAC,AT_NONCE_S):                
                eap_aka_decoded.append((attribute,data[position+4:position+20]))
            elif attribute in (AT_AUTS,):                
                eap_aka_decoded.append((attribute,data[position+2:position+16]))
            elif attribute in (AT_CHECKCODE,):               
                if data[position+1] == 0:            
                    eap_aka_decoded.append((attribute,struct.unpack("!H", data[position+2:position+4])[0]))
                else:
                    eap_aka_decoded.append((attribute,data[position+4:position+24]))
            
            elif attribute in (AT_ENCR_DATA,):
                eap_aka_decoded.append((attribute,data[position+4:position+4*data[position+1]]))

            elif attribute in (AT_PADDING,):
                eap_aka_decoded.append((attribute,data[position+2:position+4*data[position+1]]))
            
            position += data[position+1]*4
        return eap_aka_decoded
        
#######################################################################################################################
#######################################################################################################################
################                           E N C O D E     F U N C T I O N S                           ################
#######################################################################################################################
#######################################################################################################################
        
    def set_sa_list(self,sa_list):
        self.sa_list = sa_list    

    def set_sa_list_child(self,sa_list):
        self.sa_list_child = sa_list   

    def set_ts_list(self,type, ts_list):
        if type == TSI: self.ts_list_initiator = ts_list
        if type == TSR: self.ts_list_responder = ts_list    

    def set_cp_list(self, cp_list):
         self.cp_list = cp_list

    def set_device_identity(self, imei, imeisv=""):
        """Set the IMEI / IMEISV used to answer the ePDG's DEVICE_IDENTITY request.

        IMEI: digits only (a formatted '35212721-360029-6' is accepted and stripped).
        IMEISV: 16 digits; if blank, derive it from the IMEI's first 14 digits (TAC+SNR, i.e.
        without the check digit) + a '00' SVN. Empty IMEI keeps the module defaults so a bare
        run still works."""
        imei_d = "".join(ch for ch in str(imei or "") if ch.isdigit())
        isv_d = "".join(ch for ch in str(imeisv or "") if ch.isdigit())
        if imei_d:
            self.imei = imei_d
        if isv_d:
            self.imeisv = (isv_d + "0" * 16)[:16]
        elif imei_d:
            self.imeisv = (imei_d[:14].ljust(14, "0")) + "00"
        swu_log("device identity configured")

    def set_identification(self,payload_type, id_type,value):
        if payload_type == IDI: self.identification_initiator = (id_type, value)
        if payload_type == IDR: self.identification_responder = (id_type, value)        
        
    def set_ike_packet_length(self,packet):
        packet = bytearray(packet)
        packet[24:28] = struct.pack("!I",len(packet))
        return packet

    def encode_header(self,initiator_spi, responder_spi, next_payload, major_version, minor_version, exchange_type, flags, message_id, length = 0):
        header = b''
        header += initiator_spi
        header += responder_spi
        header += bytes([next_payload])
        header += bytes([major_version*16+minor_version])
        header += bytes([exchange_type])
        header += bytes([self.return_flags(flags)])
        header += struct.pack("!I",message_id)  
        header += struct.pack("!I",length)  
        return header
        

    def encode_generic_payload_header(self,next_payload,c,data):
        payload = b''
        payload += bytes([next_payload])
        payload += bytes([c*128])
        payload += struct.pack("!H",len(data)+4)  
        payload += data
        return payload
        
        
    def encode_payload_type_sa(self, sa_list):
        payload_sa = b''
        proposal_list = []
        self.sa_spi_list = []
        m = 0
        
        proposal = 1
        for i in sa_list:
            transform_list = []
            
            protocol_id = i[0][0]
            spi_size = i[0][1]
            spi_bytes = self.return_random_bytes(spi_size)
            self.sa_spi_list.append(spi_bytes)

            for m in range(1,len(i)): #transform_list
                
                transform_type = i[m][0]
                transform_id = i[m][1]
                if len(i[m])==3: #attributes 
                    attribute_type = i[m][2][0][0]
                    attribute_format = i[m][2][0][1]
                    attribute_value = i[m][2][1]
                    if attribute_format == 0: #TLV: Value in bytes format
                        attribute_bytes = struct.pack("!H",attribute_type) 
                        attribute_bytes += struct.pack("!H",len(attribute_value)) 
                        attribute_bytes += attribute_value
                    else: # TV
                        attribute_bytes = struct.pack("!H",32768+attribute_type) 
                        attribute_bytes += struct.pack("!H",attribute_value)
                else:
                    attribute_bytes = b''                
                
                
                if proposal == 1 and transform_type == D_H and protocol_id == IKE:
                    self.dh_create_private_key_and_public_bytes(self.iana_diffie_hellman.get(transform_id))   
                    self.dh_group_num = transform_id       
     
                
                last = 3
                if m == len(i)-1: last = 0 # last transform
                    
                transform_bytes = bytes([last]) + b'\x00\x00\x00' + bytes([transform_type]) + b'\x00' + struct.pack("!H",transform_id) + attribute_bytes
                transform_bytes = bytearray(transform_bytes)
                transform_bytes[2:4] = struct.pack("!H",len(transform_bytes))

                transform_list.append(transform_bytes)
                           
            last = 2
            if proposal == len(sa_list): last = 0 #last proposal
            
            proposal_bytes = bytes([last]) + b'\x00\x00\x00' + bytes([proposal]) + bytes([protocol_id]) + bytes([spi_size]) + bytes([m]) + spi_bytes + b''.join(transform_list)            
                
            proposal_bytes = bytearray(proposal_bytes)
            proposal_bytes[2:4] = struct.pack("!H",len(proposal_bytes))
            
            proposal_list.append(proposal_bytes)


            proposal += 1

        return b''.join(proposal_list)




    def encode_payload_type_ke(self):        
        payload_ke = struct.pack("!H",self.dh_group_num) + b'\x00\x00' + self.dh_public_key_bytes
        return payload_ke


    def encode_payload_type_ninr(self, lowest = 0):
        if lowest == 0:    
            payload_ninr = self.return_random_bytes(16)
        elif lowest == -1:
            payload_ninr = b'\x00'*8 + self.return_random_bytes(8)
        elif lowest == 1:
            payload_ninr = b'\xff'*8 + self.return_random_bytes(8)
        self.nounce = payload_ninr
        return payload_ninr


    def encode_payload_type_tsi(self):
        return self.encode_payload_type_ts(TSI)

    def encode_payload_type_tsr(self):
        return self.encode_payload_type_ts(TSR)
        
    def encode_payload_type_ts(self,type):
        if type == TSI: ts_list = self.ts_list_initiator
        if type == TSR: ts_list = self.ts_list_responder
        
        payload_ts = bytes([len(ts_list)]) + b'\x00\x00\x00'
        
        for i in ts_list:
            ts_type = bytes([i[0]])
            ip_protocol = bytes([i[1]])
            start_port = struct.pack("!H",i[2])
            end_port = struct.pack("!H",i[3])
            if i[0] == TS_IPV4_ADDR_RANGE:
                length = struct.pack("!H",16)
                starting_address = socket.inet_pton(socket.AF_INET,i[4])
                ending_address = socket.inet_pton(socket.AF_INET,i[5])
            elif i[0] == TS_IPV6_ADDR_RANGE:
                length = struct.pack("!H",40)
                starting_address = socket.inet_pton(socket.AF_INET6,i[4])
                ending_address = socket.inet_pton(socket.AF_INET6,i[5])
            payload_ts += ts_type + ip_protocol + length + start_port + end_port + starting_address + ending_address
        
        return payload_ts
        
    def encode_payload_type_cp(self):
    
        payload_cp = bytes([self.cp_list[0]]) + b'\x00\x00\x00'
        for i in self.cp_list[1:]:
            if len(i) == 1: #no value
                payload_cp += struct.pack("!H",i[0]) + b'\x00\x00'
            else:
                length = self.configuration_payload_len_bytes.get(i[0])              
                if length == 4: #ip address
                    value = socket.inet_pton(socket.AF_INET,i[1])
                    payload_cp += struct.pack("!H",i[0]) + struct.pack("!H",4) + value
                elif length == 8: #ip address, netmask
                    value_1, value_2 = socket.inet_pton(socket.AF_INET,i[1]), socket.inet_pton(socket.AF_INET,i[2])
                    payload_cp += struct.pack("!H",i[0]) + struct.pack("!H",8) + value_1 + value_2
                elif length == 16: #ipv6 address
                    value = socket.inet_pton(socket.AF_INET6,i[1])
                    payload_cp += struct.pack("!H",i[0]) + struct.pack("!H",16) + value
                elif length == 17: #ipv6 address, mask length
                    value = socket.inet_pton(socket.AF_INET6,i[1])
                    payload_cp += struct.pack("!H",i[0]) + struct.pack("!H",17) + value + bytes([i[2]])
                else: # not stricted
                    payload_cp += struct.pack("!H",i[0]) + struct.pack("!H",len(i[1])) + i[1]

        return payload_cp
        
    def encode_payload_type_idi(self):
        return self.encode_payload_type_id(IDI)

    def encode_payload_type_idr(self):
        return self.encode_payload_type_id(IDR)
        
    def encode_payload_type_id(self,type): #id
        if type == IDI: (id_type,value) = self.identification_initiator
        if type == IDR: (id_type,value) = self.identification_responder
        if id_type in (ID_FQDN, ID_RFC822_ADDR): 
            value = value.encode('utf-8')
        elif id_type == ID_IPV4_ADDR:
            value = socket.inet_pton(socket.AF_INET,value)
        elif id_type == ID_IPV6_ADDR:
            value = socket.inet_pton(socket.AF_INET6,value)
        #else binary, so use value as is.    
        payload_id = bytes([id_type]) + b'\x00\x00\x00' + value

        return payload_id
 
 
 
    def encode_payload_type_eap(self): 
        return self.eap_payload_response

    def encode_payload_type_auth(self,auth_method): 
        return bytes([auth_method]) + b'\x00'*3 + self.AUTH_payload
 
    def encode_payload_type_d(self, protocol, spi_list = b''): 
        if protocol == IKE:
            return bytes([IKE]) + b'\x00\x00\x00'
        elif protocol == ESP:
            num_spi = len(spi_list) // 4
            return bytes([ESP]) + b'\x04' + struct.pack("!H",num_spi) + spi_list
            
 
    def encode_payload_type_n(self,protocol,spi,notify_message_type,notification_data= b''):
        spi_size = len(spi)
        return bytes([protocol]) + bytes([spi_size]) + struct.pack("!H",notify_message_type) + spi + notification_data


    def encode_device_identity_notification_data(self):
        """ Encodes the DEVICE_IDENTITY notification data.
        Format: [2 bytes length][1 byte identity type][BCD encoded IMEI/IMEISV]
        Identity type 0x01 = IMEI (15 digits, padded with F to 16)
        Identity type 0x02 = IMEISV (16 digits)
        Uses the per-instance self.imei / self.imeisv (set via set_device_identity).
        """
        if self.device_identity_type == 0x02:
            identity_type = 0x02
            digits = (self.imeisv or IMEISV)
        else:  # 0x01 or fallback
            identity_type = 0x01
            imei = (self.imei or IMEI)
            digits = imei[:15] + 'F'  # pad 15-digit IMEI to 16 chars with trailing F
        swu_log("answering DEVICE_IDENTITY (type=0x%02x) with %s" %
                (identity_type, digits.rstrip('F')))

        bcd = b''
        for i in range(0, len(digits), 2):
            low_nibble  = int(digits[i],   16)
            high_nibble = int(digits[i+1], 16)
            bcd += bytes([low_nibble | (high_nibble << 4)])

        data = bytes([identity_type]) + bcd
        return struct.pack("!H", len(data)) + data


    def encode_payload_type_sk(self,ike_packet):
        # P2-4 (RFC 7383): outbound fragmentation. Only when BOTH sides advertised
        # IKEV2_FRAGMENTATION_SUPPORTED AND the plaintext body exceeds the fragment threshold do
        # we split into SKF fragments (returns a list[bytes] the caller/send_data iterates). The
        # common single-SK path is byte-for-byte identical to the previous implementation, so
        # Telus (which never fragments — its messages fit under MTU) is completely unaffected.
        inner_next_payload = ike_packet[16]
        plaintext_body = ike_packet[28:]
        if (self.fragmentation_enabled and self.peer_supports_fragmentation
                and len(plaintext_body) > self.fragment_size):
            return self._encode_sk_fragments(ike_packet, inner_next_payload, plaintext_body)
        return self._encode_protected_payload(ike_packet, SK, inner_next_payload, plaintext_body, None)

    def _encode_protected_payload(self, ike_packet, outer_payload_type, inner_next_payload,
                                  plaintext_body, fragment_info=None):
        """Encrypt+integrity-protect one plaintext IKE payload body into a single SK (or SKF)
        payload and return the complete IKE packet bytes ready to send.

        outer_payload_type:  SK for a normal message, SKF for a fragment.
        inner_next_payload:  the payload-type of the first inner payload (fragment #1 / whole SK)
                             or NONE for fragments #2..N (RFC 7383 §2.5).
        fragment_info:       None for SK; the 4-byte 'FragNum(2)+Total(2)' header for SKF, placed
                             (in the clear) between the generic payload header and the IV, exactly
                             where _handle_skf expects it and covered by the integrity check.

        Factored out of the old encode_payload_type_sk so the fragment path reuses the identical
        key-selection / AES-CBC / HMAC logic. Called with (ike_packet, SK, ike_packet[16],
        ike_packet[28:], None) this reproduces the previous single-SK output exactly."""
        hash_size = self.integ_key_truncated_len_bytes.get(self.negotiated_integrity_algorithm)
        flags_role = self.return_flags(ike_packet[19])[2]
        frag_prefix = fragment_info if fragment_info else b''

        if self.negotiated_encryption_algorithm in (ENCR_AES_CBC,):
            vector = self.return_random_bytes(16)
            data_to_encrypt = bytes(plaintext_body)
            res = 16 - (len(data_to_encrypt) % 16)
            if res > 1:
                data_to_encrypt += b'\x00'*(res-1) + bytes([res-1])
            else:
                data_to_encrypt += b'\x00'*(15+res) + bytes([15+res])

            if flags_role == ROLE_INITIATOR:
                key = self.SK_EI_old if self.old_ike_message_received == True else self.SK_EI
            else:
                key = self.SK_ER_old if self.old_ike_message_received == True else self.SK_ER

            cipher = Cipher(algorithms.AES(key), modes.CBC(vector))
            encryptor = cipher.encryptor()
            cipher_data = encryptor.update(data_to_encrypt) + encryptor.finalize()

            sk_payload = self.encode_generic_payload_header(inner_next_payload, 0,
                                                            frag_prefix + vector + cipher_data + b'\x00'*hash_size)
            new_ike_packet = ike_packet[0:16] + bytes([outer_payload_type]) + ike_packet[17:28] + sk_payload
            new_ike_packet = self.set_ike_packet_length(new_ike_packet)
            new_ike_packet_to_integrity = new_ike_packet[0:-hash_size]
            hashf = self.integ_function.get(self.negotiated_integrity_algorithm)

            if flags_role == ROLE_INITIATOR:
                ikey = self.SK_AI_old if self.old_ike_message_received == True else self.SK_AI
            else:
                ikey = self.SK_AR_old if self.old_ike_message_received == True else self.SK_AR

            h = hmac.HMAC(ikey, hashf)
            h.update(new_ike_packet_to_integrity)
            digest = h.finalize()[0:hash_size]
            return new_ike_packet_to_integrity + digest

        elif self.negotiated_encryption_algorithm in (ENCR_NULL,):
            data_to_encrypt = bytes(plaintext_body)
            sk_payload = self.encode_generic_payload_header(inner_next_payload, 0,
                                                            frag_prefix + data_to_encrypt + b'\x00'*(hash_size + self.sk_ENCR_NULL_pad_length))
            new_ike_packet = ike_packet[0:16] + bytes([outer_payload_type]) + ike_packet[17:28] + sk_payload
            new_ike_packet = self.set_ike_packet_length(new_ike_packet)
            new_ike_packet_to_integrity = new_ike_packet[0:-hash_size]
            hashf = self.integ_function.get(self.negotiated_integrity_algorithm)

            if flags_role == ROLE_INITIATOR:
                ikey = self.SK_AI_old if self.old_ike_message_received == True else self.SK_AI
            else:
                ikey = self.SK_AR_old if self.old_ike_message_received == True else self.SK_AR

            h = hmac.HMAC(ikey, hashf)
            h.update(new_ike_packet_to_integrity)
            digest = h.finalize()[0:hash_size]
            return new_ike_packet_to_integrity + digest

    def _encode_sk_fragments(self, ike_packet, inner_next_payload, plaintext_body):
        """P2-4 (RFC 7383 §2.5): split plaintext_body into fragments of at most fragment_size and
        encode each as its own SKF-protected IKE packet. Each fragment is independently encrypted
        (own IV / padding / ICV) — you cannot encrypt once and slice ciphertext. Fragment #1 carries
        the original first-payload type as its inner next_payload; #2..N use NONE. Returns
        list[bytes]. _handle_skf reassembles the mirror image."""
        chunk = self.fragment_size if self.fragment_size > 0 else len(plaintext_body)
        pieces = [plaintext_body[i:i+chunk] for i in range(0, len(plaintext_body), chunk)] or [b'']
        total = len(pieces)
        packets = []
        for idx, piece in enumerate(pieces, start=1):
            frag_hdr = struct.pack("!HH", idx, total)
            inner_np = inner_next_payload if idx == 1 else NONE
            packets.append(self._encode_protected_payload(ike_packet, SKF, inner_np, bytes(piece), frag_hdr))
        swu_log("IKEv2 outbound fragmentation: %d plaintext bytes -> %d SKF fragments (chunk=%d)" %
                (len(plaintext_body), total, chunk))
        return packets
        
        
        
        
        
        

#######################################################################################################################
#######################################################################################################################
############                    S T A T E    &    M E S S A G E S     F U N C T I O N S                    ############
#######################################################################################################################
#######################################################################################################################

### USER PLANE FUNCTIONS AND INTER PROCESS COMMUNICATION ####

    def exec_in_netns(self, cmd, shell=True):
        if self.netns_name:
            cmd = "ip netns exec %s %s" % (self.netns_name, cmd)
        print("cmd: %s" % cmd)
        subprocess.call(cmd, shell=shell)


    def _install_lan_bypass_policy(self):
        """Keep the container's own (non-tunnel) traffic on the LAN link when the IPv4 IMS PDN makes
        the tunnel the default route for ALL IPv4 (the 0.0.0.0/1 + 128.0.0.0/1 routes below).

        Without this, every reply the container sources from its docker-bridge address (SWU_SOURCE,
        e.g. 172.17.0.3) — DNS lookups AND, crucially, the SYN-ACK/return traffic of any published
        port (the WebRTC WSS softphone on 8089, the manager AMI) — matches a /1 route
        and is sent into the ePDG, which drops it. Symptom: a LAN client's TCP to the mapped WSS port
        never completes its handshake (SYN in on eth0, SYN-ACK out on ipsec0, lost), so the softphone
        can't connect; and container DNS times out (40s).

        Fix with SOURCE-based policy routing (pure iproute2 — no iptables/nft, which aren't in the
        image): a dedicated table whose default goes via the docker gateway, selected by
        `ip rule from <container-eth0-ip>`. IMS traffic is sourced from the tunnel INNER address, not
        the eth0 address, so it is unaffected and still uses the main table's /1 tunnel routes. This
        is robust even in the pathological case where a LAN client's IP equals the P-CSCF's IP,
        because the discriminator is the packet's SOURCE (eth0 addr vs inner addr), never the
        destination. IPv6 IMS PDNs (Telus/EE) only route ::/1 so IPv4 is untouched and this is a
        no-op there.

        Idempotent (del-before-add). Best-effort: any failure is logged and ignored so it can never
        block tunnel bring-up."""
        if self.netns_name:
            return
        src = self.source_address
        gw = self.default_gateway or (self.get_default_gateway_linux() or [None])[0]
        if not src or not gw:
            swu_log("LAN-bypass policy skipped (no source addr / gateway)")
            return
        table = os.environ.get("SWU_LAN_BYPASS_TABLE", "51820")
        pref = os.environ.get("SWU_LAN_BYPASS_PREF", "100")
        # Rebuild cleanly in case a previous attach left stale state.
        self.exec_in_netns("ip rule del from %s lookup %s pref %s" % (src, table, pref) + " 2>/dev/null")
        self.exec_in_netns("ip route flush table %s" % table + " 2>/dev/null")
        self.exec_in_netns("ip route add default via %s dev %s table %s" % (gw, self._lan_egress_iface(), table))
        self.exec_in_netns("ip rule add from %s lookup %s pref %s" % (src, table, pref))
        swu_log("LAN-bypass policy: replies sourced from %s -> table %s (default via %s), tunnel "
                "unaffected (IMS sources from the inner address)" % (src, table, gw))
        # The source rule fixes REPLY traffic (whose source is already the eth0 addr via DNAT
        # conntrack). It does NOT fix container-ORIGINATED outbound flows (DNS lookups, the manager
        # callback): there the kernel does the route lookup BEFORE choosing a source, the tunnel /1
        # route wins, and it picks the inner address as source -> the `from <eth0>` rule never
        # matches and the flow still enters the tunnel (DNS -> 40s timeouts, which also delays the
        # IMS SMS RP-ACK past its correlation window). Keep those destinations off the tunnel by
        # DESTINATION: pin each resolver nameserver (and they're LAN/RFC1918 anyway) to the LAN gw.
        for _ns in self._resolver_nameservers_v4():
            self.exec_in_netns("route add " + _ns + "/32 gw " + gw + " 2>/dev/null")

    def _lan_egress_iface(self):
        """The container's outbound LAN interface (the one carrying SWU_SOURCE). Almost always eth0
        in the docker bridge; derived from the source address's owning link so we never hard-code.
        Parsed in pure Python + `ip` (no awk — the engine image ships iproute but not awk)."""
        try:
            out = subprocess.check_output("ip -o -4 addr show", shell=True).decode()
            token = self.source_address + "/"
            for line in out.splitlines():
                # "2: eth0    inet 172.17.0.3/16 brd ... scope global eth0"
                fields = line.split()
                if len(fields) >= 4 and fields[2] == "inet" and fields[3].startswith(token):
                    return fields[1]
        except Exception:
            pass
        return "eth0"

    def _resolver_nameservers_v4(self):
        """IPv4 nameservers from /etc/resolv.conf. Used by the LAN-bypass policy to keep DNS on the
        LAN link (by destination) instead of the IPv4 IMS tunnel's default route. Best-effort:
        returns [] on any error so a missing/odd resolv.conf never blocks tunnel bring-up."""
        ns = []
        try:
            with open("/etc/resolv.conf") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0] == "nameserver" and "." in parts[1] and ":" not in parts[1]:
                        if parts[1] not in ns:
                            ns.append(parts[1])
        except Exception:
            pass
        return ns

    # --- Data-plane MTU sizing + outbound fragmentation --------------------------------------
    def _iface_mtu(self, iface):
        """Current MTU of a link, read straight from sysfs (no iproute parsing). 0 on any error."""
        try:
            with open("/sys/class/net/%s/mtu" % iface) as f:
                return int(f.read().strip())
        except Exception:
            return 0

    def _esp_overhead(self):
        """Bytes added to an inner IP packet to turn it into the OUTER IPv4 datagram that leaves
        eth0: outer IPv4 header + (UDP for NAT-T) + ESP header + IV + worst-case pad/trailer +
        ICV/tag. Derived from the negotiated CHILD-SA cipher/integrity (known before the dataplane
        workers fork) so the tun MTU is sized to guarantee ANY inner packet <= (outer_mtu -
        overhead) encapsulates into a single outer datagram <= outer_mtu. The outer family is
        always IPv4 here (all ePDG sockets are AF_INET)."""
        outer_ip = 20
        udp = 8 if self.userplane_mode == NAT_TRAVERSAL else 0
        esp_hdr = 8                                            # SPI(4) + sequence(4)
        encr = self.negotiated_encryption_algorithm_child
        integ = self.negotiated_integrity_algorithm_child
        if encr in (ENCR_AES_GCM_8, ENCR_AES_GCM_12, ENCR_AES_GCM_16):
            iv = 8
            block = 4                                         # GCM path pads (len+2) to a /4
            icv = {ENCR_AES_GCM_8: 8, ENCR_AES_GCM_12: 12, ENCR_AES_GCM_16: 16}[encr]
        elif encr == ENCR_NULL:
            iv = 0
            block = 1
            icv = self.integ_key_truncated_len_bytes.get(integ, 0)
        else:                                                 # AES-CBC (and any other CBC cipher)
            iv = 16
            block = 16
            icv = self.integ_key_truncated_len_bytes.get(integ, 0)
        # ESP encrypts (payload + padlen + nexthdr) padded up to the cipher block: worst case the
        # padding is block-1 bytes on top of the mandatory 2-byte trailer.
        trailer = 2 + (block - 1 if block > 1 else 0)
        return outer_ip + udp + esp_hdr + iv + trailer + icv

    def _sense_outer_mtu(self):
        """Sense the REAL outer path MTU toward the ePDG, seeing PAST the container's own NIC.
        `ip route get <epdg>` reports the egress device AND, once PMTU discovery has learned it,
        the route-cached path MTU — so a WireGuard/constrained hop running on the HOST outside the
        container (common when bypassing carrier IP filtering) is picked up as it is discovered.
        Returns the min of {SWU_OUTER_MTU override, route-cached PMTU, egress-NIC MTU}, or 1500
        when nothing can be determined. Best-effort and cheap enough to re-run periodically."""
        if SWU_OUTER_MTU_ENV:
            return SWU_OUTER_MTU_ENV
        candidates = []
        try:
            out = subprocess.check_output("ip route get %s" % self.epdg_address,
                                          shell=True, stderr=subprocess.DEVNULL).decode()
            toks = out.split()
            dev = None
            for i, t in enumerate(toks):
                if t == "dev" and i + 1 < len(toks):
                    dev = toks[i + 1]
                elif t == "mtu" and i + 1 < len(toks):
                    try:
                        candidates.append(int(toks[i + 1]))     # route-cached PMTU (from PMTUD)
                    except ValueError:
                        pass
            if dev:
                dm = self._iface_mtu(dev)                       # egress NIC's own MTU
                if dm:
                    candidates.append(dm)
        except Exception:
            pass
        return min(candidates) if candidates else 1500

    def _inner_mtu_from(self, outer_mtu):
        """Largest inner IP packet whose ESP encapsulation fits one outer datagram of outer_mtu."""
        m = outer_mtu - self._esp_overhead() - SWU_MTU_MARGIN
        return m if m >= 68 else 68

    def _compute_and_apply_tun_mtu(self):
        """Fix the ipsec0 (inner) MTU so the IMS/pjsip layer sees a stable value and is unaware of
        any path constraint, and record self.inner_mtu — the fragmentation THRESHOLD derived from
        the sensed outer path MTU (NOT the device MTU). The encapsulation worker refreshes the
        threshold periodically (PMTUD may lower it after traffic starts).

        - ipsec0 MTU = SWU_TUN_MTU (default 1400): 1400 + ESP overhead (~81) = ~1481, still inside
          the common 1500/1480 uplinks, so a normal deployment fragments NOTHING while giving the
          IMS layer a fixed MTU.
        - inner_mtu (fragmentation threshold) = sensed_outer_mtu - ESP overhead. Only inner packets
          larger than this are split by swu (transparently) so every ESP datagram is complete.
        Best-effort; on error ipsec0 keeps its default and inner_mtu is left unset (no fragmenting)."""
        try:
            outer_mtu = self._sense_outer_mtu()
            overhead = self._esp_overhead()
            inner_mtu = self._inner_mtu_from(outer_mtu)
            tun_mtu = SWU_TUN_MTU_ENV or 1400
            self.outer_mtu = outer_mtu
            self.inner_mtu = inner_mtu
            self.tun_mtu = tun_mtu
            # read buffer must never truncate a full inner packet (up to the fixed tun MTU)
            self._tun_read_size = max(2048, tun_mtu + 128)

            self.exec_in_netns("ip link set dev %s mtu %d" % (self.tun_device, tun_mtu))
            swu_log("dataplane MTU: ipsec0(fixed)=%d esp_overhead=%d sensed_outer(%s)=%d "
                    "frag_threshold(inner_mtu)=%d inner_frag=%s"
                    % (tun_mtu, overhead, self._lan_egress_iface(), outer_mtu, inner_mtu, SWU_INNER_FRAG))
            if inner_mtu < tun_mtu:
                swu_log("note: outer path (%d) < ipsec0(%d)+overhead; inner packets above %d bytes "
                        "are split by the tunnel client (transparent to IMS) so each ESP datagram "
                        "is a complete, unfragmented outer packet%s"
                        % (outer_mtu, tun_mtu, inner_mtu,
                           "" if SWU_INNER_FRAG else " — DISABLED (SWU_INNER_FRAG=0)"))
        except Exception as e:
            swu_log("tun MTU setup failed (keeping default): %r" % e)
            self.inner_mtu = getattr(self, "inner_mtu", 0)
            self._tun_read_size = getattr(self, "_tun_read_size", 2048)

    @staticmethod
    def _ones_complement_checksum(data):
        """16-bit one's-complement sum (IPv4 header checksum / ICMP checksum core)."""
        if len(data) % 2:
            data += b'\x00'
        total = 0
        for i in range(0, len(data), 2):
            total += (data[i] << 8) | data[i + 1]
        total = (total & 0xffff) + (total >> 16)
        total = (total & 0xffff) + (total >> 16)
        return (~total) & 0xffff

    def _fragment_ipv4_inner(self, packet, mtu):
        """RFC 791 fragmentation of an inner IPv4 datagram so each piece, once ESP-encapsulated,
        fits one outer datagram. Returns a list of IPv4 fragments, or None when the packet cannot
        be fragmented (DF set, or header/length inconsistent) so the caller can fall back to outer
        fragmentation / an ICMP signal. Each fragment carries a recomputed header checksum and a
        payload that is a multiple of 8 bytes (except possibly the last)."""
        try:
            if len(packet) < 20:
                return None
            ihl = (packet[0] & 0x0F) * 4
            if ihl < 20 or ihl > len(packet):
                return None
            total_len = struct.unpack("!H", packet[2:4])[0]
            if total_len < ihl or total_len > len(packet):
                total_len = len(packet)                       # tolerate a short/again-read frame
            flags_frag = struct.unpack("!H", packet[6:8])[0]
            df = flags_frag & 0x4000
            mf = flags_frag & 0x2000
            base_off = (flags_frag & 0x1FFF) * 8              # honour a pre-existing offset
            if df:
                return None
            header = bytearray(packet[:ihl])
            payload = packet[ihl:total_len]
            max_payload = (mtu - ihl) & ~7                    # 8-byte aligned per RFC 791
            if max_payload <= 0 or len(payload) <= max_payload:
                return None
            frags = []
            off = 0
            n = len(payload)
            while off < n:
                chunk = payload[off:off + max_payload]
                is_last = (off + len(chunk) >= n)
                frag_off_units = (base_off + off) // 8
                new_flags = (0x2000 if (mf or not is_last) else 0) | (frag_off_units & 0x1FFF)
                h = bytearray(header)
                h[2:4] = struct.pack("!H", ihl + len(chunk))
                h[6:8] = struct.pack("!H", new_flags)
                h[10:12] = b'\x00\x00'
                h[10:12] = struct.pack("!H", self._ones_complement_checksum(bytes(h)))
                frags.append(bytes(h) + chunk)
                off += len(chunk)
            return frags
        except Exception as e:
            swu_log("inner IPv4 fragmentation error: %r" % e)
            return None

    def _fragment_ipv6_inner(self, packet, mtu):
        """RFC 8200 §4.5 fragmentation of an inner IPv6 datagram: insert a Fragment extension
        header and split the payload so each fragment, once ESP-encapsulated, fits one outer
        datagram <= the outer link MTU. This is essential where the carrier DROPS outer IPv4
        fragments (e.g. EE): each swu-produced fragment is a COMPLETE, unfragmented outer datagram,
        so nothing depends on IP-layer reassembly of the outer packet. Both the inner source
        (Asterisk) and this tunnel ingress are on the same host, so preserving the inner src/dst
        makes reassembly at the P-CSCF host indistinguishable from ordinary source fragmentation.

        Only the common VoWiFi shape is fragmented: a base 40-byte header immediately followed by
        the upper-layer (TCP/UDP/ICMPv6). Packets that already carry extension headers — including
        an existing Fragment header — return None so the caller falls back to outer fragmentation
        (rare for IMS signalling; RTP is always small)."""
        try:
            if len(packet) < 40:
                return None
            nexthdr = packet[6]
            if nexthdr in (0, 43, 44, 60):        # hop-by-hop / routing / fragment / dest-opts
                return None
            payload = packet[40:]
            max_chunk = (mtu - 40 - 8) & ~7        # 40 base + 8 fragment header, 8-byte aligned
            if max_chunk <= 0 or len(payload) <= max_chunk:
                return None
            frag_id = self._next_v6_frag_id()
            base = bytearray(packet[:40])
            base[6] = 44                            # base next-header -> Fragment (44)
            frags = []
            off = 0
            n = len(payload)
            while off < n:
                chunk = payload[off:off + max_chunk]
                m = 0 if (off + len(chunk) >= n) else 1
                # fragment header: next-hdr(1) reserved(1) offset<<3|res|M (2) identification(4)
                fh = bytes([nexthdr, 0]) + struct.pack("!H", (off & 0xFFF8) | m) + struct.pack("!I", frag_id)
                h = bytearray(base)
                struct.pack_into("!H", h, 4, 8 + len(chunk))   # IPv6 payload length
                frags.append(bytes(h) + fh + chunk)
                off += len(chunk)
            return frags
        except Exception as e:
            swu_log("inner IPv6 fragmentation error: %r" % e)
            return None

    def _next_v6_frag_id(self):
        self._v6_frag_id = (getattr(self, "_v6_frag_id", 0) + 1) & 0xFFFFFFFF
        return self._v6_frag_id

    def _icmp_too_big(self, packet, mtu):
        """Build an ICMP 'packet too big' / 'fragmentation needed' addressed back to the inner
        packet's source, so the LOCAL stack (Asterisk/kernel) learns the tunnel path MTU and
        self-sizes (TCP MSS shrinks; IPv6 sources emit sub-1280 fragments). Written into the tun,
        it looks exactly like an on-path router's PMTUD signal. Returns the ICMP datagram bytes or
        None. Never raises."""
        try:
            ver = packet[0] >> 4
            quote = packet[:min(len(packet), 1232)]           # keep the whole ICMP under 1280
            if ver == 6:
                src = packet[8:24]                            # inner IPv6 src/dst
                dst = packet[24:40]
                # ICMPv6 type 2 (Packet Too Big), code 0, MTU in the 4-byte body, then the quote.
                body = struct.pack("!I", mtu) + quote
                icmp = bytearray(b'\x02\x00\x00\x00' + body)
                pseudo = dst + src + struct.pack("!I", len(icmp)) + b'\x00\x00\x00\x3a'
                csum = self._ones_complement_checksum(pseudo + bytes(icmp))
                icmp[2:4] = struct.pack("!H", csum)
                ip = bytearray(40)
                ip[0] = 0x60
                struct.pack_into("!H", ip, 4, len(icmp))       # payload length
                ip[6] = 0x3a                                    # next header = ICMPv6
                ip[7] = 64                                      # hop limit
                ip[8:24] = dst                                  # from the "router" (the dst) ...
                ip[24:40] = src                                 # ... back to the source
                return bytes(ip) + bytes(icmp)
            elif ver == 4:
                ihl = (packet[0] & 0x0F) * 4
                src = packet[12:16]
                dst = packet[16:20]
                q = packet[:min(len(packet), ihl + 8)]         # RFC 792: IP header + 8 bytes
                # type 3 (dest unreachable) code 4 (frag needed, DF set); next-hop MTU in low 16b.
                icmp = bytearray(b'\x03\x04\x00\x00' + struct.pack("!HH", 0, mtu) + q)
                icmp[2:4] = struct.pack("!H", self._ones_complement_checksum(bytes(icmp)))
                total = 20 + len(icmp)
                ip = bytearray(20)
                ip[0] = 0x45
                struct.pack_into("!H", ip, 2, total)
                ip[8] = 64                                      # TTL
                ip[9] = 1                                       # protocol = ICMP
                ip[12:16] = dst
                ip[16:20] = src
                ip[10:12] = struct.pack("!H", self._ones_complement_checksum(bytes(ip)))
                return bytes(ip) + bytes(icmp)
        except Exception as e:
            swu_log("ICMP PTB build error: %r" % e)
        return None

    def set_routes(self):

        self.tunnel = self.open_tun(1)
        # The ePDG's INTERNAL_IP6_ADDRESS is the UE identity for the IMS PDN.
        # Do not let Linux also create a SLAAC/privacy address from an RA seen
        # inside the userspace tunnel: RFC 6724 may select that second address
        # for SIP, and O2 then rejects REGISTER because its source is not the
        # exact address assigned to this IKE session.
        self.exec_in_netns(
            "sysctl -qw net.ipv6.conf.%s.accept_ra=0 "
            "net.ipv6.conf.%s.autoconf=0 "
            "net.ipv6.conf.%s.use_tempaddr=0"
            % (self.tun_device, self.tun_device, self.tun_device)
        )
        self.exec_in_netns("ip -6 addr flush dev %s scope global" % self.tun_device)
        if self.netns_name:
            # create netns adn move the tun device into it
            subprocess.call("ip netns add %s" % self.netns_name, shell=True)
            subprocess.call("ip link set dev %s netns %s" % (self.tun_device, self.netns_name), shell=True)
            # moving to netns brings device down again
            self.exec_in_netns("ip link set dev %s up" % (self.tun_device))

        if self.ip_address_list != []:
            self.exec_in_netns("ip addr add " + self.ip_address_list[0] + "/32 dev " + self.tun_device)
            #set host route, only  required if no netns
            if not self.netns_name:
                if self.default_gateway is None:
                    self.exec_in_netns("route add " + self.server_address[0] + "/32 gw " + self.get_default_gateway_linux()[0])
                else:
                    self.exec_in_netns("route add " + self.server_address[0] + "/32 gw " + self.default_gateway)

            # VoWiFi engine addition: on an IPv4 IMS PDN (e.g. Vodafone UK, cp_mode=v4) the two /1
            # routes below make the tunnel the default route for ALL IPv4. That blackholes every
            # packet the container sources from its docker-bridge address (SWU_SOURCE): DNS lookups
            # (-> 40s timeouts, delaying the IMS SMS RP-ACK past its correlation window so the SMSC
            # 488s it and re-pushes the same SM forever) AND the return traffic of any published port
            # (the WebRTC WSS softphone, AMI) — a LAN client's SYN-ACK goes out ipsec0
            # and is lost, so the softphone can never connect. Fix both at once with SOURCE-based
            # policy routing: traffic sourced from the container's LAN address goes out the LAN link,
            # while IMS traffic (sourced from the tunnel INNER address) still uses the /1 tunnel
            # routes. Robust even if a LAN client's IP equals the P-CSCF's (the discriminator is the
            # source address, not the destination). IPv6 PDNs (Telus/EE) only route ::/1 so IPv4 is
            # untouched and this is a no-op there.
            if not self.netns_name:
                self._install_lan_bypass_policy()

            self.exec_in_netns("route add -net 0.0.0.0/1 gw " + self.ip_address_list[0])
            self.exec_in_netns("route add -net 128.0.0.0/1 gw " + self.ip_address_list[0])
        
        if self.ipv6_address_list != []:
            ipv6_address_prefix = ':'.join(self.ipv6_address_list[0].split(':')[0:4])
            ipv6_address_identifier = 'fe80::' + ':'.join(self.ipv6_address_list[0].split(':')[4:8])
            self.exec_in_netns("ip -6 addr add " + ipv6_address_identifier + "/64 dev " + self.tun_device)
            # VoWiFi engine addition: the upstream code only puts a derived link-local on the
            # tun (the assigned global is never assigned). That is fine for a plain data APN
            # where the ePDG routes by tunnel, but IMS/P-CSCF validates the SIP source against
            # the assigned inner address, so Asterisk must be able to source from the assigned
            # global (RFC 6724 then picks it for the global P-CSCF). Assign it additively,
            # leaving the confirmed-working link-local in place.
            if SWU_ASSIGN_IPV6_GLOBAL:
                self.exec_in_netns("ip -6 addr add " + self.ipv6_address_list[0] + "/64 dev " + self.tun_device)
            self.exec_in_netns("route -A inet6 add ::/1 dev " + self.tun_device)
            self.exec_in_netns("route -A inet6 add 8000::/1 dev " + self.tun_device)

        elif getattr(self, "ipv6_subnet_list", []):
            # P0-1: the ePDG assigned the inner IPv6 by prefix (INTERNAL_IP6_SUBNET) rather than a
            # full INTERNAL_IP6_ADDRESS. Derive one stable address inside the prefix and assign it
            # (using the ePDG-returned prefix length, not a hard-coded /64). Publish it as
            # ipv6_address_list[0] so state_connected/reporting and the SIP source pick it up.
            (prefix, prefix_len) = self.ipv6_subnet_list[0]
            derived, plen = self._derive_ipv6_address_from_subnet(prefix, prefix_len)
            if derived:
                self.ipv6_address_list = [derived]
                self.exec_in_netns("ip -6 addr add %s/%d dev %s" % (derived, plen, self.tun_device))
                self.exec_in_netns("route -A inet6 add ::/1 dev " + self.tun_device)
                self.exec_in_netns("route -A inet6 add 8000::/1 dev " + self.tun_device)
                swu_log("assigned derived inner IPv6 %s/%d from INTERNAL_IP6_SUBNET %s/%s" %
                        (derived, plen, prefix, prefix_len))
            else:
                swu_log("INTERNAL_IP6_SUBNET %s/%s could not be turned into an address" %
                        (prefix, prefix_len))


        if (self.dns_address_list != [] or self.dnsv6_address_list != []) and \
                (self.netns_name or SWU_WRITE_RESOLV):
            # In the engine container we do NOT rewrite /etc/resolv.conf (guarded off by
            # default): P-CSCF is an explicit IP and clobbering resolv.conf breaks the manager
            # callback + ePDG FQDN resolution. Netns mode still writes its own per-ns resolv.
            if self.netns_name:
                self.add_dir() #create directory for namespace if it doesn't exist
                
                with open("/etc/netns/%s/resolv.conf" % self.netns_name, "w") as file_obj:
                    for i in self.dns_address_list:
                        file_obj.write("nameserver %s\n" % i)
                    for i in self.dnsv6_address_list:
                        file_obj.write("nameserver %s\n" % i)
            else:
                subprocess.call("cp /etc/resolv.conf /etc/resolv.backup.conf", shell=True)  
                subprocess.call("echo > /etc/resolv.conf", shell=True) 
                for i in self.dns_address_list:
                    subprocess.call("echo 'nameserver " + i +"' >> /etc/resolv.conf", shell=True)  
                for i in self.dnsv6_address_list:
                    subprocess.call("echo 'nameserver " + i +"' >> /etc/resolv.conf", shell=True)            

        # VoWiFi engine addition: size ipsec0 to the outer path now that the inner address family
        # (v4/v6) and the CHILD-SA cipher are both known, so the encapsulation worker (forked next
        # in state_connected, inheriting self.inner_mtu) never overflows the outer link MTU.
        self._compute_and_apply_tun_mtu()

    def add_dir(self):
        if not os.path.isdir('/etc/netns'):
            os.mkdir('/etc/netns')
        if not os.path.isdir('/etc/netns/' + self.netns_name):
            os.mkdir('/etc/netns/'  + self.netns_name)

    def _derive_ipv6_address_from_subnet(self, prefix, prefix_len):
        """P0-1: derive ONE stable inner IPv6 address inside an ePDG-assigned prefix
        (INTERNAL_IP6_SUBNET, TS 24.302 7.4.1.1) when no full INTERNAL_IP6_ADDRESS was supplied.
        The interface identifier is SHA-256(IMSI) top 64 bits (RFC 4291 modified EUI-64 range,
        local bit set, never all-zero) so the address is STABLE across reconnects/reauths — a
        random IID would drift the SIP/IMS source address every time. The ePDG-returned prefix
        length is honoured (VoWiFi is normally /64, but a non-/64 still yields an in-prefix
        address). Returns (address_string, prefix_len_int) or (None, prefix_len) on error."""
        try:
            plen = int(prefix_len)
        except Exception:
            plen = 64
        if plen < 0 or plen > 128:
            plen = 64
        try:
            network = ipaddress.IPv6Network((prefix, plen), strict=False)
        except Exception as e:
            swu_log("could not build IPv6 network from %s/%s: %r" % (prefix, prefix_len, e))
            return (None, plen)
        digest = hashlib.sha256(self.imsi.encode("ascii")).digest()
        iid = int.from_bytes(digest[:8], "big")
        iid |= 0x0200000000000000        # set the universal/local bit (local), avoid all-zero IID
        iid &= 0x02ffffffffffffff
        host_bits = 128 - plen
        host_mask = ((1 << host_bits) - 1) if host_bits > 0 else 0
        addr_int = int(network.network_address) | (iid & host_mask)
        return (str(ipaddress.IPv6Address(addr_int)), plen)
     
    def delete_routes(self):
        if self.netns_name:
            subprocess.call("ip netns del %s" % self.netns_name, shell=True)
        else:
            # Tear down the IPv4 LAN-bypass policy (source rule + dedicated table) if it was
            # installed, so a re-establish starts clean and nothing leaks across attaches.
            src = getattr(self, "source_address", None)
            if src:
                table = os.environ.get("SWU_LAN_BYPASS_TABLE", "51820")
                pref = os.environ.get("SWU_LAN_BYPASS_PREF", "100")
                self.exec_in_netns("ip rule del from %s lookup %s pref %s" % (src, table, pref) + " 2>/dev/null")
                self.exec_in_netns("ip route flush table %s" % table + " 2>/dev/null")
            self.exec_in_netns("route del " + self.server_address[0] + "/32", shell=True)
            os.close(self.tunnel)
            if self.dns_address_list != []:
                subprocess.call("cp /etc/resolv.backup.conf /etc/resolv.conf", shell=True)
      

    def get_default_source_address(self):
    
        proc = subprocess.Popen("/sbin/ifconfig | grep -A 1 " + get_default_gateway_linux()[1] + " | grep inet", stdout=subprocess.PIPE, shell=True)
        output = str(proc.stdout.read())
        if 'addr:' in output:
            addr = output.split('addr:')[1].split()[0]
        else:
            addr = output.split('inet ')[1].split()[0]
        return addr
    
    def get_default_gateway_linux(self):
        """Read the default gateway directly from /proc."""
        with open("/proc/net/route") as fh:
            for line in fh:
                fields = line.strip().split()
                if fields[1] != '00000000' or not int(fields[3], 16) & 2:
                    continue
    
                return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16))), fields[0]


    def open_tun(self,n):
        TUNSETIFF = 0x400454ca
        IFF_TUN   = 0x0001
        IFF_TAP   = 0x0002
        IFF_NO_PI = 0x1000 # No Packet Information - to avoid 4 extra bytes
    
        TUNMODE = IFF_TUN | IFF_NO_PI
        MODE = 0
        DEBUG = 0

        self.tun_device = SWU_IFACE

        f = os.open("/dev/net/tun", os.O_RDWR)
        ifs = fcntl.ioctl(f, TUNSETIFF, struct.pack("16sH", bytes(self.tun_device, "utf-8"), TUNMODE))
        subprocess.call("ip link set dev %s up" % self.tun_device, shell=True)

        return f


    def esp_padding(self,length):
        padding = b''
        for i in range(length):
            padding += bytes([i+1])
        return padding

    def encapsulate_esp_packet(self,packet,encr_alg,encr_key,integ_alg,integ_key,spi_resp,sqn):

        hash_size = self.integ_key_truncated_len_bytes.get(integ_alg)
        if packet[0] // 16 == 4: #ipv4
            packet_type = 4
        elif packet[0] // 16 == 6: #ipv6
            packet_type = 41
        else:
            return None
        
        if encr_alg in (ENCR_AES_CBC,):
            vector = self.return_random_bytes(16)
            data_to_encrypt = packet
            
            res = 16 - (len(data_to_encrypt) % 16)
            if res>1:
                data_to_encrypt += self.esp_padding(res-2) + bytes([res-2]) + bytes([packet_type])
            else:
                data_to_encrypt += self.esp_padding(14+res) + bytes([14+res]) + bytes([packet_type])
                   
            cipher = Cipher(algorithms.AES(encr_key), modes.CBC(vector))
            encryptor = cipher.encryptor()          
            cipher_data = encryptor.update(data_to_encrypt) + encryptor.finalize()
                      
            new_ike_packet = spi_resp + struct.pack("!I",sqn) + vector + cipher_data         
            
            if hash_size != 0:          
                hash = self.integ_function.get(integ_alg) 
                h = hmac.HMAC(integ_key,hash)
                h.update(new_ike_packet)
                hash = h.finalize()[0:hash_size]         
            else:
                hash = b''
                
            return new_ike_packet + hash

        elif encr_alg in (ENCR_AES_GCM_8, ENCR_AES_GCM_12, ENCR_AES_GCM_16):
            
            if encr_alg == ENCR_AES_GCM_8: mac_length = 8
            if encr_alg == ENCR_AES_GCM_12: mac_length = 12
            if encr_alg == ENCR_AES_GCM_16: mac_length = 16        
        
            aad = spi_resp + struct.pack("!I",sqn) 
            vector = self.return_random_bytes(8)
          
            data_to_encrypt = packet
            
            res = (len(data_to_encrypt)+2) % 4            
            if res== 0:
                data_to_encrypt += bytes([res]) + bytes([packet_type])                
            else:
                data_to_encrypt += self.esp_padding(4-res) + bytes([4-res]) + bytes([packet_type])                  
                        
            cipher = AES.new(encr_key[:-4], AES.MODE_GCM, nonce=encr_key[-4:] + vector, mac_len=mac_length)
            cipher.update(aad)
            
            cipher_data, tag = cipher.encrypt_and_digest(data_to_encrypt)
                                           
            new_ike_packet = spi_resp + struct.pack("!I",sqn) + vector + cipher_data + tag
          
            return new_ike_packet

        elif encr_alg in (ENCR_NULL,):
            
            new_ike_packet = spi_resp + struct.pack("!I",sqn) + packet + bytes([0]) + bytes([packet_type])
            
            if hash_size !=0:            
                hash = self.integ_function.get(integ_alg) 
                h = hmac.HMAC(integ_key,hash)
                h.update(new_ike_packet)
                hash = h.finalize()[0:hash_size]         
            else:
                hash = b''
                
            return new_ike_packet + hash            
 
        
        return None

    def _prepare_egress(self, packet):
        """Decide how an inner packet read off the tun leaves the tunnel. Returns the list of inner
        IP packets to ESP-encapsulate: normally [packet]; several pieces when an oversized inner
        packet is fragmented so each encapsulates into one COMPLETE outer datagram <= the sensed
        path MTU. Inner IPv4 uses RFC 791 fragmentation (DF clear); inner IPv6 uses an RFC 8200
        Fragment header. Fragmenting at the INNER layer (never letting the kernel IP-fragment the
        outer carrier — the outer sockets set DF via PMTUDISC_DO) is what makes a constrained path
        work on carriers that DROP IP fragments (e.g. EE): every packet on the wire is a self-
        contained outer datagram. The rare packet that cannot be inner-fragmented (DF-set IPv4, or
        an IPv6 packet already carrying extension headers) is passed whole; with DF set it is then
        dropped-and-PMTU-signalled by the path rather than silently corrupted. SWU_ICMP_PTB (off by
        default, to keep the IMS layer unaware) additionally injects an ICMP PTB toward the local
        source (IPv6 only when the reportable MTU is >= 1280 — RFC 8201)."""
        inner_mtu = getattr(self, "inner_mtu", 0)
        if not inner_mtu or len(packet) <= inner_mtu or len(packet) < 20:
            return [packet]
        ver = packet[0] >> 4
        if SWU_INNER_FRAG and ver == 4:
            frags = self._fragment_ipv4_inner(packet, inner_mtu)
            if frags:
                return frags
        if SWU_INNER_FRAG and ver == 6:
            frags = self._fragment_ipv6_inner(packet, inner_mtu)
            if frags:
                return frags
        if SWU_ICMP_PTB and (ver == 4 or (ver == 6 and inner_mtu >= IPV6_MIN_MTU)):
            icmp = self._icmp_too_big(packet, inner_mtu)
            if icmp:
                try:
                    os.write(self.tunnel, icmp)
                except Exception:
                    pass
        return [packet]

    def _outer_path_mtu(self):
        """The kernel's currently-known path MTU toward the ePDG on the NAT-T (UDP) socket, via
        IP_MTU. After an EMSGSIZE (or once PMTUD has run) this reflects the exact largest outer
        datagram the path accepts. Returns None if unavailable (e.g. raw ESP socket / no route)."""
        try:
            ip_mtu = getattr(socket, "IP_MTU", 14)
            m = self.socket_nat.getsockopt(socket.IPPROTO_IP, ip_mtu)
            return m if m and m > 0 else None
        except Exception:
            return None

    def _lower_inner_mtu(self, hint_outer_mtu=None):
        """Lower the inner fragmentation threshold in response to a path that rejected an outer
        datagram as too large. Uses the kernel's exact IP_MTU (or a caller-supplied hint) when it
        yields a strictly smaller threshold; otherwise steps down from the current value so we
        ALWAYS make progress (IP_MTU can still read the 1500 NIC MTU right after the drop). Bounded
        below by 68 (min IPv4 datagram). Returns the new inner_mtu."""
        overhead = self._esp_overhead()
        outer = hint_outer_mtu or self._outer_path_mtu()
        cur = getattr(self, "inner_mtu", 0) or (SWU_TUN_MTU_ENV or 1400)
        new_inner = None
        if outer:
            candidate = outer - overhead - SWU_MTU_MARGIN
            if candidate < cur:
                new_inner = candidate
        if new_inner is None:
            # No usable exact figure: back off ~10% (min 32 bytes) so repeated hits converge fast.
            new_inner = cur - max(32, cur // 10)
        if new_inner < 68:
            new_inner = 68
        if new_inner < cur:
            swu_log("outer datagram rejected (EMSGSIZE); lowering fragmentation threshold "
                    "%d -> %d (outer_mtu=%s overhead=%d)"
                    % (cur, new_inner, outer if outer else "?", overhead))
            self.inner_mtu = new_inner
            self.outer_mtu = outer or getattr(self, "outer_mtu", 0)
            # Remember this as a ceiling so the periodic re-sense can't raise the threshold back
            # above a level the path has actually rejected.
            prev = getattr(self, "_emsgsize_floor", None)
            self._emsgsize_floor = new_inner if prev is None else min(prev, new_inner)
        return self.inner_mtu

    def _sendto_outer(self, encrypted_packet):
        """Send one already-encapsulated ESP datagram on the correct outer socket. Returns True on
        success. Raises OSError(EMSGSIZE) up to the caller for MTU recovery; swallows every OTHER
        transient socket error (a single dropped ESP datagram must NEVER kill the dataplane worker,
        which previously died on an uncaught EMSGSIZE -> the whole tunnel went dead)."""
        if self.userplane_mode == ESP_PROTOCOL:
            self.socket_esp.sendto(encrypted_packet, self.server_address_esp)
        else:
            self.socket_nat.sendto(encrypted_packet, self.server_address_nat)
        return True

    def _send_inner_esp(self, inner_packet, encr_alg, encr_key, integ_alg, integ_key, spi_resp, sqn):
        """Encapsulate one inner IP packet and send it, recovering from a too-large outer datagram
        (EMSGSIZE) instead of crashing. On EMSGSIZE we learn the real path MTU (IP_MTU), lower the
        fragmentation threshold, re-fragment THIS inner packet and resend each piece. Bounded retry
        so a pathological path can't loop forever. Returns the updated ESP sequence number."""
        encrypted_packet = self.encapsulate_esp_packet(
            inner_packet, encr_alg, encr_key, integ_alg, integ_key, spi_resp, sqn)
        if encrypted_packet is None:
            return sqn
        try:
            self._sendto_outer(encrypted_packet)
            return sqn + 1
        except OSError as e:
            if e.errno != errno.EMSGSIZE:
                # Any other send error: drop this datagram, keep the worker alive.
                swu_log("outer ESP send error (dropping datagram): %r" % e)
                return sqn + 1
        # EMSGSIZE: the outer datagram exceeded the path MTU. Learn it, lower the threshold and
        # re-fragment this inner packet so each ESP datagram now fits. Retry a few times in case
        # the path MTU drops in more than one step.
        for _ in range(4):
            self._lower_inner_mtu()
            pieces = self._prepare_egress(inner_packet)
            # _prepare_egress returns [inner_packet] unchanged when it can't fragment (e.g. DF-set
            # IPv4 or an already-extension-headed IPv6). Nothing more we can do — drop it.
            if len(pieces) == 1 and pieces[0] is inner_packet and len(inner_packet) > self.inner_mtu:
                swu_log("inner packet %dB cannot be fragmented below path MTU; dropping"
                        % len(inner_packet))
                return sqn + 1
            ok = True
            for piece in pieces:
                ep = self.encapsulate_esp_packet(
                    piece, encr_alg, encr_key, integ_alg, integ_key, spi_resp, sqn)
                if ep is None:
                    continue
                try:
                    self._sendto_outer(ep)
                    sqn += 1
                except OSError as e2:
                    if e2.errno == errno.EMSGSIZE:
                        ok = False
                        break                     # still too big -> lower again and re-fragment
                    swu_log("outer ESP send error on fragment (dropping): %r" % e2)
                    sqn += 1
            if ok:
                return sqn
        swu_log("giving up sending inner packet after repeated EMSGSIZE (path MTU too small)")
        return sqn

    def encapsulate_ipsec(self,args, parent_pid=None):

        if parent_pid is not None:
            prepare_ipsec_worker(parent_pid, "encoder")

        pipe_ike = args[0]
        socket_list = [self.tunnel, pipe_ike, self.socket_esp]
        encr_alg = None
        integ_alg = None
        sqn = 1

        # NAT-T keepalive (VoWiFi engine addition): behind NAT (docker bridge / home router) the
        # ESP-in-UDP:4500 flow has a conntrack/NAT mapping that the kernel drops after ~30-120s
        # of silence, after which inbound ESP from the ePDG can no longer reach us and the tunnel
        # goes silently dead on idle. strongSwan sends an RFC 3948 NAT-keepalive (a single 0xFF
        # byte to the peer's 4500) every ~20s to hold the mapping open; the upstream emulator
        # sends nothing. Wake the select() loop on a timer and emit the keepalive when idle in
        # NAT-traversal mode. (In raw-ESP mode there is no UDP flow, so no keepalive is needed.)
        keepalive_interval = float(os.environ.get("SWU_NATT_KEEPALIVE", "20") or 20)
        last_keepalive = time.time()

        # Periodically re-sense the outer path MTU: PMTU discovery only learns a constrained hop
        # (e.g. a WireGuard/1280 on the host) AFTER traffic starts, so the fragmentation threshold
        # self.inner_mtu must be refreshed once the route cache updates. Cheap `ip route get`.
        last_mtu_check = 0.0

        while True:
            timeout = keepalive_interval if keepalive_interval > 0 else None
            if SWU_MTU_REFRESH > 0:
                timeout = SWU_MTU_REFRESH if timeout is None else min(timeout, SWU_MTU_REFRESH)
            read_sockets, write_sockets, error_sockets = select.select(socket_list, [], [], timeout)
            if SWU_MTU_REFRESH > 0 and (time.time() - last_mtu_check) >= SWU_MTU_REFRESH:
                try:
                    # Re-sense from the route cache, but also fold in the kernel's exact path MTU
                    # (IP_MTU) which reflects any PMTUD-learned / EMSGSIZE drop, and never raise the
                    # threshold above a level a real EMSGSIZE already proved too big (self._emsgsize_
                    # floor) — otherwise we'd oscillate right back into the too-large send.
                    sensed_outer = self._sense_outer_mtu()
                    kern = self._outer_path_mtu()
                    outer = min([m for m in (sensed_outer, kern) if m]) if (sensed_outer or kern) else sensed_outer
                    new_inner = self._inner_mtu_from(outer)
                    floor = getattr(self, "_emsgsize_floor", None)
                    if floor is not None:
                        new_inner = min(new_inner, floor)
                    if new_inner != getattr(self, "inner_mtu", 0):
                        swu_log("outer path MTU refresh -> fragmentation threshold now %d "
                                "(was %d)" % (new_inner, getattr(self, "inner_mtu", 0)))
                        self.inner_mtu = new_inner
                except Exception:
                    pass
                last_mtu_check = time.time()
            if keepalive_interval > 0 and self.userplane_mode == NAT_TRAVERSAL and \
                    (time.time() - last_keepalive) >= keepalive_interval:
                try:
                    self.socket_nat.sendto(b'\xff', self.server_address_nat)
                except Exception:
                    pass
                last_keepalive = time.time()
            for sock in read_sockets:
                if sock == self.tunnel:
                    tap_packet = os.read(self.tunnel, getattr(self, "_tun_read_size", 1514))

                    if encr_alg is not None:

                        # Transparent inner fragmentation: split an oversized inner packet so each
                        # ESP datagram is a complete outer packet <= the sensed path MTU. Normally
                        # (path can carry ipsec0-MTU + overhead) _prepare_egress returns [packet].
                        for inner_packet in self._prepare_egress(tap_packet):
                            sqn = self._send_inner_esp(inner_packet, encr_alg, encr_key,
                                                       integ_alg, integ_key, spi_resp, sqn)

                elif sock == pipe_ike:
                    pipe_packet = pipe_ike.recv()                     
                    decode_list = self.decode_inter_process_protocol(pipe_packet)
                    if decode_list[0] == INTER_PROCESS_DELETE_SA:
                        sys.exit()
                    elif decode_list[0] in (INTER_PROCESS_CREATE_SA, INTER_PROCESS_UPDATE_SA):
                        for i in decode_list[1]:
                            if i[0] == INTER_PROCESS_IE_ENCR_ALG: encr_alg = i[1]
                            if i[0] == INTER_PROCESS_IE_INTEG_ALG: integ_alg = i[1]
                            if i[0] == INTER_PROCESS_IE_ENCR_KEY: encr_key = i[1]
                            if i[0] == INTER_PROCESS_IE_INTEG_KEY: integ_key = i[1]                            
                            if i[0] == INTER_PROCESS_IE_SPI_RESP: spi_resp = i[1]
                    elif decode_list[0] == INTER_PROCESS_IKE and decode_list[1][0] == INTER_PROCESS_IE_IKE_MESSAGE: #not used for now. check 4 bytes zero if nat transversal
                        ike_message = decode_list[1][1]                    
                        self.socket_nat.sendto(ike_message, self.server_address_nat)
        
        return 0
    
    
    def decapsulate_ipsec(self,args, parent_pid=None):

        if parent_pid is not None:
            prepare_ipsec_worker(parent_pid, "decoder")
        
        pipe_ike = args[0]
                
        socket_list = [self.socket_nat, pipe_ike, self.socket_esp]
        encr_alg = None
        integ_alg = None
        
        while True:
            read_sockets, write_sockets, error_sockets = select.select(socket_list, [], [])
            for sock in read_sockets:
                if sock == self.socket_nat:
                    packet, address = self.socket_nat.recvfrom(4096)
                    
                    if encr_alg is not None:
                        if packet[0:4] == b'\x00\x00\x00\x00': #is ike message
                            inter_process_list_ike_message = [INTER_PROCESS_IKE,[(INTER_PROCESS_IE_IKE_MESSAGE, packet)]]
                            pipe_ike.send(self.encode_inter_process_protocol(inter_process_list_ike_message))
                            
                        elif packet[0:4] == spi_init:

                            if encr_alg is not None:
                                decrypted_packet = self.decapsulate_esp_packet(packet,encr_alg,encr_key,integ_alg,integ_key)
                                if decrypted_packet is not None:

                                    os.write(self.tunnel,decrypted_packet)
                                    self._note_esp_activity(pipe_ike)

                elif sock == self.socket_esp:
                    packet, address = self.socket_esp.recvfrom(4096)
                    if encr_alg is not None:
                        if packet[20:24] == spi_init:

                            if encr_alg is not None:
                                decrypted_packet = self.decapsulate_esp_packet(packet[20:],encr_alg,encr_key,integ_alg,integ_key)
                                if decrypted_packet is not None:

                                    os.write(self.tunnel,decrypted_packet)
                                    self._note_esp_activity(pipe_ike)
                        
               
                elif sock == pipe_ike:
                    pipe_packet = pipe_ike.recv()                     
                    decode_list = self.decode_inter_process_protocol(pipe_packet)
                    if decode_list[0] == INTER_PROCESS_DELETE_SA:
                        sys.exit()
                    elif decode_list[0] in (INTER_PROCESS_CREATE_SA, INTER_PROCESS_UPDATE_SA):
                        for i in decode_list[1]:
                            if i[0] == INTER_PROCESS_IE_ENCR_ALG: encr_alg = i[1]
                            if i[0] == INTER_PROCESS_IE_INTEG_ALG: integ_alg = i[1]
                            if i[0] == INTER_PROCESS_IE_ENCR_KEY: encr_key = i[1]
                            if i[0] == INTER_PROCESS_IE_INTEG_KEY: integ_key = i[1]                            
                            if i[0] == INTER_PROCESS_IE_SPI_INIT: spi_init = i[1]


        return 0

    def decapsulate_esp_packet(self,packet,encr_alg,encr_key,integ_alg,integ_key):       
    
        if encr_alg in (ENCR_AES_CBC,):
            vector = packet[8:24]
            hash_size = self.integ_key_truncated_len_bytes.get(integ_alg)
            hash_data = packet[-hash_size:]
        
            encrypted_data = packet[24:len(packet)-hash_size]
        
            cipher = Cipher(algorithms.AES(encr_key), modes.CBC(vector))            
            decryptor = cipher.decryptor()
            
            uncipher_data = decryptor.update(encrypted_data) + decryptor.finalize()
            padding_length = uncipher_data[-2]
            uncipher_packet = uncipher_data[0:-padding_length-2]

            return uncipher_packet
            
        elif encr_alg in (ENCR_AES_GCM_8, ENCR_AES_GCM_12, ENCR_AES_GCM_16):
            if encr_alg == ENCR_AES_GCM_8: mac_length = 8
            if encr_alg == ENCR_AES_GCM_12: mac_length = 12
            if encr_alg == ENCR_AES_GCM_16: mac_length = 16
            
            aad = packet[0:8]            
            cipher = AES.new(encr_key[:-4], AES.MODE_GCM, nonce=encr_key[-4:] + packet[8:16],mac_len=mac_length)
            cipher.update(aad)

            uncipher_data = cipher.decrypt_and_verify(packet[16:-mac_length],packet[-mac_length:])                      
            padding_length = uncipher_data[-2]
            uncipher_packet = uncipher_data[0:-padding_length-2]                               
                     
            return uncipher_packet

        elif encr_alg in (ENCR_NULL,):
            hash_size = self.integ_key_truncated_len_bytes.get(integ_alg)
            hash_data = packet[-hash_size:]
        
            uncipher_data = packet[8:len(packet)-hash_size]
            padding_length = uncipher_data[-2]
            uncipher_packet = uncipher_data[0:-padding_length-2]

            return uncipher_packet

        return None
        
    def decode_inter_process_protocol(self,packet):
        try:
            ie_list = []
            message = packet[0]
            position = 3
            while position < len(packet):
                if packet[position+1] == 0 and packet[position+2] == 1:
                    ie_list.append((packet[position], packet[position+3]))
                else:
                    ie_list.append((packet[position], packet[position+3:position+3+packet[position+1]*256+packet[position+2]])) 
                position += 3+packet[position+1]*256+packet[position+2]
            return [message, ie_list]
        except:
            return [None,None]   



    def encode_inter_process_protocol(self,message):
        packet = b''
        for i in message[1]: 
            if type(i[1]) is int:
                packet += bytes([i[0]]) + b'\x00\x01' + bytes([i[1]])
            else:
                packet += bytes([i[0]]) + struct.pack("!H",len(i[1])) + i[1]
                
        packet = bytes([message[0]]) + struct.pack("!H",len(packet)) + packet        
        return packet
       
       
       
#### AUX FUNCTIONS RELATED TO STATES OR MESSAGES

    def get_eap_aka_attribute_value(self,list,id):
        for i in list:
           if i[0] == id: return i[1]
        return None

    def get_cp_attribute_value(self,list,id):
        return_list = []
        for i in list:
           if i[0] == id: return_list.append(i[1])
        return return_list

    def set_sa_negotiated(self,num):
        sa_negotiated = self.sa_list[num-1]
        self.sa_list_negotiated = [self.sa_list[num-1]]
        
        #default values
        self.negotiated_integrity_algorithm = NONE
        self.negotiated_encryption_algorithm = ENCR_NULL
        self.negotiated_encryption_algorithm_key_size = 0        
          
        for i in sa_negotiated[1:]:
            if i[0] == ENCR: 
                self.negotiated_encryption_algorithm = i[1]
                if self.negotiated_encryption_algorithm != ENCR_NULL: 
                    self.negotiated_encryption_algorithm_key_size = i[2][1]
            if i[0] == PRF: self.negotiated_prf = i[1]
            if i[0] == INTEG: self.negotiated_integrity_algorithm = i[1]            
            if i[0] == D_H: self.negotiated_diffie_hellman_group = i[1]            
   
    def remove_sa_from_list(self,accepted_dh_group): 
        new_sa_list = []
        for p in self.sa_list:
            for i in p:
                if i[0] == D_H and i[1] == accepted_dh_group:  
                    new_sa_list.append(p)
                    break              
        self.sa_list = new_sa_list
        

    def set_sa_negotiated_child(self,num):
        sa_negotiated = self.sa_list_child[num-1]
        self.spi_init_child = self.sa_spi_list[num-1]
        self.sa_list_negotiated_child = [self.sa_list_child[num-1]]
        
        #default values
        self.negotiated_integrity_algorithm_child = NONE
        self.negotiated_encryption_algorithm_child = ENCR_NULL
        self.negotiated_encryption_algorithm_key_size_child = 0
        
        for i in sa_negotiated[1:]:
            if i[0] == ENCR: 
                self.negotiated_encryption_algorithm_child = i[1]
                if self.negotiated_encryption_algorithm_child != ENCR_NULL:                
                    self.negotiated_encryption_algorithm_key_size_child = i[2][1]
            if i[0] == ESN: self.negotiated_esn_child = i[1]
            if i[0] == INTEG: self.negotiated_integrity_algorithm_child = i[1]            
            if i[0] == D_H: self.negotiated_diffie_hellman_group_child = i[1]            
   
    def generate_keying_material_child(self):
        
        STREAM = self.nounce + self.nounce_received
        
        AUTH_KEY_SIZE = self.integ_key_len_bytes.get(self.negotiated_integrity_algorithm_child) 
        ENCR_KEY_SIZE = self.negotiated_encryption_algorithm_key_size_child//8
        
        #exception for GCM since we need extra 4 bytes for SALT
        if self.negotiated_encryption_algorithm_child in (ENCR_AES_GCM_8, ENCR_AES_GCM_12, ENCR_AES_GCM_16):
            ENCR_KEY_SIZE += 4    
        
        KEY_LENGHT_TOTAL = 2*AUTH_KEY_SIZE + 2*ENCR_KEY_SIZE
        KEYMAT = self.prf_plus(self.negotiated_prf,self.SK_D,STREAM,KEY_LENGHT_TOTAL)
        
        self.SK_IPSEC_EI = KEYMAT[0:ENCR_KEY_SIZE]
        self.SK_IPSEC_AI = KEYMAT[ENCR_KEY_SIZE:ENCR_KEY_SIZE+AUTH_KEY_SIZE]
        self.SK_IPSEC_ER = KEYMAT[ENCR_KEY_SIZE+AUTH_KEY_SIZE:2*ENCR_KEY_SIZE+AUTH_KEY_SIZE]
        self.SK_IPSEC_AR = KEYMAT[2*ENCR_KEY_SIZE+AUTH_KEY_SIZE:2*ENCR_KEY_SIZE+2*AUTH_KEY_SIZE]
               
        
        self.print_esp_sa()
        
    def generate_keying_material(self):

        hash = self.prf_function.get(self.negotiated_prf) 
        h = hmac.HMAC(self.nounce + self.nounce_received,hash)
        h.update(self.dh_shared_key)
        SKEYSEED = h.finalize() 
        
        STREAM = self.nounce + self.nounce_received + self.ike_spi_initiator + self.ike_spi_responder
        
        PRF_KEY_SIZE = self.prf_key_len_bytes.get(self.negotiated_prf)
        AUTH_KEY_SIZE = self.integ_key_len_bytes.get(self.negotiated_integrity_algorithm) 
        ENCR_KEY_SIZE = self.negotiated_encryption_algorithm_key_size//8
        
        KEY_LENGHT_TOTAL = 3*PRF_KEY_SIZE + 2*AUTH_KEY_SIZE + 2*ENCR_KEY_SIZE

        KEY_STREAM = self.prf_plus(self.negotiated_prf,SKEYSEED,STREAM,KEY_LENGHT_TOTAL)
        
        self.SK_D  = KEY_STREAM[0:PRF_KEY_SIZE]
        self.SK_AI = KEY_STREAM[PRF_KEY_SIZE:PRF_KEY_SIZE+AUTH_KEY_SIZE]
        self.SK_AR = KEY_STREAM[PRF_KEY_SIZE+AUTH_KEY_SIZE:PRF_KEY_SIZE+2*AUTH_KEY_SIZE]
        self.SK_EI = KEY_STREAM[PRF_KEY_SIZE+2*AUTH_KEY_SIZE:PRF_KEY_SIZE+2*AUTH_KEY_SIZE+ENCR_KEY_SIZE]
        self.SK_ER = KEY_STREAM[PRF_KEY_SIZE+2*AUTH_KEY_SIZE+ENCR_KEY_SIZE:PRF_KEY_SIZE+2*AUTH_KEY_SIZE+2*ENCR_KEY_SIZE]
        self.SK_PI = KEY_STREAM[PRF_KEY_SIZE+2*AUTH_KEY_SIZE+2*ENCR_KEY_SIZE:2*PRF_KEY_SIZE+2*AUTH_KEY_SIZE+2*ENCR_KEY_SIZE]
        self.SK_PR = KEY_STREAM[2*PRF_KEY_SIZE+2*AUTH_KEY_SIZE+2*ENCR_KEY_SIZE:3*PRF_KEY_SIZE+2*AUTH_KEY_SIZE+2*ENCR_KEY_SIZE]

        self.print_ikev2_decryption_table()
        
    def generate_new_ike_keying_material(self):
        
        self.SK_D_old  = self.SK_D 
        self.SK_AI_old = self.SK_AI
        self.SK_AR_old = self.SK_AR
        self.SK_EI_old = self.SK_EI
        self.SK_ER_old = self.SK_ER
        self.SK_PI_old = self.SK_PI
        self.SK_PR_old = self.SK_PR

        hash = self.prf_function.get(self.negotiated_prf) 
        h = hmac.HMAC(self.SK_D,hash)
        h.update(self.dh_shared_key + self.nounce + self.nounce_received)
        SKEYSEED = h.finalize() 
        
        STREAM = self.nounce + self.nounce_received + self.ike_spi_initiator + self.ike_spi_responder    
        
        PRF_KEY_SIZE = self.prf_key_len_bytes.get(self.negotiated_prf)
        AUTH_KEY_SIZE = self.integ_key_len_bytes.get(self.negotiated_integrity_algorithm) 
        ENCR_KEY_SIZE = self.negotiated_encryption_algorithm_key_size//8
        
        KEY_LENGHT_TOTAL = PRF_KEY_SIZE + 2*AUTH_KEY_SIZE + 2*ENCR_KEY_SIZE

        KEY_STREAM = self.prf_plus(self.negotiated_prf,SKEYSEED,STREAM,KEY_LENGHT_TOTAL)
        
        self.SK_D  = KEY_STREAM[0:PRF_KEY_SIZE]
        self.SK_AI = KEY_STREAM[PRF_KEY_SIZE:PRF_KEY_SIZE+AUTH_KEY_SIZE]
        self.SK_AR = KEY_STREAM[PRF_KEY_SIZE+AUTH_KEY_SIZE:PRF_KEY_SIZE+2*AUTH_KEY_SIZE]
        self.SK_EI = KEY_STREAM[PRF_KEY_SIZE+2*AUTH_KEY_SIZE:PRF_KEY_SIZE+2*AUTH_KEY_SIZE+ENCR_KEY_SIZE]
        self.SK_ER = KEY_STREAM[PRF_KEY_SIZE+2*AUTH_KEY_SIZE+ENCR_KEY_SIZE:PRF_KEY_SIZE+2*AUTH_KEY_SIZE+2*ENCR_KEY_SIZE]
   
        self.print_ikev2_decryption_table()
        
    def prf_plus(self,algorithm,key,stream,size):
        hash = self.prf_function.get(algorithm)  
        t = b''
        t_total = b''
        iter = 1
        while len(t_total)<size:
            h = hmac.HMAC(key,hash)
            h.update(t + stream + bytes([iter]))
            t = h.finalize()
            t_total += t
            iter += 1
    
        return t_total[0:size]
 

    def sha1_nat_source(self,print_info=True):
        digest = hashes.Hash(hashes.SHA1())
        if self.userplane_mode == ESP_PROTOCOL:
            digest.update(self.ike_spi_initiator + self.ike_spi_responder + socket.inet_pton(socket.AF_INET,self.source_address) + struct.pack('!H',self.port))    
        else: #NAT_TRAVERSAL
            digest.update(self.ike_spi_initiator + self.ike_spi_responder + socket.inet_pton(socket.AF_INET,self.source_address) + struct.pack('!H',self.port_nat))
        hash = digest.finalize()
        if print_info == True: print('NAT SOURCE',toHex(hash))
        return hash

    def sha1_nat_destination(self, print_info=True):
        digest = hashes.Hash(hashes.SHA1())
        if self.userplane_mode == ESP_PROTOCOL:
            digest.update(self.ike_spi_initiator + self.ike_spi_responder + socket.inet_pton(socket.AF_INET,self.epdg_address) + struct.pack('!H',self.port))    
        else: #NAT_TRAVERSAL
            digest.update(self.ike_spi_initiator + self.ike_spi_responder + socket.inet_pton(socket.AF_INET,self.epdg_address) + struct.pack('!H',self.port_nat))
        hash = digest.finalize()
        if print_info == True: print('NAT DESTINATION',toHex(hash))
        return hash

 
#### MESSAGES ####

    def create_IKE_SA_INIT(self, same_spi = False, cookie = False):
        #create SPIi
        if same_spi == False: self.ike_spi_initiator = self.return_random_bytes(8)
        self.ike_spi_responder = (0).to_bytes(8,'big')
        if cookie == False:
            header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, SA, 2, 0, IKE_SA_INIT, (0,0,1), self.message_id_request)
            payload = b''
        else:
            header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, N, 2, 0, IKE_SA_INIT, (0,0,1), self.message_id_request)
            payload = self.encode_generic_payload_header(SA,0,self.encode_payload_type_n(RESERVED,b'',COOKIE,self.cookie_received_bytes)) 
            
        payload += self.encode_generic_payload_header(KE,0,self.encode_payload_type_sa(self.sa_list))

        payload += self.encode_generic_payload_header(NINR,0,self.encode_payload_type_ke())

        # G1: optionally advertise IKEv2 fragmentation (RFC 7383) as the LAST payload. When we do,
        # whatever payload currently terminates the chain (next_payload=NONE) must instead point to
        # N, and the new fragmentation notify (no data, protocol RESERVED) becomes the NONE
        # terminator. Additive/optional: an ePDG that doesn't support it simply ignores the notify.
        frag = self.fragmentation_enabled
        last_np = N if frag else NONE
        if self.check_nat == False:
            if cookie == True:
                payload += self.encode_generic_payload_header(last_np,0,self.nounce)
            else:
                payload += self.encode_generic_payload_header(last_np,0,self.encode_payload_type_ninr())
        else:
            if cookie == True:
                payload += self.encode_generic_payload_header(N,0,self.nounce)
            else:
                payload += self.encode_generic_payload_header(N,0,self.encode_payload_type_ninr())

            payload += self.encode_generic_payload_header(N,0,self.encode_payload_type_n(RESERVED,b'',NAT_DETECTION_SOURCE_IP,self.sha1_nat_source()))
            payload += self.encode_generic_payload_header(last_np,0,self.encode_payload_type_n(RESERVED,b'',NAT_DETECTION_DESTINATION_IP,self.sha1_nat_destination()))
        if frag:
            payload += self.encode_generic_payload_header(NONE,0,self.encode_payload_type_n(RESERVED,b'',IKEV2_FRAGMENTATION_SUPPORTED))
        packet = self.set_ike_packet_length(header+payload)
        return packet

    def create_IKE_AUTH(self):
        header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, IDI, 2, 0, IKE_AUTH, (0,0,1), self.message_id_request)
        payload = self.encode_generic_payload_header(IDR,0,self.encode_payload_type_idi())
        payload += self.encode_generic_payload_header(CP,0,self.encode_payload_type_idr())        
        payload += self.encode_generic_payload_header(SA,0,self.encode_payload_type_cp())   
        payload += self.encode_generic_payload_header(TSI,0,self.encode_payload_type_sa(self.sa_list_child))         
        payload += self.encode_generic_payload_header(TSR,0,self.encode_payload_type_tsi())          
        payload += self.encode_generic_payload_header(N,0,self.encode_payload_type_tsr())
        # VoWiFi engine addition: INITIAL_CONTACT tells the ePDG this is a fresh attach so it
        # deletes any older IKE SA it still holds for this subscriber (e.g. after a hard kill),
        # avoiding single-SA-per-subscriber conflicts on reconnect. Standard IKEv2 behaviour
        # (strongSwan sends it too).
        payload += self.encode_generic_payload_header(N,0,self.encode_payload_type_n(RESERVED,b'',INITIAL_CONTACT))
        # P0-3 (optional, env SWU_PCSCF_RESELECTION_SUPPORT=1): advertise UE support for the
        # P-CSCF restoration extension (TS 24.302 7.2.2.1) so the ePDG may enable P-CSCF
        # reselection. Default OFF for carrier compatibility (Telus is CFG/notify-sensitive).
        # Inserted BEFORE the terminating EAP_ONLY_AUTHENTICATION notify so it stays a mid-chain
        # Notify (next_payload=N); the final notify remains the sole next_payload=NONE terminator.
        if os.environ.get("SWU_PCSCF_RESELECTION_SUPPORT", "0") not in ("0", "", "no"):
            payload += self.encode_generic_payload_header(N,0,self.encode_payload_type_n(RESERVED,b'',P_CSCF_RESELECTION_SUPPORT))
        payload += self.encode_generic_payload_header(NONE,0,self.encode_payload_type_n(RESERVED,b'',EAP_ONLY_AUTHENTICATION))
        packet = self.set_ike_packet_length(header+payload)        
        
        encrypted_and_integrity_packet = self.encode_payload_type_sk(packet)     
        return encrypted_and_integrity_packet

    def create_IKE_AUTH_EAP_IDENTITY(self):
        header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, EAP, 2, 0, IKE_AUTH, (0,0,1), self.message_id_request)        
        payload = self.encode_generic_payload_header(NONE,0,self.encode_payload_type_eap())        
        packet = self.set_ike_packet_length(header+payload)        
        
        encrypted_and_integrity_packet = self.encode_payload_type_sk(packet)                       
        return encrypted_and_integrity_packet


    def create_IKE_AUTH_2(self):
        header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, EAP, 2, 0, IKE_AUTH, (0,0,1), self.message_id_request)
        if self.device_identity_requested:
            payload  = self.encode_generic_payload_header(N,0,self.encode_payload_type_eap())
            payload += self.encode_generic_payload_header(NONE,0,self.encode_payload_type_n(RESERVED,b'',DEVICE_IDENTITY,self.encode_device_identity_notification_data()))
            self.device_identity_requested = False
        else:
            payload = self.encode_generic_payload_header(NONE,0,self.encode_payload_type_eap())
        packet = self.set_ike_packet_length(header+payload)

        encrypted_and_integrity_packet = self.encode_payload_type_sk(packet)
        return encrypted_and_integrity_packet

    def create_IKE_AUTH_3(self):
        header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, AUTH, 2, 0, IKE_AUTH, (0,0,1), self.message_id_request)
        payload = self.encode_generic_payload_header(NONE,0,self.encode_payload_type_auth(SHARED_KEY_MESSAGE_INTEGRITY_CODE))
        packet = self.set_ike_packet_length(header+payload)

        encrypted_and_integrity_packet = self.encode_payload_type_sk(packet)
        return encrypted_and_integrity_packet
        

    def answer_INFORMATIONAL_delete(self):
        if self.old_ike_message_received == True:    
            header = self.encode_header(self.ike_spi_initiator_old, self.ike_spi_responder_old, NONE, 2, 0, INFORMATIONAL, (1,0,1), self.ike_decoded_header['message_id'])        
        else:           
            header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, NONE, 2, 0, INFORMATIONAL, (1,0,1), self.ike_decoded_header['message_id'])        
        
        packet = self.set_ike_packet_length(header)        
        
        encrypted_and_integrity_packet = self.encode_payload_type_sk(packet)                       
        return encrypted_and_integrity_packet

    def answer_INFORMATIONAL_delete_CHILD(self,protocol,spi_list = b''):

        header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, D, 2, 0, INFORMATIONAL, (1,0,1), self.ike_decoded_header['message_id'])

        payload = self.encode_generic_payload_header(NONE,0,self.encode_payload_type_d(protocol,spi_list))
        packet = self.set_ike_packet_length(header+payload)

        encrypted_and_integrity_packet = self.encode_payload_type_sk(packet)
        return encrypted_and_integrity_packet

    def answer_INFORMATIONAL_error(self, notify_type):
        """P2-2: INFORMATIONAL response carrying a single error Notify (e.g. INVALID_SPI) for an
        ePDG INFORMATIONAL request we cannot satisfy. Responder flags (1,0,1); echoes the request
        message-id and mirrors the (possibly old) IKE SPIs like answer_INFORMATIONAL_delete."""
        if self.old_ike_message_received == True:
            header = self.encode_header(self.ike_spi_initiator_old, self.ike_spi_responder_old, N, 2, 0, INFORMATIONAL, (1,0,1), self.ike_decoded_header['message_id'])
        else:
            header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, N, 2, 0, INFORMATIONAL, (1,0,1), self.ike_decoded_header['message_id'])
        payload = self.encode_generic_payload_header(NONE, 0, self.encode_payload_type_n(RESERVED, b'', notify_type))
        packet = self.set_ike_packet_length(header + payload)
        return self.encode_payload_type_sk(packet)

    def map_deleted_esp_spis_to_ue_inbound(self, peer_spi_list):
        """P2-2: map the ePDG's ESP SPIs from a received DELETE to the UE's corresponding INBOUND
        SPIs for the response. RFC 7296 1.4.1: a DELETE lists the sender's inbound SPIs (= the SPIs
        WE send to = self.spi_resp_child); our response must list OUR inbound SPIs (= what the ePDG
        sends to = self.spi_init_child). Single-bearer mapping: spi_resp_child -> spi_init_child and
        spi_resp_child_old -> spi_init_child_old. `peer_spi_list` is a list of 4-byte SPIs (as
        decoded by decode_payload_type_d). Returns (all_known: bool, response_spi_list: list[bytes]).
        Future multiple-bearer support looks this up per bearer context."""
        mapping = {}
        if getattr(self, "spi_resp_child", None) and getattr(self, "spi_init_child", None):
            mapping[bytes(self.spi_resp_child)] = bytes(self.spi_init_child)
        if getattr(self, "spi_resp_child_old", None) and getattr(self, "spi_init_child_old", None):
            mapping[bytes(self.spi_resp_child_old)] = bytes(self.spi_init_child_old)
        response = []
        all_known = True
        for spi in peer_spi_list:
            ue_inbound = mapping.get(bytes(spi))
            if ue_inbound is None:
                all_known = False
            else:
                response.append(ue_inbound)
        return (all_known, response)

    def answer_INFORMATIONAL_empty(self):
        """Empty INFORMATIONAL response (no payloads). Used to acknowledge a Dead-Peer-Detection
        liveness check (RFC 7296 clause 2.4) and any ePDG-initiated INFORMATIONAL request we do
        not need to add payloads to. Mirrors the responder SPIs/message-id of the request."""
        if self.old_ike_message_received == True:
            header = self.encode_header(self.ike_spi_initiator_old, self.ike_spi_responder_old, NONE, 2, 0, INFORMATIONAL, (1,0,1), self.ike_decoded_header['message_id'])
        else:
            header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, NONE, 2, 0, INFORMATIONAL, (1,0,1), self.ike_decoded_header['message_id'])
        packet = self.set_ike_packet_length(header)
        return self.encode_payload_type_sk(packet)

    def answer_INFORMATIONAL_device_identity(self):
        """INFORMATIONAL response carrying a DEVICE_IDENTITY Notify (3GPP TS 24.302 clause 7.2.6):
        the ePDG asked for the Mobile Equipment Identity after tunnel establishment via an
        INFORMATIONAL request with an empty-value DEVICE_IDENTITY notify; answer with our
        IMEISV (preferred) or IMEI."""
        header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, N, 2, 0, INFORMATIONAL, (1,0,1), self.ike_decoded_header['message_id'])
        payload = self.encode_generic_payload_header(NONE,0,self.encode_payload_type_n(RESERVED,b'',DEVICE_IDENTITY,self.encode_device_identity_notification_data()))
        packet = self.set_ike_packet_length(header+payload)
        return self.encode_payload_type_sk(packet)

    def answer_INFORMATIONAL_notify(self, notify_message_type, notification_data=b''):
        """INFORMATIONAL response carrying a single arbitrary Notify. Generic helper for
        acknowledging ePDG-initiated status requests where we must echo a specific notify."""
        header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, N, 2, 0, INFORMATIONAL, (1,0,1), self.ike_decoded_header['message_id'])
        payload = self.encode_generic_payload_header(NONE,0,self.encode_payload_type_n(RESERVED,b'',notify_message_type,notification_data))
        packet = self.set_ike_packet_length(header+payload)
        return self.encode_payload_type_sk(packet)

    def encode_configuration_payload_reply(self, cfg_type, attribute_types):
        """Encode a Configuration Payload (CP) with the given cfg_type (CFG_REPLY/CFG_ACK) that
        echoes each attribute type with LENGTH 0 (no value). Used for the untrusted-WLAN P-CSCF
        restoration procedure (3GPP TS 24.302 clause 7.2.3.2 / TS 23.380): the ePDG sends a
        CFG_REQUEST with P_CSCF_IP*_ADDRESS attributes and the UE must reply CFG_REPLY echoing
        those attribute types with zero-length value. Does NOT touch self.cp_list (which holds the
        initial IKE_AUTH request attributes)."""
        payload_cp = bytes([cfg_type]) + b'\x00\x00\x00'
        for at in attribute_types:
            payload_cp += struct.pack("!H", at) + struct.pack("!H", 0)   # type + length 0
        return payload_cp

    def answer_INFORMATIONAL_cfg_reply(self, attribute_types):
        """INFORMATIONAL response carrying a CFG_REPLY that echoes the requested P-CSCF (and any
        other) attribute types with length 0 — the acknowledgement half of the ePDG-initiated
        P-CSCF restoration procedure (see encode_configuration_payload_reply)."""
        header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, CP, 2, 0, INFORMATIONAL, (1,0,1), self.ike_decoded_header['message_id'])
        payload = self.encode_generic_payload_header(NONE, 0, self.encode_configuration_payload_reply(CFG_REPLY, attribute_types))
        packet = self.set_ike_packet_length(header + payload)
        return self.encode_payload_type_sk(packet)

    def log_notify_error(self, code, context=""):
        """Log a received Private/RFC7296 error-type Notify with its friendly name + description
        (3GPP TS 24.302 Table 8.1.2.2-1). Central place so every auth-flow error site renders the
        numeric code in human terms instead of a bare integer."""
        name = notify_name(code)
        desc = notify_describe(code)
        where = (" during %s" % context) if context else ""
        if desc:
            swu_log("received ERROR Notify%s: %s (%d) - %s" % (where, name, code, desc))
        else:
            swu_log("received ERROR Notify%s: %s (%d)" % (where, name, code))

    def _scan_backoff_timer(self, payload_list):
        """Scan a decoded SK payload list for a BACKOFF_TIMER Notify (3GPP TS 24.302 8.2.9.1) that
        the ePDG may attach to a reject, and return its value in seconds (or None). Used by the
        reject-classification path so a network-supplied Tw3 can be surfaced/honoured."""
        for j in payload_list:
            if j[0] == N and j[1][1] == BACKOFF_TIMER:
                ndata = j[1][3]
                if ndata:
                    secs, _txt = self.decode_backoff_timer(ndata[-1])
                    return secs
        return None

    def note_reject(self, code, payload_list=None):
        """Record a hard reject Notify for start_ike + the manager: classify its retry policy
        (7.2.2.2) and capture any attached BACKOFF_TIMER. Also log the classification. Central so
        every auth-flow error return can call it consistently before returning OTHER_ERROR."""
        self.reject_reason_code = notify_name(code)
        self.reject_reason_policy = reject_policy(code)
        if payload_list is not None:
            self.reject_backoff_seconds = self._scan_backoff_timer(payload_list)
        bo = (" backoff=%ss" % self.reject_backoff_seconds) if self.reject_backoff_seconds else ""
        swu_log("reject classified: %s (%d) policy=%s%s" %
                (self.reject_reason_code, code, self.reject_reason_policy, bo))

    @staticmethod
    def decode_backoff_timer(value_byte):
        """Decode a one-octet GPRS timer 3 value (3GPP TS 24.008 clause 10.5.7.4a) carried in a
        BACKOFF_TIMER Notify (clause 8.2.9.1). Returns (seconds_or_None, human_text).
        Unit is bits 6-8, timer value is bits 1-5. Unit 7 = deactivated/no timer."""
        unit = (value_byte >> 5) & 0x07
        val = value_byte & 0x1F
        if unit == 0:   return (val * 2,      "%d s" % (val * 2))          # 2 s increments
        if unit == 1:   return (val * 60,     "%d min" % val)              # 1 min increments
        if unit == 2:   return (val * 600,    "%d min" % (val * 10))       # 10 min increments
        if unit == 3:   return (val * 3600,   "%d h" % val)                # 1 hour increments
        if unit == 4:   return (val * 36000,  "%d h" % (val * 10))         # 10 hour increments
        if unit == 5:   return (val * 120,    "%d min" % (val * 2))        # 2 min increments  (release 10+)
        if unit == 6:   return (val * 30,     "%d s" % (val * 30))         # 30 s increments
        return (None, "deactivated")                                       # unit == 7

    def handle_INFORMATIONAL_request(self):
        """Handle an ePDG-initiated INFORMATIONAL *request* received while CONNECTED that is NOT
        a DELETE (those go through state_delete). Covers:
          - Dead-Peer-Detection liveness check (empty request)  -> empty response  [RFC 7296 2.4]
          - DEVICE_IDENTITY request (empty value)               -> answer IMEISV/IMEI [TS 24.302 7.2.6]
          - P-CSCF restoration: CFG_REQUEST with P_CSCF addrs   -> CFG_REPLY (len-0) + re-register
                                                                   [TS 24.302 7.2.3.2 / TS 23.380]
          - REACTIVATION_REQUESTED_CAUSE (usually with DELETE, handled there)
          - any other status notify (bearer mod, PTI, ...)      -> acknowledge with empty response
        Every payload is logged with its friendly name so operators can see what the ePDG asked.
        Returns True if a response was sent, False if the message was not an INFORMATIONAL request
        we recognised (caller may fall through)."""
        device_identity_requested = False
        saw_notify = False
        payload_names = []
        cfg_request = None          # (cfg_type, attribute_list) if a CP is present
        for i in self.decoded_payload[0][1]:
            if i[0] == CP:
                cfg_request = i[1]  # [cfg_type, attribute_list]
            elif i[0] == N:
                saw_notify = True
                ntype = i[1][1]
                ndata = i[1][3]
                payload_names.append(notify_name(ntype))
                if ntype == DEVICE_IDENTITY:
                    # Identity Value empty (only the 1-byte identity type present, or nothing) =>
                    # the ePDG is asking us for the ME identity.
                    id_type = ndata[-1] if ndata else None
                    # notification_data layout for a request is [len(2)][identity type(1)] with no
                    # value; treat <=3 bytes as "value empty" -> a request, not an echo of ours.
                    if ndata is None or len(ndata) <= 3:
                        device_identity_requested = True
                        # honour the requested identity type (0x01 IMEI / 0x02 IMEISV); default IMEISV
                        self.device_identity_type = id_type if id_type in (0x01, 0x02) else 0x02
                elif ntype == BACKOFF_TIMER:
                    secs, txt = (None, "")
                    if ndata and len(ndata) >= 1:
                        secs, txt = self.decode_backoff_timer(ndata[-1])
                    swu_log("INFORMATIONAL: BACKOFF_TIMER = %s" % (txt or "?"))

        # P-CSCF restoration (ePDG-initiated modification): a CFG_REQUEST carrying P-CSCF address
        # attribute(s). Per TS 24.302 7.2.3.2 the UE MUST answer with a CFG_REPLY echoing those
        # attribute types with length 0, then restore (re-register) — the new P-CSCF value, when
        # provided, becomes the active one. Handle this BEFORE DEVICE_IDENTITY: a restoration
        # message is its own exchange.
        if cfg_request is not None:
            return self.handle_pcscf_restoration(cfg_request)

        # A DEVICE_IDENTITY request wins over a plain status ack: answer with our identity.
        if device_identity_requested:
            swu_log("INFORMATIONAL request: ePDG asked for DEVICE_IDENTITY; answering")
            self.send_data(self.answer_INFORMATIONAL_device_identity())
            return True

        if saw_notify:
            swu_log("INFORMATIONAL request (status: %s); acknowledging" % ", ".join(payload_names))
            self.send_data(self.answer_INFORMATIONAL_empty())
            return True

        # No payloads at all => Dead-Peer-Detection liveness probe. Must answer or the ePDG tears
        # the tunnel down for being unresponsive.
        swu_log("INFORMATIONAL request (DPD liveness check); answering")
        self.send_data(self.answer_INFORMATIONAL_empty())
        return True

    def handle_pcscf_restoration(self, cfg_request):
        """P-CSCF restoration over untrusted WLAN (3GPP TS 24.302 clause 7.2.3.2, TS 23.380).
        cfg_request = [cfg_type, attribute_list]; attribute_list entries are (type, value...) as
        decoded by decode_payload_type_cp (length-0 attrs decode to (type, b'')).

        The UE must:
          1) reply with an INFORMATIONAL response containing a CFG_REPLY that echoes EVERY
             requested attribute type with length 0 (no value), and
          2) apply the restoration: if the ePDG supplied a new P-CSCF address, adopt it and
             re-render pjsip + re-register so SIP signalling uses the restored P-CSCF; if no
             address was supplied, just trigger a re-register against the current P-CSCF."""
        cfg_type = cfg_request[0]
        attrs = cfg_request[1] if len(cfg_request) > 1 else []
        attr_types = [a[0] for a in attrs]
        # New P-CSCF address(es) supplied in the request, if any (value present).
        new_pcscf6 = [a[1] for a in attrs if a[0] == P_CSCF_IP6_ADDRESS and len(a) > 1 and a[1]]
        new_pcscf4 = [a[1] for a in attrs if a[0] == P_CSCF_IP4_ADDRESS and len(a) > 1 and a[1]]
        names = [self.cp_attr_name(t) for t in attr_types]
        swu_log("INFORMATIONAL request: P-CSCF restoration (CFG_REQUEST %s); replying CFG_REPLY "
                "(len 0) + restoring" % ", ".join(names))

        # 1) Acknowledge with a CFG_REPLY echoing the requested attribute types at length 0.
        self.send_data(self.answer_INFORMATIONAL_cfg_reply(attr_types))

        # 2) Apply the restoration. Prefer a v6 P-CSCF (Telus/IMS is v6), fall back to v4.
        new_pcscf = (new_pcscf6[0] if new_pcscf6 else (new_pcscf4[0] if new_pcscf4 else None))
        if new_pcscf:
            # Adopt the new P-CSCF at the head of the appropriate list so bring-up/reconnect
            # reporting stays consistent, then push it to pjsip (idempotent if unchanged).
            if new_pcscf6:
                self.pcscfv6_address_list = [new_pcscf] + [a for a in getattr(self, "pcscfv6_address_list", []) if a != new_pcscf]
            else:
                self.pcscf_address_list = [new_pcscf] + [a for a in getattr(self, "pcscf_address_list", []) if a != new_pcscf]
            swu_log("P-CSCF restoration: new P-CSCF %s" % new_pcscf)
            swu_write_pcscf(new_pcscf)
            swu_notify("pcscf", new_pcscf)
            swu_apply_pcscf(new_pcscf)
        else:
            # No new address: restoration is a re-register against the current P-CSCF.
            swu_log("P-CSCF restoration: no new address supplied; re-registering")
            try:
                subprocess.call(["asterisk", "-rx", "pjsip send register volte_ims"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                swu_log("re-register failed: %r" % e)
        return True

    @staticmethod
    def cp_attr_name(t):
        """Friendly name for a Configuration Payload attribute type (for logging)."""
        return {
            INTERNAL_IP4_ADDRESS: "INTERNAL_IP4_ADDRESS", INTERNAL_IP4_DNS: "INTERNAL_IP4_DNS",
            INTERNAL_IP6_ADDRESS: "INTERNAL_IP6_ADDRESS", INTERNAL_IP6_DNS: "INTERNAL_IP6_DNS",
            P_CSCF_IP4_ADDRESS: "P_CSCF_IP4_ADDRESS", P_CSCF_IP6_ADDRESS: "P_CSCF_IP6_ADDRESS",
        }.get(t, "CFG_ATTR_%d" % t)




    def create_INFORMATIONAL_delete(self,protocol,spi_list = b''):

        header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, D, 2, 0, INFORMATIONAL, (0,0,1), self.message_id_request)

        payload = self.encode_generic_payload_header(NONE,0,self.encode_payload_type_d(protocol,spi_list))
        packet = self.set_ike_packet_length(header+payload)

        encrypted_and_integrity_packet = self.encode_payload_type_sk(packet)
        return encrypted_and_integrity_packet


    def create_INFORMATIONAL_liveness(self):
        """G4: an initiator-side Dead-Peer-Detection probe — an empty INFORMATIONAL *request*
        (no payloads) per RFC 7296 clause 2.4 / 3GPP TS 24.302 clause 7.2.2A. Uses the request
        message-id counter and initiator flags (0,0,1); a live ePDG must answer with an
        INFORMATIONAL response bearing the same message-id and responder flags. Distinct from the
        RFC 3948 NAT-T keepalive (which is unacknowledged and proves nothing about SA liveness).
        The empty SK payload carries only encrypted padding, exactly like answer_INFORMATIONAL_empty
        but with a request header."""
        self.message_id_request += 1
        header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, NONE, 2, 0, INFORMATIONAL, (0,0,1), self.message_id_request)
        packet = self.set_ike_packet_length(header)
        return self.encode_payload_type_sk(packet)


    def answer_CREATE_CHILD_SA(self):
        
        header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, SA, 2, 0, CREATE_CHILD_SA, (1,0,1), self.ike_decoded_header['message_id'])        
       
        payload = self.encode_generic_payload_header(KE,0,self.encode_payload_type_sa(self.sa_list_create_child_sa))
        payload += self.encode_generic_payload_header(NINR,0,self.encode_payload_type_ke())  
        payload += self.encode_generic_payload_header(NONE,0,self.encode_payload_type_ninr())          
        packet = self.set_ike_packet_length(header+payload)   
        
        encrypted_and_integrity_packet = self.encode_payload_type_sk(packet)                       
        return encrypted_and_integrity_packet        


    def answer_NOTIFY_NO_PROPOSAL_CHOSEN(self):

        header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, N, 2, 0, CREATE_CHILD_SA, (1,0,1), self.ike_decoded_header['message_id'])

        payload = self.encode_generic_payload_header(NONE,0,self.encode_payload_type_n(IKE,b'',NO_PROPOSAL_CHOSEN))
        packet = self.set_ike_packet_length(header+payload)

        encrypted_and_integrity_packet = self.encode_payload_type_sk(packet)
        return encrypted_and_integrity_packet

    def classify_create_child_request(self, payloads):
        """P2-1: classify an ePDG-initiated CREATE_CHILD_SA request (TS 24.302 / RFC 7296 1.3):
          "ike_rekey"          - SA protocol ID is IKE (rekey the IKE SA)
          "esp_rekey"          - SA protocol ID is ESP and a REKEY_SA notify is present
          "additional_bearer"  - SA protocol ID is ESP with NO REKEY_SA (with or without EPS_QOS/
                                 TFT): a new/additional child SA we do not support
          "unknown"            - anything else
        `payloads` is self.decoded_payload[0][1] (the inner payload list)."""
        sa_protocol = None
        has_rekey = False
        for i in payloads:
            if i[0] == SA:
                sa_protocol = i[1][1]        # protocol_id
            elif i[0] == N and i[1][1] == REKEY_SA:
                has_rekey = True
        if sa_protocol == IKE:
            return "ike_rekey"
        if sa_protocol == ESP:
            return "esp_rekey" if has_rekey else "additional_bearer"
        return "unknown"

    def answer_CREATE_CHILD_SA_error(self, notify_type):
        """P2-1: respond to an ePDG-initiated CREATE_CHILD_SA we cannot satisfy with a single
        error Notify (NO_ADDITIONAL_SAS / NO_PROPOSAL_CHOSEN / INVALID_SPI). These are RFC 7296
        error notifies carrying no 3GPP-specific data. Responder flags (1,0,1); echoes the
        request's message-id."""
        header = self.encode_header(
            self.ike_spi_initiator, self.ike_spi_responder, N, 2, 0, CREATE_CHILD_SA,
            (1, 0, 1), self.ike_decoded_header['message_id'])
        payload = self.encode_generic_payload_header(
            NONE, 0, self.encode_payload_type_n(RESERVED, b'', notify_type))
        packet = self.set_ike_packet_length(header + payload)
        return self.encode_payload_type_sk(packet)


    def create_CREATE_CHILD_SA(self, lowest = 0):
       
        header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, SA, 2, 0, CREATE_CHILD_SA, (0,0,1), self.message_id_request)        
        
        payload = self.encode_generic_payload_header(KE,0,self.encode_payload_type_sa(self.sa_list_create_child_sa))
        payload += self.encode_generic_payload_header(NINR,0,self.encode_payload_type_ke())  
        payload += self.encode_generic_payload_header(NONE,0,self.encode_payload_type_ninr(lowest))          
        packet = self.set_ike_packet_length(header+payload)   
        
        encrypted_and_integrity_packet = self.encode_payload_type_sk(packet)                       
        return encrypted_and_integrity_packet   


    def create_CREATE_CHILD_SA_CHILD(self,lowest = 0):

        header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, SA, 2, 0, CREATE_CHILD_SA, (0,0,1), self.message_id_request)

        payload = self.encode_generic_payload_header(NINR,0,self.encode_payload_type_sa(self.sa_list_create_child_sa_child))
        payload += self.encode_generic_payload_header(N,0,self.encode_payload_type_ninr(lowest))
        payload += self.encode_generic_payload_header(TSI,0,self.encode_payload_type_n(ESP,self.spi_init_child,REKEY_SA))
        payload += self.encode_generic_payload_header(TSR,0,self.encode_payload_type_tsi())
        payload += self.encode_generic_payload_header(NONE,0,self.encode_payload_type_tsr())

        packet = self.set_ike_packet_length(header+payload)

        encrypted_and_integrity_packet = self.encode_payload_type_sk(packet)
        return encrypted_and_integrity_packet

    def create_CREATE_CHILD_SA_CHILD_pfs(self, lowest = 0):
        """UE-initiated CHILD_SA (ESP) rekey WITH PFS. Same as create_CREATE_CHILD_SA_CHILD but the
        proposal carries a D-H transform and the message includes a KE payload, so the new ESP keys
        are derived from a fresh DH exchange (RFC 7296 2.17). Telus rejects a no-PFS child rekey with
        NO_PROPOSAL_CHOSEN, so proactive UE rekey must offer PFS. Payload order per RFC 7296:
        SA, Ni, KEi, TSi, TSr. The DH keypair must already be generated (state_ue_rekey_child)."""
        header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, SA, 2, 0, CREATE_CHILD_SA, (0,0,1), self.message_id_request)
        # next_payload chain: SA -> Ni -> KE -> TSi -> TSr -> (none)
        payload  = self.encode_generic_payload_header(NINR,0,self.encode_payload_type_sa(self.sa_list_create_child_sa_child))
        payload += self.encode_generic_payload_header(KE,0,self.encode_payload_type_ninr(lowest))          # Ni
        payload += self.encode_generic_payload_header(N,0,self.encode_payload_type_ke())                   # KEi
        payload += self.encode_generic_payload_header(TSI,0,self.encode_payload_type_n(ESP,self.spi_init_child,REKEY_SA))
        payload += self.encode_generic_payload_header(TSR,0,self.encode_payload_type_tsi())
        payload += self.encode_generic_payload_header(NONE,0,self.encode_payload_type_tsr())
        packet = self.set_ike_packet_length(header+payload)
        return self.encode_payload_type_sk(packet)

    def _child_sa_list_with_dh(self, group):
        """The negotiated child proposal (ENCR/INTEG/ESN from IKE_AUTH) with one D-H transform.

        Index 0 of a proposal is its [protocol_id, spi_size] header, not a transform, and is
        preserved: only transform entries are filtered.
        """
        base = [list(t) for t in self.sa_list_negotiated_child[0]]   # shallow-copy transforms
        # Drop any existing D_H (defensive) then append the requested group.
        base = [t for t in base if not (len(t) >= 1 and t[0] == D_H)]
        base.append([D_H, group])
        return [base]

    def _child_sa_list_with_pfs(self):
        """Proposal for our own PFS CHILD_SA rekey: MODP_2048, the group Telus accepts on
        rekey (see render.py default_esp note)."""
        return self._child_sa_list_with_dh(MODP_2048_bit)

    def answer_CREATE_CHILD_SA_rekey(self, sa_list, with_ke):
        """Responder side of an ePDG-initiated ESP CHILD_SA rekey: SA, Nr, [KEr], TSi, TSr.

        encode_payload_type_sa allocates the SPI we advertise, which becomes OUR new inbound
        SPI; encode_payload_type_ninr sets self.nounce to the Nr we send. Both are read back
        by the caller, so this must be sent (and its side effects kept) exactly once — the
        duplicate-request cache in _dispatch_epdg_request guarantees that for retransmissions.
        No REKEY_SA notify: that payload belongs to whoever initiated the exchange.
        """
        header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, SA, 2, 0,
                                    CREATE_CHILD_SA, (1, 0, 1),
                                    self.ike_decoded_header['message_id'])
        payload = self.encode_generic_payload_header(NINR, 0, self.encode_payload_type_sa(sa_list))
        payload += self.encode_generic_payload_header(KE if with_ke else TSI, 0,
                                                      self.encode_payload_type_ninr())
        if with_ke:
            payload += self.encode_generic_payload_header(TSI, 0, self.encode_payload_type_ke())
        payload += self.encode_generic_payload_header(TSR, 0, self.encode_payload_type_tsi())
        payload += self.encode_generic_payload_header(NONE, 0, self.encode_payload_type_tsr())
        packet = self.set_ike_packet_length(header + payload)
        return self.encode_payload_type_sk(packet)

    def generate_keying_material_child_responder(self, ke_received):
        """CHILD_SA keymat when the PEER initiated the rekey (RFC 7296 2.17).

        Two things invert relative to the UE-initiated path, and both are silent if wrong —
        the SA installs cleanly and the dataplane simply goes dead:

          nonce order   KEYMAT = prf+(SK_d, [g^ir |] Ni | Nr) with Ni the INITIATOR's nonce.
                        The ePDG initiated, so Ni is the nonce we received and Nr is ours.
          key direction the *_I keys protect initiator->responder traffic, which here is
                        ePDG->UE, i.e. our INBOUND. The caller must therefore install *_R on
                        the encoder and *_I on the decoder — the mirror of the UE-initiated
                        case. The slicing itself is positional and unchanged.
        """
        STREAM = self.nounce_received + self.nounce
        if ke_received:
            STREAM = self.dh_shared_key + STREAM
        AUTH_KEY_SIZE = self.integ_key_len_bytes.get(self.negotiated_integrity_algorithm_child)
        ENCR_KEY_SIZE = self.negotiated_encryption_algorithm_key_size_child // 8
        if self.negotiated_encryption_algorithm_child in (ENCR_AES_GCM_8, ENCR_AES_GCM_12, ENCR_AES_GCM_16):
            ENCR_KEY_SIZE += 4
        KEY_LENGHT_TOTAL = 2*AUTH_KEY_SIZE + 2*ENCR_KEY_SIZE
        KEYMAT = self.prf_plus(self.negotiated_prf, self.SK_D, STREAM, KEY_LENGHT_TOTAL)
        self.SK_IPSEC_EI = KEYMAT[0:ENCR_KEY_SIZE]
        self.SK_IPSEC_AI = KEYMAT[ENCR_KEY_SIZE:ENCR_KEY_SIZE+AUTH_KEY_SIZE]
        self.SK_IPSEC_ER = KEYMAT[ENCR_KEY_SIZE+AUTH_KEY_SIZE:2*ENCR_KEY_SIZE+AUTH_KEY_SIZE]
        self.SK_IPSEC_AR = KEYMAT[2*ENCR_KEY_SIZE+AUTH_KEY_SIZE:2*ENCR_KEY_SIZE+2*AUTH_KEY_SIZE]
        self.print_esp_sa()

    def generate_keying_material_child_pfs(self):
        """CHILD_SA keymat WITH PFS (RFC 7296 2.17): KEYMAT = prf+(SK_d, g^ir | Ni | Nr), where
        g^ir is the NEW DH shared secret from this rekey. (The no-PFS variant omits g^ir.) SPIs,
        algorithm sizes and output slicing are identical to generate_keying_material_child."""
        STREAM = self.dh_shared_key + self.nounce + self.nounce_received
        AUTH_KEY_SIZE = self.integ_key_len_bytes.get(self.negotiated_integrity_algorithm_child)
        ENCR_KEY_SIZE = self.negotiated_encryption_algorithm_key_size_child // 8
        if self.negotiated_encryption_algorithm_child in (ENCR_AES_GCM_8, ENCR_AES_GCM_12, ENCR_AES_GCM_16):
            ENCR_KEY_SIZE += 4
        KEY_LENGHT_TOTAL = 2*AUTH_KEY_SIZE + 2*ENCR_KEY_SIZE
        KEYMAT = self.prf_plus(self.negotiated_prf, self.SK_D, STREAM, KEY_LENGHT_TOTAL)
        self.SK_IPSEC_EI = KEYMAT[0:ENCR_KEY_SIZE]
        self.SK_IPSEC_AI = KEYMAT[ENCR_KEY_SIZE:ENCR_KEY_SIZE+AUTH_KEY_SIZE]
        self.SK_IPSEC_ER = KEYMAT[ENCR_KEY_SIZE+AUTH_KEY_SIZE:2*ENCR_KEY_SIZE+AUTH_KEY_SIZE]
        self.SK_IPSEC_AR = KEYMAT[2*ENCR_KEY_SIZE+AUTH_KEY_SIZE:2*ENCR_KEY_SIZE+2*AUTH_KEY_SIZE]
        self.print_esp_sa()

    def answer_CREATE_CHILD_SA_CHILD(self,lowest = 0):
        
        header = self.encode_header(self.ike_spi_initiator, self.ike_spi_responder, SA, 2, 0, CREATE_CHILD_SA, (1,0,1), self.ike_decoded_header['message_id'])        
        
        payload = self.encode_generic_payload_header(NINR,0,self.encode_payload_type_sa(self.sa_list_create_child_sa_child)) 
        payload += self.encode_generic_payload_header(N,0,self.encode_payload_type_ninr(lowest))          
        payload += self.encode_generic_payload_header(TSI,0,self.encode_payload_type_n(ESP,self.spi_init_child,REKEY_SA))
        payload += self.encode_generic_payload_header(TSR,0,self.encode_payload_type_tsi())          
        payload += self.encode_generic_payload_header(NONE,0,self.encode_payload_type_tsr())   
        
        packet = self.set_ike_packet_length(header+payload)   
        
        encrypted_and_integrity_packet = self.encode_payload_type_sk(packet)                       
        return encrypted_and_integrity_packet  



#### STATES ####

    def state_1(self, retry = False, cookie = False): #Send IKE_SA_INIT and process answer
        self.message_id_request = 0
        
        packet = self.create_IKE_SA_INIT(retry, cookie)

        self.AUTH_SA_INIT_packet = packet #needed for AUTH Payload in state 4

        print('sending IKE_SA_INIT')
        # P2-5: send + await response, retransmitting the same IKE_SA_INIT bytes on timeout.
        if not self._send_request_await_response(packet):
            return TIMEOUT,'TIMEOUT'

        
        if self.ike_decoded_header['exchange_type'] == IKE_SA_INIT:
            print('received IKE_SA_INIT')        
            for i in self.decoded_payload:
                if i[0] == NINR:
                    self.nounce_received = i[1][0]
                
                elif i[0] == SA:
                    proposal = i[1][0]
                    protocol_id = i[1][1]
                    if protocol_id == IKE:
                        self.set_sa_negotiated(proposal)
                    else:
                        return MANDATORY_INFORMATION_MISSING,'MANDATORY_INFORMATION_MISSING'
                
                elif i[0] == KE:
                    dh_peer_public_key_bytes = i[1][1]
                    self.dh_calculate_shared_key(dh_peer_public_key_bytes)
                
                elif i[0] == N:    #protocol_id, notify_message_type, spi, notification_data
                    if i[1][1] == INVALID_KE_PAYLOAD:
                        accepted_dh_group = struct.unpack("!H", i[1][3])[0]
                        self.remove_sa_from_list(accepted_dh_group)
                        return REPEAT_STATE,'INVALID_KE_PAYLOAD'
                    elif i[1][1]<16384: #error
                        self.log_notify_error(i[1][1], "IKE_SA_INIT")
                        self.note_reject(i[1][1], self.decoded_payload)
                        return OTHER_ERROR,str(i[1][1])
                        
                    elif i[1][1] == COOKIE:
                        self.cookie = True
                        self.cookie_received_bytes = i[1][3]
                        return REPEAT_STATE_COOKIE, 'REPEAT SA_INIT WITH COOKIE'
                        
                    elif i[1][1] == NAT_DETECTION_DESTINATION_IP:
                        received_nat_detection_destination = i[1][3]
                        print('NAT DESTINATION RECEIVED',toHex(received_nat_detection_destination))
                        calculated_nat_detection_destination = self.sha1_nat_source(False)
                        print('NAT DESTINATION CALCULATED',toHex(calculated_nat_detection_destination))
                        if received_nat_detection_destination != calculated_nat_detection_destination:
                            self.userplane_mode = NAT_TRAVERSAL
                        
                    elif i[1][1] == NAT_DETECTION_SOURCE_IP:
                        received_nat_detection_source = i[1][3]
                        print('NAT SOURCE RECEIVED',toHex(received_nat_detection_source))
                        calculated_nat_detection_source = self.sha1_nat_destination(False)
                        print('NAT SOURCE CALCULATED',toHex(calculated_nat_detection_source))
                        if received_nat_detection_source != calculated_nat_detection_source:
                            self.userplane_mode = NAT_TRAVERSAL

                    elif i[1][1] == IKEV2_FRAGMENTATION_SUPPORTED:
                        # G1 (RFC 7383): the ePDG echoes this when it can fragment large IKE
                        # messages (e.g. an IKE_AUTH bearing a cert chain). We then accept inbound
                        # SKF fragments and reassemble them (see decode_ike).
                        self.peer_supports_fragmentation = True
                        swu_log("ePDG supports IKEv2 fragmentation (RFC 7383)")
                        
            self.generate_keying_material()
            
            
            return OK,''
        else:
            return DECODING_ERROR,'DECODING_ERROR'


    def state_2(self, retry = False):
        self.message_id_request += 1
        if retry == False:
            packet = self.create_IKE_AUTH()
        else:
            packet = self.create_IKE_AUTH_EAP_IDENTITY()
        print('sending IKE_AUTH (1)')
        # P2-5: send + await, retransmitting the same IKE_AUTH bytes on timeout.
        if not self._send_request_await_response(packet):
            return TIMEOUT,'TIMEOUT'

        eap_received = False
        eap_summary = None
        if self.ike_decoded_header['exchange_type'] == IKE_AUTH and self.decoded_payload[0][0] == SK:
            print('received IKE_AUTH (1)')             
            for i in self.decoded_payload[0][1]:
                
                if i[0] == N:    #protocol_id, notify_message_type, spi, notification_data
                    if i[1][1] == DEVICE_IDENTITY:
                        self.device_identity_requested = True
                        self.device_identity_type = i[1][3][-1] if i[1][3] else None

                    elif i[1][1]<16384: #error
                        code = i[1][1]
                        if code == AUTHENTICATION_FAILED:
                            # The ePDG rejected our identity at IKE_AUTH *before* sending any
                            # EAP-AKA challenge — the SIM was never queried. This is an AAA/HSS
                            # decision, NOT a SIM/PIN problem. Typical causes: the line is not
                            # subscribed/provisioned for VoWiFi, or the ePDG refuses attaches
                            # from this (foreign) source IP. Nothing tunable on our side fixes it.
                            swu_log("IKE_AUTH rejected with AUTHENTICATION_FAILED before any "
                                    "EAP-AKA challenge (SIM not queried). The ePDG/AAA refused "
                                    "the identity: the SIM is likely not provisioned for VoWiFi, "
                                    "or the ePDG blocks this source IP/region.")
                        else:
                            self.log_notify_error(code, "IKE_AUTH")
                        self.note_reject(code, self.decoded_payload[0][1])
                        return OTHER_ERROR,str(code)

                elif i[0] == EAP:
                    eap_summary = 'code=%s type=%s' % (i[1][0], i[1][2] if len(i[1]) > 2 else '-')
                    if i[1][0] in (EAP_REQUEST,) and i[1][2] == EAP_IDENTITY:
                        # Lebara UK's ePDG (234-87) opens with a bare RFC 3748
                        # EAP-Request/Identity before starting EAP-AKA (issue #43);
                        # answer with the NAI and resend, like the AKA-Identity path.
                        self.eap_identifier = i[1][1]
                        identity = (
                                '0'
                                + self.imsi
                                + '@nai.epc.mnc' + self.mnc
                                + '.mcc' + self.mcc
                                + '.3gppnetwork.org'
                        )
                        self.eap_payload_response = build_eap_identity_response(self.eap_identifier, identity)
                        return REPEAT_STATE,'EAP IDENTITY REQUESTED'

                    elif i[1][0] in (EAP_REQUEST,) and i[1][2] in (EAP_AKA,):
                        if i[1][3] in (AKA_Challenge, AKA_Reauthentication):
                            
                            eap_received = True
                            
                            RAND = self.get_eap_aka_attribute_value(i[1][4],AT_RAND)
                            AUTN = self.get_eap_aka_attribute_value(i[1][4],AT_AUTN)
                            MAC = self.get_eap_aka_attribute_value(i[1][4],AT_MAC)
                            
                            VECTOR =  self.get_eap_aka_attribute_value(i[1][4],AT_IV)
                            ENCR_DATA =  self.get_eap_aka_attribute_value(i[1][4],AT_ENCR_DATA)
                           
                            self.eap_identifier = i[1][1]
                                                     
                            if (RAND is not None and AUTN is not None) or (VECTOR is not None and ENCR_DATA is not None):
                                if RAND is not None and AUTN is not None:
                                    self.current_counter = None
                                    res,ck,ik = return_res_ck_ik(self.com_port,toHex(RAND),toHex(AUTN))
                                        
                                    if res is not None and ck is None and ik is None:
                                        # RES is AUTS returned by the USIM after a sequence error.
                                        auts, res = res, None
                                        eap_payload_response = bytes([2]) + bytes([self.eap_identifier]) + fromHex('0018170400000404') + fromHex(auts)
                                        self.eap_payload_response = eap_payload_response
                                        return REPEAT_STATE,'SYNC FAILURE'

                                    elif res is None or ck is None or ik is None:
                                        # Hard SIM AUTHENTICATE failure (0x9862 etc.): don't
                                        # fromHex(None) and crash — reject cleanly.
                                        return self._sim_auth_failed()

                                    else:

                                        self.RES, CK, IK = fromHex(res), fromHex(ck), fromHex(ik)
                                        self.KENCR, self.KAUT, self.MSK, self.EMSK, self.MK = self.eap_keys_calculation(CK,IK)

                                        # Calculate dynamic EAP payload with proper padding
                                        eap_payload_response = self.build_eap_aka_response(self.eap_identifier, self.RES)

                                        h = hmac.HMAC(self.KAUT,hashes.SHA1())
                                        h.update(eap_payload_response)
                                        hash = h.finalize()[0:16]
                                        self.eap_payload_response = eap_payload_response[:-16] + hash
                                    
                                if VECTOR is not None and ENCR_DATA is not None:                                
                                    cipher = Cipher(algorithms.AES(self.KENCR), modes.CBC(VECTOR))
                                    decryptor = cipher.decryptor()
                                    uncipher_data = decryptor.update(ENCR_DATA) + decryptor.finalize()
                                    eap_attributes = self.decode_eap_attributes(uncipher_data)
                                    NEXT_REAUTH_ID = self.get_eap_aka_attribute_value(eap_attributes,AT_NEXT_REAUTH_ID)
                                    COUNTER = self.get_eap_aka_attribute_value(eap_attributes,AT_COUNTER)
                                    NONCE_S = self.get_eap_aka_attribute_value(eap_attributes,AT_NONCE_S)
                                    
                                    
                                    
                                    if NEXT_REAUTH_ID is not None: 
                                        self.next_reauth_id = NEXT_REAUTH_ID.decode('utf-8')
                                    else:
                                        #should use permanent identity next 
                                        self.next_reauth_id = None
                                        
                                        
                                    if COUNTER is not None and NONCE_S is not None:
                                        ERROR = False
                                        if self.current_counter is None:
                                            self.current_counter = COUNTER
                                        else:
                                            if COUNTER > self.current_counter:
                                                self.current_counter = COUNTER
                                                
                                            else:
                                                #error: include AT_COUNTER_TOO_SMALL
                                                ERROR = True
                                                
                                                                            
                                        #XKEY' = SHA1(Identity|counter|NONCE_S| MK)
                                        self.MSK, self.EMSK, self.XKEY = self.eap_keys_calculation_fast_reauth(COUNTER, NONCE_S)
                                        
                                        vector = self.return_random_bytes(16)
                                        at_iv = bytes([AT_IV]) + fromHex('050000') + vector
                                        
                                        if ERROR == False:
                                            at_padding = bytes([AT_PADDING]) + fromHex('0300000000000000000000')
                                            at_counter = bytes([AT_COUNTER]) + b'\x01' + struct.pack('!H',COUNTER)
                                            at_counter_too_small = b''
                                        else:
                                            at_padding = bytes([AT_PADDING]) + fromHex('02000000000000')
                                            at_counter = bytes([AT_COUNTER]) + b'\x01' + struct.pack('!H',COUNTER)
                                            at_counter_too_small = bytes([AT_COUNTER_TOO_SMALL]) + b'\x01\x00\x00' 
                                            
                                        cipher = Cipher(algorithms.AES(self.KENCR), modes.CBC(vector)) 
                                        encryptor = cipher.encryptor()          
                                        cipher_data = encryptor.update(at_counter + at_counter_too_small + at_padding) + encryptor.finalize()                                        
                                        
                                        at_encr_data = bytes([AT_ENCR_DATA]) + fromHex('050000') + cipher_data
                                        length = struct.pack('!H',len(at_iv)+len(at_encr_data)+28)
                                        
                                        eap_payload_response = bytes([2]) + bytes([self.eap_identifier]) + length + fromHex('170d0000') + at_iv + at_encr_data + fromHex('0b050000' + 16*'00')
   
                                        h = hmac.HMAC(self.KAUT,hashes.SHA1())
                                        h.update(eap_payload_response + NONCE_S)
                                        hash = h.finalize()[0:16]  
                                        self.eap_payload_response = eap_payload_response[:-16] + hash
                                    
                            else:
                                return OTHER_ERROR,'NO RAND/AUTN IN EAP'
                        
                        elif i[1][3] in (AKA_Identity,):
                            
                      
                            if requests_permanent_eap_identity(i[1][4]):
                                self.eap_identifier = i[1][1]
                                identity = (
                                        '0'
                                        + self.imsi
                                        + '@nai.epc.mnc' + self.mnc
                                        + '.mcc' + self.mcc
                                        + '.3gppnetwork.org'
                                )
                                self.eap_payload_response = (
                                        bytes([2])
                                        + bytes([self.eap_identifier])
                                        + fromHex("004417050000")
                                        + self.encode_eap_at_identity(identity))

                                # update the EAP length
                                eap = bytearray(self.eap_payload_response)
                                eap_length = struct.pack('>H', len(eap))
                                eap[2] = eap_length[0]
                                eap[3] = eap_length[1]
                                self.eap_payload_response = bytes(eap)

                                return REPEAT_STATE,'EAP IDENTITY REQUESTED'


            if eap_received == True:
                return OK,''               
            elif eap_summary is not None:
                # An EAP payload WAS present — we just don't handle this method.
                return MANDATORY_INFORMATION_MISSING,'UNHANDLED EAP PAYLOAD (%s)' % eap_summary
            else:
                return MANDATORY_INFORMATION_MISSING,'NO EAP PAYLOAD RECEIVED'              
            
        
    def state_3(self):
        self.message_id_request += 1
        packet = self.create_IKE_AUTH_2()
        print('sending IKE_SA_AUTH (2)')
        # P2-5: send + await, retransmitting the same IKE_AUTH(2) bytes on timeout.
        if not self._send_request_await_response(packet):
            return TIMEOUT,'TIMEOUT'

        
        eap_received = False
        if self.ike_decoded_header['exchange_type'] == IKE_AUTH and self.decoded_payload[0][0] == SK:
            print('received IKE_AUTH (2)')              
            for i in self.decoded_payload[0][1]:
                
                if i[0] == N:    #protocol_id, notify_message_type, spi, notification_data
                    if i[1][1]<16384: #error
                        self.log_notify_error(i[1][1], "IKE_AUTH")
                        self.note_reject(i[1][1], self.decoded_payload[0][1])
                        return OTHER_ERROR,str(i[1][1])

                elif i[0] == EAP:
                    eap_received = True
                    if i[1][0] in (EAP_SUCCESS,):
                    
                        hash = self.prf_function.get(self.negotiated_prf) 
                        h = hmac.HMAC(self.SK_PI,hash)
                        h.update(bytes([self.identification_initiator[0]]) + b'\x00'*3 + self.identification_initiator[1].encode('utf-8'))
                        hash_result = h.finalize() 
                        self.AUTH_SA_INIT_packet += self.nounce_received + hash_result
                        
                        keypad = b'Key Pad for IKEv2'
                        h = hmac.HMAC(self.MSK,hash)
                        h.update(keypad)
                        hash_result = h.finalize() 
                        h = hmac.HMAC(hash_result,hash)
                        h.update(self.AUTH_SA_INIT_packet)
                        self.AUTH_payload = h.finalize()                        
                        
                    elif i[1][0] in (EAP_REQUEST,) and i[1][2] in (EAP_AKA,):
                        if i[1][3] in (AKA_Challenge,):
                            
                            RAND = self.get_eap_aka_attribute_value(i[1][4],AT_RAND)
                            AUTN = self.get_eap_aka_attribute_value(i[1][4],AT_AUTN)
                            MAC = self.get_eap_aka_attribute_value(i[1][4],AT_MAC)
                            
                            VECTOR =  self.get_eap_aka_attribute_value(i[1][4],AT_IV)
                            ENCR_DATA =  self.get_eap_aka_attribute_value(i[1][4],AT_ENCR_DATA)
                           
                            self.eap_identifier = i[1][1]
                                                     
                            if (RAND is not None and AUTN is not None) or (VECTOR is not None and ENCR_DATA is not None):
                                if RAND is not None and AUTN is not None:
                                    self.current_counter = None
                                    res,ck,ik = return_res_ck_ik(self.com_port,toHex(RAND),toHex(AUTN))

                                    if res is None or ck is None or ik is None:
                                        # SIM AUTHENTICATE failed (or returned AUTS) on the reauth
                                        # challenge: reject cleanly instead of fromHex(None) crash.
                                        return self._sim_auth_failed()

                                    self.RES, CK, IK = fromHex(res), fromHex(ck), fromHex(ik)
                                    self.KENCR, self.KAUT, self.MSK, self.EMSK, self.MK = self.eap_keys_calculation(CK,IK)
                                    
                                    # Calculate dynamic EAP payload with proper padding
                                    eap_payload_response = self.build_eap_aka_response(self.eap_identifier, self.RES)
                                    
                                    h = hmac.HMAC(self.KAUT,hashes.SHA1())
                                    h.update(eap_payload_response)
                                    hash = h.finalize()[0:16]  
                                    self.eap_payload_response = eap_payload_response[:-16] + hash
                                
                                if VECTOR is not None and ENCR_DATA is not None:                                
                                    cipher = Cipher(algorithms.AES(self.KENCR), modes.CBC(VECTOR))
                                    decryptor = cipher.decryptor()
                                    uncipher_data = decryptor.update(ENCR_DATA) + decryptor.finalize()
                                    eap_attributes = self.decode_eap_attributes(uncipher_data)
                                    NEXT_REAUTH_ID = self.get_eap_aka_attribute_value(eap_attributes,AT_NEXT_REAUTH_ID)

                                    if NEXT_REAUTH_ID is not None: 
                                        self.next_reauth_id = NEXT_REAUTH_ID.decode('utf-8')
                                    else:
                                        #should use permanent identity next 
                                        self.next_reauth_id = None


                                
                                return REPEAT_STATE,'NEW AKA_Challenge'


                        elif i[1][3] in (AKA_Notification,):
                            self.eap_identifier = i[1][1]
                            
                            NOTIFICATION = self.get_eap_aka_attribute_value(i[1][4],AT_NOTIFICATION)
                            
                            if NOTIFICATION < 32768: #error
                                print('EAP AT_NOTIFICATION with ERROR ' + str(NOTIFICATION))
                                self.eap_payload_response = bytes([2]) + bytes([self.eap_identifier]) + fromHex('0008170c0000') 
                                return REPEAT_STATE, 'General_Failure'
                     
                    elif i[1][0] in (EAP_FAILURE,):
                        return OTHER_ERROR,'EAP FAILURE'                     
                     
                     
                    else:
                        #check error
                        return MANDATORY_INFORMATION_MISSING,'NO RAND/AUTN IN EAP'

            if eap_received == True:
                return OK,''               
            else:
                return MANDATORY_INFORMATION_MISSING,'NO EAP PAYLOAD RECEIVED'
        


    def state_4(self):
        self.message_id_request += 1
        packet = self.create_IKE_AUTH_3()
        print('sending IKE_AUTH (3)')
        # P2-5: send + await, retransmitting the same IKE_AUTH(3) bytes on timeout.
        if not self._send_request_await_response(packet):
            return TIMEOUT,'TIMEOUT'

        if self.ike_decoded_header['exchange_type'] == IKE_AUTH and self.decoded_payload[0][0] == SK:
            print('received IKE_AUTH (3)')             
            for i in self.decoded_payload[0][1]:
                
                if i[0] == N:    #protocol_id, notify_message_type, spi, notification_data
                    ntype = i[1][1]
                    if ntype<16384: #error
                        self.log_notify_error(ntype, "IKE_AUTH")
                        self.note_reject(ntype, self.decoded_payload[0][1])
                        return OTHER_ERROR,str(ntype)
                    elif ntype in (PDN_TYPE_IPv4_ONLY_ALLOWED, PDN_TYPE_IPv6_ONLY_ALLOWED):
                        # The network is telling us only one PDN type is allowed. We request an
                        # IPv6-only CFG for Telus already, so this is informational; log it.
                        swu_log("IKE_AUTH status: %s (%s)" % (notify_name(ntype), notify_describe(ntype)))
                    elif ntype == BACKOFF_TIMER:
                        secs, txt = (None, "?")
                        if i[1][3]:
                            secs, txt = self.decode_backoff_timer(i[1][3][-1])
                        swu_log("IKE_AUTH status: BACKOFF_TIMER = %s" % txt)

                elif i[0] == CP:
                    
                    if i[1][0] == CFG_REPLY:
                        self.ip_address_list = self.get_cp_attribute_value(i[1][1],INTERNAL_IP4_ADDRESS)
                        self.dns_address_list = self.get_cp_attribute_value(i[1][1],INTERNAL_IP4_DNS)
                        self.pcscf_address_list = self.get_cp_attribute_value(i[1][1],P_CSCF_IP4_ADDRESS)
                        self.ipv6_address_list = self.get_cp_attribute_value(i[1][1],INTERNAL_IP6_ADDRESS)
                        self.dnsv6_address_list = self.get_cp_attribute_value(i[1][1],INTERNAL_IP6_DNS) 
                        self.pcscfv6_address_list = self.get_cp_attribute_value(i[1][1],P_CSCF_IP6_ADDRESS)
                        # P0-1: INTERNAL_IP6_SUBNET assigns the inner IPv6 as prefix+prefix_len
                        # (decoded as (type, prefix_str, prefix_len)); keep the length so
                        # set_routes can derive a stable address inside the prefix.
                        self.ipv6_subnet_list = [(a[1], a[2]) for a in i[1][1]
                                                 if a[0] == INTERNAL_IP6_SUBNET and len(a) >= 3]
                        print('network configuration received from ePDG')
                        # G4.3 (optional, gated): honour a network-supplied liveness period ONLY if
                        # the ePDG volunteers TIMEOUT_PERIOD_FOR_LIVENESS_CHECK (8.2.4.2, 4-byte
                        # seconds). We do NOT add it to the default cp_list (Telus is sensitive to
                        # extra CFG attributes — see the IPv6-only note), so on Telus this never
                        # fires; it just means a carrier that returns it gets its period respected.
                        _liveness_attr = self.get_cp_attribute_value(i[1][1], TIMEOUT_PERIOD_FOR_LIVENESS_CHECK)
                        if _liveness_attr:
                            try:
                                val = _liveness_attr[0]
                                period = struct.unpack("!I", val[:4])[0] if isinstance(val, (bytes, bytearray)) and len(val) >= 4 else int(val)
                                if period > 0:
                                    self.liveness_period = float(period)
                                    swu_log("ePDG supplied liveness period = %ds (TIMEOUT_PERIOD_FOR_LIVENESS_CHECK); adopting" % period)
                            except Exception as _e:
                                swu_log("could not parse TIMEOUT_PERIOD_FOR_LIVENESS_CHECK: %r" % _e)
                        if self.ip_address_list == [] and self.ipv6_address_list == [] and self.ipv6_subnet_list == []:
                            return OTHER_ERROR,'NO IP ADDRESS (IPV4 or IPV6)'
                    else:
                        #check error
                        return OTHER_ERROR,'NO CP REPLY'                     
                        
                elif i[0] == SA:
                    proposal = i[1][0]
                    protocol_id = i[1][1]
                    self.spi_resp_child = i[1][2]
                    if protocol_id == ESP:
                        self.set_sa_negotiated_child(proposal)
                    else:
                        return MANDATORY_INFORMATION_MISSING,'MANDATORY_INFORMATION_MISSING'

            self.generate_keying_material_child()
            return OK,''               


    def state_delete(self,initiator,kill = True):
        if initiator == True:
            
            #if kill == True: #reauth scenario without delete (comment this line, and uncomment the next one)
            if True:        
                self.message_id_request += 1
                packet = self.create_INFORMATIONAL_delete(IKE)
                self.send_data(packet)
                print('sending INFORMATIONAL (delete IKE)')
                
            self.ike_to_ipsec_encoder.send(bytes([INTER_PROCESS_DELETE_SA]))
            self.ike_to_ipsec_decoder.send(bytes([INTER_PROCESS_DELETE_SA])) 
            self.delete_routes()   
            if kill == True:
                exit(1)

        
        else:
            # Scan the request: is there a DELETE payload, and is REACTIVATION_REQUESTED_CAUSE
            # present? (TS 24.302 clause 7.2.4.2: a DELETE carrying REACTIVATION_REQUESTED_CAUSE
            # means the UE should re-establish the tunnel — our entrypoint supervisor does this
            # automatically on exit.)
            has_delete = any(i[0] == D for i in self.decoded_payload[0][1])
            reactivation = any(i[0] == N and i[1][1] == REACTIVATION_REQUESTED_CAUSE
                               for i in self.decoded_payload[0][1])
            if reactivation:
                swu_log("INFORMATIONAL: REACTIVATION_REQUESTED_CAUSE received - tunnel will be "
                        "re-established after release")

            if not has_delete:
                # Not a DELETE: DPD liveness check, DEVICE_IDENTITY request, or a status notify.
                # Answer it so the ePDG does not consider us dead. (Previously ignored -> the
                # ePDG's DPD would eventually tear the tunnel down.)
                self.handle_INFORMATIONAL_request()
                return

            for i in self.decoded_payload[0][1]:
                if i[0] == D: # delete

                    protocol = i[1][0]
                    num_spi = i[1][1]
                    spi_list = i[1][2]
                    if protocol == IKE:
                        print('received INFORMATIONAL (DELETE IKE)')
                        #delete everything, answer and quit
                        packet = self.answer_INFORMATIONAL_delete()
                        self.send_data(packet)
                        print('answering INFORMATIONAL (DELETE IKE)')
                        if self.old_ike_message_received == False:
                            self.ike_to_ipsec_encoder.send(bytes([INTER_PROCESS_DELETE_SA]))
                            self.ike_to_ipsec_decoder.send(bytes([INTER_PROCESS_DELETE_SA]))
                            self.delete_routes()
                            exit(1)

                    elif protocol == ESP:
                        print('received INFORMATIONAL (DELETE SA CHILD)')
                        # P2-2: map the ePDG's ESP SPI(s) to OUR inbound SPI(s) for the response.
                        # An empty SPI list or any unknown SPI -> INVALID_SPI (never blindly delete
                        # the live SA using a stale spi_init_child_old, as the old code did). All
                        # known -> reply DELETE ESP listing OUR inbound SPI(s).
                        if num_spi == 0:
                            swu_log("ESP DELETE with no SPI; replying INVALID_SPI")
                            self.send_data(self.answer_INFORMATIONAL_error(INVALID_SPI))
                        else:
                            all_known, resp_spis = self.map_deleted_esp_spis_to_ue_inbound(spi_list)
                            if not all_known:
                                swu_log("ESP DELETE references unknown SPI(s) %s; replying INVALID_SPI" %
                                        ",".join(s.hex() for s in spi_list))
                                self.send_data(self.answer_INFORMATIONAL_error(INVALID_SPI))
                            else:
                                self.send_data(self.answer_INFORMATIONAL_delete_CHILD(ESP, b''.join(resp_spis)))
                                print('answering INFORMATIONAL (DELETE SA CHILD)')
                                swu_log("ESP DELETE ack: peer SPI(s) %s -> our inbound SPI(s) %s" %
                                        (",".join(s.hex() for s in spi_list),
                                         ",".join(s.hex() for s in resp_spis)))
                                # If the ePDG deleted our CURRENT active ESP SA (single-bearer) the
                                # dataplane is gone -> tear down for a supervised re-establish
                                # (fresh EAP-AKA + PIN verify). A delete that only referenced an OLD
                                # (rekeyed-away) SPI is acked without disturbing the live SA.
                                current_deleted = bool(getattr(self, "spi_resp_child", None)) and \
                                    any(bytes(s) == bytes(self.spi_resp_child) for s in spi_list)
                                if current_deleted or reactivation:
                                    swu_log("active ESP SA deleted by ePDG%s; tearing down for "
                                            "supervised re-establish" %
                                            (" (REACTIVATION requested)" if reactivation else ""))
                                    self.ike_to_ipsec_encoder.send(bytes([INTER_PROCESS_DELETE_SA]))
                                    self.ike_to_ipsec_decoder.send(bytes([INTER_PROCESS_DELETE_SA]))
                                    self.delete_routes()
                                    exit(1)
                        
                        
    def state_epdg_create_sa(self):
        """P2-1: handle an ePDG-INITIATED CREATE_CHILD_SA request. Classify it (TS 24.302 / RFC
        7296) and respond with the correct, unambiguous message instead of the old 'reply
        NO_PROPOSAL_CHOSEN then start our OWN CHILD rekey' behaviour, which confuses a strict ePDG
        state machine. We do not support multiple bearers or in-place IKE-SA rekey, so we return a
        clean error Notify and let the entrypoint supervisor re-establish if the ePDG then tears
        down — deterministic and self-healing. NOTE: the UE-initiated proactive PFS rekey
        (_rekey_tick / state_ue_rekey_child) is a SEPARATE path and is unchanged."""
        print('\nSTATE ePDG CREATE_CHILD_SA:\n----------------------------------')
        print('CREATE_CHILD_SA payload decoded')
        payloads = self.decoded_payload[0][1]
        # Keep the received nonce (some responses/paths reference it) exactly as before.
        for i in payloads:
            if i[0] == NINR:
                self.nounce_received = i[1][0]

        kind = self.classify_create_child_request(payloads)
        swu_log("ePDG-initiated CREATE_CHILD_SA classified as: %s" % kind)

        if kind == "ike_rekey":
            # Accepting an IKE SA rekey safely requires full DH + new-SPI + key rederivation while
            # retaining the old SA until the DELETE completes; not implemented (as RESPONDER — the
            # proactive _ike_rekey_tick timer normally rekeys first as initiator, so a compliant
            # ePDG's own rekey clock never fires). Refuse cleanly and do NOT start our own rekey.
            swu_log("ePDG IKE SA rekey not supported; replying NO_PROPOSAL_CHOSEN")
            self.send_data(self.answer_CREATE_CHILD_SA_error(NO_PROPOSAL_CHOSEN))
        elif kind == "esp_rekey":
            self._handle_epdg_esp_rekey(payloads)
        elif kind == "additional_bearer":
            # We do not advertise IKEV2_MULTIPLE_BEARER_PDN_CONNECTIVITY and do not implement TFT
            # validation, so refuse an additional bearer with NO_ADDITIONAL_SAS (RFC 7296 3.10.1).
            swu_log("ePDG additional-bearer CREATE_CHILD_SA not supported; replying NO_ADDITIONAL_SAS")
            self.send_data(self.answer_CREATE_CHILD_SA_error(NO_ADDITIONAL_SAS))
        else:
            swu_log("ePDG CREATE_CHILD_SA of unrecognised type; replying NO_PROPOSAL_CHOSEN")
            self.send_data(self.answer_CREATE_CHILD_SA_error(NO_PROPOSAL_CHOSEN))

    def _handle_epdg_esp_rekey(self, payloads):
        """P2-1: an ePDG-initiated ESP CHILD_SA rekey (SA=ESP + REKEY_SA notify). Validate the
        REKEY_SA SPI against our known ESP SPIs: an unknown SPI gets INVALID_SPI and the live SA is
        never touched. For a known SPI, full in-place acceptance (new inbound SPI + key
        rederivation + IPsec-worker switch) is gated behind SWU_ACCEPT_EPDG_ESP_REKEY (default off,
        unverified against Telus); by default we refuse with NO_PROPOSAL_CHOSEN so the ePDG
        re-establishes deterministically rather than risking an untested mid-call SA switch."""
        rekey_spi = None
        for i in payloads:
            if i[0] == N and i[1][1] == REKEY_SA:
                rekey_spi = i[1][2]           # notify SPI = the ePDG's (responder) ESP SPI to rekey
                break
        known_spis = [s for s in (getattr(self, "spi_resp_child", None),
                                  getattr(self, "spi_resp_child_old", None)) if s]
        if rekey_spi is None or rekey_spi not in known_spis:
            swu_log("ePDG ESP rekey REKEY_SA SPI %s not recognised; replying INVALID_SPI" %
                    (rekey_spi.hex() if rekey_spi else "(none)"))
            self.send_data(self.answer_CREATE_CHILD_SA_error(INVALID_SPI))
            return
        if os.environ.get("SWU_ACCEPT_EPDG_ESP_REKEY", "0") not in ("0", "", "no"):
            if self._accept_epdg_esp_rekey(payloads, rekey_spi):
                return
            # Anything we could not satisfy falls through to the refusal below, which is the
            # behaviour this gateway ran on for months: the ePDG re-establishes deterministically.
            swu_log("in-place ESP rekey accept declined; falling back to NO_PROPOSAL_CHOSEN")
        else:
            swu_log("ePDG ESP rekey for known SPI %s; not accepting in place -> NO_PROPOSAL_CHOSEN "
                    "(supervised re-establish)" % rekey_spi.hex())
        self.send_data(self.answer_CREATE_CHILD_SA_error(NO_PROPOSAL_CHOSEN))

    def _accept_epdg_esp_rekey(self, payloads, rekey_spi):
        """Install a peer-initiated ESP rekey in place. True when the new SA is live.

        Refuses (returns False, nothing touched) unless every precondition holds: the peer's
        proposal must carry the ESP algorithms we already negotiated, and a KE payload must
        name a group we can complete. Half-satisfying a rekey would leave the dataplane keyed
        one way and the peer's the other, which is silent — so anything unexpected is declined
        while the live SA keeps running on its current keys.
        """
        peer_spi = None
        ke_received = False
        peer_nonce = None
        for i in payloads:
            if i[0] == SA:
                if i[1][1] != ESP:
                    return False
                peer_spi = i[1][2]
            elif i[0] == KE:
                if not self.iana_diffie_hellman.get(i[1][0]):
                    swu_log("ePDG ESP rekey KE names unsupported DH group %s" % i[1][0])
                    return False
                self.dh_group_num = i[1][0]
                self.dh_create_private_key_and_public_bytes(
                    self.iana_diffie_hellman.get(self.dh_group_num))
                self.dh_calculate_shared_key(i[1][1])
                ke_received = True
            elif i[0] == NINR:
                peer_nonce = i[1][0]
        if not peer_spi or len(peer_spi) != 4 or peer_nonce is None:
            swu_log("ePDG ESP rekey missing a usable SPI or nonce; declining")
            return False
        # nounce_received is Ni here: the initiator's nonce, which is the peer's.
        self.nounce_received = peer_nonce

        # The response proposal must name the group we actually completed the exchange with —
        # _child_sa_list_with_pfs() hardcodes MODP_2048 for our own rekey, which would be a
        # silent mismatch if the ePDG picked a different group.
        sa_list = (self._child_sa_list_with_dh(self.dh_group_num) if ke_received
                   else self.sa_list_negotiated_child)
        response = self.answer_CREATE_CHILD_SA_rekey(sa_list, ke_received)
        # encode_payload_type_sa/ninr have now produced the SPI and Nr we are committing to.
        new_inbound_spi = self.sa_spi_list[0]

        self.spi_init_child_old = self.spi_init_child
        self.spi_resp_child_old = self.spi_resp_child
        self.spi_init_child = new_inbound_spi     # ours: what the ePDG will send to
        self.spi_resp_child = peer_spi            # theirs: where we send

        self.generate_keying_material_child_responder(ke_received)
        # Direction inverted on purpose (see generate_keying_material_child_responder): the
        # ePDG initiated, so the *_I keys carry ePDG->UE and belong to our decoder.
        self.ike_to_ipsec_encoder.send(self.encode_inter_process_protocol([
            INTER_PROCESS_UPDATE_SA,
            [
                (INTER_PROCESS_IE_ENCR_ALG, self.negotiated_encryption_algorithm_child),
                (INTER_PROCESS_IE_ENCR_KEY, self.SK_IPSEC_ER),
                (INTER_PROCESS_IE_INTEG_ALG, self.negotiated_integrity_algorithm_child),
                (INTER_PROCESS_IE_INTEG_KEY, self.SK_IPSEC_AR),
                (INTER_PROCESS_IE_SPI_RESP, self.spi_resp_child),
            ]]))
        self.ike_to_ipsec_decoder.send(self.encode_inter_process_protocol([
            INTER_PROCESS_UPDATE_SA,
            [
                (INTER_PROCESS_IE_ENCR_ALG, self.negotiated_encryption_algorithm_child),
                (INTER_PROCESS_IE_ENCR_KEY, self.SK_IPSEC_EI),
                (INTER_PROCESS_IE_INTEG_ALG, self.negotiated_integrity_algorithm_child),
                (INTER_PROCESS_IE_INTEG_KEY, self.SK_IPSEC_AI),
                (INTER_PROCESS_IE_SPI_INIT, self.spi_init_child),
            ]]))
        self.send_data(response)
        # The peer deletes the old SA; our INFORMATIONAL handler acks that without tearing
        # anything down because the deleted SPI is now spi_resp_child_old, not the live one.
        self._child_sa_time = time.monotonic()
        self._rekey_outstanding = False
        self._rekey_sent_at = None
        swu_log("accepted ePDG ESP rekey%s: old SPI %s -> our inbound %s, peer %s" %
                (" (PFS)" if ke_received else "", rekey_spi.hex(),
                 new_inbound_spi.hex(), peer_spi.hex()))
        return True


  


    def state_ue_create_sa(self,lowest = 0): #IKEv2 REKEY
        print('\nSTATE UE STARTED IKE REKEY:\n--------------------------')
        self.sa_list_negotiated[0][0][1] = 8
        self.sa_list_create_child_sa = self.sa_list_negotiated

        self.dh_create_private_key_and_public_bytes(self.iana_diffie_hellman.get(self.negotiated_diffie_hellman_group))
        self.dh_group_num = self.negotiated_diffie_hellman_group

        self.message_id_request += 1
        self._begin_create_child_request()
        self._create_child_kind = "ike"
        packet = self.create_CREATE_CHILD_SA(lowest)
        # Retained so a timeout resends these exact bytes (same message id, nonce, SPI and KE —
        # RFC 7296 2.1). The SPI/nonce snapshots keep the response handler correct even if some
        # other exchange re-runs encode_payload_type_sa/_ninr while this request is in flight.
        self._ike_rekey_packet = packet
        self._ike_rekey_tries = 1
        self._ike_rekey_spi_i = self.sa_spi_list[0]
        self._ike_rekey_nonce_i = self.nounce
        #send request
        self.send_data(packet)
        print('sending CREATE_CHILD_SA (IKE)')

    def state_ue_create_sa_child(self,lowest = 0): #IPSEC REKEY
        print('\nSTATE UE STARTED IPSEC REKEY:\n--------------------------')

        self.sa_list_create_child_sa_child = self.sa_list_negotiated_child


        self.message_id_request += 1
        self._begin_create_child_request()
        self._create_child_kind = "child"
        packet = self.create_CREATE_CHILD_SA_CHILD(lowest)
        #send request
        self.send_data(packet)
        print('sending CREATE_CHILD_SA (IPSEC)')

    def state_ue_rekey_child(self):
        """Proactive UE-initiated CHILD_SA (ESP) rekey WITH PFS (the periodic-rekey path). Builds a
        child proposal augmented with MODP_2048, generates a fresh DH keypair, and sends
        CREATE_CHILD_SA [ SA Ni KEi TSi TSr ]. The response is processed by
        state_epdg_create_sa_response (ESP branch, which folds in the peer KE when present). On
        NO_PROPOSAL_CHOSEN / TEMPORARY_FAILURE / no-response the caller (_rekey_tick) falls back to a
        supervised re-establish."""
        print('\nSTATE UE STARTED PFS IPSEC REKEY:\n--------------------------')
        swu_log("proactive CHILD_SA rekey (PFS, MODP_2048): sending CREATE_CHILD_SA")
        self.sa_list_create_child_sa_child = self._child_sa_list_with_pfs()
        # Fresh DH keypair for this rekey (group 14).
        self.dh_create_private_key_and_public_bytes(self.iana_diffie_hellman.get(MODP_2048_bit))
        self.dh_group_num = MODP_2048_bit
        self.message_id_request += 1
        self._begin_create_child_request()
        self._create_child_kind = "child"
        packet = self.create_CREATE_CHILD_SA_CHILD_pfs(0)
        # Retained so a timeout resends these exact bytes: a retransmission must carry the same
        # message id, nonce and KE, or the peer sees an unrelated second request.
        self._rekey_packet = packet
        self._rekey_tries = 1
        self.send_data(packet)
        print('sending CREATE_CHILD_SA (IPSEC PFS)')

    def _begin_create_child_request(self):
        self._create_child_request_id = self.message_id_request
        self._create_child_response_handled = False

    def _accept_create_child_response(self, message_id):
        expected = getattr(self, "_create_child_request_id", None)
        if expected is None or int(message_id) != int(expected):
            swu_log("ignoring unexpected CREATE_CHILD_SA response message id %s (expected %s)" %
                    (message_id, expected))
            return False
        if getattr(self, "_create_child_response_handled", False):
            swu_log("ignoring duplicate CREATE_CHILD_SA response message id %s" % message_id)
            return False
        self._create_child_response_handled = True
        return True
                
    def state_epdg_create_sa_response(self):
        isIKE = False
        isESP = False
        ke_received = False
        error_notify = None

        for i in self.decoded_payload[0][1]:
            if i[0] == SA: #
                proposal = i[1][0]
                protocol_id = i[1][1]
                spi = i[1][2]

                if protocol_id == IKE:
                    isIKE = True
                elif protocol_id == ESP:
                    isESP = True

            elif i[0] == KE:
                dh_peer_group = i[1][0]
                dh_peer_public_key_bytes = i[1][1]
                self.dh_calculate_shared_key(dh_peer_public_key_bytes)
                ke_received = True

            elif i[0] == NINR:
                self.nounce_received = i[1][0]

            elif i[0] == N and i[1][1] < 16384:   # error-type notify => rekey rejected
                error_notify = i[1][1]

        # A CREATE_CHILD_SA response carrying an error notify (e.g. NO_PROPOSAL_CHOSEN(14),
        # TEMPORARY_FAILURE(43)) means our rekey was refused. Do NOT install anything. The
        # response consumed this message id, so _rekey_tick keeps the old CHILD SA and schedules
        # a later attempt with the next id.
        if error_notify is not None:
            self.log_notify_error(error_notify, "CREATE_CHILD_SA (rekey) response")
            if getattr(self, "_create_child_kind", None) == "ike":
                self._ike_rekey_outstanding = False
                self._ike_rekey_failed = True
            else:
                self._rekey_outstanding = False
                self._rekey_failed = True
            return


        if isIKE == True:
            print('received CREATE_CHILD_SA response IKE')
            self.message_id_request += 1
            packet = self.create_INFORMATIONAL_delete(IKE)

            self.ike_spi_responder_old = self.ike_spi_responder
            self.ike_spi_initiator_old = self.ike_spi_initiator

            self.ike_spi_responder = spi
            # Our proposed initiator SPI / Ni, snapshotted when the request was built (another
            # exchange may have re-run encode_payload_type_sa/_ninr since).
            self.ike_spi_initiator = getattr(self, "_ike_rekey_spi_i", None) or self.sa_spi_list[0]
            if getattr(self, "_ike_rekey_nonce_i", None):
                self.nounce = self._ike_rekey_nonce_i

            self.generate_new_ike_keying_material()
            self.message_id_request = -1

            # The new IKE SA restarts both message-id spaces at 0: cached responder replies from
            # the old SA would otherwise shadow the ePDG's first requests on the new one (and
            # replay bytes protected with retired keys).
            self._response_cache = {}
            self._response_cache_order = []

            #send request
            self.send_data(packet)
            print('sending INFORMATIONAL (DELETE IKE old)')
            swu_log("IKE SA rekeyed (make-before-break): new SPIi=%s SPIr=%s; DELETE for the old "
                    "SA sent on the old SA" % (self.ike_spi_initiator.hex(), self.ike_spi_responder.hex()))

            # Proactive-rekey bookkeeping: restart the IKE SA age clock, clear the in-flight state.
            self._ike_sa_time = time.monotonic()
            self._ike_rekey_outstanding = False
            self._ike_rekey_sent_at = None
            self._ike_rekey_packet = None
            self._ike_rekey_tries = 0
            self._ike_rekey_retry_at = None
            if self.ike_rekey_period > 0:
                swu_log("next proactive IKE SA rekey in ~%d min" % int(self.ike_rekey_period / 60))

        if isESP == True:
            print('received CREATE_CHILD_SA response IPSEC')                
            self.message_id_request += 1    
            self.spi_init_child_old = self.spi_init_child
            self.spi_resp_child_old = self.spi_resp_child            
            packet = self.create_INFORMATIONAL_delete(ESP,self.spi_init_child_old)

            self.spi_init_child = self.sa_spi_list[0] #only one proposal was made
            self.spi_resp_child = spi


            # PFS rekey (our proactive path) folds the fresh DH secret into the child keymat;
            # a no-PFS rekey (ePDG-initiated, no KE) uses SK_d + nonces only.
            if ke_received:
                self.generate_keying_material_child_pfs()
            else:
                self.generate_keying_material_child()
            inter_process_list_start_encoder = [
                INTER_PROCESS_UPDATE_SA,
                [
                    (INTER_PROCESS_IE_ENCR_ALG, self.negotiated_encryption_algorithm_child),
                    (INTER_PROCESS_IE_ENCR_KEY, self.SK_IPSEC_EI),
                    (INTER_PROCESS_IE_INTEG_ALG, self.negotiated_integrity_algorithm_child),
                    (INTER_PROCESS_IE_INTEG_KEY, self.SK_IPSEC_AI),
                    (INTER_PROCESS_IE_SPI_RESP, self.spi_resp_child)         
                ]
            ]
            
            inter_process_list_start_decoder = [
                INTER_PROCESS_UPDATE_SA,
                [
                    (INTER_PROCESS_IE_ENCR_ALG, self.negotiated_encryption_algorithm_child),
                    (INTER_PROCESS_IE_ENCR_KEY, self.SK_IPSEC_ER),
                    (INTER_PROCESS_IE_INTEG_ALG, self.negotiated_integrity_algorithm_child),
                    (INTER_PROCESS_IE_INTEG_KEY, self.SK_IPSEC_AR),
                    (INTER_PROCESS_IE_SPI_INIT, self.spi_init_child)         
                ]
            ]
                        
            self.ike_to_ipsec_encoder.send(self.encode_inter_process_protocol(inter_process_list_start_encoder))
            self.ike_to_ipsec_decoder.send(self.encode_inter_process_protocol(inter_process_list_start_decoder))            
            
            #send request
            self.send_data(packet)
            print('sending INFORMATIONAL (DELETE IPSEC old)')

            # Proactive-rekey bookkeeping: the CHILD SA was just refreshed -> restart its rekey
            # clock and clear the in-flight flag so _rekey_tick schedules the next one.
            self._child_sa_time = time.monotonic()
            self._rekey_outstanding = False
            self._rekey_sent_at = None
            self._rekey_packet = None
            self._rekey_tries = 0
            self._rekey_retry_at = None
            if self.child_rekey_period > 0:
                swu_log("CHILD_SA rekey complete; next proactive rekey in ~%d min" %
                        int(self.child_rekey_period / 60))

    def state_connected(self):
        #set udp 4500 socket (self.socket_nat)
     
        self.set_routes()
    
        #set ipsec tunnel handlers
        self.ike_to_ipsec_encoder, self.ipsec_encoder_to_ike = multiprocessing.Pipe()
        self.ike_to_ipsec_decoder, self.ipsec_decoder_to_ike = multiprocessing.Pipe()
           
        worker_parent_pid = os.getpid()
        ipsec_input_worker = multiprocessing.Process(
            target=self.encapsulate_ipsec,
            args=([self.ipsec_encoder_to_ike], worker_parent_pid))
        ipsec_input_worker.start()
        ipsec_output_worker = multiprocessing.Process(
            target=self.decapsulate_ipsec,
            args=([self.ipsec_decoder_to_ike], worker_parent_pid))
        ipsec_output_worker.start()
        
        inter_process_list_start_encoder = [
            INTER_PROCESS_CREATE_SA,
            [
                (INTER_PROCESS_IE_ENCR_ALG, self.negotiated_encryption_algorithm_child),
                (INTER_PROCESS_IE_ENCR_KEY, self.SK_IPSEC_EI),
                (INTER_PROCESS_IE_INTEG_ALG, self.negotiated_integrity_algorithm_child),
                (INTER_PROCESS_IE_INTEG_KEY, self.SK_IPSEC_AI),
                (INTER_PROCESS_IE_SPI_RESP, self.spi_resp_child)         
            ]
        ]

        inter_process_list_start_decoder = [
            INTER_PROCESS_CREATE_SA,
            [
                (INTER_PROCESS_IE_ENCR_ALG, self.negotiated_encryption_algorithm_child),
                (INTER_PROCESS_IE_ENCR_KEY, self.SK_IPSEC_ER),
                (INTER_PROCESS_IE_INTEG_ALG, self.negotiated_integrity_algorithm_child),
                (INTER_PROCESS_IE_INTEG_KEY, self.SK_IPSEC_AR),
                (INTER_PROCESS_IE_SPI_INIT, self.spi_init_child)         
            ]
        ]
             
        self.ike_to_ipsec_encoder.send(self.encode_inter_process_protocol(inter_process_list_start_encoder))
        self.ike_to_ipsec_decoder.send(self.encode_inter_process_protocol(inter_process_list_start_decoder))

        # VoWiFi engine addition: tunnel is up. Publish P-CSCF + state and notify the manager
        # (in-process, replacing any external updown script + IKE-log P-CSCF parsing).
        pcscf = (self.pcscfv6_address_list[0] if getattr(self, "pcscfv6_address_list", [])
                 else (self.pcscf_address_list[0] if getattr(self, "pcscf_address_list", []) else ""))
        inner = (self.ipv6_address_list[0] if self.ipv6_address_list
                 else (self.ip_address_list[0] if self.ip_address_list else ""))
        swu_write_pcscf(pcscf)
        swu_write_status("CONNECTED", inner_ip=inner, pcscf=pcscf, iface=self.tun_device)
        swu_log("tunnel CONNECTED inner=%s pcscf=%s iface=%s" % (inner, pcscf, self.tun_device))
        swu_notify("tunnel_up")
        if pcscf:
            swu_notify("pcscf", pcscf)
            # Keep pjsip's P-CSCF (identify/resolve/register) in sync when the ePDG assigns a
            # different P-CSCF on reconnect/reauth. No-op on first bring-up (entrypoint seeds
            # pcscf.applied after its own initial render, before Asterisk starts).
            swu_apply_pcscf(pcscf)

        # Headless control channel replaces interactive stdin. Open a FIFO O_RDWR so select()
        # never sees EOF (a plain stdin/EOF would busy-spin). The manager/entrypoint can echo
        # q/i/c/r into it. Absent a FIFO we simply run without a control channel.
        ctl_f = None
        if not self.netns_name:  # netns probe mode keeps interactive stdin
            try:
                ctl_path = os.environ.get("SWU_CTL_FIFO", os.path.join(SWU_RUNDIR, "swu.ctl"))
                os.makedirs(SWU_RUNDIR, exist_ok=True)
                if not os.path.exists(ctl_path):
                    os.mkfifo(ctl_path)
                ctl_f = os.fdopen(os.open(ctl_path, os.O_RDWR), "r")
            except Exception as e:
                swu_log("control FIFO unavailable: %r" % e)
                ctl_f = None
        ctl_src = ctl_f if ctl_f is not None else sys.stdin
        socket_list = [ctl_src, self.socket, self.ike_to_ipsec_decoder]

        # G4: seed the liveness clock; reset on every protected IKE message decoded below.
        self._last_rx = time.monotonic()
        self._liveness_outstanding = 0
        # P2-5: start each (re)connection with an EMPTY duplicate-request cache. After an in-process
        # reauth ('r' control command) the IKE SPIs and SK_* keys change and the ePDG restarts its
        # message-id sequence, so a response cached under the OLD SA must never be resent for a
        # colliding (exchange_type, message_id) on the NEW SA (it would carry stale SPIs/ICV and the
        # real handler — DPD ack / P-CSCF re-register / DELETE — would be wrongly skipped).
        self._response_cache = {}
        self._response_cache_order = []
        # Proactive rekey: start the CHILD-SA clock now (SA was just installed above). Cleared to
        # None only if rekey is disabled, so _rekey_tick is a no-op.
        self._child_sa_time = time.monotonic() if self.child_rekey_period > 0 else None
        self._rekey_outstanding = False
        self._rekey_sent_at = None
        self._rekey_failed = False
        # A reconnect reuses this object, and an in-process reauth restarts the message-id
        # sequence: anything held over from the previous SA's attempt would be retransmitted
        # onto a peer that never saw it.
        self._rekey_packet = None
        self._rekey_tries = 0
        self._rekey_retry_at = None
        self._create_child_request_id = None
        self._create_child_response_handled = False
        self._create_child_kind = None
        if self.child_rekey_period > 0:
            swu_log("proactive CHILD_SA rekey enabled: every %d min (0=off via SWU_CHILD_REKEY_MINUTES)" %
                    int(self.child_rekey_period / 60))
        # Proactive IKE SA rekey clock: measured from tunnel establishment (this SA's birth).
        self._ike_sa_time = time.monotonic() if self.ike_rekey_period > 0 else None
        self._ike_rekey_outstanding = False
        self._ike_rekey_sent_at = None
        self._ike_rekey_failed = False
        self._ike_rekey_packet = None
        self._ike_rekey_tries = 0
        self._ike_rekey_retry_at = None
        if self.ike_rekey_period > 0:
            swu_log("proactive IKE SA rekey enabled: every %d min (0=off via SWU_IKE_REKEY_MINUTES)" %
                    int(self.ike_rekey_period / 60))

        while True:

            # Wake select() for whichever timer fires first: G4 liveness OR proactive rekey. On a
            # busy tunnel a packet wakes us sooner; on idle this bounds the sleep so the timers run.
            _timeouts = [t for t in (
                self.liveness_period if self.liveness_period > 0 else None,
                self._rekey_select_timeout(),
                self._ike_rekey_select_timeout(),
            ) if t is not None]
            _timeout = min(_timeouts) if _timeouts else None
            read_sockets, write_sockets, error_sockets = select.select(socket_list, [], [], _timeout)

            for sock in read_sockets:
     
                if sock == self.socket:

                        
                    packet, server_address = self.socket.recvfrom(2000)
                    if server_address[0] == self.server_address[0]: #check server IP address. source port could be different than 500 or 4500, if it's a request reponse must be sent to the same port

                        self.decode_ike(packet)
                        if self.ike_decoded_ok == True:
                            # G4: any validly-decoded protected message from the ePDG proves the SA
                            # is alive — reset the liveness clock and clear any outstanding probe.
                            self._note_liveness_rx()

                            if self.ike_decoded_header['exchange_type'] == INFORMATIONAL and self.decoded_payload[0][0] == SK and self.ike_decoded_header['flags'][0] == 1:
                                # G4: an INFORMATIONAL *response* (flags[0]==1). This is the ack to
                                # our own liveness probe (or a rekey/delete response we initiated).
                                # _note_liveness_rx() above already cleared the pending state; just
                                # consume it (no responder action).
                                pass
                            elif self.ike_decoded_header['exchange_type'] == INFORMATIONAL and self.decoded_payload[0][0] == SK and self.ike_decoded_header['flags'][0] == 0:
                                self._dispatch_epdg_request(lambda: self.state_delete(False))

                            elif self.ike_decoded_header['exchange_type'] == CREATE_CHILD_SA and self.decoded_payload[0][0] == SK and self.ike_decoded_header['flags'][0] == 0:
                                self._dispatch_epdg_request(self.state_epdg_create_sa)

                            elif self.ike_decoded_header['exchange_type'] == CREATE_CHILD_SA and self.decoded_payload[0][0] == SK and self.ike_decoded_header['flags'][0] == 1:
                                if self._accept_create_child_response(
                                        self.ike_decoded_header['message_id']):
                                    self.state_epdg_create_sa_response()
                            
                            
                        
                        if self.old_ike_message_received == True:
                            self.old_ike_message_received = False                                
                            
                   
                                    

                elif sock == self.ike_to_ipsec_decoder:
                    pipe_packet = self.ike_to_ipsec_decoder.recv()
                    decode_list = self.decode_inter_process_protocol(pipe_packet)
                    if decode_list[0] == INTER_PROCESS_LIVENESS_RX:
                        # P2-3: the ESP-decap worker saw protected IPsec traffic -> SA is alive.
                        self._note_liveness_rx()
                    elif decode_list[0] == INTER_PROCESS_IKE:

                        packet = decode_list[1][0][1]
                        
                        #if received via pipe it was sent to port udp 4500 (exclude 4 initial bytes)
                        self.decode_ike(packet[4:])

                        if self.ike_decoded_ok == True:
                            # G4: protected message received via the ESP-decap pipe -> SA alive.
                            self._note_liveness_rx()

                            if self.ike_decoded_header['exchange_type'] == INFORMATIONAL and self.decoded_payload[0][0] == SK and self.ike_decoded_header['flags'][0] == 1:
                                # G4: response to our liveness probe (or our rekey/delete). Consume.
                                pass
                            elif self.ike_decoded_header['exchange_type'] == INFORMATIONAL and self.decoded_payload[0][0] == SK and self.ike_decoded_header['flags'][0] == 0:
                                self._dispatch_epdg_request(lambda: self.state_delete(False))

                            elif self.ike_decoded_header['exchange_type'] == CREATE_CHILD_SA and self.decoded_payload[0][0] == SK and self.ike_decoded_header['flags'][0] == 0:
                                self._dispatch_epdg_request(self.state_epdg_create_sa)

                            elif self.ike_decoded_header['exchange_type'] == CREATE_CHILD_SA and self.decoded_payload[0][0] == SK and self.ike_decoded_header['flags'][0] == 1:
                                if self._accept_create_child_response(
                                        self.ike_decoded_header['message_id']):
                                    self.state_epdg_create_sa_response()
                        
                        if self.old_ike_message_received == True:
                            self.old_ike_message_received = False

                else:
                    msg = ctl_src.readline()
                    if msg == "":            # control channel closed -> ignore (daemon keeps running)
                        continue
                    msg = msg.strip() + "\n"
                    if msg == "q\n":  #quit
                        self.state_delete(True)
                    elif msg =="i\n": #rekey ike
                        self.state_ue_create_sa()
                    elif msg =="c\n": #rekey sa child
                        self.state_ue_create_sa_child()
                    elif msg =="r\n": # restart process
                        self.state_delete(True,False)
                        if self.next_reauth_id is not None:
                            self.set_identification(IDI,ID_RFC822_ADDR,self.next_reauth_id)
                        else:
                            self.set_identification(IDI,ID_RFC822_ADDR,'0' + self.imsi + '@nai.epc.mnc' + self.mnc + '.mcc' + self.mcc + '.3gppnetwork.org')

                        self.iterations = 2
                        return

                    else:
                        pass

            # G4: after servicing whatever woke select() (or on a plain timeout with no sockets
            # ready), run the initiator liveness check. On an idle tunnel this is the only thing
            # that fires and it sends/evaluates the DPD probe.
            self._liveness_tick()
            # Proactive CHILD-SA rekey: refresh the ESP SA before it silently ages out on the ePDG.
            self._rekey_tick()
            # Proactive IKE-SA rekey: refresh the IKE SA before the ePDG's own rekey clock fires
            # (we refuse the responder role, so being second costs a teardown).
            self._ike_rekey_tick()


                        
    

    def _note_liveness_rx(self):
        """G4: record that a cryptographically-protected IKE message arrived from the ePDG — the SA
        is alive. Resets the idle clock and clears any outstanding DPD probe count."""
        self._last_rx = time.monotonic()
        if self._liveness_outstanding:
            swu_log("liveness: peer responded, clearing %d outstanding probe(s)" % self._liveness_outstanding)
        self._liveness_outstanding = 0
        self._liveness_probe_mid = None

    def _note_esp_activity(self, pipe_ike):
        """P2-3: called in the ESP-decap worker after a protected IPsec packet is written to the
        tun. Tells the IKE main loop (via INTER_PROCESS_LIVENESS_RX) that the SA is alive so a busy
        ESP tunnel with no IKE traffic does not trip the initiator DPD (TS 24.302 7.2.2A: ANY
        protected IKEv2 OR IPsec message proves liveness). Throttled to at most once per
        SWU_ESP_LIVENESS_INTERVAL to avoid flooding the pipe at high throughput. Runs in the forked
        decap process, so self._esp_liveness_last_tx is that process's own copy."""
        now = time.monotonic()
        if (now - self._esp_liveness_last_tx) < self._esp_liveness_min_interval:
            return
        self._esp_liveness_last_tx = now
        try:
            pipe_ike.send(self.encode_inter_process_protocol([INTER_PROCESS_LIVENESS_RX, []]))
        except Exception:
            pass

    def _liveness_tick(self):
        """G4 (TS 24.302 7.2.2A): if no protected message has arrived for `liveness_period`, send an
        empty INFORMATIONAL request (an acknowledged Dead-Peer-Detection probe). After
        `liveness_retries` consecutive unanswered probes, declare the ePDG dead and tear the tunnel
        down so the entrypoint supervisor re-establishes it. Disabled when liveness_period<=0.
        Separate from — and complementary to — the RFC 3948 NAT-T keepalive (unacknowledged)."""
        if self.liveness_period <= 0:
            return
        # With the default IKEv2 request window, do not send a higher-message-id DPD while a
        # CREATE_CHILD_SA request is awaiting its response. The rekey request itself is an
        # acknowledged liveness check, and its bounded retransmission path decides recovery.
        if self._rekey_outstanding or self._ike_rekey_outstanding:
            return
        idle = time.monotonic() - (self._last_rx if self._last_rx is not None else time.monotonic())
        if idle < self.liveness_period:
            return
        if self._liveness_outstanding >= self.liveness_retries:
            swu_log("liveness: %d consecutive DPD probes unanswered (~%ds of silence) -> ePDG "
                    "declared dead, tearing down for supervised re-establish" %
                    (self._liveness_outstanding, int(idle)))
            try:
                # best-effort DELETE in case the peer is actually still there; kill=False so we
                # exit ourselves (fresh sockets on restart) rather than lingering.
                self.state_delete(True, kill=False)
            except Exception:
                pass
            try:
                swu_write_status("DOWN", reason_code="liveness_timeout", reason_policy="transient")
                swu_notify("tunnel_down")
            except Exception:
                pass
            exit(1)
        try:
            packet = self.create_INFORMATIONAL_liveness()
            self.send_data(packet)
            self._liveness_outstanding += 1
            self._liveness_probe_mid = self.message_id_request
            swu_log("liveness: sent DPD probe #%d (message_id=%d, idle=%ds, period=%ds)" %
                    (self._liveness_outstanding, self._liveness_probe_mid, int(idle), int(self.liveness_period)))
        except Exception as e:
            swu_log("liveness: probe send failed: %r" % e)

    def _rekey_select_timeout(self):
        """Seconds until the next proactive-rekey action (rekey due, or in-flight rekey response
        timeout), so state_connected's select() wakes in time. None when rekey is disabled/idle."""
        if self.child_rekey_period <= 0 or self._child_sa_time is None:
            return None
        now = time.monotonic()
        if self._rekey_outstanding and self._rekey_sent_at is not None:
            return max(0.0, self.rekey_response_timeout - (now - self._rekey_sent_at))
        if self._rekey_retry_at is not None:
            return max(0.0, self._rekey_retry_at - now)
        return max(0.0, self.child_rekey_period - (now - self._child_sa_time))

    def _rekey_tick(self):
        """Proactive CHILD-SA rekey driver (TS 24.302 7.2.2C / RFC 7296).

        When the CHILD SA reaches child_rekey_period since establishment, initiate a PFS
        make-before-break rekey. An unanswered request is retransmitted verbatim, as RFC 7296
        2.1 requires of an initiator.

        An explicit rejection does not tear the tunnel down: the old CHILD SA remains installed
        and a new request can safely use the next message id after a delay. By contrast, after
        every verbatim retransmission goes unanswered it is unsafe to either skip the request id
        or reuse it for different bytes. That ambiguous IKE SA is re-established; retransmission
        makes one lost packet insufficient to trigger that recovery.

        Disabled when child_rekey_period<=0."""
        if self.child_rekey_period <= 0 or self._child_sa_time is None:
            return
        now = time.monotonic()

        # The response handler clears outstanding before returning. Consume its rejection here
        # rather than immediately starting another request because the CHILD SA is already due.
        if getattr(self, "_rekey_failed", False):
            self._rekey_give_up("rejected by the ePDG")
            return

        # An in-flight rekey we started: handle rejection, retransmission or give-up.
        if self._rekey_outstanding:
            if self._rekey_sent_at is not None and (now - self._rekey_sent_at) >= self.rekey_response_timeout:
                if self._rekey_tries < max(1, self.rekey_retransmits) and self._rekey_packet:
                    self._rekey_tries += 1
                    try:
                        self.send_data(self._rekey_packet)
                        self._rekey_sent_at = now
                        swu_log("proactive rekey unanswered in %ds; retransmission %d/%d "
                                "(same message id; the current SA keeps carrying traffic)" %
                                (int(self.rekey_response_timeout), self._rekey_tries,
                                 max(1, self.rekey_retransmits)))
                    except Exception as e:
                        swu_log("proactive rekey retransmission failed (%r) -> supervised "
                                "re-establish" % e)
                        self._rekey_teardown("rekey_send_error")
                    return
                swu_log("proactive rekey got no response after %d transmissions -> supervised "
                        "re-establish" % self._rekey_tries)
                self._rekey_teardown("rekey_timeout")
                return
            return   # still waiting for the response

        # Never open a second CREATE_CHILD_SA exchange while our IKE rekey awaits its response.
        if self._ike_rekey_outstanding:
            return

        # Not yet due? A given-up attempt sets its own next time; otherwise it is SA age.
        due_at = (self._rekey_retry_at if self._rekey_retry_at is not None
                  else self._child_sa_time + self.child_rekey_period)
        if now < due_at:
            return

        # Due: fire a PFS CHILD_SA rekey. A send error leaves the message-id sequence in an
        # unknown state, which the peer would reject for every later request, so that one case
        # still takes the supervised re-establish.
        age = int(now - self._child_sa_time)
        swu_log("CHILD_SA reached rekey age (~%d min); initiating proactive PFS rekey" % (age // 60))
        try:
            self._rekey_failed = False
            self.state_ue_rekey_child()
            self._rekey_outstanding = True
            self._rekey_sent_at = now
        except Exception as e:
            swu_log("proactive rekey send failed (%r) -> supervised re-establish" % e)
            self._rekey_teardown("rekey_send_error")

    def _ike_rekey_select_timeout(self):
        """Seconds until the next proactive IKE-SA-rekey action, mirroring _rekey_select_timeout,
        so state_connected's select() wakes in time. None when disabled/idle."""
        if self.ike_rekey_period <= 0 or self._ike_sa_time is None:
            return None
        now = time.monotonic()
        if self._ike_rekey_outstanding and self._ike_rekey_sent_at is not None:
            return max(0.0, self.rekey_response_timeout - (now - self._ike_rekey_sent_at))
        if self._ike_rekey_retry_at is not None:
            return max(0.0, self._ike_rekey_retry_at - now)
        return max(0.0, self.ike_rekey_period - (now - self._ike_sa_time))

    def _ike_rekey_tick(self):
        """Proactive IKE-SA rekey driver (RFC 7296 2.18), same decision structure as _rekey_tick.

        When the IKE SA reaches ike_rekey_period since establishment, initiate a make-before-break
        IKE rekey (state_ue_create_sa). Staying the initiator keeps every role assumption in this
        file valid AND resets the ePDG's own rekey clock — the whole point, since we refuse the
        responder role and an ePDG-initiated rekey therefore ends in a teardown (EE does this at
        ~12 h SA age; giffgaff/O2 UK instead ages the session out silently at ~2h50m — keep
        SWU_IKE_REKEY_MINUTES comfortably below the shortest carrier clock in use).

        An explicit rejection keeps the established IKE SA (retry in ike_rekey_retry_interval).
        An unanswered request is retransmitted verbatim; exhausting the retransmissions leaves the
        message-id window ambiguous, so that path re-establishes — no worse than the ePDG-initiated
        teardown this timer exists to preempt. Disabled when ike_rekey_period<=0."""
        if self.ike_rekey_period <= 0 or self._ike_sa_time is None:
            return
        now = time.monotonic()

        if getattr(self, "_ike_rekey_failed", False):
            self._ike_rekey_give_up("rejected by the ePDG")
            return

        if self._ike_rekey_outstanding:
            if self._ike_rekey_sent_at is not None and (now - self._ike_rekey_sent_at) >= self.rekey_response_timeout:
                if self._ike_rekey_tries < max(1, self.rekey_retransmits) and self._ike_rekey_packet:
                    self._ike_rekey_tries += 1
                    try:
                        self.send_data(self._ike_rekey_packet)
                        self._ike_rekey_sent_at = now
                        swu_log("proactive IKE rekey unanswered in %ds; retransmission %d/%d "
                                "(same message id; the current SA keeps carrying traffic)" %
                                (int(self.rekey_response_timeout), self._ike_rekey_tries,
                                 max(1, self.rekey_retransmits)))
                    except Exception as e:
                        swu_log("proactive IKE rekey retransmission failed (%r) -> supervised "
                                "re-establish" % e)
                        self._rekey_teardown("ike_rekey_send_error")
                    return
                swu_log("proactive IKE rekey got no response after %d transmissions -> supervised "
                        "re-establish" % self._ike_rekey_tries)
                self._rekey_teardown("ike_rekey_timeout")
                return
            return   # still waiting for the response

        # Never open a second CREATE_CHILD_SA exchange while a CHILD rekey awaits its response.
        if self._rekey_outstanding:
            return

        due_at = (self._ike_rekey_retry_at if self._ike_rekey_retry_at is not None
                  else self._ike_sa_time + self.ike_rekey_period)
        if now < due_at:
            return

        age = int(now - self._ike_sa_time)
        swu_log("IKE SA reached rekey age (~%d min); initiating proactive IKE SA rekey "
                "(make-before-break)" % (age // 60))
        try:
            self._ike_rekey_failed = False
            self.state_ue_create_sa()
            self._ike_rekey_outstanding = True
            self._ike_rekey_sent_at = now
        except Exception as e:
            swu_log("proactive IKE rekey send failed (%r) -> supervised re-establish" % e)
            self._rekey_teardown("ike_rekey_send_error")

    def _ike_rekey_give_up(self, why):
        """After an explicit rejection, keep the established IKE SA and schedule a later attempt.
        The response consumed our message id, so the next attempt uses the next id — safe."""
        self._ike_rekey_outstanding = False
        self._ike_rekey_failed = False
        self._ike_rekey_sent_at = None
        self._ike_rekey_packet = None
        self._ike_rekey_tries = 0
        self._ike_rekey_retry_at = time.monotonic() + self.ike_rekey_retry_interval
        swu_log("proactive IKE rekey abandoned (%s); keeping the established IKE SA and retrying "
                "in ~%d min. DPD still guards the tunnel." % (why, int(self.ike_rekey_retry_interval / 60)))

    def _rekey_give_up(self, why):
        """After an explicit rejection, keep the old CHILD SA and schedule a new request.

        A response proves the peer consumed this message id, so it must never be reclaimed.
        The no-response path cannot make that proof and therefore re-establishes the IKE SA.
        """
        self._rekey_outstanding = False
        self._rekey_failed = False
        self._rekey_sent_at = None
        self._rekey_packet = None
        self._rekey_tries = 0
        self._rekey_retry_at = time.monotonic() + self.rekey_retry_interval
        swu_log("proactive rekey abandoned (%s); keeping the established SA and retrying in "
                "~%d min. DPD still guards the tunnel." % (why, int(self.rekey_retry_interval / 60)))

    def _rekey_teardown(self, reason_code):
        """Fallback used when a proactive rekey can't complete: best-effort DELETE, publish a
        transient DOWN, and exit so the entrypoint supervisor re-establishes the tunnel."""
        try:
            self.state_delete(True, kill=False)   # DELETE + tear down SAs; we exit ourselves
        except Exception:
            pass
        try:
            swu_write_status("DOWN", reason_code=reason_code, reason_policy="transient")
            swu_notify("tunnel_down")
        except Exception:
            pass
        exit(1)

    def _sim_auth_failed(self):
        """Handle a SIM AUTHENTICATE that returned no RES/CK/IK — e.g. the wrong SIM is in the
        bound reader (0x9862 'incorrect MAC'), a blocked/mismatched card, or an unhandled sync
        failure. The upstream code blindly did fromHex(None) here and crashed the ENTIRE tunnel
        process with a TypeError (ugly traceback + supervised restart storm). Instead classify it
        as an EAP-AKA authentication failure and reject cleanly so the supervisor + manager health
        back off. The 'authentication failed' phrase is what control/app/status.classify_ike matches
        to surface a 'SIM authentication (EAP-AKA)' reason instead of a raw traceback."""
        swu_log("SIM AUTHENTICATE returned no RES/CK/IK — EAP-AKA authentication failed "
                "(wrong SIM in the bound reader, or a card auth error); rejecting attach")
        self.reject_reason_code = "AUTHENTICATION_FAILED"
        self.reject_reason_policy = "backoff"
        return OTHER_ERROR, 'SIM AUTHENTICATE FAILED'

    def _reject_guard(self):
        """G2: after a failed attach attempt, decide whether to keep looping. If the last reject
        carried a no_retry policy (subscription/equipment/PLMN — 7.2.2.2), or a backoff policy,
        stop the tight local loop: write a DOWN status with the classified reason and exit so the
        entrypoint supervisor (and the manager's health/back-off) space out re-attempts instead of
        this process hammering the ePDG. Generic/transient errors fall through to the bounded
        `iterations` loop as before. Returns True if the caller should abort (has already exited)."""
        policy = self.reject_reason_policy
        if policy in ("no_retry", "backoff"):
            reason = self.reject_reason_code or "reject"
            extra = {"reason_code": reason, "reason_policy": policy}
            if self.reject_backoff_seconds:
                extra["backoff"] = self.reject_backoff_seconds
            if policy == "no_retry":
                swu_log("attach rejected with no-retry policy (%s); not re-attempting on the same "
                        "SIM/PLMN — exiting for supervised back-off (durable Tw3 is the manager's "
                        "job, see control/app/status.py / main.apply_health)" % reason)
            else:
                bo = ("%ss" % self.reject_backoff_seconds) if self.reject_backoff_seconds \
                    else "implementation default (supervised restart cadence)"
                swu_log("attach rejected with backoff policy (%s); backoff=%s — not busy-looping, "
                        "exiting for supervised re-attempt" % (reason, bo))
            swu_write_status("DOWN", **extra)
            swu_notify("tunnel_down")
            exit(1)
        return False

    def apply_cp_ts_mode(self, mode):
        """Build the IKE CFG (CP) config-request + Traffic Selectors for one address-family mode and
        set them on this object. mode in {'v6','v4','dual'}: v6 = INTERNAL_IP6_ADDRESS/DNS +
        P_CSCF_IP6_ADDRESS (Telus/EE — IPv6 IMS PDNs); v4 = the IPv4 attrs (Vodafone UK — IPv4 IMS,
        else private Notify 16375); dual = both (note dual suppresses Telus's P-CSCF). TS families
        follow the CP families (SWU_TS_MODE=auto). Called once per attach attempt so the auto
        heuristic can retry a different family in the SAME process (see start_ike)."""
        if mode not in ("v6", "v4", "dual"):
            mode = "v6"
        cp_list = [CFG_REQUEST]
        if mode in ("v4", "dual"):
            cp_list += [[INTERNAL_IP4_ADDRESS], [INTERNAL_IP4_DNS], [P_CSCF_IP4_ADDRESS]]
        if mode in ("v6", "dual"):
            cp_list += [[INTERNAL_IP6_ADDRESS], [INTERNAL_IP6_DNS], [P_CSCF_IP6_ADDRESS]]
        # P2-3 (optional): also ask the ePDG to supply a liveness period. Default OFF (Telus is
        # sensitive to extra CFG attributes and returns NO P-CSCF unless the request is exactly the
        # strongSwan set); state_4 already adopts a volunteered value.
        if os.environ.get("SWU_REQUEST_LIVENESS_ATTR", "0") not in ("0", "", "no"):
            cp_list.append([TIMEOUT_PERIOD_FOR_LIVENESS_CHECK])
        print("[swu_ike] CP request family: %s" % mode)
        # Traffic Selectors MUST be consistent with the CFG/PDN address family. SWU_TS_MODE=auto
        # (default) derives them from cp_list; dual/v4/v6 force it (escape hatch).
        _TS_V4 = [TS_IPV4_ADDR_RANGE, ANY, 0, 65535, '0.0.0.0', '255.255.255.255']
        _TS_V6 = [TS_IPV6_ADDR_RANGE, ANY, 0, 65535, '::', 'ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff']
        _ts_mode = os.environ.get("SWU_TS_MODE", "auto").strip().lower()
        if _ts_mode not in ("auto", "dual", "v4", "v6"):
            _ts_mode = "auto"
        if _ts_mode == "auto":
            _want_v4 = any(x[0] == INTERNAL_IP4_ADDRESS for x in cp_list[1:])
            _want_v6 = any(x[0] == INTERNAL_IP6_ADDRESS for x in cp_list[1:])
            if _want_v6 and not _want_v4:
                _fams = ("v6",)
            elif _want_v4 and not _want_v6:
                _fams = ("v4",)
            else:
                _fams = ("v4", "v6")
        elif _ts_mode == "dual":
            _fams = ("v4", "v6")
        else:
            _fams = (_ts_mode,)
        _ts = ([list(_TS_V4)] if "v4" in _fams else []) + ([list(_TS_V6)] if "v6" in _fams else [])
        print("[swu_ike] TS families: %s (SWU_TS_MODE=%s)" % (",".join(_fams), _ts_mode))
        self.set_cp_list(cp_list)
        self.set_ts_list(TSI, [list(x) for x in _ts])
        self.set_ts_list(TSR, [list(x) for x in _ts])
        self.cp_mode_current = mode
        # Clear any address/P-CSCF lists from a previous family attempt so _have_pcscf() and the
        # inner-IP selection never see a stale value carried over across the auto ladder.
        self.pcscf_address_list = []
        self.pcscfv6_address_list = []
        self.ip_address_list = []
        self.ipv6_address_list = []

    def _have_pcscf(self):
        """True if the ePDG returned at least one P-CSCF (v4 or v6) in the CFG reply. An empty
        P-CSCF on a CONNECTED tunnel = unusable IMS (the Telus-on-dual case)."""
        return bool(getattr(self, "pcscfv6_address_list", []) or getattr(self, "pcscf_address_list", []))

    def _light_delete(self):
        """Send an IKE DELETE to free the ePDG SA when abandoning a CP family BEFORE state_connected()
        (no ipsec pipes / routes exist yet, so state_delete's teardown can't be used). Best-effort: a
        lingering half-SA would otherwise risk NO_PROPOSAL_CHOSEN on the next IKE_SA_INIT."""
        try:
            self.message_id_request += 1
            self.send_data(self.create_INFORMATIONAL_delete(IKE))
            print('sending INFORMATIONAL (delete IKE) — abandoning CP family %s'
                  % getattr(self, "cp_mode_current", "?"))
        except Exception as e:
            swu_log("light delete failed: %r" % e)

    def _report_resolved_mode(self):
        """Auto-discovery success: pin the line to the CP family that worked. Cache it locally so an
        in-container swu restart skips the ladder, and notify the manager so it persists the resolved
        mode across container recreation (control/app/main.api_engine_event -> overwrites cp_mode)."""
        if not getattr(self, "cp_mode_auto", False) or getattr(self, "_winner_reported", False):
            return
        self._winner_reported = True
        mode = getattr(self, "cp_mode_current", "")
        try:
            with open(os.path.join(SWU_RUNDIR, "cp_mode.resolved"), "w") as f:
                f.write(mode)
        except Exception:
            pass
        swu_log("CP auto-discovery: family %s works — reporting resolved mode to manager" % mode)
        swu_notify("cp_mode_resolved", mode)

    def start_ike(self):
        # CP-mode ladder. For an auto line, self.cp_mode_candidates is the ordered family list to try
        # (e.g. ['v6','dual','v4'], DB-preferred first). Advance to the next family ONLY when SIM auth
        # SUCCEEDED (EAP-Success) but the PDN was unusable — the final IKE_AUTH was rejected (e.g.
        # Vodafone 16375) or the ePDG returned NO P-CSCF (Telus-on-dual). A failure before/at EAP is
        # not a CP problem and does not cycle families. A pinned (non-auto) line has one candidate, so
        # behaviour is unchanged.
        candidates = getattr(self, "cp_mode_candidates", None) or [getattr(self, "cp_mode_current", "v6")]
        mode_idx = 0
        while mode_idx < len(candidates):
            self.apply_cp_ts_mode(candidates[mode_idx])
            advance = False
            self.iterations = 2
            self.cookie = False
            while self.iterations>0:

                self.iterations -= 1
                # G2: clear any reject classification from a prior attempt before this one.
                self.reject_reason_code = None
                self.reject_reason_policy = None
                self.reject_backoff_seconds = None
                # Auto CP-mode heuristic: track whether EAP-AKA reached EAP-Success this attempt, so a
                # post-EAP PDN failure can be told apart from a genuine SIM/identity failure.
                self.eap_succeeded = False

                print('\nSTATE 1:\n-------')
                result,info = self.state_1()
                if result in (REPEAT_STATE, TIMEOUT):
                    print(self.errors.get(result),':',info)
                    print('\nSTATE 1 (retry 1):\n------- -------')
                    result,info = self.state_1(retry=True)
                elif result in (REPEAT_STATE_COOKIE,):
                    print(self.errors.get(result),':',info)
                    print('\nSTATE 1 (retry 1 with cookie):\n------- -------')
                    result,info = self.state_1(retry=True, cookie=True)

                if result in (REPEAT_STATE, TIMEOUT):
                    print(self.errors.get(result),':',info)
                    print('\nSTATE 1: (retry 2)\n------- -------')
                    if self.cookie == True:
                        result,info = self.state_1(retry=True, cookie=True)
                    else:
                        result,info = self.state_1(retry=True)

                if result == OK:
                    print('\nSTATE 2:\n-------')
                    result,info = self.state_2()
                else:
                    print(self.errors.get(result),':',info)
                    self._reject_guard()
                    continue

                if result in (REPEAT_STATE, OK):
                    if result in (REPEAT_STATE,):
                        print(self.errors.get(result),':',info)
                        print('\nSTATE 2 (repeat):\n---------------')
                        result,info = self.state_2(retry=True)
                    if result in (OK,):
                        print('\nSTATE 3:\n-------')
                        result,info = self.state_3()
                else:
                    print(self.errors.get(result),':',info)
                    self._reject_guard()
                    continue

                if result in (OK, REPEAT_STATE):
                    if result in (REPEAT_STATE,):
                        print(self.errors.get(result),':',info)
                        print('\nSTATE 3 (repeat):\n---------------')
                        result,info = self.state_3()
                    if result in (OK,):
                        # EAP-AKA succeeded (EAP-Success received). From here a failure is a PDN/CP
                        # problem, not a SIM problem — the auto heuristic may retry another family.
                        self.eap_succeeded = True
                        print('\nSTATE 4:\n-------')
                        result,info = self.state_4()
                else:
                    print(self.errors.get(result),':',info)
                    self._reject_guard()
                    continue

                if result == OK:
                    # Full attach. On an auto line a CONNECTED tunnel with NO P-CSCF is unusable
                    # (Telus-on-dual): abandon this family and try the next.
                    if getattr(self, "cp_mode_auto", False) and not self._have_pcscf():
                        print('[swu_ike] tunnel established but ePDG returned NO P-CSCF for CP '
                              'family %s — trying next family' % self.cp_mode_current)
                        self._light_delete()
                        advance = True
                        break
                    print('\nSTATE CONNECTED. Press q to quit, i to rekey ike, c to rekey child sa, r to reauth.\n')
                    self._report_resolved_mode()
                    self.state_connected()
                else:
                    # state_4 (final IKE_AUTH) failed. On an auto line, if EAP already succeeded this
                    # is a post-EAP PDN reject (e.g. 16375) => wrong CP family: advance instead of
                    # burning the bounded retries re-trying the same family.
                    if getattr(self, "cp_mode_auto", False) and self.eap_succeeded:
                        print(self.errors.get(result),':',info)
                        advance = True
                        break
                    print(self.errors.get(result),':',info)
                    self._reject_guard()
                    continue

            if advance and mode_idx < len(candidates) - 1:
                swu_log("CP family %s reached EAP-Success but no usable PDN; falling back to %s"
                        % (candidates[mode_idx], candidates[mode_idx + 1]))
                mode_idx += 1
                continue
            break

        exit(1)
           
        

#######################################################################################################################
#######################################################################################################################
#######################################################################################################################
#######################################################################################################################
#######################################################################################################################
#######################################################################################################################
#######################################################################################################################
#######################################################################################################################
#######################################################################################################################
#######################################################################################################################

def get_default_gateway_linux():
    """Read the default gateway directly from /proc."""
    with open("/proc/net/route") as fh:
        for line in fh:
            fields = line.strip().split()
            if fields[1] != '00000000' or not int(fields[3], 16) & 2:
                continue
           
            return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16))), fields[0]

def get_default_source_address():

    proc = subprocess.Popen("/sbin/ifconfig | grep -A 1 " + get_default_gateway_linux()[1] + " | grep inet", stdout=subprocess.PIPE, shell=True)
    output = str(proc.stdout.read())
    if 'addr:' in output:
        addr = output.split('addr:')[1].split()[0]
    else:
        addr = output.split('inet ')[1].split()[0]
    return addr

def toHex(value): # bytes hex string
    return hexlify(value).decode('utf-8')

def fromHex(value): # hex string to bytes
    return unhexlify(value)
    

def sha1_dss(data):  #for MSK
#based on code from https://codereview.stackexchange.com/questions/37648/python-implementation-of-sha1    

    h0 = 0x67452301
    h1 = 0xEFCDAB89
    h2 = 0x98BADCFE
    h3 = 0x10325476
    h4 = 0xC3D2E1F0

    def rol(n, b):
        return ((n << b) | (n >> (32 - b))) & 0xffffffff

    #special padding. data always 160 bits (20 bytes, so 44 bytes left to 64Bytes block)
    padding = 44*b'\x00'
    padded_data = data + padding 
    
    thunks = [padded_data[i:i+64] for i in range(0, len(padded_data), 64)]
    for thunk in thunks:
        w = list(struct.unpack('>16L', thunk)) + [0] * 64
        for i in range(16, 80):
            w[i] = rol((w[i-3] ^ w[i-8] ^ w[i-14] ^ w[i-16]), 1)

        a, b, c, d, e = h0, h1, h2, h3, h4

        # Main loop
        for i in range(0, 80):
            if 0 <= i < 20:
                f = (b & c) | ((~b) & d)
                k = 0x5A827999
            elif 20 <= i < 40:
                f = b ^ c ^ d
                k = 0x6ED9EBA1
            elif 40 <= i < 60:
                f = (b & c) | (b & d) | (c & d) 
                k = 0x8F1BBCDC
            elif 60 <= i < 80:
                f = b ^ c ^ d
                k = 0xCA62C1D6

            a, b, c, d, e = rol(a, 5) + f + e + k + w[i] & 0xffffffff, \
                            a, rol(b, 30), c, d

        h0 = h0 + a & 0xffffffff
        h1 = h1 + b & 0xffffffff
        h2 = h2 + c & 0xffffffff
        h3 = h3 + d & 0xffffffff
        h4 = h4 + e & 0xffffffff

    #return '%08x%08x%08x%08x%08x' % (h0, h1, h2, h3, h4)
    return struct.pack('!I',h0) + struct.pack('!I',h1) + struct.pack('!I',h2) + struct.pack('!I',h3) + struct.pack('!I',h4)


#abstraction functions

def return_imsi(serial_interface_or_reader_index):
    try:
        return read_imsi_2(serial_interface_or_reader_index)
    except:
        try:
            return get_imsi(serial_interface_or_reader_index)
        except:
            try:
                return https_imsi(serial_interface_or_reader_index)
            except:
                print('Unable to access serial port/smartcard reader/server; using test identity')
                return DEFAULT_IMSI
        
def return_res_ck_ik(serial_interface_or_reader_index, rand, autn):
    """Run AKA only in the physical SIM/eSIM.

    MDD Sim Gateway deliberately does not accept Ki/OP/OPc and never falls back to demo
    vectors.  A card access failure must fail closed so operators see the real fault.
    """
    try:
        return read_res_ck_ik_2(serial_interface_or_reader_index, rand, autn)
    except Exception as pcsc_error:
        try:
            return get_res_ck_ik(serial_interface_or_reader_index, rand, autn)
        except Exception as serial_error:
            try:
                return https_res_ck_ik(serial_interface_or_reader_index, rand, autn)
            except Exception as remote_error:
                print('Unable to authenticate with the SIM (PC/SC=%r, serial=%r, remote=%r)' %
                      (pcsc_error, serial_error, remote_error))
                return None, None, None




def get_imsi(serial_interface):

    imsi = None
    
    ser = serial.Serial(serial_interface,38400, timeout=0.5,xonxoff=True, rtscts=True, dsrdtr=True, exclusive =True)

    CLI = []
    CLI.append('AT+CIMI\r\n')
    
    a = time.time()
    for i in range(len(CLI)):
        ser.write(CLI[i].encode())
        buffer = ''

        while "OK\r\n" not in buffer and "ERROR\r\n" not in buffer:
            buffer +=  ser.read().decode("utf-8")
            
            if time.time()-a > 0.5:
                ser.write(CLI[i].encode())
                a = time.time() +1
            
        if i==0:    
            for m in buffer.split('\r\n'):
                if len(m) == 15:
                    imsi = m
         
    ser.close()
    return imsi


def get_res_ck_ik(serial_interface, rand, autn):
    res = None
    ck = None
    ik = None
    
    ser = serial.Serial(serial_interface,38400, timeout=0.5,xonxoff=True, rtscts=True, dsrdtr=True, exclusive =True)

    CLI = []
   
    #CLI.append('AT+CRSM=178,12032,1,4,0\r\n')
    CLI.append('AT+CSIM=14,"00A40000023F00"\r\n')
    CLI.append('AT+CSIM=14,"00A40000022F00"\r\n')
    CLI.append('AT+CSIM=42,"00A4040010A0000000871002FF44FFFF8901010100"\r\n')
    CLI.append('AT+CSIM=78,\"008800812210' + rand.upper() + '10' + autn.upper() + '\"\r\n')

    a = time.time()
    for i in CLI:
        ser.write(i.encode())
        buffer = ''
    
        while "OK" not in buffer and "ERROR" not in buffer:
            buffer +=  ser.read().decode("utf-8")
        
            if time.time()-a > 0.5:
                ser.write(i.encode())

                a = time.time() + 1
                
    for i in buffer.split('"'):
        if len(i)==4:
            if i[0:2] == '61':
                len_result = i[-2:]
    
    LAST_CLI = 'AT+CSIM=10,"00C00000' + len_result + '\"\r\n'
    ser.write(LAST_CLI.encode())
    buffer = ''
    
    while "OK\r\n" not in buffer and "ERROR\r\n" not in buffer:
        buffer +=  ser.read().decode("utf-8")
        
    for result in buffer.split('"'):
        if len(result) > 10:
        

            res = result[4:20]
            ck = result[22:54]
            ik = result[56:88]
    
    ser.close()    
    return res, ck, ik
    

#reader functions
def bcd(chars):
    bcd_string = ""
    for i in range(len(chars) // 2):
        bcd_string += chars[1+2*i] + chars[2*i]
    return bcd_string

# EF.ICCID is read with pcsc-lite's blocking transmit(), which takes no timeout. On a VPCD
# logical channel that read can HANG rather than fail: the bridge multiplexes several channels
# onto one physical SIM, so a channel it has not wired up leaves transmit() parked forever and
# reader_holds_foreign_card() below never reaches its "only a successful read convicts"
# fallback. Bound the read and surface the deadline as the exception that handler already
# catches, so the tunnel proceeds instead of stalling.
#
# The check itself stays in force on every reader, VPCD included: two serial-less modems that
# swap USB paths is precisely the mix-up it exists to catch.
# Asterisk allows the whole AKA exchange SIM_TIMEOUT = 3s, so nothing on a card path may
# block longer than that; this probe runs once at startup rather than per authentication.
ICCID_READ_TIMEOUT = 2.0


class CardReadTimeout(Exception):
    """EF.ICCID did not answer within the deadline."""


def _with_deadline(fn, timeout=None):
    """Run a blocking card read on a daemon thread and give up after `timeout` seconds."""
    # Resolved per call, not bound as a default, so the module constant stays adjustable at
    # runtime (and patchable in tests) instead of being frozen when this function is defined.
    timeout = ICCID_READ_TIMEOUT if timeout is None else timeout
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa - handed back to the calling thread below
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise CardReadTimeout("EF.ICCID read did not answer within %gs" % timeout)
    if "error" in box:
        raise box["error"]
    return box.get("value")


_ICCID_BYTES = 10
_ICCID_MIN_DIGITS = 15


def read_iccid_at_index(reader_index):
    """Read EF.ICCID from a reader index. No PIN needed; None when it will not answer readably.

    EF.ICCID is exactly 10 BCD bytes (TS 31.102). A short read, or a value that is not
    all digits, is a card or reader fault rather than an identity -- and every caller
    convicts a reader on ANY non-empty ICCID that differs from the line's, so handing one
    a truncated value would strand a line whose binding is perfectly correct. Returning
    None keeps the documented fail-open direction: we simply cannot convict.
    """
    r = readers()
    connection = r[int(reader_index)].createConnection()
    connection.connect()
    try:
        connection.transmit(toBytes('00A40000023F00'))
        connection.transmit(toBytes('00A40000022FE2'))
        data, sw1, sw2 = connection.transmit(toBytes('00B000000A'))
        if sw1 != 0x90 or len(data) != _ICCID_BYTES:
            return None
        iccid = bcd(toHexString(data).replace(" ", "")).rstrip("Ff")
        return iccid if iccid.isdigit() and len(iccid) >= _ICCID_MIN_DIGITS else None
    finally:
        try:
            connection.disconnect()
        except Exception:
            pass


def reader_holds_foreign_card(reader_index, expected_iccid):
    """The ICCID actually in `reader_index` when it is provably not this line's, else None.

    The SWu tunnel addresses its SIM by PC/SC index, which names a slot rather than a card.
    Running IKE_AUTH against whatever card occupies that slot fails with SW=9862 -- the same
    code a carrier returns for a genuinely rejected subscriber -- so the two are impossible to
    tell apart downstream. Reading EF.ICCID first costs one PIN-free APDU exchange and makes
    the difference explicit. An unreadable card returns None: only a successful read convicts.
    """
    if not expected_iccid:
        return None
    try:
        actual = _with_deadline(lambda: read_iccid_at_index(reader_index))
    except Exception as e:  # noqa - PC/SC hiccup is not evidence of a swapped card
        swu_log("reader bind: could not read EF.ICCID at index %s (%r)" % (reader_index, e))
        return None
    if actual and actual != expected_iccid:
        return actual
    return None


def read_imsi(reader_index):
    imsi = None
    r = readers()
    connection = r[int(reader_index)].createConnection()
    connection.connect()
    data, sw1, sw2 = connection.transmit(toBytes('00A40000023F00'))     
    data, sw1, sw2 = connection.transmit(toBytes('00A40000027F20'))
    data, sw1, sw2 = connection.transmit(toBytes('00A40000026F07'))
    data, sw1, sw2 = connection.transmit(toBytes('00B0000009'))  
    result = toHexString(data).replace(" ","")
    imsi = bcd(result)[-15:]
    
    return imsi

def read_res_ck_ik(reader_index, rand, autn):
    res = None
    ck = None
    ik = None
    r = readers()
    connection = r[int(reader_index)].createConnection()
    connection.connect()
    data, sw1, sw2 = connection.transmit(toBytes('00A40000023F00'))    
    data, sw1, sw2 = connection.transmit(toBytes('00A40000022F00')) 
    data, sw1, sw2 = connection.transmit(toBytes('00A4040010A0000000871002FF44FFFF8901010100'))
    data, sw1, sw2 = connection.transmit(toBytes('008800812210' + rand.upper() + '10' + autn.upper()))   
    if sw1 == 97:
        data, sw1, sw2 = connection.transmit(toBytes('00C00000') + [sw2])         
        result = toHexString(data).replace(" ", "")
        res = result[4:20]
        ck = result[22:54]
        ik = result[56:88]          

    return res, ck, ik

#reader functions - more generic using card module
def read_imsi_2(reader_index): #prepared for AUTS
    a = USIM(int(reader_index))
    return a.get_imsi()
    
def read_res_ck_ik_2(reader_index,rand,autn):
    # PIN handling (VoWiFi engine addition): if USIM_PIN is set, we MUST VERIFY CHV1 before
    # AUTHENTICATE or PIN-enabled cards (e.g. Telus) return 0x6982. VERIFY + AUTHENTICATE must
    # happen in the SAME PC/SC connection (CHV1 does not reliably persist across a fresh
    # connection when the card is reset). When no PIN is configured we fall through to the
    # UPSTREAM path (mitshell card.USIM) unchanged, so PIN-less / 3rd-party cards behave exactly
    # as the original emulator.
    pin = os.environ.get("USIM_PIN", "").strip()
    if pin and pin.lower() not in ("none", "disabled"):
        return _read_res_ck_ik_pin(reader_index, rand, autn, pin)

    # PIN-disabled: use the explicit reader-by-index path (VERIFY skipped), NOT the upstream
    # mitshell USIM(int) constructor. That constructor mis-selects the reader: card.ICC.__init__
    # treats its arg as a reader *name* for CardRequest(readers=[...]) — reader index 0 is FALSY so
    # it falls back to "any card" and happens to grab reader 0, but a non-zero index passes a bare
    # int as a reader object and waitforcard() HANGS (blocks before the first APDU). So a PIN-less
    # SIM on any reader other than 0 could never authenticate. The explicit path addresses
    # readers()[index] directly and works on every reader.
    return _read_res_ck_ik_pin(reader_index, rand, autn, None)


# --- USIM AKA with CHV1 verify, single connection (VoWiFi engine addition) ------------------
# Mirrors engine/pin_keeper.py: SELECT MF -> EF.DIR (scan for the 3GPP USIM AID, robust across
# vendors that list CSIM first) -> ADF.USIM -> [VERIFY CHV1] -> AUTHENTICATE(AKA). Returns the
# same (res, ck, ik) / (auts, None, None) convention the caller expects.
_USIM_AID_PREFIX = "A0000000871002"


def _swu_apdu_with_le(apdu, le):
    """Rebuild an APDU carrying the Le the card asked for in a 6Cxx status. Case 2 replaces
    the trailing Le byte; case 3/4 keeps Lc and the command data and re-stamps only Le.
    Truncating to CLA/INS/P1/P2 (what the first version did) turned a case-4 SELECT into a
    malformed command, so the retry failed and a healthy file read as unselectable."""
    head = list(apdu[:4])
    if len(apdu) <= 5:
        return head + [le]
    lc = apdu[4]
    return head + [lc] + list(apdu[5:5 + lc]) + [le]


def _swu_xfr(conn, apdu):
    """Transmit one APDU, normalizing reader/protocol variance (issue #51). TPDU-level
    T=0 readers answer case-4 commands with 61xx and expect an explicit GET RESPONSE;
    APDU-level readers and T=1 hand back the data with 9000 directly. 6Cxx means
    "wrong Le, retry with mine". Callers see one shape: (data, 0x90, 0x00) on success.

    61xx already means the card ACCEPTED the command -- the GET RESPONSE that follows only
    fetches the body. Reporting that fetch's status as the command's status made a good
    SELECT ADF.USIM read as a card fault on cards whose GET RESPONSE answers anything but
    9000, which is issue #60: read_card bailed out with no PIN state at all and the start
    preflight then asked for a PIN the card never wanted. Callers that need the body check
    the body they got, so answering "accepted, here is what we could fetch" is safe."""
    data, s1, s2 = conn.transmit(apdu)
    data = list(data)
    accepted = False
    for _ in range(8):      # bound: a card that keeps re-asking cannot spin us forever
        if s1 == 0x61:
            accepted = True
            more, s1, s2 = conn.transmit([0x00, 0xC0, 0x00, 0x00, s2])
            data += list(more)
            continue
        if s1 == 0x6C and s2 and not accepted:
            data, s1, s2 = conn.transmit(_swu_apdu_with_le(apdu, s2))
            data = list(data)
            continue
        break
    if accepted and s1 != 0x90:
        return data, 0x90, 0x00
    return data, s1, s2


def _swu_select_adf_usim(conn):
    conn.transmit(toBytes("00a40004023f0000"))               # SELECT MF
    fcp, s1, s2 = _swu_xfr(conn, toBytes("00a40004022f0000"))  # SELECT EF.DIR
    if s1 != 0x90 or len(fcp) < 8:
        return False
    rec_len = fcp[7]
    aid = None
    first = None
    for rec in range(1, 11):
        d, s1, s2 = _swu_xfr(conn, toBytes("00b2") + [rec, 0x04, rec_len])
        if s1 != 0x90 or len(d) < 5 or d[0] != 0x61 or d[2] != 0x4F:
            break
        aid_len = d[3]
        a = "".join("%02X" % b for b in d[4:4 + aid_len])
        if len(a) < aid_len * 2:
            break
        if a.startswith(_USIM_AID_PREFIX):
            aid = (aid_len, a)
            break
        if first is None:
            first = (aid_len, a)
    if aid is None:
        aid = first
    if aid is None:
        return False
    aid_len, a = aid
    d, s1, s2 = _swu_xfr(conn, toBytes("00a40404") + [aid_len] + toBytes(a))
    return s1 == 0x90


def _swu_verify_chv1(conn, pin):
    """Non-consuming tries probe, then VERIFY if needed. PUK-safe (never spend below 2 tries)."""
    d, s1, s2 = conn.transmit(toBytes("0020000100"))         # VERIFY, empty body
    if (s1, s2) == (0x90, 0x00):
        return True                                          # already verified this session
    if s1 == 0x63:
        if (s2 & 0x0F) < 2:
            print("VoWiFi: refusing PIN VERIFY, only %d tries left (PUK-safe)" % (s2 & 0x0F))
            return False
    elif (s1, s2) == (0x69, 0x83):
        print("VoWiFi: CHV1 blocked")
        return False
    if not (4 <= len(pin) <= 8) or not pin.isdigit():
        print("VoWiFi: refusing PIN VERIFY, malformed PIN (want 4-8 digits)")
        return False
    body = [ord(c) for c in pin] + [0xFF] * (8 - len(pin))
    d, s1, s2 = conn.transmit(toBytes("00200001") + [0x08] + body)
    ok = (s1, s2) == (0x90, 0x00)
    print("VoWiFi: VERIFY CHV1 %s before AUTHENTICATE" % ("ok" if ok else "FAILED sw=%02x%02x" % (s1, s2)))
    return ok


def _pcsc_hcard(conn):
    obj = conn
    for _ in range(5):
        if hasattr(obj, "hcard"):
            return obj.hcard
        if hasattr(obj, "component") and obj.component is not None:
            obj = obj.component
            continue
        break
    return None


def _read_res_ck_ik_pin(reader_index, rand, autn, pin):
    r = readers()
    conn = r[int(reader_index)].createConnection()
    conn.connect()
    # Exclusive PC/SC transaction for the whole SELECT->VERIFY->AUTHENTICATE sequence: pcscd
    # serializes single APDUs but NOT multi-APDU groups, so without this ami_usim's own AKA
    # APDUs could interleave during a reauth and corrupt the sequence.
    hcard = _pcsc_hcard(conn) if SCardBeginTransaction else None
    if hcard is not None:
        try:
            SCardBeginTransaction(hcard)
        except Exception:
            hcard = None
    try:
        if not _swu_select_adf_usim(conn):
            print("VoWiFi: ADF.USIM select failed")
            return None, None, None
        if pin:
            _swu_verify_chv1(conn, pin)
        # AUTHENTICATE (AKA security context): 00 88 00 81 22 10 <RAND> 10 <AUTN>
        apdu = "00880081" + "22" + "10" + rand.upper() + "10" + autn.upper()
        d, s1, s2 = conn.transmit(toBytes(apdu))
        if s1 == 0x61:
            d, s1, s2 = conn.transmit(toBytes("00C00000") + [s2])
        if (s1, s2) != (0x90, 0x00):
            print("VoWiFi: AUTHENTICATE failed sw=%02x%02x" % (s1, s2))
            return None, None, None
        result = toHexString(d).replace(" ", "")
        if not result:
            return None, None, None
        tag = result[0:2].lower()
        if tag == "db":            # success: 'db' <res_len> RES <ck_len> CK <ik_len> IK
            p = 2
            rl = int(result[p:p + 2], 16); p += 2
            res = result[p:p + rl * 2]; p += rl * 2
            cl = int(result[p:p + 2], 16); p += 2
            ck = result[p:p + cl * 2]; p += cl * 2
            il = int(result[p:p + 2], 16); p += 2
            ik = result[p:p + il * 2]
            return res, ck, ik
        if tag == "dc":            # sync failure: 'dc' <auts_len> AUTS  (CK=None signals AUTS)
            al = int(result[2:4], 16)
            return result[4:4 + al * 2], None, None
        return None, None, None
    finally:
        if hcard is not None:
            try:
                SCardEndTransaction(hcard, SCARD_LEAVE_CARD)
            except Exception:
                pass
        try:
            conn.disconnect()
        except Exception:
            pass


# --- Reader binding by physical USB port (VoWiFi engine addition) ----------------------------
# The engine is launched with a fixed reader index (-m N). But the rig may hold two IDENTICAL
# readers (no USB serial), so pcscd/libccid tell them apart only by enumeration order — which
# races at boot / pcscd restart and can FLIP the two readers' indices with the cables untouched.
# The container self-heals swu_ike in-process on every reauth (and docker may auto-restart it),
# always reusing the baked -m index; once the index flips, the engine reads the WRONG (or an
# empty) reader -> card access fails -> return_res_ck_ik falls through to "Using DEFAULT RES, CK
# and IK" -> the ePDG rejects EAP-AKA, forever. To fix this at the source we bind to the reader's
# STABLE physical USB port path (e.g. "3-2") and resolve the live index from it at startup.
#
# index -> (bus,devnum): SCARD_ATTR_CHANNEL_ID (USB) is little-endian u32 0x00200000|(bus<<8)|dev.
# (bus,devnum) -> port path: walk /sys/bus/usb/devices/*/{busnum,devnum} (host sysfs is visible
# read-only inside the container). Best-effort: any failure returns the fallback index unchanged.
try:
    from smartcard.scard import (
        SCardEstablishContext as _SCEstablish, SCardReleaseContext as _SCRelease,
        SCardConnect as _SCConnect,
        SCardGetAttrib as _SCGetAttrib, SCardDisconnect as _SCDisconnect,
        SCARD_SCOPE_USER as _SC_SCOPE_USER, SCARD_SHARE_DIRECT as _SC_SHARE_DIRECT,
        SCARD_LEAVE_CARD as _SC_LEAVE, SCARD_PROTOCOL_T0 as _SC_T0,
        SCARD_PROTOCOL_T1 as _SC_T1, SCARD_ATTR_CHANNEL_ID as _SC_CHANNEL_ID,
        SCARD_S_SUCCESS as _SC_OK,
    )
    _SC_PORT_OK = True
except Exception:                        # pragma: no cover
    _SC_PORT_OK = False


def _reader_bus_dev(reader_name):
    """(bus, devnum) via SCARD_ATTR_CHANNEL_ID (SHARE_DIRECT, no card / no APDU), or None."""
    if not _SC_PORT_OK:
        return None
    hctx = hcard = None
    try:
        hr, hctx = _SCEstablish(_SC_SCOPE_USER)
        if hr != _SC_OK:
            return None
        hr, hcard, _p = _SCConnect(hctx, reader_name, _SC_SHARE_DIRECT, _SC_T0 | _SC_T1)
        if hr != _SC_OK:
            return None
        hr, val = _SCGetAttrib(hcard, _SC_CHANNEL_ID)
        if hr != _SC_OK or not val or len(val) < 4:
            return None
        v = val[0] | (val[1] << 8) | (val[2] << 16) | (val[3] << 24)
        if (v >> 16) != 0x0020:          # not a USB channel id
            return None
        return (v >> 8) & 0xff, v & 0xff
    except Exception:
        return None
    finally:
        if hcard is not None:
            try:
                _SCDisconnect(hcard, _SC_LEAVE)
            except Exception:
                pass
        if hctx is not None:
            try:
                _SCRelease(hctx)
            except Exception:
                pass


def _usb_port_path(bus, devnum):
    import glob as _glob
    try:
        entries = _glob.glob("/sys/bus/usb/devices/*/")
    except Exception:
        return None
    for d in entries:
        try:
            with open(d + "busnum") as f:
                b = int(f.read())
            with open(d + "devnum") as f:
                n = int(f.read())
        except Exception:
            continue
        if b == bus and n == devnum:
            return os.path.basename(d.rstrip("/"))
    return None


def resolve_reader_index_by_port(port, fallback_index):
    """Return the live PC/SC reader index whose physical USB port == `port`, or `fallback_index`
    if it can't be resolved (no port configured, port absent, or PC/SC/sysfs unavailable)."""
    if not port:
        return fallback_index
    try:
        rlist = readers()
    except Exception as e:  # noqa
        swu_log("reader bind: PC/SC unavailable (%r); using index %s" % (e, fallback_index))
        return fallback_index
    for i, r in enumerate(rlist):
        bd = _reader_bus_dev(str(r))
        if not bd:
            continue
        if _usb_port_path(bd[0], bd[1]) == port:
            if str(i) != str(fallback_index):
                swu_log("reader bind: USB port %s is at index %d (was -m %s) -> using %d"
                        % (port, i, fallback_index, i))
            else:
                swu_log("reader bind: USB port %s confirmed at index %d" % (port, i))
            return i
    swu_log("reader bind: USB port %s not found on any reader; using fallback index %s"
            % (port, fallback_index))
    return fallback_index


#https functions
def https_imsi(server):
    r = requests.get('https://' + server + '/?type=imsi', verify=False)
    return r.json()['imsi']

def https_res_ck_ik(server, rand, autn):
    r = requests.get('https://' + server + '/?type=rand-autn&rand=' + rand + '&autn=' + autn, verify=False)
    return r.json()['res'], r.json()['ck'], r.json()['ik']





#################################################################################################################    
#####
#####   SA Structure:
#####   ------------
#####
#####   sa_list = [ (proposal 1), (proposal 2), ... , (proposal n)   ]
#####
#####   proposal = (Protocol ID, SPI Size) , (Transform 1), (transform 2), ... , (transform n)
#####
#####   transform = Tranform Type, Transform ID, (Transform Attributes)
#####
#####   transform attribute = Attribute type, value
#####
#################################################################################################################


#################################################################################################################    
#####
#####   TS Structure:
#####   ------------
#####
#####   ts_list = [ (ts 1), (ts 2), ... , (ts n)   ]
#####
#####   ts = ts_type, ip_protocol_id, start_port, end_port, starting_address, ending_address
#####
#################################################################################################################


#################################################################################################################    
#####
#####   CP Structure:
#####   ------------
#####
#####   cp_list = [ cfg_type, (attribute 1), ... , (attribute n)   ]
#####
#####   attribute = attribute type, value1, value2, .... (depends on the attribute type)
#####
#################################################################################################################



def main():

    # CP+TS address-family mode (per line, from cfg.cp_mode). The CFG config-request family MUST
    # match the carrier's IMS PDN or the ePDG rejects the PDN after EAP-Success (Telus: no P-CSCF on
    # a v4/dual request; Vodafone UK: private Notify 16375 on a v6-only request). Modes:
    #   auto (default) — try a ladder of families and keep the one that yields a USABLE PDN (a
    #                    CONNECTED tunnel WITH a P-CSCF). Order = SWU_CP_MODE_ORDER (the control plane
    #                    puts the carrier-DB preference first), default v6,dual,v4. The winning family
    #                    is reported back so the line stops re-discovering.
    #   v6 / v4 / dual — pinned single family (no discovery). TS follows CP via SWU_TS_MODE=auto.
    # The cp_list/ts_list are built per-attempt in swu.apply_cp_ts_mode() (called by start_ike).
    _cp_mode = os.environ.get("SWU_CP_MODE", "auto").strip().lower()
    if _cp_mode not in ("auto", "v6", "v4", "dual"):
        _cp_mode = "auto"
    _cp_auto = (_cp_mode == "auto")
    if _cp_auto:
        # Prefer a family this line already resolved to in a previous run of THIS container (avoids
        # re-walking the ladder on an in-container swu restart; the manager separately persists it
        # across container recreation by overwriting cfg.cp_mode).
        _resolved = None
        try:
            with open(os.path.join(SWU_RUNDIR, "cp_mode.resolved")) as _f:
                _r = _f.read().strip().lower()
                if _r in ("v6", "v4", "dual"):
                    _resolved = _r
        except Exception:
            _resolved = None
        if _resolved:
            _candidates = [_resolved]
            print("[swu_ike] CP mode auto: using previously-resolved family %s" % _resolved)
        else:
            _seen = set(); _candidates = []
            for m in os.environ.get("SWU_CP_MODE_ORDER", "v6,dual,v4").split(","):
                m = m.strip().lower()
                if m in ("v6", "v4", "dual") and m not in _seen:
                    _seen.add(m); _candidates.append(m)
            if not _candidates:
                _candidates = ["v6", "dual", "v4"]
            print("[swu_ike] CP mode auto: discovery ladder %s" % ",".join(_candidates))
    else:
        _candidates = [_cp_mode]
        print("[swu_ike] CP mode pinned: %s" % _cp_mode)


    # Child/ESP proposals. AES_CBC_128/HMAC_SHA1_96 first — the transform Telus selected with
    # strongSwan (render.py default_esp). Remaining kept as fallbacks. No DH transform here
    # (no PFS at initial IKE_AUTH).
    sa_list_child = [
    [
        [ESP,4],
        [ENCR,ENCR_AES_CBC,[KEY_LENGTH,128]],
        [INTEG,AUTH_HMAC_SHA1_96],
        [ESN,ESN_NO_ESN]
    ] ,
    [
        [ESP,4],
        [ENCR,ENCR_AES_CBC,[KEY_LENGTH,256]],
        [INTEG,AUTH_HMAC_SHA2_256_128],
        [ESN,ESN_NO_ESN]
    ] ,
    [
        [ESP,4],
        [ENCR,ENCR_AES_CBC,[KEY_LENGTH,128]],
        [INTEG,AUTH_HMAC_SHA2_256_128],
        [ESN,ESN_NO_ESN]
    ] ,
    [
        [ESP,4],
        [ENCR,ENCR_AES_CBC,[KEY_LENGTH,256]],
        [INTEG,AUTH_HMAC_SHA1_96],
        [ESN,ESN_NO_ESN]
    ]
    ]


    parser = OptionParser()    
    parser.add_option("-m", "--modem", dest="modem", default=DEFAULT_COM, help="modem port (i.e. COMX, or /dev/ttyUSBX), smartcard reader index (0, 1, 2, ...), or server for https")
    parser.add_option("-s", "--source", dest="source_addr",default=get_default_source_address(),help="IP address of source interface used for IKE/IPSEC")
    parser.add_option("-d", "--dest", dest="destination_addr",default=DEFAULT_SERVER,help="ip address or fqdn of ePDG") 
    parser.add_option("-a", "--apn", dest="apn", default=DEFAULT_APN, help="APN to use")    
    parser.add_option("-g", "--gateway_ip_address", dest="gateway_ip_address", help="gateway IP address")    
    parser.add_option("-I", "--imsi", dest="imsi",default=DEFAULT_IMSI,help="IMSI") 
    parser.add_option("-M", "--mcc", dest="mcc",default=DEFAULT_MCC,help="MCC of ePDG (3 digits)") 
    parser.add_option("-N", "--mnc", dest="mnc",default=DEFAULT_MNC,help="MNC of ePDG (3 digits)")   

    parser.add_option("-n", "--netns", dest="netns", help="Name of network namespace for tun device")
    parser.add_option("-E", "--imei", dest="imei", default="",
                      help="IMEI (15 digits) for the ePDG DEVICE_IDENTITY response")
    parser.add_option("-V", "--imeisv", dest="imeisv", default="",
                      help="IMEISV (16 digits) for DEVICE_IDENTITY; auto-derived from IMEI if blank")

    (options, args) = parser.parse_args()
    sa_list = ike_proposals_for_plmn(options.mcc, options.mnc)
    if (str(options.mcc).zfill(3), str(options.mnc).zfill(3)) == ("515", "066"):
        print("[swu_ike] DITO 515-66: using AES-CBC-128/SHA1/MODP-1024 IKE proposal")
    
    try:
        destination_addr = socket.gethostbyname(options.destination_addr)
    except:
        print('Unable to resolve ' + options.destination_addr + '. Exiting.')
        exit(1)

    # Bind the smartcard reader to its STABLE physical USB port (USIM_READER_PORT), resolving the
    # live PC/SC index from it. This keeps the engine addressing the reader that physically holds
    # its SIM even when pcscd re-enumerated two identical readers in a different order (see
    # resolve_reader_index_by_port). Only applies when -m is an integer reader index (not a serial
    # port / https server); no-op when USIM_READER_PORT is unset -> falls back to the -m index.
    modem = options.modem
    try:
        _mi = int(str(modem))
    except (TypeError, ValueError):
        _mi = None
    if _mi is not None:
        modem = str(resolve_reader_index_by_port(os.environ.get("USIM_READER_PORT", "").strip(), _mi))
        # Prove the slot holds THIS line's SIM before spending an IKE_AUTH on it. Without this
        # the only symptom of a mis-bound reader is SW=9862 from the ePDG, indistinguishable
        # from the carrier refusing the subscriber, and the rebuild loop that follows gets
        # blamed on the exit node.
        _want_iccid = os.environ.get("USIM_ICCID", "").strip()
        _foreign = reader_holds_foreign_card(modem, _want_iccid)
        if _foreign is not None:
            swu_log("reader bind: index %s holds ICCID %s but this line is %s -- refusing to "
                    "authenticate against another line's SIM" % (modem, _foreign, _want_iccid))
            swu_write_status("WRONG_CARD", reader_index=modem,
                             iccid=_foreign, expected=_want_iccid)
            exit(1)

    a = swu(options.source_addr,destination_addr,options.apn,modem,options.gateway_ip_address,options.mcc,options.mnc,options.imsi,options.netns)

    if options.imsi == DEFAULT_IMSI: a.get_identity()
    a.set_sa_list(sa_list)
    a.set_sa_list_child(sa_list_child)
    # CP/TS are applied per attach attempt by start_ike() -> apply_cp_ts_mode(), so the auto ladder
    # can retry a different address family in the same process. Wire the mode decision here.
    a.cp_mode_auto = _cp_auto
    a.cp_mode_candidates = _candidates
    a.cp_mode_current = _candidates[0]
    a._winner_reported = False
    a.set_device_identity(options.imei, options.imeisv)

    # VoWiFi engine addition: clean shutdown. On SIGTERM/SIGINT send an IKE DELETE so the ePDG
    # frees the SA immediately -- a hard kill leaves a lingering SA that makes the NEXT
    # IKE_SA_INIT get NO_PROPOSAL_CHOSEN until the ePDG's liveness timer expires.
    def _term(signum, frame):
        swu_log("signal %d -> tearing down tunnel (IKE DELETE)" % signum)
        try:
            swu_write_status("DOWN")
            swu_notify("tunnel_down")
        except Exception:
            pass
        try:
            a.state_delete(True)     # sends INFORMATIONAL(DELETE IKE), removes routes, exit(1)
        except SystemExit:
            raise
        except Exception:
            os._exit(0)
    _signal.signal(_signal.SIGTERM, _term)
    _signal.signal(_signal.SIGINT, _term)

    swu_write_status("CONNECTING")
    try:
        a.start_ike()
    finally:
        # start_ike only returns/raises when the tunnel could not be (re)established. Preserve any
        # G2 reject classification the auth flow recorded so the manager/WebUI keep the accurate
        # reason (and so _reject_guard's DOWN+reason isn't clobbered by a bare DOWN here).
        _extra = {}
        if getattr(a, "reject_reason_code", None):
            _extra["reason_code"] = a.reject_reason_code
            if getattr(a, "reject_reason_policy", None):
                _extra["reason_policy"] = a.reject_reason_policy
            if getattr(a, "reject_backoff_seconds", None):
                _extra["backoff"] = a.reject_backoff_seconds
        swu_write_status("DOWN", **_extra)
        swu_notify("tunnel_down")
    
    
    
if __name__ == "__main__":
    main()
    
