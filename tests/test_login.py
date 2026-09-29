"""Tests for ``ccshift add --login`` (sign in with a browser in a staging profile)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ccshift import cli, login
from ccshift.exceptions import ConfigError
from ccshift.switcher import ClaudeAccountSwitcher


def _fake_claude(tmp_path: Path, body: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return bin_dir


@pytest.fixture
def recorder(tmp_path: Path) -> Path:
    return tmp_path / "claude-calls"


def test_login_runs_claude_in_a_staging_profile_and_captures_from_it(
    temp_home: Path, tmp_path: Path, recorder: Path, monkeypatch,
):
    bin_dir = _fake_claude(tmp_path, f"""
printf '%s\\n' "$CLAUDE_CONFIG_DIR" >> '{recorder}'
printf '%s\\n' "$*" >> '{recorder}'
printf '%s\\n' "${{CLAUDE_SECURESTORAGE_CONFIG_DIR-unset}}" >> '{recorder}'
exit 0
""")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "/elsewhere")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    switcher = ClaudeAccountSwitcher()
    seen: dict = {}

    def fake_add(**kwargs):
        seen["config_dir"] = os.environ.get("CLAUDE_CONFIG_DIR")
        seen["secure"] = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
        seen["kwargs"] = kwargs
        return {"number": 3, "email": "new@example.com", "alias": "dev", "created": True}

    with patch.object(switcher, "add_account", side_effect=fake_add), \
         patch.object(login, "delete_macos_keychain_entry") as delete_keychain:
        result = login.login_and_add(
            switcher, sso=True, email="new@example.com", alias="dev", json_mode=True,
        )

    assert result == {"number": 3, "email": "new@example.com", "alias": "dev", "created": True}
    staging_dir, arguments, secure_env = recorder.read_text().splitlines()
    staging = Path(staging_dir)
    assert staging.parent == switcher.backup_dir / login.STAGING_DIR_NAME
    assert arguments == "auth login --sso --email new@example.com"
    assert secure_env == "unset"  # claude's secure storage follows the staging profile
    # The capture reads the staging profile, and must not claim the live login.
    assert seen["config_dir"] == staging_dir
    assert seen["secure"] is None
    assert seen["kwargs"] == {"slot": None, "alias": "dev", "assume_yes": False, "set_active": False}
    # Afterwards the environment is restored and the staging profile is gone.
    assert "CLAUDE_CONFIG_DIR" not in os.environ
    assert os.environ["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == "/elsewhere"
    assert not staging.exists()
    delete_keychain.assert_called_once_with(staging)


def test_private_sign_in_points_browser_at_a_private_opener(
    temp_home: Path, tmp_path: Path, recorder: Path, monkeypatch,
):
    bin_dir = _fake_claude(tmp_path, f"""
printf '%s\\n' "$BROWSER" >> '{recorder}'
cat "$BROWSER" >> '{recorder}'
exit 0
""")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    switcher = ClaudeAccountSwitcher()

    with patch.object(switcher, "add_account", return_value={
        "number": 1, "email": "a@example.com", "alias": None, "created": True,
    }), patch.object(login, "delete_macos_keychain_entry"):
        login.login_and_add(switcher, private=True, browser="com.google.Chrome", json_mode=True)

    lines = recorder.read_text().splitlines()
    opener = Path(lines[0])
    assert opener.name == "open-private"
    assert opener.parent.parent == switcher.backup_dir / login.STAGING_DIR_NAME
    assert lines[1] == "#!/bin/sh"
    assert "-m ccshift.private_browser --browser com.google.Chrome" in lines[2]
    assert lines[2].endswith('"$@"')
    assert not opener.exists()  # removed with the staging profile


def test_handoff_sign_in_writes_the_url_for_the_app(
    temp_home: Path, tmp_path: Path, monkeypatch,
):
    handoff = tmp_path / "handoff" / "url"
    handoff.parent.mkdir()
    # Like claude: run $BROWSER with the sign-in URL, then finish.
    bin_dir = _fake_claude(tmp_path, """
