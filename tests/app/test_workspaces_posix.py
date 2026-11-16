"""The POSIX workspace rules: the path grammar, root keys, symlink and network refusals,
containment and the deny set. Every root is a synthetic folder in `tmp_path`; the deny probes
are keys only and touch no system folder."""

from __future__ import annotations

import ntpath
import os
import sys
import threading
import unicodedata
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from viki_agent import osfs, workspaces
from viki_agent.domain import CanonicalRoot
from viki_agent.state import AppState, Prefs
from viki_agent.workspaces import (
    WorkspaceError,
    WorkspaceService,
    canonical_root,
    denied_roots,
    refuse_denied,
    root_key,
)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX registration only")
macos_only = pytest.mark.skipif(sys.platform != "darwin", reason="APFS case and normalization")
linux_only = pytest.mark.skipif(sys.platform != "linux", reason="Linux mount and home layout")
# Only stops a test if the fix regresses and the read blocks on the FIFO again; a fixed read
# refuses a FIFO record at once, without waiting for a writer.
HANG_GUARD_S = 5

COMMAND = "11111111-1111-4111-8111-111111111111"
OTHER_COMMAND = "22222222-2222-4222-8222-222222222222"


def _run_without_blocking(call: Callable[[], object], fifo: Path) -> object:
    """Run `call` in a thread and return what it returns (or raises), failing the test if it is
    still blocked after HANG_GUARD_S. POSIX only: the release write below needs a FIFO."""
    assert sys.platform != "win32"
    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(call())
        except BaseException as exc:  # noqa: BLE001 - returned for the caller to assert on
            outcome.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(HANG_GUARD_S)
    if thread.is_alive():
        # Release the blocked reader before failing, so a failing run never leaves a hung thread.
        writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(writer)
        thread.join(HANG_GUARD_S)
        pytest.fail("the read blocked on the FIFO instead of refusing it at once")
    return outcome[0]


def service_at(tmp_path: Path) -> WorkspaceService:
    return WorkspaceService(
        AppState(tmp_path / "data" / "state.json", Prefs()).store,
        env={},
        data_dir=tmp_path / "data",
        home=tmp_path / "home",
        install_dir=tmp_path / "install",
        git_available=lambda: False,
        hide_previews=lambda: False,
    )


def refusal(raw: str) -> tuple[str, dict[str, object]]:
    with pytest.raises(WorkspaceError) as failure:
        canonical_root(raw, deny=())
    return failure.value.code, dict(failure.value.extra)


# --- every OS: the pure rules -------------------------------------------------------------


def test_posix_grammar_accepts_absolute_paths_and_refuses_the_rest() -> None:
    assert workspaces._syntax_ok_posix("/a/b")
    assert workspaces._syntax_ok_posix("/a/b/")  # resolve() drops a trailing separator
    assert workspaces._syntax_ok_posix("/" + "x" * 255)
    refused = (
        "a/b",
        "",
        "/a/b\x01c",
        "/a/b\x7f",
        "/a/b\x00",
        "/" + "x" * 256,
        "/" + "é" * 128,  # 128 characters, 256 bytes of UTF-8
        "/a/../b",
        "/a/./b",
        "/" + "/".join(["x" * 200] * 21),  # 4,221 bytes in all
        "/a/\udcff",  # a lone surrogate has no UTF-8 spelling
    )
    for raw in refused:
        assert not workspaces._syntax_ok_posix(raw), repr(raw)


def test_root_keys_per_platform() -> None:
    nfd, nfc = unicodedata.normalize("NFD", "/Users/me/Café"), "/Users/me/café"
    assert nfd != nfc
    assert workspaces._key_for("/Users/me/Notes", "darwin") == "/users/me/notes"
    assert workspaces._key_for(nfd, "darwin") == workspaces._key_for(nfc, "darwin")
    assert workspaces._key_for("/home/me/Notes", "linux") == "/home/me/Notes"
    assert workspaces._key_for("C:\\Vault", "win32") == ntpath.normcase("C:\\Vault")


# --- POSIX: registration on a real filesystem ---------------------------------------------


