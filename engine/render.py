#!/usr/bin/env python3
"""
render.py - Render /config/instance.json into Asterisk configs + the SWu launch env.

Reads the per-SIM instance descriptor (written by the manager, or hand-authored for
bring-up), fills the Jinja2 templates in /opt/mdd-sim-gateway/templates, and writes the final
config files. Also derives values (NAI, realm, ePDG FQDN) and computes the container
source IP used as the SWu tunnel local address.

Env overrides (used by entrypoint / keeper / ami_usim after render): USIM_PIN, USIM_READER.
"""
import ipaddress
import json
import os
import shutil
import shlex
import socket
import subprocess
import sys


def env_value(value) -> str:
    """Quote one engine.env value. Only None means "unset".

    `str(value or '')` used to swallow 0, which is how a line says "no proactive rekey": it
    reached swu_ike as an empty string and was read back as the 30-minute default, so the
    setting could not actually be turned off.
    """
    return shlex.quote("" if value is None else str(value))

from jinja2 import Environment, FileSystemLoader

TPL_DIR = os.environ.get("MDD_TPL", "/opt/mdd-sim-gateway/templates")
CFG_PATH = os.environ.get("MDD_INSTANCE", "/config/instance.json")


def _default_gateway_ipv4():
    """The container's default-route next hop (the docker bridge gateway, e.g. 172.17.0.1), read
    from /proc/net/route. Used to source-probe the docker-bridge interface reliably even when an
    IPv4 VoWiFi tunnel has made itself the default route. Returns "" if not found."""
    try:
        with open("/proc/net/route") as fh:
            for line in fh.readlines()[1:]:
                f = line.strip().split()
                # Iface Destination Gateway Flags ... ; destination 00000000 = default route,
                # skip the tunnel (ipsecN/tunN) — we want the bridge (eth0) default.
                if len(f) >= 3 and f[1] == "00000000" and int(f[3], 16) & 2:
                    if f[0].startswith(("ipsec", "tun")):
                        continue
                    gw = f[2]
                    # little-endian hex -> dotted quad
                    return ".".join(str((int(gw, 16) >> (8 * i)) & 0xFF) for i in range(4))
    except Exception:
        pass
    return ""


def container_ipv4():
    """The container's own docker-bridge IPv4 (e.g. 172.17.0.3). MUST be the bridge address, never
    the VoWiFi tunnel inner IP: it is used as the IKE source (SWU_SOURCE) and as the local SIP
    transport bind, both of which must sit on the docker bridge. A public-IP connect() probe would
    pick the tunnel's inner IP once an IPv4 PDN has made the tunnel the default route (the re-render
    after P-CSCF discovery runs post-tunnel), so probe the DOCKER GATEWAY instead — that next hop is
    always reached over the bridge, so the chosen source is the bridge IP."""
    gw = _default_gateway_ipv4()
    if gw:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((gw, 9))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            pass
        finally:
            s.close()
    try:
        out = subprocess.check_output(["hostname", "-I"], text=True).split()
        for tok in out:
            try:
                ip = ipaddress.ip_address(tok)
                if ip.version == 4 and not ip.is_loopback:
                    return str(ip)
            except ValueError:
                continue
    except Exception:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def imeisv_from_imei(imei, imeisv="", svn="00"):
    """Return a 16-digit IMEISV for the ePDG DEVICE_IDENTITY response.

    Explicit IMEISV wins (digits only, padded/truncated to 16). Otherwise derive it from the
    IMEI's first 14 digits (TAC+SNR, i.e. the IMEI without its check digit) + a 2-digit SVN
    (default '00'). Mirrors control/app/config.imeisv_from_imei so a hand-authored instance.json
    (no imeisv field) still gets a valid value. Returns '' if there is no usable IMEI/IMEISV.
    """
    isv = "".join(ch for ch in str(imeisv or "") if ch.isdigit())
    if isv:
        return (isv + "0" * 16)[:16]
    digits = "".join(ch for ch in str(imei or "") if ch.isdigit())
    if not digits:
        return ""
    base14 = digits[:14].ljust(14, "0")
    svn2 = ("".join(ch for ch in str(svn or "") if ch.isdigit()) or "00")[:2].rjust(2, "0")
    return base14 + svn2