"$BROWSER" 'https://claude.com/cai/oauth/authorize?code=true&state=abc'
exit 0
""")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    switcher = ClaudeAccountSwitcher()

    with patch.object(switcher, "add_account", return_value={
        "number": 2, "email": "b@example.com", "alias": None, "created": True,
    }), patch.object(login, "delete_macos_keychain_entry"):
        login.login_and_add(switcher, handoff_file=str(handoff), json_mode=True)

    assert handoff.read_text() == "https://claude.com/cai/oauth/authorize?code=true&state=abc"
    assert oct(handoff.stat().st_mode & 0o777) == "0o600"


def test_failed_sign_in_adds_nothing_and_cleans_up(
    temp_home: Path, tmp_path: Path, recorder: Path, monkeypatch,
):
    bin_dir = _fake_claude(tmp_path, f"""
printf '%s\\n' "$CLAUDE_CONFIG_DIR" >> '{recorder}'
exit 1
""")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    switcher = ClaudeAccountSwitcher()

    with patch.object(switcher, "add_account") as add, \
         patch.object(login, "delete_macos_keychain_entry"):
        with pytest.raises(ConfigError, match="did not complete"):
            login.login_and_add(switcher, json_mode=True)

    add.assert_not_called()
    assert not Path(recorder.read_text().strip()).exists()


def test_sign_in_that_never_finishes_times_out(
    temp_home: Path, tmp_path: Path, monkeypatch,
):
    bin_dir = _fake_claude(tmp_path, "exec /bin/sleep 30\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    switcher = ClaudeAccountSwitcher()

    with patch.object(switcher, "add_account") as add, \
         patch.object(login, "delete_macos_keychain_entry"):
        with pytest.raises(ConfigError, match="did not finish"):
            login.login_and_add(switcher, json_mode=True, timeout_s=0.5)
    add.assert_not_called()
    assert list((switcher.backup_dir / login.STAGING_DIR_NAME).iterdir()) == []


def test_missing_claude_is_explained(temp_home: Path, tmp_path: Path, monkeypatch):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(ConfigError, match="'claude' command"):
        login.login_and_add(ClaudeAccountSwitcher(), json_mode=True)


def test_stale_staging_profiles_are_swept(temp_home: Path, tmp_path: Path, monkeypatch):
    bin_dir = _fake_claude(tmp_path, "exit 1\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    switcher = ClaudeAccountSwitcher()
    root = switcher.backup_dir / login.STAGING_DIR_NAME
    root.mkdir(parents=True)
    stale = root / "stale"
    stale.mkdir()
    os.utime(stale, (0, 0))

    with patch.object(login, "delete_macos_keychain_entry") as delete_keychain:
        with pytest.raises(ConfigError):
            login.login_and_add(switcher, json_mode=True)
    assert not stale.exists()
    assert stale in [call.args[0] for call in delete_keychain.call_args_list]


class TestLoginCli:
    @pytest.mark.parametrize("argv, message", [
        (["ccshift", "list", "--login"], "--login can only be used with 'add'"),
        (["ccshift", "add", "--sso"], "--sso and --private can only be used with 'add --login'"),
        (["ccshift", "add", "--private"], "--sso and --private can only be used with 'add --login'"),
        (["ccshift", "add", "--login", "--browser", "com.google.Chrome"], "--browser can only be used with"),
        (["ccshift", "add", "--handoff-file", "/tmp/x"], "--handoff-file can only be used with 'add --login'"),
        (["ccshift", "add", "--login", "--private", "--handoff-file", "/tmp/x"], "cannot be combined"),
        (["ccshift", "add", "--login", "--handoff-file", "relative"], "must be an absolute path"),
        (["ccshift", "add", "--email", "a@example.com"], "--email can only be used with"),
    ])
    def test_flag_combinations_are_validated(self, argv, message, capsys):
        with patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2
        assert message in capsys.readouterr().err

    def test_add_login_json_dispatches_and_serializes(self, capsys):
        with patch("ccshift.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch("ccshift.login.login_and_add") as login_and_add, \
             patch.object(sys, "argv", [
                 "ccshift", "add", "--login", "--sso", "--email", "a@example.com",
                 "--alias", "work", "--json",
             ]), \
             patch("os.geteuid", return_value=1000, create=True):
            login_and_add.return_value = {
                "number": 4, "email": "a@example.com", "alias": "work", "created": True,
            }
            cli.main()

        login_and_add.assert_called_once_with(
            switcher_cls.return_value, sso=True, email="a@example.com", alias="work",
            slot=None, assume_yes=False, json_mode=True, private=False, browser=None,
            handoff_file=None,
        )
        assert json.loads(capsys.readouterr().out) == {
            "schemaVersion": 1,
            "action": "added",
            "account": {"number": 4, "email": "a@example.com", "alias": "work"},
        }


class TestPrivateBrowser:
    def test_default_browser_is_read_from_launch_services(self, tmp_path: Path):
        import plistlib
        from ccshift import private_browser

        plist = tmp_path / "ls.plist"
        plist.write_bytes(plistlib.dumps({"LSHandlers": [
            {"LSHandlerContentType": "public.html", "LSHandlerRoleAll": "com.apple.safari"},
            {"LSHandlerURLScheme": "https", "LSHandlerRoleAll": "company.thebrowser.browser"},
        ]}, fmt=plistlib.FMT_BINARY))
        assert private_browser.default_browser_bundle_id(plist) == "company.thebrowser.browser"
        assert private_browser.default_browser_bundle_id(tmp_path / "missing.plist") is None

    def test_default_browser_with_a_private_mode_is_used(self):
        from ccshift import private_browser

        with patch.object(private_browser, "is_installed", return_value=True) as installed:
            assert private_browser.choose_browser(None, "org.mozilla.firefox") == "org.mozilla.firefox"
        installed.assert_not_called()

    def test_arc_or_safari_falls_back_to_the_first_installed_private_browser(self):
        from ccshift import private_browser

        installed = {"com.brave.browser", "org.mozilla.firefox"}
        with patch.object(private_browser, "is_installed", side_effect=installed.__contains__):
            assert private_browser.choose_browser(None, "company.thebrowser.browser") == "com.brave.browser"
            assert private_browser.choose_browser(None, None) == "com.brave.browser"
        with patch.object(private_browser, "is_installed", return_value=False):
            assert private_browser.choose_browser(None, "com.apple.safari") is None

    def test_requested_browser_must_support_private_windows(self):
        from ccshift import private_browser

        assert private_browser.choose_browser("com.google.Chrome", None) == "com.google.Chrome"
        assert private_browser.choose_browser("company.thebrowser.Browser", None) is None

    def test_private_open_commands(self):
        from ccshift import private_browser

        assert private_browser.private_open_command("https://x.test", "com.google.Chrome") == [
            "open", "-n", "-b", "com.google.Chrome", "--args", "--incognito", "https://x.test",
        ]
        assert private_browser.private_open_command("https://x.test", "org.mozilla.firefox")[-2:] == [
            "-private-window", "https://x.test",
        ]
        assert "--inprivate" in private_browser.private_open_command("https://x.test", "com.microsoft.edgemac")

    def test_main_falls_back_to_a_normal_window(self, capsys):
        from ccshift import private_browser

        with patch.object(private_browser, "default_browser_bundle_id", return_value="com.apple.safari"), \
             patch.object(private_browser, "is_installed", return_value=False), \
             patch.object(private_browser.subprocess, "run") as run:
            run.return_value.returncode = 0
            assert private_browser.main(["https://x.test"]) == 0
        run.assert_called_once_with(["open", "https://x.test"])
        assert "normal window" in capsys.readouterr().err
