from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path

import pytest

import cc_swaper.profiles as profiles_module
from cc_swaper.profiles import ProfileStore


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_default_and_managed_profiles_are_private_and_record_projects_anchor(
    tmp_path: Path,
) -> None:
    store = ProfileStore(tmp_path / "store")
    default = store.add_default("personal")
    managed = store.add_managed("work")

    fake_home = Path.home()
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
    assert (managed_dir / "projects").is_symlink()
    assert os.readlink(managed_dir / "projects") == str(shared_projects)
    assert shared_projects.is_dir()

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


@pytest.mark.parametrize("purge_data", [False, True])
def test_remove_managed_archive_and_purge_keep_shared_projects(
    tmp_path: Path, purge_data: bool,
) -> None:
    home = tmp_path / "store"
    store = ProfileStore(home)
    store.add_default("personal")
    managed = store.add_managed("work")
    assert managed.config_dir is not None
    shared_marker = store.shared_projects / "shared-history-marker.txt"
    shared_marker.write_text("shared", encoding="utf-8")
    archive = home / "removed"
    archive.mkdir(mode=0o700)
    destination = archive / "work-20260923T000000Z-a1b2c3d4"

    removed_path = store.remove_managed(
        "work", archive_to=destination, purge_data=purge_data
    )

    assert removed_path == destination
    assert shared_marker.read_text(encoding="utf-8") == "shared"
    if purge_data:
        assert destination.is_dir()
        shutil.rmtree(destination)
        store.finish_purge(destination)
    else:
        assert (destination / "projects").is_symlink()
        assert os.readlink(destination / "projects") == str(store.shared_projects)
    assert shared_marker.read_text(encoding="utf-8") == "shared"


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
    run_id = "a1b2c3d4e5f6"
    conversation_id = "550e8400-e29b-41d4-a716-446655440000"
    store.set_background_session(run_id, cwd, conversation_id, "work", transcript)
    original_sessions = json.loads((home / "sessions.json").read_text())["sessions"]
    original_background = store.background_sessions()
    archive = home / "removed"
    archive.mkdir(mode=0o700)
    destination = archive / "work-20260923T000000Z-a1b2c3d4"
    journal = home / "pending-removal.json"
    journal.write_text(json.dumps({
        "version": 1, "name": "work", "source": str(managed.config_dir),
        "destination": str(destination), "sessions_existed": True,
        "sessions_before": original_sessions,
        "background_existed": True, "background_before": original_background,
    }))
    journal.chmod(0o600)
    managed.config_dir.rename(destination)
    other_cwd = tmp_path / "other-project"
    other_cwd.mkdir()
    other_id = "550e8400-e29b-41d4-a716-446655440001"
    other_record = {"id": other_id, "profile": "personal", "path": str(tmp_path / "other.jsonl")}
    (home / "sessions.json").write_text(json.dumps({
        "version": 1, "sessions": {str(other_cwd): other_record},
    }))
    other_run = {
        "cwd": str(other_cwd), "id": other_id, "profile": "personal",
        "transcript_profile": "personal", "path": str(tmp_path / "other.jsonl"),
    }
    (home / "background-sessions.json").write_text(json.dumps({
        "version": 1, "runs": {"b1c2d3e4f5a6": other_run},
    }))

    reopened = ProfileStore(home)
    assert reopened.get("work").config_dir == managed.config_dir
    assert reopened.last_session(cwd) == "session-1"
    assert reopened.last_transcript(cwd) == ("work", transcript)
    assert reopened.background_session(run_id) == original_background[run_id]
    assert reopened.last_transcript(other_cwd) == ("personal", Path(other_record["path"]))
    assert reopened.background_session("b1c2d3e4f5a6") == other_run


def test_pending_purge_is_completed_on_next_open(tmp_path: Path) -> None:
    home = tmp_path / "store"
    store = ProfileStore(home)
    store.add_default("personal")
    managed = store.add_managed("work")
    assert managed.config_dir is not None
    (managed.config_dir / "old-history.txt").write_text("private")
    shared_marker = store.shared_projects / "shared-history-marker.txt"
    shared_marker.write_text("shared", encoding="utf-8")
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
    assert shared_marker.read_text(encoding="utf-8") == "shared"


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


