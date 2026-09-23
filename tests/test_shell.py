from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from cc_swaper.profiles import ProfileStore
from cc_swaper.shell import _source_script, install, installed, uninstall


def test_shell_install_is_reversible_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    zdotdir = tmp_path / "zsh"
    zdotdir.mkdir()
    rc = zdotdir / ".zshrc"
    original = "export EXAMPLE=1\n"
    rc.write_text(original)
    rc.chmod(0o600)
    monkeypatch.setenv("ZDOTDIR", str(zdotdir))
    store = ProfileStore(tmp_path / "store")

    assert install(store) == rc
    assert installed(store)
    first = rc.read_text()
    assert "source " in first
    assert install(store) == rc
    assert rc.read_text() == first
    assert (store.home / "claude.zsh").stat().st_mode & 0o777 == 0o600

    assert uninstall(store) == rc
    assert rc.read_text() == original
    assert not (store.home / "claude.zsh").exists()
    assert not installed(store)


def test_shell_wrapper_routes_interactive_and_native_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    zdotdir = tmp_path / "zsh"
    zdotdir.mkdir()
    monkeypatch.setenv("ZDOTDIR", str(zdotdir))
    store = ProfileStore(tmp_path / "store")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ccs = fake_bin / "ccs"
    fake_ccs.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['CCS_LOG'], 'a') as h: h.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "with open(os.environ['CCS_BIN_LOG'], 'a') as h: h.write(os.environ.get('CC_SWAPER_CLAUDE_BIN', '') + '\\n')\n"
    )
    fake_ccs.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    install(store)
    log = tmp_path / "calls.jsonl"
    bin_log = tmp_path / "claude-bins.txt"
    env = dict(os.environ)
    env.update({
        "ZDOTDIR": str(zdotdir),
        "CCS_LOG": str(log),
        "CCS_BIN_LOG": str(bin_log),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    })
    result = subprocess.run(
        ["zsh", "-ic", "claude 'fix bug'; claude auth status; claude -c; claude --print hi; claude --version"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls == [
        ["run", "--", "fix bug"],
        ["native", "--", "auth", "status"],
        ["resume"],
        ["native", "--", "--print", "hi"],
        ["native", "--", "--version"],
    ]
    assert all(Path(line).is_absolute() and Path(line).is_file() for line in bin_log.read_text().splitlines())


def test_shell_install_refuses_existing_claude_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    zdotdir = tmp_path / "zsh"
    zdotdir.mkdir()
    rc = zdotdir / ".zshrc"
    original = "alias claude='other-wrapper'\n"
    rc.write_text(original)
    monkeypatch.setenv("ZDOTDIR", str(zdotdir))
    store = ProfileStore(tmp_path / "store")

    with pytest.raises(RuntimeError, match="existing claude alias"):
        install(store)
    assert rc.read_text() == original
    assert not (store.home / "claude.zsh").exists()


def test_shell_uninstall_preserves_rc_without_trailing_newline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    zdotdir = tmp_path / "zsh"
    zdotdir.mkdir()
    rc = zdotdir / ".zshrc"
    original = "export EXAMPLE=1"
    rc.write_text(original)
    monkeypatch.setenv("ZDOTDIR", str(zdotdir))
    store = ProfileStore(tmp_path / "store")

    install(store)
    uninstall(store)
    assert rc.read_text() == original


def test_shell_uninstall_preserves_modified_generated_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    zdotdir = tmp_path / "zsh"
    zdotdir.mkdir()
    monkeypatch.setenv("ZDOTDIR", str(zdotdir))
    store = ProfileStore(tmp_path / "store")
    install(store)
    script = store.home / "claude.zsh"
    script.write_text(script.read_text() + "# my addition\n")

    uninstall(store)
    assert script.is_file()
    assert not installed(store)


def test_shell_uninstall_rejects_tampered_source_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    zdotdir = tmp_path / "zsh"
    zdotdir.mkdir()
    monkeypatch.setenv("ZDOTDIR", str(zdotdir))
    store = ProfileStore(tmp_path / "store")
    rc = install(store)
    current = rc.read_text()
    tampered = current.replace(str(store.home / "claude.zsh"), "/tmp/other-script")
    rc.write_text(tampered)

    with pytest.raises(RuntimeError, match="expected script"):
        uninstall(store)
    assert rc.read_text() == tampered


def test_shell_install_rejects_writable_zshrc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    zdotdir = tmp_path / "zsh"
    zdotdir.mkdir()
    rc = zdotdir / ".zshrc"
    rc.write_text("# user config\n")
    rc.chmod(0o666)
    monkeypatch.setenv("ZDOTDIR", str(zdotdir))
    store = ProfileStore(tmp_path / "store")

    with pytest.raises(RuntimeError, match="writable by others"):
        install(store)


def test_shell_store_change_refuses_to_modify_existing_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    zdotdir = tmp_path / "zsh"
    zdotdir.mkdir()
    monkeypatch.setenv("ZDOTDIR", str(zdotdir))
    old_store = ProfileStore(tmp_path / "old-store")
    new_store = ProfileStore(tmp_path / "new-store")
    rc = install(old_store)
    original = rc.read_text()

    with pytest.raises(RuntimeError, match="expected script"):
        install(new_store)
    with pytest.raises(RuntimeError, match="expected script"):
        uninstall(new_store)
    assert rc.read_text() == original
    assert installed(old_store)
    uninstall(old_store)


def test_source_script_does_not_resubstitute_marker_inside_quoted_path(tmp_path: Path) -> None:
    binary_dir = tmp_path / "c c" / "__CLAUDE_BIN__"
    binary_dir.mkdir(parents=True)
    fake_ccs = binary_dir / "ccs"
    fake_ccs.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$CC_SWAPER_CLAUDE_BIN\" > \"$CCS_LOG\"\n"
    )
    fake_ccs.chmod(0o755)
    suspicious_claude = tmp_path / "claude; echo INJECTED"
    wrapper = tmp_path / "wrapper.zsh"
    wrapper.write_text(_source_script(fake_ccs, suspicious_claude))
    log = tmp_path / "seen-path"
    env = dict(os.environ, CCS_LOG=str(log))

    result = subprocess.run(
        ["zsh", "-f", "-c", f"source {shlex.quote(str(wrapper))}; claude hello"],
        env=env, capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text().strip() == str(suspicious_claude)
    assert "INJECTED" not in result.stdout + result.stderr