DEFAULT_USER_AGENT = "MDD-Sim-Gateway"
MAX_USER_AGENT_LEN = 64


def sanitize_user_agent(value):
    """Return a single-line SIP User-Agent, or '' meaning "use DEFAULT_USER_AGENT".

    Mirrors control/app/config.sanitize_user_agent so a hand-authored instance.json cannot put
    a newline (and with it arbitrary Asterisk configuration) into pjsip.conf's [global]
    user_agent. ';' is dropped for the same reason it is there: Asterisk reads it as the start
    of a comment.
    """
    cleaned = "".join(ch if " " <= ch <= "~" and ch != ";" else " "
                      for ch in str(value or ""))
    return " ".join(cleaned.split())[:MAX_USER_AGENT_LEN].strip()


def build_context(cfg):
    mcc = str(cfg["mcc"])
    mnc = str(cfg["mnc"]).zfill(3)
    imsi = str(cfg["imsi"])
    realm = cfg.get("realm") or f"ims.mnc{mnc}.mcc{mcc}.3gppnetwork.org"
    epdg = cfg.get("epdg") or f"epdg.epc.mnc{mnc}.mcc{mcc}.pub.3gppnetwork.org"
    nai = f"0{imsi}@nai.epc.mnc{mnc}.mcc{mcc}.3gppnetwork.org"
    # P-CSCF: explicit config wins; else a discovered address exported by entrypoint.
    pcscf = cfg.get("pcscf", "")
    if not pcscf and os.path.exists("/run/mdd-sim-gateway/pcscf"):
        try:
            pcscf = open("/run/mdd-sim-gateway/pcscf").read().strip()
        except Exception:
            pcscf = ""
    sip = cfg.get("sip", {})
    webrtc = sip.get("webrtc", {}) or {}
    ami_secret = str(cfg.get("ami_secret") or "")
    webrtc_password = str(webrtc.get("password") or "")
    if not ami_secret:
        raise ValueError("AMI credential is missing from instance configuration")
    if webrtc.get("enable", True) and not webrtc_password:
        raise ValueError("WebRTC credential is missing from instance configuration")
    ike = cfg.get("ike", {}) or {}
    default_ike = ("aes256-sha256-prfsha256-modp2048,aes128-sha256-prfsha256-modp2048,"
                   "aes256-sha1-prfsha1-modp2048,aes128-sha1-prfsha1-modp2048,"
                   "aes256-sha1-prfsha1-modp1024,aes128-sha1-prfsha1-modp1024")
    # No-PFS variants first (initial IKE_AUTH child picks one, as it always has), then
    # PFS variants (modp2048, matching the IKE DH group). The Telus ePDG accepts a
    # no-PFS CHILD_SA at IKE_AUTH but rejects a no-PFS CHILD rekey (CREATE_CHILD_SA)
    # with NO_PROPOSAL_CHOSEN -- it requires PFS on rekey. Offering both lets the SA
    # actually rekey (select a PFS proposal) instead of dying and forcing a full re-auth.
    default_esp = ("aes128-sha1,aes256-sha256,aes128-sha256,aes256-sha1,"
                   "aes128-sha1-modp2048,aes256-sha256-modp2048,"
                   "aes128-sha256-modp2048,aes256-sha1-modp2048")
    ctx = {
        "id": str(cfg.get("id", "1")),
        "imsi": imsi,
        "reader": cfg.get("reader") or f"imsi:{imsi}",
        "ami_reader": cfg.get("ami_reader") or cfg.get("reader") or f"imsi:{imsi}",
        "mcc": mcc,
        "mnc": mnc,
        "imei": cfg.get("imei", ""),
        "realm": realm,
        "epdg": epdg,
        "nai": nai,
        "msisdn": cfg.get("msisdn", ""),
        "smsc": cfg.get("smsc", ""),
        "pcscf": pcscf,          # explicit or discovered
        # Address family of the discovered P-CSCF. The IMS core transport must bind the same
        # family or Asterisk cannot reach the P-CSCF over the tunnel: IPv6 P-CSCF (Telus, EE)
        # -> bind [::]:5060; IPv4 P-CSCF (Vodafone UK, cp_mode=v4) -> bind 0.0.0.0:5060.
        "pcscf_is_v6": (":" in pcscf),
        "local_addr": cfg.get("local_addr") or container_ipv4(),
        "ike_proposals": ike.get("proposals", default_ike),
        "esp_proposals": ike.get("esp_proposals", default_esp),
        # P-Access-Network-Info: i-wlan-node-id should be the Wi-Fi AP BSSID (MAC). The
        # carrier P-CSCF augments this; a bogus value can make some SMSCs reject MO SMS.
        "pani": (sip.get("pani") or r"IEEE-802.11\;i-wlan-node-id=ffffffffffff"),
        "access_type": (sip.get("access_type") or ""),
        # Transparent product identity unless the line overrides it (carrier User-Agent
        # whitelists answer 403 to an unknown terminal). Blank/unset keeps the default.
        "user_agent": sanitize_user_agent(sip.get("user_agent")) or DEFAULT_USER_AGENT,
        "user_eq_phone": bool(sip.get("user_eq_phone", False)),
        # SDP identity (s=/o= lines) — Asterisk defaults s=Asterisk which fingerprints it.
        "sdp_session": (sip.get("sdp_session") or "-"),
        "sdp_owner": (sip.get("sdp_owner") or "-"),
        "ami_user": cfg.get("ami_user", "vowifi"),
        "ami_secret": ami_secret,
        "manager_url": cfg.get("manager_url", ""),
        "webrtc_enable": bool(webrtc.get("enable", True)),
        "webrtc_user": webrtc.get("username", "webrtc"),
        "webrtc_password": webrtc_password,
        # Container-internal plain WS listener; must match control/app/softphone_ws.py.
        "webrtc_ws_port": 8088,
        "domain": cfg.get("domain", ""),
        # Host-reachable address to advertise to LOCAL SIP clients (Contact + SDP). The
        # container's own IP is not routable off the docker bridge, so in-dialog requests
        # (BYE) from a LAN client would be undeliverable without this. Supplied by the
        # manager (host LAN IP); empty falls back to no external address.
        "advertise_addr": sip.get("advertise_address", ""),
        # Asterisk's [ice_host_candidates] parser requires an IP literal; unlike the PJSIP
        # external address above, a TLS DNS name is invalid here.
        "ice_advertise_addr": sip.get("ice_advertise_address", ""),
        # Outbound ring timeout (s) for Dial() — see extensions.conf.j2. Default 35.
        "ring_timeout": int(sip.get("ring_timeout", 35) or 35),
        # Voicemail. Off by default: recording a caller is a decision the operator makes, not
        # a default the product makes for them. vm_ring_seconds replaces the inbound Dial()
        # timeout only while it is on, so turning it off restores the previous behaviour
        # exactly. See extensions.conf.j2 [volte_ims].
        "vm_enabled": bool(sip.get("vm_enabled", False)),
        "vm_ring_seconds": int(sip.get("vm_ring_seconds", 25) or 25),
        "vm_max_seconds": int(sip.get("vm_max_seconds", 120) or 120),
        # The container's own RTP bind IP (docker-bridge private, e.g. 172.17.0.2). Used as the
        # LHS of rtp.conf [ice_host_candidates] to rewrite that unreachable host candidate to
        # the host LAN IP (advertise_addr) so a LAN WebRTC browser can reach our RTP.
        "rtp_bind_addr": cfg.get("local_addr") or container_ipv4(),
        "rtp_start": cfg.get("rtp_start", 10000),
        "rtp_end": cfg.get("rtp_end", 11000),
        "debug_asterisk": cfg.get("debug", {}).get("asterisk", False),
        "debug_charon": cfg.get("debug", {}).get("charon", False),
    }
    return ctx