def test_background_session_records_are_independent_in_one_project(tmp_path: Path) -> None:
    home = tmp_path / "store"
    store = ProfileStore(home)
    store.add_default("main")
    cwd = tmp_path / "project"
    cwd.mkdir()
    first_id = "550e8400-e29b-41d4-a716-446655440000"
    second_id = "550e8400-e29b-41d4-a716-446655440001"
    first_path = tmp_path / f"{first_id}.jsonl"
    second_path = tmp_path / f"{second_id}.jsonl"
    store.set_background_session("a1b2c3d4e5f6", cwd, first_id, "main", first_path)
    another = ProfileStore(home)
    another.set_background_session("b1c2d3e4f5a6", cwd, second_id, "main", second_path)

    records = ProfileStore(home).background_sessions()
    assert set(records) == {"a1b2c3d4e5f6", "b1c2d3e4f5a6"}
    assert records["a1b2c3d4e5f6"]["id"] == first_id
    assert records["b1c2d3e4f5a6"]["path"] == str(second_path)
    assert _mode(home / "background-sessions.json") == 0o600

    store.remove_background_session("a1b2c3d4e5f6")
    assert set(ProfileStore(home).background_sessions()) == {"b1c2d3e4f5a6"}


def test_removing_transcript_owner_prunes_failed_cross_profile_run(tmp_path: Path) -> None:
    home = tmp_path / "store"
    store = ProfileStore(home)
    store.add_default("main")
    source = store.add_managed("source")
    store.add_managed("target")
    assert source.config_dir is not None
    cwd = tmp_path / "project"
    cwd.mkdir()
    session_id = "550e8400-e29b-41d4-a716-446655440000"
    transcript = source.config_dir / "projects" / "project" / f"{session_id}.jsonl"
    store.set_background_session(
        "a1b2c3d4e5f6", cwd, session_id, "target", transcript,
        transcript_profile_name="source",
    )
    archive = home / "removed"
    archive.mkdir(mode=0o700)
    store.remove_managed(
        "source", archive_to=archive / "source-20260923T000000Z-a1b2c3d4"
    )
    assert ProfileStore(home).background_session("a1b2c3d4e5f6") is None


def test_no_archive_removal_restores_run_records_after_precommit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "store"
    store = ProfileStore(home)
    store.add_default("main")
    store.add_managed("second")
    cwd = tmp_path / "project"
    cwd.mkdir()
    session_id = "550e8400-e29b-41d4-a716-446655440000"
    transcript = tmp_path / f"{session_id}.jsonl"
    store.set_last_session(cwd, session_id, profile_name="second", transcript_path=transcript)
    store.set_background_session("a1b2c3d4e5f6", cwd, session_id, "second", transcript)

    def fail_before_commit(_next_state):
        raise OSError("state write failed")

    monkeypatch.setattr(store, "_write_state", fail_before_commit)
    with pytest.raises(OSError, match="state write failed"):
        store.remove_managed("second")
    reopened = ProfileStore(home)
    assert reopened.get("second").config_dir is not None
    assert reopened.last_session(cwd) == session_id
    assert reopened.background_session("a1b2c3d4e5f6") is not None
    assert not (home / "pending-removal.json").exists()


def test_no_archive_removal_recovers_after_committed_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "store"
    store = ProfileStore(home)
    store.add_default("main")
    managed = store.add_managed("second")
    cwd = tmp_path / "project"
    cwd.mkdir()
    session_id = "550e8400-e29b-41d4-a716-446655440000"
    transcript = tmp_path / f"{session_id}.jsonl"
    store.set_last_session(cwd, session_id, profile_name="second", transcript_path=transcript)
    store.set_background_session("a1b2c3d4e5f6", cwd, session_id, "second", transcript)
    original_write = store._write_state

    def fail_after_commit(next_state):
        original_write(next_state)
        raise OSError("post-commit fsync failed")

    monkeypatch.setattr(store, "_write_state", fail_after_commit)
    with pytest.raises(OSError, match="post-commit fsync failed"):
        store.remove_managed("second")
    assert (home / "pending-removal.json").exists()
    reopened = ProfileStore(home)
    with pytest.raises(KeyError):
        reopened.get("second")
    assert managed.config_dir is not None and managed.config_dir.is_dir()
    assert reopened.last_session(cwd) is None
    assert reopened.background_session("a1b2c3d4e5f6") is None
    assert not (home / "pending-removal.json").exists()


