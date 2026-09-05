import unittest

from control.app import failover


class AttributionTests(unittest.TestCase):
    """Which failures are evidence against the exit, and which are not. Measured over fifty
    freezes on a live gateway, twenty had an established tunnel and no unanswered IKE at all —
    every one of those moved the exit, and every one of those moves was wrong."""

    def test_a_tunnel_that_never_came_up_blames_the_exit(self):
        for state in ("CONNECTING", "DOWN"):
            with self.subTest(state=state):
                self.assertEqual(failover.classify(state, 0, reason_code="tunnel_network"),
                                 failover.BLAMES_EXIT)

    def test_missing_or_unrecognized_state_is_not_exit_evidence(self):
        for state in ("", None, "STARTING", "UNKNOWN"):
            with self.subTest(state=state):
                self.assertEqual(failover.classify(state, 0), failover.UNCLEAR)

    def test_local_failures_do_not_blame_a_disconnected_exit(self):
        for reason in ("epdg_unresolved", "tunnel_sim_auth", "tunnel_proposal",
                       "tunnel_rekey_send_error", "client_engine_failure"):
            with self.subTest(reason=reason):
                self.assertEqual(failover.classify("DOWN", 10, reason_code=reason),
                                 failover.BLAMES_ELSEWHERE)

    def test_ambiguous_setup_or_authorization_failure_does_not_evict(self):
        for reason in ("tunnel_setup", "tunnel_not_authorized", "unknown"):
            self.assertEqual(failover.classify("DOWN", 0, reason_code=reason), failover.UNCLEAR)

    def test_missing_retransmit_evidence_does_not_prove_a_clean_tunnel(self):
        self.assertEqual(failover.classify("CONNECTED", None), failover.UNCLEAR)

    def test_disconnected_state_alone_does_not_prove_an_exit_failure(self):
        self.assertEqual(failover.classify("DOWN", 0), failover.UNCLEAR)

    def test_unreadable_ike_evidence_does_not_strike_a_disconnected_exit(self):
        self.assertEqual(failover.classify("DOWN", None, reason_code="tunnel_network"),
                         failover.UNCLEAR)

    def test_an_established_tunnel_with_nothing_unanswered_clears_the_exit(self):
        self.assertEqual(failover.classify("CONNECTED", 0), failover.BLAMES_ELSEWHERE)

    def test_unanswered_ike_on_an_established_tunnel_is_left_undecided(self):
        self.assertEqual(failover.classify("CONNECTED", failover.SUSPICIOUS_RETRANSMITS),
                         failover.UNCLEAR)
        self.assertEqual(failover.classify("CONNECTED", failover.SUSPICIOUS_RETRANSMITS - 1),
                         failover.BLAMES_ELSEWHERE)

    def test_a_node_that_carried_the_line_is_not_blamed_for_what_broke_later(self):
        self.assertEqual(failover.classify("DOWN", 99, stable_seconds=700,
                                           stable_threshold=600), failover.BLAMES_ELSEWHERE)


