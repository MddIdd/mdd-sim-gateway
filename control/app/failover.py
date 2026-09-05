"""Decide whether a line's failure is evidence against its exit node, and when to stop trying.

A line that cannot register used to be treated as proof that its exit was unusable, and every
such failure moved the exit. Measured over fifty freezes on a live gateway that inference was
wrong more often than right: the node blamed most often was chosen again nine times and then
carried the same line for eight uninterrupted hours. Registration fails for carrier-side and
IMS-side reasons too, and each exit change tears down a working tunnel — which produces the
next failure, which moves the exit again. Countries with one candidate never churned; the one
with eleven walked its whole pool in forty minutes. That is the loop this module ends.

Two decisions, deliberately separate:

  classify()  attributes a single failure — is the exit implicated at all?
  record()    folds attributed failures into a per-line ledger and says what to do next.

The five outcomes are not symmetric, because the cost of continuing differs:

  HOLD      keep rebuilding on the same exit; nothing is proven yet
  SWITCH    this node has had its chances and others remain untried
  BACK_OFF  every exit failed the same way — which is what a host-side outage or a dead
            subscription looks like, not what bad nodes look like. Announce once, then
            retry on a slow cadence: upstream faults pass, and a line that stops forever
            over a transient outage trades one failure for a worse one
  GIVE_UP   stop rebuilding and report — the operator pinned this exit and it has had its
            chances, so the only useful moves left belong to a person
  REPORT    report but keep rebuilding — the evidence does not justify moving the exit;
            report local/upstream failures or missing evidence without claiming packet loss
            has been ruled out

Stopping the churn matters as much as switching. The previous policy could not stop: once
every candidate was cooling down it cleared the cooldown and started the pool over, forever,
and a locked exit rebuilt its container every few minutes for as long as it kept failing.

One more veto, because a line's exit is shared by every line of its country: when a sibling
line is registered over the same exit right now, the exit is demonstrably carrying IMS and
moving it would tear down that sibling's tunnel — the disruption this design exists to avoid.
Eviction is off the table while the peer holds. A tunnel that will not establish over an exit
that provably works is most likely the carrier refusing this SIM's traffic from that address,
which is a person's call to make, so the failure is recorded and in time reported.
"""

# How many attributed failures on one node before it is abandoned. One is too few — a tunnel
# that fails to establish once often establishes on the next attempt over the same path.
STRIKES_PER_NODE = 3

# Consecutive failures the exits cannot explain before the operator is told. High enough that
# a carrier hiccup passes unmentioned, low enough to surface a line that is really stuck.
FAILURES_BEFORE_REPORT = 6

# IKE requests that went unanswered on an established tunnel. Real evidence of loss, but the
# tunnel still came up, so it is not enough to evict a node on: recorded and left for a person.
SUSPICIOUS_RETRANSMITS = 6

# How often an exhausted line re-tests its exit. Every candidate failing the same way points
# upstream of the nodes — a host-side outage, a dead subscription — and those pass. Hourly is
# frequent enough to catch the recovery without anyone touching the line, and rare enough not
# to hammer the carrier's ePDG while the outage lasts.
EXHAUSTED_RETRY_SECONDS = 3600.0

BLAMES_EXIT = "exit"
BLAMES_ELSEWHERE = "elsewhere"
UNCLEAR = "unclear"

HOLD = "hold"
SWITCH = "switch"
BACK_OFF = "back-off"
GIVE_UP = "give-up"
REPORT = "report"


def classify(swu_state: str | None, retransmits: int | None, stable_seconds: float = 0.0,
             stable_threshold: float = 0.0, reason_code: str = "") -> str:
    """Attribute one line failure to the exit, to something else, or to neither."""
    if stable_threshold and stable_seconds >= stable_threshold:
        # The node carried a registered line for long enough to prove it can. Whatever broke
        # afterwards is not the path: a carrier-side problem, or a rekey it failed to survive.
        return BLAMES_ELSEWHERE
    if reason_code in {"epdg_unresolved", "tunnel_sim_auth", "tunnel_proposal",
                       "tunnel_rekey_send_error", "client_engine_failure", "wrong_card"}:
        return BLAMES_ELSEWHERE
    state = str(swu_state or "").upper()
    if retransmits is None:
        return UNCLEAR
    if state != "CONNECTED":
        if state not in {"CONNECTING", "DOWN", "DISCONNECTED"}:
            return UNCLEAR
        if reason_code != "tunnel_network":
            return UNCLEAR
        return BLAMES_EXIT
    if int(retransmits) >= SUSPICIOUS_RETRANSMITS:
        return UNCLEAR
    # Tunnel established and nothing went unanswered: the exit carried every packet it was
    # given. A registration that fails here failed for a reason the exit cannot explain.
    return BLAMES_ELSEWHERE


def blank_ledger() -> dict:
    return {"node": "", "strikes": 0, "tried": [], "failures": 0,
            "given_up": False, "exhausted": False, "held_for_peer": False,
            "reported": False}


