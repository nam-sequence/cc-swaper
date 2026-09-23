from __future__ import annotations

import json
import multiprocessing
import stat
import time
from pathlib import Path

import pytest

import cc_swaper.profiles as profiles_module
from cc_swaper.profiles import ProfileStore


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_default_and_managed_profiles_are_private_and_record_projects_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "user-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    store = ProfileStore(tmp_path / "store")
    default = store.add_default("personal")
    managed = store.add_managed("work")

    shared_projects = fake_home / ".claude" / "projects"
    assert store.shared_projects == shared_projects.absolute()
    assert default.config_dir is None
    assert managed.config_dir == tmp_path / "store" / "profiles" / "work"
    assert store.all() == [default, managed]
    assert store.selected() == default

    default_dir = tmp_path / "store" / "profiles" / "personal"
    managed_dir = tmp_path / "store" / "profiles" / "work"
    assert _mode(default_dir) == 0o700
    assert _mode(managed_dir) == 0o700
    assert not (default_dir / "projects").exists()
    assert not (managed_dir / "projects").exists()

    assert not (tmp_path / "store" / "profiles" / "work" / "credentials.json").exists()


def test_default_must_be_added_first_and_selection_persists(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "store")

    with pytest.raises(ValueError, match="add_default"):
        store.add_managed("work")

    store.add_default("personal")
    store.add_managed("work")
    selected = store.select("work")
    assert selected.name == "work"
    assert store.selected() == selected

    reopened = ProfileStore(tmp_path / "store")
    assert [profile.name for profile in reopened.all()] == ["personal", "work"]
    assert reopened.selected().name == "work"


def test_remove_managed_profile_selects_first_remaining_and_preserves_directory(
    tmp_path: Path,
) -> None:
    store = ProfileStore(tmp_path / "store")
    store.add_default("personal")
    managed = store.add_managed("work")
    store.add_managed("backup")
    assert managed.config_dir is not None
    marker = managed.config_dir / "credentials.json"
    marker.write_text("keep", encoding="utf-8")
    store.select("work")

    removed_path = store.remove_managed("work")

    assert removed_path == managed.config_dir
    assert marker.read_text(encoding="utf-8") == "keep"
    assert removed_path.is_dir()
    assert [profile.name for profile in store.all()] == ["personal", "backup"]
    assert store.selected().name == "personal"


def test_remove_managed_cleans_session_metadata_for_that_profile(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "store")
    store.add_default("personal")
    store.add_managed("work")
    cwd_work = tmp_path / "work-project"
    cwd_personal = tmp_path / "personal-project"
    cwd_plain = tmp_path / "plain-project"
    for cwd in (cwd_work, cwd_personal, cwd_plain):
        cwd.mkdir()
    work_transcript = tmp_path / "work.jsonl"
    personal_transcript = tmp_path / "personal.jsonl"
    work_transcript.touch()
    personal_transcript.touch()
    store.set_last_session(
        cwd_work,
        "work-session",
        profile_name="work",
        transcript_path=work_transcript,
    )
    store.set_last_session(
        cwd_personal,
        "personal-session",
        profile_name="personal",
        transcript_path=personal_transcript,
    )
    store.set_last_session(cwd_plain, "plain-session")

    store.remove_managed("work")

    reopened = ProfileStore(tmp_path / "store")
    assert reopened.last_session(cwd_work) is None
    assert reopened.last_transcript(cwd_personal) == ("personal", personal_transcript)
    assert reopened.last_session(cwd_plain) == "plain-session"


def test_remove_managed_rejects_default_profile(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "store")
    store.add_default("personal")
    store.add_managed("work")

    with pytest.raises(ValueError, match="default profile"):
        store.remove_managed("personal")

    assert [profile.name for profile in store.all()] == ["personal", "work"]
    assert store.selected().name == "personal"


def test_remove_archive_rename_failure_keeps_profile_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "store"
    store = ProfileStore(home)
    store.add_default("personal")
    managed = store.add_managed("work")
    assert managed.config_dir is not None
    archive = home / "removed"
    archive.mkdir(mode=0o700)
    destination = archive / "work-20260923T000000Z-a1b2c3d4"
    original_rename = Path.rename

    def fail_move(path: Path, target: Path) -> Path:
        if path == managed.config_dir:
            raise OSError("simulated rename failure")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_move)
    with pytest.raises(OSError, match="simulated rename failure"):
        store.remove_managed("work", archive_to=destination)
    monkeypatch.setattr(Path, "rename", original_rename)

    reopened = ProfileStore(home)
    assert reopened.get("work").config_dir == managed.config_dir
    assert managed.config_dir.is_dir()
    assert not destination.exists()
    assert not (home / "pending-removal.json").exists()


