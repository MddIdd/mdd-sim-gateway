"""Evidence for engine bounces that nothing used to record.

The engine containers were being restarted by Docker's restart policy several times a day —
Asterisk went away a few seconds after the P-CSCF reload that follows an ePDG teardown — and
none of it reached the timeline: the bounce resolves faster than the health policy's threshold,
so no recovery is scheduled and lifecycle.jsonl stays empty. These tests cover the three pieces
that make such a bounce legible afterwards, plus the privacy boundary the new logs must respect.
"""
import importlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO / "engine" / "entrypoint.sh"


def engine_module():
    fake_docker = SimpleNamespace(
        from_env=lambda: None,
        errors=SimpleNamespace(NotFound=type("NotFound", (Exception,), {})),
    )
    with patch.dict(sys.modules, {"docker": fake_docker}):
        sys.modules.pop("control.app.engine", None)
        return importlib.import_module("control.app.engine")


class SupervisorRecordTests(unittest.TestCase):
    """The entrypoint's record of how Asterisk left, read back by the manager."""

    def test_last_exit_reports_the_most_recent_disposition(self):
        engine = engine_module()
        with tempfile.TemporaryDirectory() as temp:
            logs = Path(temp) / "instances" / "7" / "logs" / "asterisk"
            logs.mkdir(parents=True)
            (logs / "supervisor.jsonl").write_text(
                json.dumps({"ts": 1, "event": "asterisk_exited",
                            "rc": 0, "disposition": "exit"}) + "\n"
                + json.dumps({"ts": 2, "event": "swu_ike_exited", "rc": 1}) + "\n"
                + json.dumps({"ts": 3, "event": "asterisk_exited", "rc": 139,
                              "signal": 11, "disposition": "signal"}) + "\n")
            with patch.object(engine, "DATA_DIR", temp):
                record = engine.last_engine_exit("7")
        self.assertEqual(record["disposition"], "signal")
        self.assertEqual(record["signal"], 11)

    def test_missing_file_is_not_an_error(self):
        engine = engine_module()
        with tempfile.TemporaryDirectory() as temp, patch.object(engine, "DATA_DIR", temp):
            self.assertEqual(engine.last_engine_exit("7"), {})

    def test_engine_restarted_is_an_accepted_lifecycle_event(self):
        engine = engine_module()
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "instances" / "7" / "logs"
            base.mkdir(parents=True)
            with patch.object(engine, "DATA_DIR", temp):
                engine.record_lifecycle("7", "engine_restarted", reason_code="engine_exit")
            written = json.loads((base / "lifecycle.jsonl").read_text().strip())
        self.assertEqual(written["event"], "engine_restarted")
        self.assertEqual(written["reason_code"], "engine_exit")

    def test_runtime_reports_restart_count(self):
        """A restart-policy bounce increments RestartCount on the same container; a rebuild
        the manager performs starts a fresh one at zero. Only the counter separates them."""
        engine = engine_module()
        container = SimpleNamespace(
            status="running", id="abc",
            attrs={"NetworkSettings": {"Networks": {"bridge": {"IPAddress": "172.17.0.4"}}},
                   "RestartCount": 6, "State": {"StartedAt": "2026-09-15T07:09:02Z"}})
        client = SimpleNamespace(containers=SimpleNamespace(get=lambda _name: container))
        with patch.object(engine, "_client", return_value=client):
            runtime = engine.container_runtime("7")
        self.assertEqual(runtime["restart_count"], 6)
        self.assertEqual(runtime["started_at"], "2026-09-15T07:09:02Z")


class SupportBundleBoundaryTests(unittest.TestCase):
    """Asterisk's own logs are now persistent. They must not follow into a support bundle."""

    def test_asterisk_logs_are_not_collected_but_supervisor_is(self):
        source = (REPO / "control" / "app" / "operations.py").read_text()
        # The allow-list block that decides which files a bundle may carry.
        block = source[source.index("Explicit allow-list"):]
        block = block[:block.index("for path in sorted(paths)")]
        self.assertIn('logs/asterisk/supervisor.jsonl', block)
        # `full` and `messages` carry the subscriber's IMS public identity on every
        # registration, so no glob may sweep the directory wholesale.
        self.assertNotIn('logs/asterisk/*', block)
        self.assertNotIn('logs/asterisk/full', block)
        self.assertNotIn('logs/asterisk/messages', block)


