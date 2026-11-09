"""
status.py - Per-instance status state machine with failure classification.

Returns a live snapshot: {state, label, reason_code, reason, detail}. The manager's
health tracker (main.py) overlays retry counters and, on exhaustion, an ERROR state.

States:      STOPPED, NO_CARD, PIN_PROBLEM, EPDG_UNRESOLVED, TUNNEL_DOWN, REGISTERING, OK
reason_code: machine key for the WebUI; `reason` is a user-friendly sentence.
detail:      raw signals (pin, pcscf, registration, ike classification) for advanced view.
"""
from __future__ import annotations

import asyncio
import socket

from . import engine

LABELS = {
    "STOPPED": "Stopped",
    "NO_CARD": "No SIM card",
    "PIN_PROBLEM": "PIN error",
    "EPDG_UNRESOLVED": "Cannot resolve ePDG",
    "TUNNEL_DOWN": "Establishing VoWiFi tunnel",
    "REGISTERING": "Registering to IMS",
    "OK": "IMS registered",
    "ERROR": "Failed",
}

# reason_code -> user-friendly message
REASONS = {
    "no_card": "No SIM card detected in the reader.",
    "wrong_card": "The reader this line is bound to holds a different line's SIM. VoWiFi will "
                  "not authenticate against it — check this line's reader binding.",
    "pin_wrong": "SIM PIN is incorrect.",
    "pin_blocked": "SIM PIN is blocked — PUK required.",
    "epdg_unresolved": "Can't resolve the carrier's VoWiFi (ePDG) address — the carrier may "
                       "not support Wi-Fi Calling, or check DNS / internet connectivity.",
    "tunnel_network": "Can't establish the VoWiFi tunnel — network problem (no response from "
                      "the carrier's ePDG).",
    "tunnel_child_rekey_timeout": "The carrier ePDG did not answer the CHILD_SA rekey; "
                                  "rebuilding the tunnel.",
    "tunnel_ike_rekey_timeout": "The carrier ePDG did not answer the IKE_SA rekey; "
                                "rebuilding the tunnel.",
    "tunnel_rekey_send_error": "The client could not send an IPsec rekey request; "
                               "rebuilding the tunnel.",
    "tunnel_sim_auth": "Can't establish the VoWiFi tunnel — SIM authentication (EAP-AKA) was "
                       "rejected by the carrier.",
    "tunnel_not_authorized": "Can't establish the VoWiFi tunnel — the carrier's ePDG refused the "
                             "identity before checking the SIM. The line is likely not provisioned "
                             "for Wi-Fi Calling, or the ePDG blocks connections from this network/region.",
    "tunnel_proposal": "Can't establish the VoWiFi tunnel — the carrier rejected the encryption "
                       "settings (IKE proposal).",
    "tunnel_setup": "Establishing the VoWiFi (IPsec/ePDG) tunnel…",
    "registering": "VoWiFi tunnel is up — registering to the carrier's IMS…",
    "reg_rejected": "Can't register to the carrier's IMS (authentication or provisioning issue).",
    "reg_unanswered": "The carrier's IMS stopped answering registration — usually a stale "
                      "IPsec session; rebuilding the tunnel clears it.",
    "ok": "IMS registration is active.",
}


# The classifier lives in engine.py so the diagnostics capture (which cannot import this
# module without a cycle) records the same verdict the health policy acted on.
registration_failure_evidence = engine.registration_failure_evidence


def registration_unanswered(log_tail: str) -> bool:
    """Compatibility predicate used by tests and the fast-recovery policy."""
    return registration_failure_evidence(log_tail)["kind"] == "unanswered"


def resolve_epdg(fqdn: str) -> bool:
    try:
        socket.getaddrinfo(fqdn, None)
        return True
    except Exception:
        return False


def nameservers() -> list[str]:
    """The resolvers a failed lookup was tried against — evidence for the outage record."""
    try:
        with open("/etc/resolv.conf", encoding="utf-8") as handle:
            return [line.split()[1] for line in handle
                    if line.strip().startswith("nameserver") and len(line.split()) > 1]
    except OSError:
        return []


def classify_ike(iid: str) -> tuple[str, str]:
    """Inspect recent charon (IKE) log to classify why the tunnel isn't up."""
    log = engine.charon_log(iid, 400)
    usim = engine.usim_status(iid)
    swu = engine.read_run_json(iid, "swu_status.json") or {}
    low = log.lower()
    # swu_ike publishes the exact terminal action before its supervisor re-establishes the
    # tunnel. Prefer that machine-readable evidence over generic words in the log tail.
    terminal = str(swu.get("reason_code") or "")
    if terminal == "rekey_timeout":
        return "tunnel_child_rekey_timeout", REASONS["tunnel_child_rekey_timeout"]
    if terminal == "ike_rekey_timeout":
        return "tunnel_ike_rekey_timeout", REASONS["tunnel_ike_rekey_timeout"]
    if terminal in {"rekey_send_error", "ike_rekey_send_error"}:
        return "tunnel_rekey_send_error", REASONS["tunnel_rekey_send_error"]
    # ePDG refused the IKE_AUTH identity BEFORE any EAP-AKA challenge (SIM never queried). This
    # is an authorization/subscription/geo decision, not a SIM/PIN fault — classify it distinctly
    # so the UI doesn't wrongly blame the SIM. swu_ike logs a clear marker for this case.
    if "before any eap-aka challenge" in low or "authentication_failed before" in low or \
            "not provisioned for vowifi" in low:
        return "tunnel_not_authorized", REASONS["tunnel_not_authorized"]
    # SIM auth failure (EAP-AKA)
    if usim.get("state") in ("AUTH_FAIL", "PIN_FAIL", "NO_CARD") or \
            "eap_aka failed" in low or "eap-aka failed" in low or \
            "authentication_failed" in low or "eap method eap_aka fail" in low or \
            "received auth_failed" in low or "authentication failed" in low:
        return "tunnel_sim_auth", REASONS["tunnel_sim_auth"]
    # Carrier rejected our crypto proposal / message
    if "invalid_syntax" in low or "no_proposal_chosen" in low or "invalid_ke" in low or \
            "no proposal" in low:
        return "tunnel_proposal", REASONS["tunnel_proposal"]
    # No response / retransmits -> network
    if "retransmit" in low or "giving up" in low or "no route" in low or \
            "destination unreachable" in low or "timeout" in low:
        return "tunnel_network", REASONS["tunnel_network"]
    # Not enough info yet -> still setting up
    return "tunnel_setup", REASONS["tunnel_setup"]