def test_delayed_selection_preserves_newer_choice_even_after_aba(tmp_path: Path) -> None:
    home = tmp_path / "store"
    store = ProfileStore(home)
    store.add_default("main")
    store.add_managed("second")
    store.add_managed("third")
    observed = store.selection_snapshot()

    another = ProfileStore(home)
    another.select("third")
    another.select("main")
    assert store.select_if_unchanged(observed, "second") is False
    assert ProfileStore(home).selected().name == "main"

    current = store.selection_snapshot()
    assert store.select_if_unchanged(current, "second") is True
    assert ProfileStore(home).selected().name == "second"


def test_delayed_selection_detects_older_writer_without_revision(tmp_path: Path) -> None:
    home = tmp_path / "store"
    store = ProfileStore(home)
    store.add_default("main")
    store.add_managed("second")
    state_file = home / "profiles.json"
    legacy = json.loads(state_file.read_text())
    legacy.pop("selection_revision")
    state_file.write_text(json.dumps(legacy))

    observed = store.selection_snapshot()
    assert "selection_revision" in json.loads(state_file.read_text())
    # An older running CLI writes a state file without the new field.
    state_file.write_text(json.dumps(legacy))
    assert store.select_if_unchanged(observed, "second") is False
    # A newer CLI then adds an unrelated profile, restoring the field at its
    # legacy default. The old token still must not become valid again.
    ProfileStore(home).add_managed("third")
    assert "selection_revision" in json.loads(state_file.read_text())
    assert store.select_if_unchanged(observed, "second") is False
    assert ProfileStore(home).selected().name == "main"


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


def test_concurrent_background_updates_keep_same_project_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("cross-process flock regression requires the fork start method")
    home = tmp_path / "store"
    ProfileStore(home).add_default("main")
    cwd = tmp_path / "project"
    cwd.mkdir()
    run_ids = ["a1b2c3d4e5f6", "b1c2d3e4f5a6"]
    session_ids = [
        "550e8400-e29b-41d4-a716-446655440000",
        "550e8400-e29b-41d4-a716-446655440001",
    ]
    original_write = profiles_module._write_json_atomic

    def delayed_write(path: Path, payload: object) -> None:
        if path.name == "background-sessions.json":
            time.sleep(0.05)
        original_write(path, payload)

    monkeypatch.setattr(profiles_module, "_write_json_atomic", delayed_write)
    context = multiprocessing.get_context("fork")
    start = context.Barrier(2)

    def update(index: int) -> None:
        store = ProfileStore(home)
        start.wait(timeout=5)
        store.set_background_session(run_ids[index], cwd, session_ids[index], "main")

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
    records = ProfileStore(home).background_sessions()
    assert set(records) == set(run_ids)
    assert {record["id"] for record in records.values()} == set(session_ids)


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
    (managed.config_dir / "projects").unlink()
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
    projects_link = managed.config_dir / "projects"
    projects_link.unlink()
    projects_link.symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="managed projects"):
        ProfileStore(store_home)


def test_managed_profile_with_legacy_regular_projects_directory_is_preserved(
    tmp_path: Path,
) -> None:
    store_home = tmp_path / "store"
    store = ProfileStore(store_home)
    store.add_default("personal")
    managed = store.add_managed("work")
    assert managed.config_dir is not None
    projects_path = managed.config_dir / "projects"
    projects_path.unlink()
    projects_path.mkdir()
    marker = projects_path / "legacy-history-marker.txt"
    marker.write_text("keep", encoding="utf-8")

    reopened = ProfileStore(store_home)

    assert reopened.get("work").config_dir == managed.config_dir
    assert projects_path.is_dir()
    assert not projects_path.is_symlink()
    assert marker.read_text(encoding="utf-8") == "keep"