class StrikeTests(unittest.TestCase):
    POOL = ["node-a", "node-b", "node-c"]

    def _strike(self, ledger, node, verdict=failover.BLAMES_EXIT, pinned=False, pool=None,
                peer=False):
        return failover.record(ledger, verdict, node, pinned,
                               self.POOL if pool is None else pool, peer_registered=peer)

    def test_one_failure_does_not_move_the_exit(self):
        action, ledger = self._strike(None, "node-a")
        self.assertEqual(action, failover.HOLD)
        self.assertEqual(ledger["strikes"], 1)

    def test_unknown_evidence_preserves_the_walk_without_slowing_recovery(self):
        ledger = {**failover.blank_ledger(), "node": "node-a", "strikes": 3,
                  "tried": ["node-a"], "exhausted": True}
        action, updated = self._strike(ledger, "node-a", verdict=failover.UNCLEAR)
        self.assertEqual(action, failover.HOLD)
        for field in ("node", "strikes", "tried", "exhausted"):
            self.assertEqual(updated[field], ledger[field])

    def test_unknown_evidence_reports_once_without_accumulating_strikes(self):
        ledger = None
        actions = []
        for attempt in range(failover.FAILURES_BEFORE_REPORT + 2):
            action, ledger = self._strike(ledger, "node-a", verdict=failover.UNCLEAR)
            actions.append(action)
        self.assertEqual(actions.count(failover.REPORT), 1)
        self.assertEqual(ledger["strikes"], 0)

    def test_the_exit_moves_only_after_the_node_has_had_its_chances(self):
        ledger = None
        for _ in range(failover.STRIKES_PER_NODE - 1):
            action, ledger = self._strike(ledger, "node-a")
            self.assertEqual(action, failover.HOLD)
        action, ledger = self._strike(ledger, "node-a")
        self.assertEqual(action, failover.SWITCH)
        self.assertEqual(ledger["tried"], ["node-a"])

    def test_a_failure_the_exit_cannot_explain_never_moves_it(self):
        ledger = None
        for _ in range(failover.STRIKES_PER_NODE * 3):
            action, ledger = self._strike(ledger, "node-a", failover.BLAMES_ELSEWHERE)
            self.assertIn(action, (failover.HOLD, failover.REPORT))
        self.assertEqual(ledger["strikes"], 0)
        self.assertEqual(ledger["tried"], [])

    def test_moving_to_a_new_node_starts_its_count_over(self):
        ledger = None
        for _ in range(failover.STRIKES_PER_NODE):
            _action, ledger = self._strike(ledger, "node-a")
        action, ledger = self._strike(ledger, "node-b")
        self.assertEqual(action, failover.HOLD)
        self.assertEqual(ledger["strikes"], 1)

    def test_the_whole_pool_is_walked_once_and_then_backed_off(self):
        ledger, actions = None, []
        for node in self.POOL:
            for _ in range(failover.STRIKES_PER_NODE):
                action, ledger = self._strike(ledger, node)
                actions.append(action)
        self.assertEqual(actions[-1], failover.BACK_OFF)
        self.assertEqual(actions.count(failover.SWITCH), len(self.POOL) - 1)
        self.assertTrue(ledger["exhausted"])
        self.assertFalse(ledger["given_up"])

    def test_an_exhausted_pool_is_never_walked_again(self):
        # The old policy cleared the cooldown and started the pool over, forever. Backing
        # off must not resurrect that: further failures pace slowly and never switch.
        ledger, actions = None, []
        for node in self.POOL + ["node-c"] * 5:
            for _ in range(failover.STRIKES_PER_NODE):
                action, ledger = self._strike(ledger, node)
                actions.append(action)
        self.assertEqual(actions.count(failover.SWITCH), len(self.POOL) - 1)
        self.assertEqual(actions[-1], failover.BACK_OFF)

    def test_a_recovered_tunnel_forgets_what_the_outage_taught(self):
        # Once any tunnel establishes again, the exits are reachable: the walk's verdicts
        # described the outage, not the nodes, so the walk may start over.
        ledger = None
        for node in self.POOL:
            for _ in range(failover.STRIKES_PER_NODE):
                _action, ledger = self._strike(ledger, node)
        _action, ledger = self._strike(ledger, "node-c", failover.BLAMES_ELSEWHERE)
        self.assertFalse(ledger["exhausted"])
        self.assertEqual(ledger["tried"], [])

    def test_a_pinned_exit_is_never_moved_and_gives_up_on_the_spot(self):
        ledger, actions = None, []
        for _ in range(failover.STRIKES_PER_NODE):
            action, ledger = self._strike(ledger, "node-a", pinned=True)
            actions.append(action)
        self.assertEqual(actions[-1], failover.GIVE_UP)
        self.assertNotIn(failover.SWITCH, actions)

    def test_a_pinned_give_up_holds_while_the_pin_stands(self):
        ledger = None
        for _ in range(failover.STRIKES_PER_NODE):
            _action, ledger = self._strike(ledger, "node-a", pinned=True)
        action, ledger = self._strike(ledger, "node-a", pinned=True)
        self.assertEqual(action, failover.HOLD)
        self.assertTrue(ledger["given_up"])

    def test_releasing_the_pin_resumes_the_walk(self):
        ledger = None
        for _ in range(failover.STRIKES_PER_NODE):
            _action, ledger = self._strike(ledger, "node-a", pinned=True)
        action, ledger = self._strike(ledger, "node-a", pinned=False)
        self.assertEqual(action, failover.SWITCH)
        self.assertFalse(ledger["given_up"])

    def test_a_country_with_one_candidate_backs_off_instead_of_looping(self):
        ledger, actions = None, []
        for _ in range(failover.STRIKES_PER_NODE):
            action, ledger = self._strike(ledger, "only", pool=["only"])
            actions.append(action)
        self.assertEqual(actions[-1], failover.BACK_OFF)
        self.assertNotIn(failover.SWITCH, actions)