def test_pending_archive_move_recovers_on_next_open(tmp_path: Path) -> None:
    home = tmp_path / "store"
    store = ProfileStore(home)
    store.add_default("personal")
    managed = store.add_managed("work")
    assert managed.config_dir is not None
    archive = home / "removed"
    archive.mkdir(mode=0o700)
    destination = archive / "work-20260923T000000Z-a1b2c3d4"
    journal = home / "pending-removal.json"
    journal.write_text(json.dumps({
        "version": 1, "name": "work", "source": str(managed.config_dir),
        "destination": str(destination),
    }))
    journal.chmod(0o600)
    managed.config_dir.rename(destination)

    reopened = ProfileStore(home)
    assert reopened.get("work").config_dir == managed.config_dir
    assert managed.config_dir.is_dir()
    assert not destination.exists()
    assert not journal.exists()


def test_pending_removal_restores_sessions_if_registry_was_not_committed(tmp_path: Path) -> None:
    home = tmp_path / "store"
    store = ProfileStore(home)
    store.add_default("personal")
    managed = store.add_managed("work")
    assert managed.config_dir is not None
    cwd = tmp_path / "project"
    cwd.mkdir()
    transcript = managed.config_dir / "projects" / "project" / "session-1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.touch()
    store.set_last_session(
        cwd, "session-1", profile_name="work", transcript_path=transcript
    )
    original_sessions = json.loads((home / "sessions.json").read_text())["sessions"]
    archive = home / "removed"
    archive.mkdir(mode=0o700)
    destination = archive / "work-20260923T000000Z-a1b2c3d4"
    journal = home / "pending-removal.json"
    journal.write_text(json.dumps({
        "version": 1, "name": "work", "source": str(managed.config_dir),
        "destination": str(destination), "sessions_existed": True,
        "sessions_before": original_sessions,
    }))
    journal.chmod(0o600)
    managed.config_dir.rename(destination)
    (home / "sessions.json").write_text(json.dumps({"version": 1, "sessions": {}}))

    reopened = ProfileStore(home)
    assert reopened.get("work").config_dir == managed.config_dir
    assert reopened.last_session(cwd) == "session-1"
    assert reopened.last_transcript(cwd) == ("work", transcript)


def test_pending_purge_is_completed_on_next_open(tmp_path: Path) -> None:
    home = tmp_path / "store"
    store = ProfileStore(home)
    store.add_default("personal")
    managed = store.add_managed("work")
    assert managed.config_dir is not None
    (managed.config_dir / "old-history.txt").write_text("private")
    archive = home / "removed"
    archive.mkdir(mode=0o700)
    destination = archive / "work-20260923T000000Z-a1b2c3d4"

    archived = store.remove_managed("work", archive_to=destination, purge_data=True)
    assert archived == destination
    assert destination.is_dir()
    assert (home / "pending-removal.json").is_file()

    reopened = ProfileStore(home)
    assert [profile.name for profile in reopened.all()] == ["personal"]
    assert not destination.exists()
    assert not (home / "pending-removal.json").exists()


def test_session_metadata_is_global_and_atomic(tmp_path: Path) -> None:
    store_home = tmp_path / "store"
    store = ProfileStore(store_home)
    cwd = tmp_path / "project"
    cwd.mkdir()

    assert store.last_session(cwd) is None
    store.set_last_session(cwd, "session-123")
    assert store.last_session(cwd) == "session-123"
    assert store.last_session(Path(str(cwd) + "/..") / cwd.name) == "session-123"

    sessions_file = store_home / "sessions.json"
    assert _mode(sessions_file) == 0o600
    payload = json.loads(sessions_file.read_text(encoding="utf-8"))
    assert payload["sessions"][str(cwd)] == "session-123"

    reopened = ProfileStore(store_home)
    assert reopened.last_session(cwd) == "session-123"

    store.add_default("personal")
    transcript = tmp_path / "session-456.jsonl"
    transcript.touch()
    store.set_last_session(
        cwd, "session-456", profile_name="personal", transcript_path=transcript
    )
    reopened = ProfileStore(store_home)
    assert reopened.last_session(cwd) == "session-456"
    assert reopened.last_transcript(cwd) == ("personal", transcript)


