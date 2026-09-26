"""Tell the operator when a line has been offline long enough to matter.

The gateway rebuilds a failing line by itself, forever, and says nothing while it does. That is
right for a thirty-second blip and wrong for a SIM that has been off the network all afternoon:
the owner only finds out when an SMS never arrives. The one existing report
(``line_unrecoverable``) fires from the exit-failover ledger, so an outage the ledger never
blames on an exit — a card that stopped answering, a reader that fell off USB, a carrier
refusing registration — could last for hours without a word.

This module owns only the timing. Whether a sample counts as "up", "down" or "not wanted" is
decided by the caller, which knows the status machine and the user's intent; here a line that
stays down for ``threshold`` seconds is announced once, and a line that was announced is
announced again when it comes back, so every alert is closed by a matching all-clear.

Elapsed time is measured on the monotonic clock. A Raspberry Pi has no RTC and boots with a
wall clock that jumps forward when NTP syncs; counting that jump as outage would announce
lines that had been down for seconds. Wall time is kept only to tell the user when it began.

Only announced outages are persisted. An unannounced one restarts its count after a manager
restart, which delays the alert by at most one threshold; an announced one must survive, or
the restart would either repeat the alert or lose the all-clear.
"""
from __future__ import annotations

UP = "up"
DOWN = "down"
# The user switched this line (or its device's VoWiFi) off. Not an outage, and switching it
# off in the middle of one withdraws the alert without an all-clear the user did not earn.
IGNORE = "ignore"

DEFAULT_MINUTES = 10
MIN_MINUTES = 1
MAX_MINUTES = 1440

# A persisted start time further back than this is taken to be a clock error, not an outage.
_MAX_PLAUSIBLE_SECONDS = 30 * 86400


def threshold_seconds(settings: dict) -> float:
    try:
        minutes = int(settings.get("line_offline_notify_minutes", DEFAULT_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_MINUTES
    return float(max(MIN_MINUTES, min(MAX_MINUTES, minutes)) * 60)


def restore(saved) -> dict[str, dict]:
    """Rebuild in-memory state from what ``persistable`` wrote.

    Restored entries have no monotonic origin of their own. They were already announced, so
    the threshold no longer applies to them; ``mono`` is only a fallback for the duration.
    """
    state: dict[str, dict] = {}
    if not isinstance(saved, dict):
        return state
    for iid, entry in saved.items():
        if not isinstance(entry, dict):
            continue
        try:
            since = float(entry.get("since"))
        except (TypeError, ValueError):
            continue
        state[str(iid)] = {"since": since, "mono": None, "notified": True,
                           "reason": str(entry.get("reason") or "")}
    return state


def persistable(state: dict[str, dict]) -> dict[str, dict]:
    return {iid: {"since": entry["since"], "reason": entry.get("reason") or ""}
            for iid, entry in state.items() if entry.get("notified")}


def duration_seconds(entry: dict, wall_now: float, mono_now: float) -> float:
    elapsed = wall_now - float(entry.get("since") or wall_now)
    if 0 <= elapsed <= _MAX_PLAUSIBLE_SECONDS:
        return elapsed
    mono = entry.get("mono")
    return max(0.0, mono_now - mono) if mono is not None else 0.0


def evaluate(state: dict[str, dict], observations: dict[str, tuple[str | None, str]],
             threshold: float, wall_now: float, mono_now: float):
    """Fold one pass of samples into ``state``.

    ``observations`` maps every configured line to ``(kind, reason)``; a kind of None means the
    sample said nothing about connectivity and leaves the line as it was. A line absent from
    ``observations`` no longer exists and is forgotten silently.

    Returns ``(went_offline, recovered, changed)``: the entries newly announced this pass, the
    announced entries that came back (with ``duration``), and whether the persisted part of
    ``state`` changed.
    """
    went_offline: list[dict] = []
    recovered: list[dict] = []
    changed = False
    for iid in [iid for iid in state if iid not in observations]:
        changed |= bool(state.pop(iid).get("notified"))
    for iid, (kind, reason) in observations.items():
        entry = state.get(iid)
        if kind is None:
            continue
        if kind == DOWN:
            if entry is None:
                state[iid] = {"since": wall_now, "mono": mono_now, "notified": False,
                              "reason": reason}
                continue
            entry["reason"] = reason or entry.get("reason") or ""
            if not entry["notified"] and mono_now - entry["mono"] >= threshold:
                entry["notified"] = True
                changed = True
                went_offline.append({"instance": iid, **entry,
                                     "duration": duration_seconds(entry, wall_now, mono_now)})
            continue
        if entry is None:
            continue
        state.pop(iid)
        if entry.get("notified"):
            changed = True
            if kind == UP:
                recovered.append({"instance": iid, **entry,
                                  "duration": duration_seconds(entry, wall_now, mono_now)})
    return went_offline, recovered, changed


def format_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{max(1, minutes)} 分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时 {minutes} 分钟" if minutes else f"{hours} 小时"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时" if hours else f"{days} 天"


# What the user can do about it differs by cause, so the reason is spelled out rather than
# passed through as a status code. Anything unlisted falls back to the generic sentence.
_STATE_TEXT = {
    "NO_CARD": "读不到 SIM 卡——读卡器或模块可能已断开，或 SIM 被拔出。",
    "PIN_PROBLEM": "SIM PIN 有问题，需要人工处理后才能恢复。",
    "EPDG_UNRESOLVED": "无法解析运营商的 VoWiFi（ePDG）地址，请检查 DNS 和网络。",
    "TUNNEL_DOWN": "VoWiFi 隧道建立不起来。",
    "REGISTERING": "隧道已建立，但 IMS 注册没有成功。",
    "STOPPED": "线路引擎没有运行。",
}


def reason_text(state: str, reason_code: str) -> str:
    text = _STATE_TEXT.get(str(state or "").upper(), "线路处于故障状态。")
    return f"{text}（{reason_code}）" if reason_code else text
