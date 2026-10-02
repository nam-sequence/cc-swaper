"""Claude Code integration: statusLine feed, rate-limit hook, installer."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from ccshift import claude_hooks
from ccshift.usage_store import FetchRecord, UsageStore

T0 = 1_800_000_000.0


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@pytest.fixture
def root(tmp_path):
    """A backup root with two accounts that have been polled once."""
    (tmp_path / "sequence.json").write_text(
        json.dumps(
            {
                "accounts": {
                    "1": {"email": "a@x.com", "organizationUuid": "org"},
                    "2": {"email": "b@x.com", "organizationUuid": "org"},
                }
            }
        )
    )
    store = UsageStore(tmp_path / "cache")
    ident = claude_hooks.read_identities(tmp_path)
    now = store.clock()
    store.record(
        {
            "1": FetchRecord(
                usage={
                    "five_hour": {"pct": 20.0, "resets_at": iso(now + 3600)},
                    "seven_day": {"pct": 30.0, "resets_at": iso(now + 3 * 86400)},
                }
            ),
            "2": FetchRecord(
                usage={
                    "five_hour": {"pct": 50.0, "resets_at": iso(now + 7200)},
                    "seven_day": {"pct": 60.0, "resets_at": iso(now + 5 * 86400)},
                }
            ),
        },
        ident,
    )
    return tmp_path


def payload(now: float, five: float, seven: float) -> str:
    return json.dumps(
        {
            "rate_limits": {
                "five_hour": {"used_percentage": five, "resets_at": int(now + 7200)},
                "seven_day": {"used_percentage": seven, "resets_at": int(now + 5 * 86400)},
            }
        }
    )


class TestFeedPayload:
    def test_records_on_the_matching_account(self, root):
        import time

        slot = claude_hooks.feed_payload(payload(time.time(), 72, 61), root)
        assert slot == "2"
        entry = UsageStore(root / "cache").entries(claude_hooks.read_identities(root))["2"]
        assert entry.last_good["five_hour"]["pct"] == 72

    @pytest.mark.parametrize("raw", ["", "not json", "{}", '{"rate_limits": 3}'])
    def test_garbage_never_raises(self, root, raw):
        assert claude_hooks.feed_payload(raw, root) is None

    def test_no_roster_is_not_an_error(self, tmp_path):
        assert claude_hooks.feed_payload(payload(T0, 10, 10), tmp_path) is None


class TestStatuslineFeedPassthrough:
    def test_wrapped_command_gets_the_payload_and_its_output_is_untouched(
        self, root, monkeypatch, capfd
    ):
        raw = payload(T0, 10, 10)
        monkeypatch.setattr(claude_hooks.paths, "get_backup_root", lambda: root)
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw.encode())))
        code = claude_hooks.statusline_feed_main(
            ["--then", f"{shlex_python()} -c \"import sys;sys.stdout.write(sys.stdin.read()[:11])\""]
        )
        assert code == 0
        assert capfd.readouterr().out == raw[:11]

    def test_child_exit_status_is_passed_through(self, root, monkeypatch):
        monkeypatch.setattr(claude_hooks.paths, "get_backup_root", lambda: root)
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
        assert claude_hooks.statusline_feed_main(["--then", "exit 7"]) == 7


def shlex_python() -> str:
    import shlex

    return shlex.quote(sys.executable)


class TestLimitHit:
    def _run(self, monkeypatch, root, stdin_text, popen):
        monkeypatch.setattr(claude_hooks.paths, "get_backup_root", lambda: root)
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
        monkeypatch.setattr(subprocess, "Popen", popen)
        return claude_hooks.limit_hit_main([])

    def test_spawns_one_detached_tick(self, root, monkeypatch):
        calls = []
        self._run(
            monkeypatch,
            root,
            json.dumps({"error_type": "rate_limit"}),
            lambda *a, **k: calls.append((a, k)),
        )
        assert len(calls) == 1
        cmd = calls[0][0][0]
        assert cmd[-4:] == ["auto", "--once", "--json", "--limit-hit"][-4:]
        assert calls[0][1]["start_new_session"] is True

    def test_other_error_types_are_ignored(self, root, monkeypatch):
        calls = []
        self._run(
            monkeypatch,
            root,
            json.dumps({"error_type": "server_error"}),
            lambda *a, **k: calls.append(1),
        )
        assert calls == []

    def test_simultaneous_hooks_collapse_into_one_tick(self, root, monkeypatch):
        calls = []
        spawn = lambda *a, **k: calls.append(1)  # noqa: E731
        self._run(monkeypatch, root, json.dumps({"error_type": "rate_limit"}), spawn)
        self._run(monkeypatch, root, json.dumps({"error_type": "rate_limit"}), spawn)
        assert len(calls) == 1

    def test_a_failing_spawn_never_escapes(self, root, monkeypatch):
        def boom(*a, **k):
            raise OSError("no fork")

        assert self._run(monkeypatch, root, "{}", boom) == 0


class TestInstaller:
    def settings(self, tmp_path, data):
        path = tmp_path / "claude" / "settings.json"
        path.parent.mkdir()
        path.write_text(json.dumps(data))
        return path

    def test_wraps_the_status_line_and_adds_the_hook(self, tmp_path):
        path = self.settings(
            tmp_path,
            {"statusLine": {"type": "command", "command": "bash ~/s.sh", "refreshInterval": 30}},
        )
        changes = claude_hooks.install_hooks(path, tmp_path, "/bin/ccshift")
        data = json.loads(path.read_text())
        assert data["statusLine"]["command"] == "/bin/ccshift statusline-feed --then 'bash ~/s.sh'"
        assert data["statusLine"]["refreshInterval"] == 30
        hook = data["hooks"]["StopFailure"][0]
        assert hook["matcher"] == "rate_limit"
        assert hook["hooks"][0]["command"] == "/bin/ccshift limit-hit"
        assert len(changes) == 2
        assert path.with_name("settings.json.ccshift-bak").exists()

    def test_install_is_idempotent(self, tmp_path):
        path = self.settings(tmp_path, {"statusLine": {"type": "command", "command": "x"}})
        claude_hooks.install_hooks(path, tmp_path, "/bin/ccshift")
        first = path.read_text()
        assert claude_hooks.install_hooks(path, tmp_path, "/bin/ccshift") == []
        assert path.read_text() == first

    def test_uninstall_restores_the_original_exactly(self, tmp_path):
        original = {
            "statusLine": {"type": "command", "command": "bash ~/s.sh", "refreshInterval": 30},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
        }
        path = self.settings(tmp_path, original)
        claude_hooks.install_hooks(path, tmp_path, "/bin/ccshift")
        claude_hooks.uninstall_hooks(path, tmp_path)
        assert json.loads(path.read_text()) == original

    def test_uninstall_recovers_the_command_without_the_record(self, tmp_path):
        path = self.settings(tmp_path, {"statusLine": {"type": "command", "command": "bash ~/s.sh"}})
        claude_hooks.install_hooks(path, tmp_path, "/bin/ccshift")
        (tmp_path / claude_hooks.INSTALL_RECORD).unlink()
        claude_hooks.uninstall_hooks(path, tmp_path)
        assert json.loads(path.read_text())["statusLine"]["command"] == "bash ~/s.sh"

    def test_no_status_line_gets_only_the_hook(self, tmp_path):
        path = self.settings(tmp_path, {})
        changes = claude_hooks.install_hooks(path, tmp_path, "/bin/ccshift")
        data = json.loads(path.read_text())
        assert "statusLine" not in data
        assert claude_hooks.hooks_status(data) == {
            "statusLine": False, "hasStatusLine": False, "stopFailure": True,
        }
        assert any("none configured" in c for c in changes)

    def test_other_hooks_are_preserved(self, tmp_path):
        other = {"matcher": "rate_limit", "hooks": [{"type": "command", "command": "notify"}]}
        path = self.settings(tmp_path, {"hooks": {"StopFailure": [other]}})
        claude_hooks.install_hooks(path, tmp_path, "/bin/ccshift")
        claude_hooks.uninstall_hooks(path, tmp_path)
        assert json.loads(path.read_text())["hooks"]["StopFailure"] == [other]