def test_concurrent_session_updates_keep_different_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two writers must merge session records under the store-wide lock."""

    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("cross-process flock regression requires the fork start method")

    store_home = tmp_path / "store"
    initial = ProfileStore(store_home)
    initial.add_default("personal")
    projects = [tmp_path / "project-a", tmp_path / "project-b"]
    for project in projects:
        project.mkdir()

    original_write = profiles_module._write_json_atomic

    def delayed_write(path: Path, payload: object) -> None:
        if path.name == "sessions.json":
            # Without the metadata lock both processes read the same old map;
            # this pause makes the lost-update window deterministic.
            time.sleep(0.05)
        original_write(path, payload)

    monkeypatch.setattr(profiles_module, "_write_json_atomic", delayed_write)

    context = multiprocessing.get_context("fork")
    start = context.Barrier(2)

    def update(index: int) -> None:
        store = ProfileStore(store_home)
        start.wait(timeout=5)
        store.set_last_session(projects[index], f"session-{index}")

    workers = [context.Process(target=update, args=(index,)) for index in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    for worker in workers:
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)

    assert [worker.exitcode for worker in workers] == [0, 0]

    records = json.loads((store_home / "sessions.json").read_text(encoding="utf-8"))["sessions"]
    assert records == {
        str(projects[0].resolve()): "session-0",
        str(projects[1].resolve()): "session-1",
    }


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "../escape", "a/b", "a\\b", "/tmp/profile", "-leading"],
)
def test_unsafe_profile_names_are_rejected(tmp_path: Path, name: str) -> None:
    store = ProfileStore(tmp_path / "store")
    with pytest.raises(ValueError):
        store.add_default(name)
    assert not (tmp_path / "escape").exists()


def test_preexisting_profile_path_is_not_overwritten(tmp_path: Path) -> None:
    store_home = tmp_path / "store"
    profiles_dir = store_home / "profiles"
    profiles_dir.mkdir(parents=True)
    conflicting = profiles_dir / "work"
    conflicting.mkdir()
    marker = conflicting / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    store = ProfileStore(store_home)
    store.add_default("personal")
    with pytest.raises(FileExistsError):
        store.add_managed("work")
    assert marker.read_text(encoding="utf-8") == "keep"


def test_managed_profile_symlink_is_rejected_on_reopen(tmp_path: Path) -> None:
    store_home = tmp_path / "store"
    store = ProfileStore(store_home)
    store.add_default("personal")
    managed = store.add_managed("work")
    assert managed.config_dir is not None
    managed.config_dir.rmdir()
    external = tmp_path / "external"
    external.mkdir()
    managed.config_dir.symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="unsafe or missing"):
        ProfileStore(store_home)


def test_managed_projects_symlink_is_rejected_on_reopen(tmp_path: Path) -> None:
    store_home = tmp_path / "store"
    store = ProfileStore(store_home)
    store.add_default("personal")
    managed = store.add_managed("work")
    assert managed.config_dir is not None
    external = tmp_path / "external"
    external.mkdir()
    (managed.config_dir / "projects").symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="managed projects directory"):
        ProfileStore(store_home)


def test_managed_profile_with_public_permissions_is_rejected(tmp_path: Path) -> None:
    store_home = tmp_path / "store"
    store = ProfileStore(store_home)
    store.add_default("personal")
    managed = store.add_managed("work")
    assert managed.config_dir is not None
    managed.config_dir.chmod(0o755)

    with pytest.raises(ValueError, match="not private"):
        ProfileStore(store_home)


def test_metadata_files_are_private(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "store")
    store.add_default("personal")
    store.set_last_session(tmp_path, "s1")

    assert _mode(store.home) == 0o700
    assert _mode(store.profiles_dir) == 0o700
    assert _mode(store.home / "profiles.json") == 0o600
    assert _mode(store.home / "sessions.json") == 0o600
    assert _mode(store.home / ".metadata.lock") == 0o600


def test_permissive_metadata_lock_is_rejected(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "store")
    store.home.joinpath(".metadata.lock").chmod(0o644)

    with pytest.raises(ValueError, match="unsafe metadata lock file"):
        ProfileStore(store.home)


def test_default_home_uses_cc_swaper_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    configured_home = tmp_path / "configured-store"
    monkeypatch.setenv("CC_SWAPER_HOME", str(configured_home))

    store = ProfileStore()
    assert store.home == configured_home
