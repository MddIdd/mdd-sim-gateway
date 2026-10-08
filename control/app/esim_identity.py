"""Decide whether ModemManager is showing the eSIM profile just enabled.

The eUICC and VPCD bridge can already report the new ICCID while ModemManager
still reports the previous SIM. A readable ICCID must match exactly. When the
modem only exposes an IMSI, that IMSI is the convergence signal.
"""


def _digits(value: object) -> str:
    text = str(value or "").strip()
    if text in {"", "--"}:
        return ""
    return text if text.isdigit() else ""


def identity_matches(observed: dict, expected: dict) -> bool:
    """Return whether the modem identity is the requested active profile."""
    expected_iccid = _digits(expected.get("iccid"))
    expected_imsi = _digits(expected.get("imsi"))
    if not expected_iccid and not expected_imsi:
        return False
    observed_iccid = _digits(observed.get("iccid"))
    if expected_iccid and observed_iccid:
        return observed_iccid == expected_iccid
    observed_imsi = _digits(observed.get("imsi"))
    return bool(expected_imsi and observed_imsi == expected_imsi)