def test_managed_projects_dangling_shared_link_is_rejected_on_reopen(
    tmp_path: Path,
) -> None:
    store_home = tmp_path / "store"
    store = ProfileStore(store_home)
    store.add_default("personal")
    managed = store.add_managed("work")
    assert managed.config_dir is not None
    projects_link = managed.config_dir / "projects"
    projects_link.unlink()
    store.shared_projects.rmdir()
    projects_link.symlink_to(store.shared_projects, target_is_directory=True)

    with pytest.raises(ValueError, match="unsafe shared projects directory"):
        ProfileStore(store_home)


@pytest.mark.parametrize("unsafe_component", ["parent", "projects"])
def test_managed_projects_link_rejects_group_or_other_writable_target(
    tmp_path: Path, unsafe_component: str,
) -> None:
    store_home = tmp_path / "store"
    store = ProfileStore(store_home)
    store.add_default("personal")
    managed = store.add_managed("work")
    assert managed.config_dir is not None
    unsafe_path = (
        store.shared_projects.parent
        if unsafe_component == "parent"
        else store.shared_projects
    )
    unsafe_path.chmod(_mode(unsafe_path) | 0o022)

    with pytest.raises(ValueError, match="unsafe shared projects directory"):
        ProfileStore(store_home)


def test_managed_projects_link_rejects_writable_home_ancestor(
    tmp_path: Path,
) -> None:
    store_home = tmp_path / "store"
    store = ProfileStore(store_home)
    store.add_default("personal")
    store.add_managed("work")
    Path.home().chmod(_mode(Path.home()) | 0o022)

    with pytest.raises(ValueError, match="unsafe shared projects ancestor"):
        ProfileStore(store_home)


def test_managed_projects_link_allows_private_home_under_root_owned_sticky_tmp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_root = Path("/tmp")
    info = tmp_root.stat()
    if info.st_uid != 0 or not stat.S_IMODE(info.st_mode) & stat.S_ISVTX:
        pytest.skip("/tmp is not a root-owned sticky directory")
    with tempfile.TemporaryDirectory(dir=tmp_root, prefix="ccs-profile-test-") as raw_home:
        monkeypatch.setenv("HOME", raw_home)
        store = ProfileStore(Path(raw_home) / "store")
        store.add_default("personal")
        managed = store.add_managed("work")
        assert managed.config_dir is not None
        assert (managed.config_dir / "projects").is_symlink()


def test_add_managed_write_failure_removes_new_link_and_empty_profile_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ProfileStore(tmp_path / "store")
    store.add_default("personal")

    def fail_write(_state: object) -> None:
        raise OSError("simulated state write failure")

    monkeypatch.setattr(store, "_write_state", fail_write)
    with pytest.raises(OSError, match="simulated state write failure"):
        store.add_managed("work")

    profile_dir = store.profiles_dir / "work"
    assert not profile_dir.exists()
    assert not profile_dir.is_symlink()
    assert store.shared_projects.is_dir()
    assert [profile.name for profile in ProfileStore(store.home).all()] == ["personal"]


def test_add_managed_post_commit_error_keeps_registered_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ProfileStore(tmp_path / "store")
    store.add_default("personal")
    original_write = store._write_state

    def fail_after_commit(next_state: object) -> None:
        original_write(next_state)
        raise OSError("post-commit fsync failed")

    monkeypatch.setattr(store, "_write_state", fail_after_commit)
    with pytest.raises(OSError, match="post-commit fsync failed"):
        store.add_managed("work")

    reopened = ProfileStore(store.home)
    managed = reopened.get("work")
    assert managed.config_dir is not None
    projects_link = managed.config_dir / "projects"
    assert projects_link.is_symlink()
    assert os.readlink(projects_link) == str(reopened.shared_projects)


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