def main():
    with open(CFG_PATH) as f:
        cfg = json.load(f)
    ctx = build_context(cfg)

    env = Environment(loader=FileSystemLoader(TPL_DIR), trim_blocks=True, lstrip_blocks=True,
                      keep_trailing_newline=True)

    outputs = {
        "asterisk.conf.j2": "/etc/asterisk/asterisk.conf",
        "modules.conf.j2": "/etc/asterisk/modules.conf",
        "logger.conf.j2": "/etc/asterisk/logger.conf",
        "manager.conf.j2": "/etc/asterisk/manager.conf",
        "acl.conf.j2": "/etc/asterisk/acl.conf",
        "cdr.conf.j2": "/etc/asterisk/cdr.conf",
        "cel.conf.j2": "/etc/asterisk/cel.conf",
        "features.conf.j2": "/etc/asterisk/features.conf",
        "ccss.conf.j2": "/etc/asterisk/ccss.conf",
        "indications.conf.j2": "/etc/asterisk/indications.conf",
        "pjproject.conf.j2": "/etc/asterisk/pjproject.conf",
        "stasis.conf.j2": "/etc/asterisk/stasis.conf",
        "udptl.conf.j2": "/etc/asterisk/udptl.conf",
        "rtp.conf.j2": "/etc/asterisk/rtp.conf",
        "http.conf.j2": "/etc/asterisk/http.conf",
        "pjsip.conf.j2": "/etc/asterisk/pjsip.conf",
        "extensions.conf.j2": "/etc/asterisk/extensions.conf",
        "ami_usim.ini.j2": "/usr/local/etc/ami_usim.ini",
    }
    os.makedirs("/etc/asterisk", exist_ok=True)
    for tpl, dest in outputs.items():
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        rendered = env.get_template(tpl).render(**ctx)
        with open(dest, "w") as f:
            f.write(rendered)
        print(f"[render] {tpl} -> {dest}")

    # Bundled prompts. Shipped in templates/ so the overlay image carries them: the base
    # image's Asterisk sound packages are a side effect of its build, not something this
    # repository asserts, so nothing here may depend on them existing.
    if ctx.get("vm_enabled"):
        # Record() will not create its own directory, and a failure there loses the message
        # with nothing but a dialplan warning to show for it.
        os.makedirs("/logs/voicemail", exist_ok=True)
    sounds_src = os.path.join(TPL_DIR, "sounds")
    if os.path.isdir(sounds_src):
        sounds_dst = "/var/lib/asterisk/mdd-sounds"
        os.makedirs(sounds_dst, exist_ok=True)
        for name in os.listdir(sounds_src):
            src = os.path.join(sounds_src, name)
            if os.path.isfile(src):
                shutil.copyfile(src, os.path.join(sounds_dst, name))
                print(f"[render] sound {name} -> {sounds_dst}")

    # Export env for keeper / ami_usim / swu_ike
    env_path = os.environ.get("MDD_ENV", "/run/mdd-sim-gateway/engine.env")
    os.makedirs(os.path.dirname(env_path), exist_ok=True)
    with open(env_path, "w") as f:
        # This file is sourced by entrypoint.sh. Reader names routinely contain spaces and
        # parentheses; writing raw values makes the shell execute the second word as a command
        # and silently truncates the selected reader. Quote every value, including credentials.
        def put(key, value):
            f.write(f"{key}={env_value(value)}\n")

        put("USIM_PIN", cfg.get("pin", ""))
        put("USIM_READER", cfg.get("reader", "imsi:" + ctx["imsi"]))
        put("PIN_USIM_READER", cfg.get("pin_reader", cfg.get("reader", "imsi:" + ctx["imsi"])))
        put("USIM_ICCID", cfg.get("iccid", ""))
        put("MDD_ID", ctx["id"])
        put("MANAGER_URL", ctx["manager_url"])
        put("MANAGER_EVENT_TOKEN", cfg.get("manager_event_token", ""))
        # SWu (python IKEv2/IPsec) launch params — consumed by entrypoint.sh to start
        # swu_ike.py. Reader is addressed by index for swu_ike's smartcard path; source is the
        # container IP; ePDG FQDN is resolved by swu_ike.
        put("USIM_READER_INDEX", cfg.get("reader_index", 0))
        # Stable physical USB port path of the reader (e.g. "3-2"). swu_ike/pin_keeper resolve it
        # back to a live PC/SC index in-container, so the SIM is always addressed by the reader
        # that PHYSICALLY holds it — surviving pcscd re-enumerating two identical readers into a
        # different order. Empty -> fall back to USIM_READER_INDEX.
        put("USIM_READER_PORT", cfg.get("reader_port", ""))
        put("USIM_IMSI", ctx["imsi"])
        # IMEI / IMEISV for the ePDG DEVICE_IDENTITY response. imeisv falls back to a value
        # derived from the IMEI if the instance didn't carry one (hand-authored config).
        imei_digits = "".join(ch for ch in str(cfg.get("imei", "")) if ch.isdigit())
        put("SWU_IMEI", imei_digits)
        put("SWU_IMEISV", cfg.get("imeisv") or imeisv_from_imei(cfg.get("imei", "")))
        put("SWU_SOURCE", ctx["local_addr"])
        put("SWU_EPDG", ctx["epdg"])
        put("SWU_APN", cfg.get("apn", "ims"))
        put("SWU_MCC", ctx["mcc"])
        put("SWU_MNC", ctx["mnc"])
        # ePDG identity (IDr) encoding: 'apn' (default) sends the bare APN — the form most
        # carriers' ePDGs expect and the proven-safe default; 'fqdn' builds the operator APN-FQDN
        # (<apn>.apn.epc.mnc<MNC3>.mcc<MCC3>.pub.3gppnetwork.org) that a minority of stricter
        # ePDGs require. Consumed by swu_ike.py's SWU_IDR_MODE. See config.normalize_idr_mode.
        put("SWU_IDR_MODE", cfg.get("idr_mode", "apn"))
        # CFG (config-request) address-family mode: 'auto' (default) walks a discovery ladder and
        # keeps the family that yields a usable PDN (see swu_ike SWU_CP_MODE / SWU_CP_MODE_ORDER);
        # 'v6' (Telus/EE), 'v4' (Vodafone UK), 'dual' pin a single family. SWU_CP_MODE_ORDER is the
        # auto ladder (carrier-DB preference first), computed by config.render_instance_json.
        put("SWU_CP_MODE", cfg.get("cp_mode", "auto"))
        put("SWU_CP_MODE_ORDER", cfg.get("cp_mode_order", "v6,dual,v4"))
        # Proactive CHILD-SA rekey period (minutes; 0 = disabled). IKEv2 does not carry SA
        # lifetime on the wire, so swu_ike uses this local-policy value to rekey the ESP SA
        # before it silently ages out (3GPP TS 24.302 7.2.2C). Set by the manager from
        # settings.rekey.minutes (default 30); hand-authored configs may omit it.
        put("SWU_CHILD_REKEY_MINUTES", cfg.get("rekey_minutes", 30))
        # Proactive IKE-SA rekey period (minutes; 0 = disabled). The engine only supports the
        # initiator role for an IKE rekey, so this clock must beat the carrier's own session
        # lifetime (giffgaff/O2 UK ages a SWu session out silently at ~2h50m; EE at ~12h).
        # Set by the manager from settings.rekey.ike_minutes / inst.ike_rekey_minutes; the
        # engine's own fallback for hand-authored configs stays at 600.
        put("SWU_IKE_REKEY_MINUTES", cfg.get("ike_rekey_minutes", 150))
        # Accept an ePDG-initiated ESP rekey in place instead of refusing it and letting the
        # tunnel be re-established. Off by default: the key-direction inversion it depends on
        # (RFC 7296 2.17, responder side) cannot be verified without a carrier that initiates
        # one, and getting it wrong is silent. Opt in per line to trial it.
        put("SWU_ACCEPT_EPDG_ESP_REKEY", "1" if cfg.get("accept_epdg_esp_rekey") else "0")
    print(f"[render] env -> {env_path}")


if __name__ == "__main__":
    main()