class EntrypointSupervisionTests(unittest.TestCase):
    """The entrypoint is shell; these assert on its text and on its actual behaviour."""

    def test_script_is_syntactically_valid(self):
        result = subprocess.run(["bash", "-n", str(ENTRYPOINT)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_asterisk_is_supervised_rather_than_exec_replaced(self):
        """Asterisk as PID 1 meant the container simply vanished with it, leaving only
        ExitCode=0 — which cannot distinguish a clean shutdown from anything else."""
        text = ENTRYPOINT.read_text()
        self.assertNotIn("exec asterisk", text)
        self.assertIn("supervisor_record asterisk_exited", text)
        # Both dispositions must be recorded, not just the signal case.
        self.assertIn("disposition=signal", text)
        self.assertIn("disposition=exit", text)

    def test_reconnect_backoff_resets_after_a_stable_run(self):
        """Extracted and run for real: the delay only ever doubled (4 -> 8 -> ... -> 60) and
        was never reset, which stayed hidden only because the container kept restarting and
        re-seeding it. Once Asterisk no longer takes the container down, an unreset backoff
        would leave a healthy line waiting a full minute to re-establish."""
        script = """
        set -u
        SWU_STABLE_SECONDS=120
        supervisor_record() { :; }
        log() { :; }
        backoff=4
        for ran in $RUNS; do
          if [ "$ran" -ge "$SWU_STABLE_SECONDS" ]; then
            backoff=4
          fi
          echo -n "$backoff "
          backoff=$((backoff*2)); [ "$backoff" -gt 60 ] && backoff=60
        done
        exit 0
        """
        # Four teardowns in quick succession, then one run that stayed up for an hour, then
        # another teardown: the last one must be back at the short delay.
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                env={"RUNS": "5 5 5 5 3600 5", "PATH": "/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0, result.stderr)
        delays = result.stdout.split()
        self.assertEqual(delays, ["4", "8", "16", "32", "4", "8"])

    def test_asterisk_logs_are_written_to_the_bind_mounted_volume(self):
        """/logs survives both a container restart and a manager rebuild; the image's default
        log directory survives neither."""
        conf = (REPO / "engine" / "templates" / "asterisk.conf.j2").read_text()
        self.assertRegex(conf, r"astlogdir\s*=\s*/logs/asterisk")
        self.assertIn("mkdir -p", ENTRYPOINT.read_text())
        self.assertIn("MDD_AST_LOGDIR", ENTRYPOINT.read_text())


class PcscfApplyModeTests(unittest.TestCase):
    """Applying a new P-CSCF is the step Asterisk has been dying just after."""

    def setUp(self):
        self.source = (REPO / "engine" / "swu_ike.py").read_text()
        start = self.source.index("def swu_apply_pcscf")
        self.func = self.source[start:self.source.index("\ndef ", start + 10)]

    def test_default_mode_is_restart(self):
        """The reload path is a confirmed use-after-free (same core-dump stack on two
        carriers); the default must not be the path that crashes."""
        self.assertIn('os.environ.get("SWU_PCSCF_APPLY_MODE") or "restart"', self.func)
        # `reload` stays available for comparison.
        self.assertIn('_asterisk_cli("module reload res_pjsip.so")', self.func)

    def test_nothing_is_applied_before_asterisk_is_running(self):
        """On first bring-up the tunnel beats the entrypoint to pcscf.applied, so every fresh
        start looked like a change. Applying then would cold-restart an Asterisk that has only
        just started; with none running, rendering the config is all there is to do."""
        guard = self.func.index("if not _asterisk_running():")
        self.assertLess(guard, self.func.index('swu_notify("pcscf_apply_start"'))
        self.assertLess(guard, self.func.index('_asterisk_cli("core restart now")'))
        early = self.func[guard:self.func.index('swu_notify("pcscf_apply_start"')]
        self.assertIn("render", early)
        self.assertIn('"pcscf.applied"), "w"', early)
        self.assertIn("return", early)

    def test_restart_mode_is_available_and_validated(self):
        self.assertIn("core restart now", self.func)
        self.assertIn('if mode not in ("reload", "restart")', self.func)

    def test_applied_marker_is_written_before_asterisk_is_touched(self):
        """Under `restart` Asterisk re-execs and this call may not return. The config is
        already on disk by then, so the marker must not be left stale."""
        marker = self.func.index('"pcscf.applied"), "w"')
        # Match the calls, not the mentions of them in the docstring above.
        self.assertLess(marker, self.func.index('_asterisk_cli("core restart now")'))
        self.assertLess(marker, self.func.index('_asterisk_cli("module reload res_pjsip.so")'))

    def test_apply_mode_is_settable_per_line_with_a_global_fallback(self):
        """The crash is a use-after-free that does not fire on every reload, so switching one
        line proves nothing unless the others stay on the old path as a control group."""
        source = (REPO / "control" / "app" / "engine.py").read_text()
        start = source.index('"SWU_PCSCF_APPLY_MODE"')
        clause = source[start:start + 320]
        self.assertIn('inst.get("pcscf_apply_mode")', clause)
        # Per-line value must win over the global one.
        self.assertLess(clause.index('inst.get("pcscf_apply_mode")'),
                        clause.index('settings.get("engine")'))
        self.assertIn('or "restart"', clause)

    def test_peer_initiated_teardown_is_reported_with_its_duration(self):
        """Which side ended the tunnel, and after how long, is the whole story behind the
        periodic outages — one carrier tears down on a ~24h timer regardless of rekeys."""
        self.assertIn('swu_notify("tunnel_deleted_by_peer"', self.source)
        self.assertIn("def seconds_since_connect", self.source)
        self.assertIn("self._connected_at = time.time()", self.source)


class RestartBaselineTests(unittest.IsolatedAsyncioTestCase):
    """A bounce must be recorded even when it is the first Docker event the manager sees."""

    async def test_bounce_after_manager_restart_is_recorded(self):
        from control.app import main
        hub = main.Hub()
        # Manager just started; the status poll sees line 5 running with no bounces yet.
        hub.seed_restart_baseline("5", {"running": True, "restart_count": 0})
        with patch.object(main.engine, "last_engine_exit",
                          return_value={"disposition": "signal"}), \
                patch.object(main.engine, "record_lifecycle") as record:
            await hub._note_unrequested_restart("5", {"running": True, "restart_count": 1})
        record.assert_called_once_with("5", "engine_restarted", reason_code="engine_signal")

    async def test_seeding_never_overwrites_an_existing_baseline(self):
        from control.app import main
        hub = main.Hub()
        hub.seed_restart_baseline("7", {"running": True, "restart_count": 0})
        # A later poll after the bounce must not swallow it by moving the baseline.
        hub.seed_restart_baseline("7", {"running": True, "restart_count": 1})
        self.assertEqual(hub._restart_counts["7"], 0)


class TunnelRebuiltApplyTests(unittest.TestCase):
    """A full attach gives the tunnel a new inner address even when the ePDG hands back the same
    P-CSCF. Keyed on the P-CSCF alone that case did nothing, and line 7 stayed "Registered" but
    unreachable for 17.5 minutes on 09-18 04:09. These run the real function with stubbed I/O."""

    def _load(self, applied, asterisk_up=True):
        import ast
        source = (REPO / "engine" / "swu_ike.py").read_text()
        tree = ast.parse(source)
        wanted = {"_asterisk_cli", "_asterisk_running", "_pcscf_debug_window", "swu_apply_pcscf"}
        code = "\n\n".join(ast.get_source_segment(source, node) for node in tree.body
                           if isinstance(node, ast.FunctionDef) and node.name in wanted)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        run = Path(temp.name)
        render = run / "render.py"
        render.write_text("")
        if applied is not None:
            (run / "pcscf.applied").write_text(applied)
        cli = []

        def call(cmd, **_kwargs):
            if cmd[:2] == ["asterisk", "-rx"]:
                if cmd[2] == "core show uptime":
                    return 0 if asterisk_up else 1
                cli.append(cmd[2])
            return 0

        import os as _os
        env = dict(_os.environ, SWU_RENDER=str(render), SWU_PCSCF_APPLY_MODE="restart",
                   SWU_PCSCF_DEBUG_SECONDS="0")
        ns = {"os": SimpleNamespace(path=_os.path, environ=env),
              "subprocess": SimpleNamespace(call=call, DEVNULL=None),
              "threading": None, "time": None, "SWU_RUNDIR": str(run),
              "swu_log": lambda _msg: None, "swu_notify": lambda *_a: None}
        exec(compile(code, "swu_ike_subset", "exec"), ns)
        return ns["swu_apply_pcscf"], cli, run

    def test_rebuilt_tunnel_with_same_pcscf_restarts_asterisk(self):
        apply, cli, _ = self._load(applied="2001:db8::1")
        apply("2001:db8::1", tunnel_rebuilt=True)
        self.assertIn("core restart now", cli)

    def test_same_pcscf_inside_a_live_tunnel_is_still_a_no_op(self):
        apply, cli, _ = self._load(applied="2001:db8::1")
        apply("2001:db8::1")
        self.assertEqual(cli, [])

    def test_first_bring_up_only_renders(self):
        apply, cli, run = self._load(applied=None, asterisk_up=False)
        apply("2001:db8::1", tunnel_rebuilt=True)
        self.assertEqual(cli, [])
        self.assertEqual((run / "pcscf.applied").read_text(), "2001:db8::1")

    def test_only_the_full_attach_call_site_marks_the_tunnel_rebuilt(self):
        source = (REPO / "engine" / "swu_ike.py").read_text()
        self.assertEqual(source.count("swu_apply_pcscf(pcscf, tunnel_rebuilt=True)"), 1)
        # P-CSCF restoration happens inside a live tunnel; it must stay keyed on the value.
        self.assertIn("swu_apply_pcscf(new_pcscf)\n", source.replace("\r\n", "\n"))
        connected = source[source.index("def state_connected"):]
        connected = connected[:connected.index("\n    def ", 10)]
        self.assertIn("tunnel_rebuilt=True", connected)