@posix_only
def test_a_plain_folder_registers(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    root = tmp_path / "vault"
    root.mkdir()
    workspace, created = service_at(tmp_path).register_existing(COMMAND, str(root) + "/")
    assert created is True and workspace.policy == "existing_readonly"
    assert workspace.root.display == str(root)
    assert workspace.root.key == root_key(str(root))
    assert workspace.root.aliased is False


@posix_only
def test_a_symlink_anywhere_in_the_typed_path_is_refused(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    real = tmp_path / "real"
    (real / "inner").mkdir(parents=True)
    (tmp_path / "linked").symlink_to(real, target_is_directory=True)
    assert refusal(str(tmp_path / "linked")) == ("unsafe_path", {})
    assert refusal(str(tmp_path / "linked" / "inner")) == ("unsafe_path", {})
    assert canonical_root(str(real / "inner"), deny=()).display == str(real / "inner")


@posix_only
def test_a_network_filesystem_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert sys.platform != "win32"
    monkeypatch.setattr(osfs, "filesystem_type", lambda path: "nfs4")
    assert refusal(str(tmp_path)) == ("unsupported_location", {"reason": "network"})

    def unreadable(path: str) -> str:
        raise OSError("mount table unavailable")

    monkeypatch.setattr(osfs, "filesystem_type", unreadable)
    assert refusal(str(tmp_path)) == ("unsupported_location", {"reason": "mount_point"})


@posix_only
def test_a_path_inside_a_registration_is_refused(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    parent = tmp_path / "vault"
    child = parent / "inner"
    child.mkdir(parents=True)
    service = service_at(tmp_path)
    registered, _ = service.register_existing(COMMAND, str(parent))
    for key in (registered.root.key, root_key(str(child)), root_key(str(child / "planned"))):
        with pytest.raises(WorkspaceError) as inside:
            service.refuse_inside_registration(key)
        assert inside.value.code == "overlaps_registration", key
    service.refuse_inside_registration(root_key(str(tmp_path / "vault-sibling")))
    with pytest.raises(WorkspaceError) as nested:
        service.register_existing(OTHER_COMMAND, str(child))
    assert nested.value.code == "overlaps_registration"


# --- POSIX: the deny set ------------------------------------------------------------------

# Paths that exist nowhere, so no probe below lies inside them and each entry refuses under its
# own category (a candidate that holds a denied root is refused by the first such entry).
HOME = Path("/viki-probe-home")
DATA = Path("/viki-probe-data")
INSTALL = Path("/viki-probe-install")
LISTS = [
    pytest.param(workspaces._LINUX_DENY, workspaces._LINUX_HOME_DENY, id="linux"),
    pytest.param(workspaces._DARWIN_DENY, workspaces._DARWIN_HOME_DENY, id="darwin"),
]


def denial(path: str | Path, deny: Sequence[CanonicalRoot]) -> tuple[str, dict[str, object]]:
    """How `refuse_denied` refuses `path`, keyed as a resolved candidate would be."""
    with pytest.raises(WorkspaceError) as failure:
        refuse_denied(root_key(os.path.realpath(path)), deny)
    return failure.value.code, dict(failure.value.extra)


@posix_only
@pytest.mark.parametrize(("fixed", "home_relative"), LISTS)
def test_each_fixed_location_is_refused_under_its_category(
    monkeypatch: pytest.MonkeyPatch,
    fixed: tuple[tuple[str, str, bool], ...],
    home_relative: tuple[tuple[str, str], ...],
) -> None:
    assert sys.platform != "win32"
    monkeypatch.setattr(workspaces, "SYSTEM_DENY", fixed)  # the full list, not the test one
    monkeypatch.setattr(workspaces, "_HOME_DENY", home_relative)
    deny = denied_roots({}, data_dir=DATA, home=HOME, install=INSTALL)
    for path, category, prefix in fixed:
        assert denial(path, deny) == ("denied_location", {"denied": category}), path
        below = f"{path}/viki-probe"
        if prefix:
            assert denial(below, deny) == ("denied_location", {"denied": category}), below
        else:  # a user's or a mounted volume's own folder stays registrable
            refuse_denied(root_key(os.path.realpath(below)), deny)
    for relative, category in home_relative:
        expected: tuple[str, dict[str, object]] = (
            ("unsupported_location", {"reason": "cloud_sync"})
            if category == "onedrive"
            else ("denied_location", {"denied": category})
        )
        assert denial(HOME / relative / "Notes", deny) == expected, relative
    assert denial("/", deny)[0] == "denied_location"
    assert denial(HOME, deny)[0] == "denied_location"
    refuse_denied(root_key(HOME / "VIKI"), deny)  # a folder under the home stays registrable


@posix_only
def test_tmpdir_from_the_environment_is_denied(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    (tmp_path / "vault").mkdir()
    deny = denied_roots({"TMPDIR": f"{tmp_path}/"}, data_dir=DATA, home=HOME, install=INSTALL)
    with pytest.raises(WorkspaceError) as failure:
        canonical_root(str(tmp_path / "vault"), deny=deny)
    assert (failure.value.code, failure.value.extra) == ("denied_location", {"denied": "temp"})


def linux_deny(monkeypatch: pytest.MonkeyPatch, home: Path = HOME) -> tuple[CanonicalRoot, ...]:
    """The deny set built from the full Linux lists, not the test one."""
    monkeypatch.setattr(workspaces, "SYSTEM_DENY", workspaces._LINUX_DENY)
    monkeypatch.setattr(workspaces, "_HOME_DENY", workspaces._LINUX_HOME_DENY)
    return denied_roots({}, data_dir=DATA, home=home, install=INSTALL)


@linux_only
def test_a_drive_mounted_under_run_media_is_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    assert sys.platform == "linux"
    refuse_denied(root_key("/run/media/viki-probe/USB"), linux_deny(monkeypatch))


@linux_only
def test_a_folder_in_a_home_under_var_home_is_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert sys.platform == "linux"
    home = Path("/var/home/viki-probe")
    refuse_denied(root_key(home / "Notes"), linux_deny(monkeypatch, home))


@linux_only
def test_run_and_var_still_refuse_outside_the_media_and_home_folders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert sys.platform == "linux"
    deny = linux_deny(monkeypatch)
    for path in ("/run/media", "/var/home", "/run/user/1000", "/var/lib/viki-probe"):
        assert denial(path, deny) == ("denied_location", {"denied": "system"}), path
    home = Path("/var/home/viki-probe")
    config = denial(home / ".config", linux_deny(monkeypatch, home))
    assert config == ("denied_location", {"denied": "system"})


@macos_only
def test_a_path_spelled_through_var_is_refused_by_its_private_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert sys.platform == "darwin"
    monkeypatch.setattr(workspaces, "SYSTEM_DENY", (("/var", "system", True),))
    deny = denied_roots({}, data_dir=DATA, home=HOME, install=INSTALL)
    assert root_key("/private/var") in {entry.key for entry in deny}
    for path in ("/var/viki-probe", "/private/var/viki-probe"):
        assert denial(path, deny) == ("denied_location", {"denied": "system"}), path


# --- macOS: one key for every spelling of one folder --------------------------------------


@macos_only
def test_case_and_normalization_variants_share_one_key(tmp_path: Path) -> None:
    assert sys.platform == "darwin"
    notes = tmp_path / "Notes"
    notes.mkdir()
    upper = canonical_root(str(notes), deny=())
    lower = canonical_root(str(tmp_path / "notes"), deny=())
    assert (lower.key, lower.physical_id) == (upper.key, upper.physical_id)
    assert lower.display == upper.display == str(notes)  # the spelling on disk

    on_disk = unicodedata.normalize("NFD", "café")
    (tmp_path / on_disk).mkdir()
    typed = canonical_root(str(tmp_path / unicodedata.normalize("NFC", "café")), deny=())
    stored = canonical_root(str(tmp_path / on_disk), deny=())
    assert typed.key == stored.key
    assert typed.display == stored.display == str(tmp_path / on_disk)


# --- POSIX: a FIFO in place of an instruction or settings file refuses instead of blocking ------


@posix_only
def test_measure_file_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    fifo = tmp_path / "CLAUDE.md"
    os.mkfifo(fifo)
    assert _run_without_blocking(lambda: workspaces._measure_file(fifo), fifo) is None


@posix_only
def test_has_hooks_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    (tmp_path / ".claude").mkdir()
    fifo = tmp_path / ".claude" / "settings.json"
    os.mkfifo(fifo)
    assert _run_without_blocking(lambda: WorkspaceService._has_hooks(tmp_path), fifo) is False


@posix_only
def test_head_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    fifo = tmp_path / "CLAUDE.md"
    os.mkfifo(fifo)
    assert _run_without_blocking(lambda: WorkspaceService._head(fifo), fifo) is None