def record(ledger: dict, verdict: str, node: str, pinned: bool,
           candidates: list[str] | None = None,
           peer_registered: bool = False) -> tuple[str, dict]:
    """Fold one failure into the ledger and say what to do about it.

    GIVE_UP and REPORT are each returned once, on the transition, so the caller can announce
    them exactly once however many further failures arrive. BACK_OFF is different: it is the
    pacing signal for every failure while the pool stays exhausted, so the caller announces
    only the first — the transition is visible as the ledger's ``exhausted`` flag flipping.

    ``peer_registered`` says a sibling line of the same country is registered right now over
    the exit this line is failing on.
    """
    ledger = {**blank_ledger(), **(ledger or {})}
    ledger["tried"] = list(ledger.get("tried") or [])
    ledger["failures"] = int(ledger.get("failures") or 0) + 1
    if ledger.get("given_up"):
        if pinned:
            return HOLD, ledger
        # The stop belonged to a pin that has since been released (or to the policy that
        # predated backing off). Automation is allowed again: resume where the walk stopped.
        ledger["given_up"] = False

    if verdict == UNCLEAR:
        ledger["held_for_peer"] = False
        if ledger["failures"] >= FAILURES_BEFORE_REPORT and not ledger.get("reported"):
            ledger["reported"] = True
            return REPORT, ledger
        return HOLD, ledger

    if verdict != BLAMES_EXIT:
        if ledger.get("exhausted"):
            ledger["exhausted"] = False
            ledger["tried"] = []
            ledger["strikes"] = 0
        ledger["held_for_peer"] = False
        if ledger["failures"] >= FAILURES_BEFORE_REPORT and not ledger.get("reported"):
            ledger["reported"] = True
            return REPORT, ledger
        return HOLD, ledger

    if peer_registered:
        # A sibling line is registered over this exit right now — living proof the exit can
        # carry IMS, and a tunnel that moving the exit would tear down. Eviction is off the
        # table while the peer holds; strikes stay where they are so the walk resumes the
        # moment the peer lets go.
        ledger["held_for_peer"] = True
        if ledger["failures"] >= FAILURES_BEFORE_REPORT and not ledger.get("reported"):
            ledger["reported"] = True
            return REPORT, ledger
        return HOLD, ledger
    ledger["held_for_peer"] = False

    if ledger.get("exhausted"):
        # Still nothing establishes and there is nowhere new to go: keep the slow cadence.
        return BACK_OFF, ledger

    if node != ledger.get("node"):
        # Strikes belong to a node, not to the line: moving to a new exit starts a new count.
        ledger["node"] = node
        ledger["strikes"] = 0
    ledger["strikes"] = int(ledger.get("strikes") or 0) + 1
    if ledger["strikes"] < STRIKES_PER_NODE:
        return HOLD, ledger
    if node and node not in ledger["tried"]:
        ledger["tried"].append(node)
    remaining = [name for name in (candidates or []) if name not in ledger["tried"]]
    if pinned:
        # A pinned exit is the operator's standing choice, including while it fails; the only
        # useful move left is to say so.
        ledger["given_up"] = True
        return GIVE_UP, ledger
    if not remaining:
        # Every exit failed the same way. That is not what bad nodes look like — it is what a
        # host-side or subscription-wide problem looks like, and those pass. Slow down
        # instead of stopping, so the line registers by itself when the outside world
        # comes back.
        ledger["exhausted"] = True
        return BACK_OFF, ledger
    ledger["strikes"] = 0
    return SWITCH, ledger


def summarise(ledger: dict, action: str, country: str, pinned: bool) -> str:
    """Explain the outcome in the terms an operator can act on."""
    node = ledger.get("node") or "当前节点"
    where = (country or "").upper() or "未知国家"
    tried = len(ledger.get("tried") or [])
    if action == REPORT:
        if ledger.get("held_for_peer"):
            return (f"线路已连续 {ledger.get('failures')} 次在 {where} 出口（{node}）上建不起隧道，"
                    f"但同一出口正承载着 {where} 另一条注册中的线路，说明出口本身可用，为不打断"
                    "那条线路没有自动换节点。更像是运营商拒绝了这条线路从该出口接入，"
                    "请为这条线路单独指定出口或人工处理。")
        return (f"线路已连续 {ledger.get('failures')} 次失败，现有证据不足以归咎于 {where} 出口，"
                "未自动换节点。自动重建会继续，请检查本机网络、隧道日志及运营商侧或 IMS 配置。")
    if action == BACK_OFF:
        return (f"{where} 的 {tried} 个候选出口都试过了，隧道都建不起来（最后一个 {node}）。"
                "所有节点同时失效更像是本机网络或订阅出了问题，而非节点本身；"
                "已转入慢速重试（约每小时一次），外部问题恢复后线路会自动注册。")
    return (f"线路在锁定的 {where} 出口上连续 {STRIKES_PER_NODE} 次建不起隧道（{node}），"
            "已停止自动重建。出口是锁定的，需要人工换节点或解除锁定。")