async def compute(inst: dict, ami_client=None, runtime: dict | None = None) -> dict:
    iid = str(inst["id"])
    mcc, mnc = inst["mcc"], str(inst["mnc"]).zfill(3)
    epdg = inst.get("epdg") or f"epdg.epc.mnc{mnc}.mcc{mcc}.pub.3gppnetwork.org"

    detail = {"msisdn": inst.get("msisdn") or None, "smsc": inst.get("smsc") or None,
              "iccid": inst.get("iccid") or None, "epdg_fqdn": epdg}

    def out(state, code):
        return {"state": state, "label": LABELS[state],
                "reason_code": code, "reason": REASONS.get(code, ""), "detail": detail}

    running = (bool(runtime.get("running")) if runtime is not None
               else await asyncio.to_thread(engine.is_running, iid))
    if not inst.get("enabled", True) or not running:
        return {"state": "STOPPED", "label": LABELS["STOPPED"],
                "reason_code": "stopped", "reason": "Stopped.", "detail": detail}

    pin = await asyncio.to_thread(engine.read_run_json, iid, "pin_status.json") or {}
    detail["pin"] = pin
    pstate = pin.get("state")
    if pstate is None:
        # A fresh/rebuilt container removes stale runtime observations before pin_keeper has
        # written its first result. A missing file is no evidence that the physical SIM was
        # removed, so make this an unreadable startup sample rather than a false card outage.
        detail["registration"] = "unknown"
        return out("REGISTERING", "registering")
    if pstate == "NO_CARD":
        return out("NO_CARD", "no_card")
    if pstate == "WRONG_CARD":
        # This line's own SIM is absent from the slot it is bound to, which is what NO_CARD
        # means for it; the reason line carries the ICCID pair that says how it went wrong.
        return out("NO_CARD", "wrong_card")
    if pstate == "WRONG_PIN":
        return out("PIN_PROBLEM", "pin_wrong")
    if pstate == "PIN_BLOCKED":
        return out("PIN_PROBLEM", "pin_blocked")

    if not await asyncio.to_thread(engine.tunnel_installed, iid):
        # DNS only matters while there is no tunnel: an established tunnel talks to an
        # address, not a name. Checking it first used to chart healthy lines as down for a
        # minute whenever the upstream resolver blipped — the ePDG records rotate every ~30s,
        # so they are always a cache miss and always the first names to fail.
        if not await asyncio.to_thread(resolve_epdg, epdg):
            # Which resolvers refused the name is the difference between "DNS was down"
            # and knowing whose DNS was down.
            detail["nameservers"] = nameservers()
            return out("EPDG_UNRESOLVED", "epdg_unresolved")
        code, _ = await asyncio.to_thread(classify_ike, iid)
        r = out("TUNNEL_DOWN", code)
        detail["ike_reason"] = code
        return r

    detail["pcscf"] = await asyncio.to_thread(engine.read_pcscf, iid)
    # The persistent AMI connection avoids a Docker exec on every sample. Its implementation
    # deliberately uses AMI Command rather than PJSIPShowRegistrationsDetailed, which hangs on
    # some supported IMS-patched builds. A bounded local CLI remains the authoritative fallback
    # while AMI is connecting or recovering.
    reg = await ami_client.registration_state() if ami_client is not None else "unknown"
    if reg == "unknown":
        try:
            reg = await asyncio.wait_for(asyncio.to_thread(engine.registration_state, iid), 5)
        except Exception:
            reg = "unknown"
    detail["registration"] = reg
    if reg == "Registered":
        return out("OK", "ok")
    if reg == "Rejected":
        # One extra docker-logs read, only in the rare Rejected state: "no answer" and
        # "refused" need different fixes, so they must not share a label.
        tail = await asyncio.to_thread(engine.logs, iid, 200)
        evidence = registration_failure_evidence(tail)
        unanswered = evidence["kind"] == "unanswered"
        result = out("REGISTERING", "reg_unanswered" if unanswered else "reg_rejected")
        if evidence.get("sip_status") is not None:
            result["detail"]["sip_status"] = evidence["sip_status"]
        if unanswered:
            # Unknown is intentionally different from zero: if AMI cannot prove there are no
            # active channels, recovery keeps the established engine and follows the old slow
            # policy rather than risking a live call.
            result["detail"]["active_channels"] = (
                await ami_client.active_channel_count() if ami_client is not None else None)
        return result
    return out("REGISTERING", "registering")