class PeerShieldTests(unittest.TestCase):
    """A country's exit is shared by every line of that country. While a sibling line is
    registered over it, the exit provably carries IMS — and moving it would tear down that
    sibling's tunnel, which is the disruption this design exists to avoid."""

    POOL = ["node-a", "node-b"]

    def _strike(self, ledger, peer, verdict=failover.BLAMES_EXIT):
        return failover.record(ledger, verdict, "node-a", False, self.POOL,
                               peer_registered=peer)

    def test_a_registered_peer_keeps_the_exit_where_it_is(self):
        ledger, actions = None, []
        for _ in range(failover.STRIKES_PER_NODE * 2):
            action, ledger = self._strike(ledger, peer=True)
            actions.append(action)
        self.assertNotIn(failover.SWITCH, actions)
        self.assertEqual(ledger["tried"], [])
        self.assertTrue(ledger["held_for_peer"])

    def test_the_walk_resumes_the_moment_the_peer_lets_go(self):
        ledger = None
        for _ in range(failover.STRIKES_PER_NODE - 1):
            _action, ledger = self._strike(ledger, peer=False)
        _action, ledger = self._strike(ledger, peer=True)
        action, ledger = self._strike(ledger, peer=False)
        self.assertEqual(action, failover.SWITCH)

    def test_a_peer_blocked_line_is_still_reported_in_time(self):
        ledger, actions = None, []
        for _ in range(failover.FAILURES_BEFORE_REPORT):
            action, ledger = self._strike(ledger, peer=True)
            actions.append(action)
        self.assertEqual(actions.count(failover.REPORT), 1)
        self.assertEqual(actions[-1], failover.REPORT)


class ReportTests(unittest.TestCase):
    """Failures the exits cannot explain are worth saying out loud, but not worth stopping
    for: the carrier may simply come back, and retrying on a working exit costs nothing."""

    def test_a_stuck_line_is_reported_without_stopping_the_rebuild(self):
        ledger, actions = None, []
        for _ in range(failover.FAILURES_BEFORE_REPORT):
            action, ledger = failover.record(ledger, failover.BLAMES_ELSEWHERE, "node-a",
                                             False, ["node-a", "node-b"])
            actions.append(action)
        self.assertEqual(actions[-1], failover.REPORT)
        self.assertFalse(ledger["given_up"])

    def test_it_is_reported_once(self):
        ledger, actions = None, []
        for _ in range(failover.FAILURES_BEFORE_REPORT * 3):
            action, ledger = failover.record(ledger, failover.BLAMES_ELSEWHERE, "node-a",
                                             False, ["node-a"])
            actions.append(action)
        self.assertEqual(actions.count(failover.REPORT), 1)

    def test_a_brief_carrier_problem_passes_unmentioned(self):
        ledger, actions = None, []
        for _ in range(failover.FAILURES_BEFORE_REPORT - 1):
            action, ledger = failover.record(ledger, failover.BLAMES_ELSEWHERE, "node-a",
                                             False, ["node-a"])
            actions.append(action)
        self.assertEqual(set(actions), {failover.HOLD})


class WordingTests(unittest.TestCase):
    def test_a_pinned_give_up_says_the_exit_is_pinned(self):
        ledger = {**failover.blank_ledger(), "node": "SD-US", "given_up": True}
        text = failover.summarise(ledger, failover.GIVE_UP, "us", True)
        self.assertIn("锁定", text)
        self.assertIn("SD-US", text)

    def test_an_exhausted_pool_does_not_claim_the_nodes_are_bad(self):
        ledger = {**failover.blank_ledger(), "node": "n3", "tried": ["n1", "n2", "n3"]}
        text = failover.summarise(ledger, failover.BACK_OFF, "us", False)
        self.assertIn("3 个", text)
        self.assertIn("本机网络", text)
        self.assertIn("慢速重试", text)

    def test_a_report_points_away_from_the_exit(self):
        ledger = {**failover.blank_ledger(), "node": "n1", "failures": 6}
        text = failover.summarise(ledger, failover.REPORT, "gb", False)
        self.assertIn("不足以归咎于 GB 出口", text)
        self.assertNotIn("没有丢包", text)
        self.assertIn("会继续", text)

    def test_a_peer_blocked_report_names_the_line_it_protects(self):
        ledger = {**failover.blank_ledger(), "node": "n1", "failures": 6,
                  "held_for_peer": True}
        text = failover.summarise(ledger, failover.REPORT, "gb", False)
        self.assertIn("另一条注册中的线路", text)
        self.assertIn("没有自动换节点", text)


if __name__ == "__main__":
    unittest.main()
