from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install.sh"
BUILDER = ROOT / "scripts" / "build-release.sh"
VERSION_MARKER = "__CC_SWAPER_RELEASE_VERSION__"


def _project_version(root: Path = ROOT) -> str:
    project_text = (root / "pyproject.toml").read_text(encoding="utf-8")
    project = re.search(r"(?ms)^\[project\]\s*$(.*?)(?=^\[|\Z)", project_text)
    assert project is not None
    match = re.search(r'^\s*version\s*=\s*"([^\"]+)"\s*$', project.group(1), re.MULTILINE)
    assert match is not None
    return match.group(1)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _run_bash(script: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
        check=False,
    )


def test_release_scripts_have_valid_bash_syntax() -> None:
    for script in (INSTALLER, BUILDER):
        result = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_checkout_no_setup_installs_project_without_running_ccs(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv_log = tmp_path / "uv.log"
    _write_executable(
        fake_bin / "uv",
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$UV_LOG"\n',
    )
    _write_executable(fake_bin / "ccs", '#!/bin/sh\nprintf "ccs 0.7.0\\n"\n')
    env = os.environ.copy()
    env.update({"PATH": f"{fake_bin}{os.pathsep}{env['PATH']}", "UV_LOG": str(uv_log)})

    result = _run_bash(INSTALLER, "--no-setup", env=env)

    assert result.returncode == 0, result.stderr
    assert uv_log.read_text(encoding="utf-8").splitlines() == [
        "tool",
        "install",
        "--force",
        str(ROOT),
    ]


def test_checkout_default_still_initializes_and_sets_up_ccs(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "uname", '#!/bin/sh\nprintf "Darwin\\n"\n')
    uv_log = tmp_path / "uv.log"
    ccs_log = tmp_path / "ccs.log"
    _write_executable(
        fake_bin / "uv",
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$UV_LOG"\n',
    )
    _write_executable(
        fake_bin / "ccs",
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CCS_LOG"\n'
        '[ "$1" = list ] && exit 1\n'
        'exit 0\n',
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "UV_LOG": str(uv_log),
            "CCS_LOG": str(ccs_log),
        }
    )

    result = _run_bash(INSTALLER, env=env)

    assert result.returncode == 0, result.stderr
    assert uv_log.read_text(encoding="utf-8").splitlines()[:3] == ["tool", "install", "--force"]
    assert ccs_log.read_text(encoding="utf-8").splitlines() == ["--version", "list", "init", "setup"]


def test_upgrade_stops_old_monitor_before_replacing_cli(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "uname", '#!/bin/sh\nprintf "Darwin\\n"\n')
    actions = tmp_path / "actions.log"
    _write_executable(
        fake_bin / "ccs",
        '#!/bin/sh\n'
        'if [ "$1" = --version ]; then printf "ccs 0.6.0\\n"; exit 0; fi\n'
        'printf "ccs %s\\n" "$*" >> "$ACTIONS"\n',
    )
    _write_executable(
        fake_bin / "uv",
        '#!/bin/sh\nprintf "uv %s\\n" "$*" >> "$ACTIONS"\n',
    )
    env = os.environ.copy()
    env.update({"PATH": f"{fake_bin}{os.pathsep}{env['PATH']}", "ACTIONS": str(actions)})

    result = _run_bash(INSTALLER, env=env)

    assert result.returncode == 0, result.stderr
    assert actions.read_text(encoding="utf-8").splitlines() == [
        "ccs service uninstall",
        f"uv tool install --force {ROOT}",
        "ccs list",
        "ccs setup",
    ]


def test_no_setup_still_removes_old_macos_monitor(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    actions = tmp_path / "actions.log"
    _write_executable(fake_bin / "uname", '#!/bin/sh\nprintf "Darwin\\n"\n')
    _write_executable(
        fake_bin / "ccs",
        '#!/bin/sh\n'
        'if [ "$1" = --version ]; then printf "ccs 0.6.0\\n"; exit 0; fi\n'
        'printf "ccs %s\\n" "$*" >> "$ACTIONS"\n',
    )
    _write_executable(fake_bin / "uv", '#!/bin/sh\nprintf "uv %s\\n" "$*" >> "$ACTIONS"\n')
    env = os.environ.copy()
    env.update({"PATH": f"{fake_bin}{os.pathsep}{env['PATH']}", "ACTIONS": str(actions)})

    result = _run_bash(INSTALLER, "--no-setup", env=env)

    assert result.returncode == 0, result.stderr
    assert actions.read_text(encoding="utf-8").splitlines() == [
        "ccs service uninstall",
        f"uv tool install --force {ROOT}",
    ]


def test_linux_upgrade_skips_macos_service_cleanup(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    actions = tmp_path / "actions.log"
    _write_executable(fake_bin / "uname", '#!/bin/sh\nprintf "Linux\\n"\n')
    _write_executable(
        fake_bin / "ccs",
        '#!/bin/sh\n'
        'if [ "$1" = --version ]; then printf "ccs 0.6.0\\n"; exit 0; fi\n'
        'printf "ccs %s\\n" "$*" >> "$ACTIONS"\n',
    )
    _write_executable(fake_bin / "uv", '#!/bin/sh\nprintf "uv %s\\n" "$*" >> "$ACTIONS"\n')
    env = os.environ.copy()
    env.update({"PATH": f"{fake_bin}{os.pathsep}{env['PATH']}", "ACTIONS": str(actions)})

    result = _run_bash(INSTALLER, "--no-setup", env=env)

    assert result.returncode == 0, result.stderr
    assert actions.read_text(encoding="utf-8").splitlines() == [
        f"uv tool install --force {ROOT}",
    ]


def test_upgrade_keeps_old_cli_when_monitor_removal_fails(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv_log = tmp_path / "uv.log"
    _write_executable(fake_bin / "uname", '#!/bin/sh\nprintf "Darwin\\n"\n')
    _write_executable(
        fake_bin / "ccs",
        '#!/bin/sh\n'
        'if [ "$1" = --version ]; then printf "ccs 0.6.0\\n"; exit 0; fi\n'
        'if [ "$1" = service ] && [ "$2" = uninstall ]; then exit 9; fi\n'
        'exit 0\n',
    )
    _write_executable(fake_bin / "uv", '#!/bin/sh\nprintf "called\\n" > "$UV_LOG"\n')
    env = os.environ.copy()
    env.update({"PATH": f"{fake_bin}{os.pathsep}{env['PATH']}", "UV_LOG": str(uv_log)})

    result = _run_bash(INSTALLER, "--no-setup", env=env)

    assert result.returncode == 9
    assert not uv_log.exists()


def test_installer_reports_missing_uv_before_installing(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PATH"] = str(tmp_path)

    result = _run_bash(INSTALLER, "--no-setup", env=env)

    assert result.returncode == 1
    assert "requires uv" in result.stderr
    assert "Install uv first" in result.stderr


def _standalone_fixture(
    tmp_path: Path, *, bad_checksum: bool = False
) -> tuple[Path, dict[str, str], Path, Path]:
    version = _project_version()
    asset_dir = tmp_path / "downloaded"
    asset_dir.mkdir()
    installer = asset_dir / "install.sh"
    installer.write_text(
        INSTALLER.read_text(encoding="utf-8").replace(VERSION_MARKER, version),
        encoding="utf-8",
    )
    installer.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv_log = tmp_path / "uv.log"
    download_log = tmp_path / "downloads.log"
    wheel = tmp_path / "fixture.whl"
    wheel.write_bytes(b"isolated fake wheel payload")
    wheel_name = f"cc_swaper-{version}-py3-none-any.whl"
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if bad_checksum:
        digest = "0" * 64
    sums = tmp_path / "SHA256SUMS.fixture"
    sums.write_text(f"{digest}  {wheel_name}\n", encoding="utf-8")

    _write_executable(
        fake_bin / "uv",
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$UV_LOG"\n',
    )
    _write_executable(fake_bin / "ccs", '#!/bin/sh\nprintf "ccs 0.7.0\\n"\n')
    _write_executable(
        fake_bin / "curl",
        "#!/bin/sh\n"
        "destination=\nurl=\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    -o) destination=$2; shift 2 ;;\n"
        "    *) url=$1; shift ;;\n"
        "  esac\n"
        "done\n"
        "printf '%s\\n' \"$url\" >> \"$DOWNLOAD_LOG\"\n"
        "case \"$url\" in\n"
        "  */SHA256SUMS) cp \"$SUMS_FIXTURE\" \"$destination\" ;;\n"
        "  *) cp \"$WHEEL_FIXTURE\" \"$destination\" ;;\n"
        "esac\n",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "UV_LOG": str(uv_log),
            "DOWNLOAD_LOG": str(download_log),
            "WHEEL_FIXTURE": str(wheel),
            "SUMS_FIXTURE": str(sums),
        }
    )
    return installer, env, uv_log, download_log


def test_standalone_installer_downloads_and_verifies_release_wheel(
    tmp_path: Path,
) -> None:
    installer, env, uv_log, download_log = _standalone_fixture(tmp_path)

    result = _run_bash(installer, "--no-setup", env=env)

    assert result.returncode == 0, result.stderr
    version = _project_version()
    wheel_name = f"cc_swaper-{version}-py3-none-any.whl"
    assert uv_log.read_text(encoding="utf-8").splitlines()[:3] == ["tool", "install", "--force"]
    assert uv_log.read_text(encoding="utf-8").splitlines()[-1].endswith(wheel_name)
    downloads = download_log.read_text(encoding="utf-8").splitlines()
    assert downloads == [
        f"https://github.com/nam-sequence/cc-swaper/releases/download/v{version}/{wheel_name}",
        f"https://github.com/nam-sequence/cc-swaper/releases/download/v{version}/SHA256SUMS",
    ]


def test_standalone_installer_rejects_bad_wheel_checksum(tmp_path: Path) -> None:
    installer, env, uv_log, _download_log = _standalone_fixture(tmp_path, bad_checksum=True)

    result = _run_bash(installer, "--no-setup", env=env)

    assert result.returncode == 1
    assert "SHA-256 verification failed" in result.stderr
    assert not uv_log.exists()


def _copy_minimal_builder_repo(tmp_path: Path, *, init_version: str = "0.8.1") -> Path:
    project_dir = tmp_path / "repo"
    (project_dir / "scripts").mkdir(parents=True)
    (project_dir / "src" / "cc_swaper").mkdir(parents=True)
    (project_dir / "pyproject.toml").write_text(
        '[project]\nname = "cc-swaper"\nversion = "0.8.1"\n',
        encoding="utf-8",
    )
    (project_dir / "src" / "cc_swaper" / "__init__.py").write_text(
        f'__version__ = "{init_version}"\n', encoding="utf-8"
    )
    shutil.copy2(INSTALLER, project_dir / "scripts" / "install.sh")
    shutil.copy2(BUILDER, project_dir / "scripts" / "build-release.sh")
    return project_dir


def _fake_builder_bin(tmp_path: Path, *, build_fails: bool = False) -> Path:
    fake_bin = tmp_path / "fake-python-bin"
    fake_bin.mkdir()
    real_python = shutil.which("python3")
    assert real_python is not None
    build_action = "exit 9" if build_fails else (
        'outdir=\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = --out-dir ]; then outdir=$2; shift 2; else shift; fi\n'
        'done\n'
        'printf "fake wheel\\n" > "$outdir/cc_swaper-0.8.1-py3-none-any.whl"\n'
        'printf "fake source\\n" > "$outdir/cc_swaper-0.8.1.tar.gz"\n'
    )
    _write_executable(
        fake_bin / "python3",
        "#!/bin/sh\n"
        'exec "$REAL_PYTHON" "$@"\n',
    )
    _write_executable(
        fake_bin / "uv",
        "#!/bin/sh\n"
        'if [ "$1" != build ]; then exit 2; fi\n'
        "shift\n"
        f"{build_action}\n",
    )
    return fake_bin


def test_release_builder_stages_only_fresh_versioned_assets(tmp_path: Path) -> None:
    project_dir = _copy_minimal_builder_repo(tmp_path)
    stale_root_asset = project_dir / "dist" / "cc_swaper-0.1.0-py3-none-any.whl"
    stale_root_asset.parent.mkdir()
    stale_root_asset.write_text("stale", encoding="utf-8")
    fake_bin = _fake_builder_bin(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "REAL_PYTHON": shutil.which("python3") or "python3",
        }
    )

    result = _run_bash(project_dir / "scripts" / "build-release.sh", env=env)

    assert result.returncode == 0, result.stderr
    output_dir = project_dir / "dist" / "release-v0.8.1"
    assert {path.name for path in output_dir.iterdir()} == {
        "cc_swaper-0.8.1-py3-none-any.whl",
        "cc_swaper-0.8.1.tar.gz",
        "install.sh",
        "SHA256SUMS",
    }
    generated_installer = (output_dir / "install.sh").read_text(encoding="utf-8")
    assert "release_version='0.8.1'" in generated_installer
    assert VERSION_MARKER not in generated_installer
    assert stale_root_asset.read_text(encoding="utf-8") == "stale"
    sums = (output_dir / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert {line.split(maxsplit=1)[1].lstrip("*") for line in sums} == {
        "cc_swaper-0.8.1-py3-none-any.whl",
        "cc_swaper-0.8.1.tar.gz",
        "install.sh",
    }
    for line in sums:
        digest, asset = line.split(maxsplit=1)
        asset = asset.lstrip("*")
        assert hashlib.sha256((output_dir / asset).read_bytes()).hexdigest() == digest
    assert "Release assets (v0.8.1)" in result.stdout
    assert "stale" not in result.stdout


def test_release_builder_rejects_version_mismatch_without_output(tmp_path: Path) -> None:
    project_dir = _copy_minimal_builder_repo(tmp_path, init_version="0.8.2")
    fake_bin = _fake_builder_bin(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "REAL_PYTHON": shutil.which("python3") or "python3",
        }
    )

    result = _run_bash(project_dir / "scripts" / "build-release.sh", env=env)

    assert result.returncode == 1
    assert "Version mismatch" in result.stderr
    assert not (project_dir / "dist").exists()


def test_release_builder_surfaces_uv_build_failure(tmp_path: Path) -> None:
    project_dir = _copy_minimal_builder_repo(tmp_path)
    fake_bin = _fake_builder_bin(tmp_path, build_fails=True)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "REAL_PYTHON": shutil.which("python3") or "python3",
        }
    )

    result = _run_bash(project_dir / "scripts" / "build-release.sh", env=env)

    assert result.returncode == 1
    assert "uv build failed" in result.stderr
    assert list((project_dir / "dist").iterdir()) == []


def test_release_builder_preserves_an_existing_release_directory(tmp_path: Path) -> None:
    project_dir = _copy_minimal_builder_repo(tmp_path)
    output_dir = project_dir / "dist" / "release-v0.8.1"
    output_dir.mkdir(parents=True)
    user_file = output_dir / "notes.txt"
    user_file.write_text("keep this file", encoding="utf-8")
    fake_bin = _fake_builder_bin(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "REAL_PYTHON": shutil.which("python3") or "python3",
        }
    )

    result = _run_bash(project_dir / "scripts" / "build-release.sh", env=env)

    assert result.returncode == 1
    assert "Release output already exists" in result.stderr
    assert user_file.read_text(encoding="utf-8") == "keep this file"
    assert {path.name for path in (project_dir / "dist").iterdir()} == {"release-v0.8.1"}
