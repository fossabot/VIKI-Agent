"""CLI interplay across OSes: the transcript slug (catalog.project_dir) and the sessions
registry's cwd and liveness comparisons (session._cwd_key, vault_sessions, find_holder)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unicodedata
from pathlib import Path

import pytest

from viki_agent import catalog, config, session, winproc
from viki_agent.drivers.claude.control import Launch, build_argv

SID = "aaaaaaaa-1111-4111-8111-111111111111"


# --- _cwd_key: every OS -----------------------------------------------------------------------


def test_cwd_key_windows_lowercases_and_strips_trailing_separator() -> None:
    assert session._cwd_key("C:\\Vault\\", "win32") == "c:\\vault"
    assert session._cwd_key("C:\\VAULT", "win32") == session._cwd_key("c:\\vault", "win32")


def test_cwd_key_linux_compares_exactly() -> None:
    assert session._cwd_key("/Vault/", "linux") == "/Vault"
    assert session._cwd_key("/Vault", "linux") != session._cwd_key("/vault", "linux")


def test_cwd_key_macos_casefolds_and_nfc_normalizes() -> None:
    nfd = unicodedata.normalize("NFD", "/Vault-café")
    nfc = unicodedata.normalize("NFC", "/Vault-café")
    assert nfd != nfc  # sanity: genuinely different byte sequences
    assert session._cwd_key(nfd, "darwin") == session._cwd_key(nfc, "darwin")
    assert session._cwd_key("/VAULT", "darwin") == session._cwd_key("/vault", "darwin")


# --- vault_sessions batches its liveness check: every OS --------------------------------------


def test_vault_sessions_makes_one_process_start_times_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, vault = tmp_path / "home", Path("/vault")
    (home / "sessions").mkdir(parents=True)
    pids = [111, 222, 333]
    for pid in pids:
        entry = {
            "pid": pid,
            "sessionId": f"s{pid}",
            "cwd": str(vault),
            "entrypoint": "claude-vscode",
            "procStart": "X",
        }
        (home / "sessions" / f"{pid}.json").write_text(json.dumps(entry), encoding="utf-8")

    # A fourth entry whose cwd differs from the vault only in letter case: Linux compares
    # exactly (excluded), Windows and macOS compare case-insensitively (included).
    case_pid = 444
    case_entry = {
        "pid": case_pid,
        "sessionId": f"s{case_pid}",
        "cwd": str(vault).upper(),
        "entrypoint": "claude-vscode",
        "procStart": "X",
    }
    (home / "sessions" / f"{case_pid}.json").write_text(json.dumps(case_entry), encoding="utf-8")

    calls: list[set[int]] = []

    def fake_process_start_times(pids_arg: object) -> dict[int, int | str]:
        seen = set(pids_arg)  # type: ignore[arg-type]
        calls.append(seen)
        return {pid: "X" for pid in seen}

    monkeypatch.setattr(winproc, "process_start_times", fake_process_start_times)

    result = session.vault_sessions(home, vault, set())

    expected = set(pids) | (set() if sys.platform == "linux" else {case_pid})
    assert len(calls) == 1
    assert calls[0] == expected
    assert {o.pid for o in result} == expected


# --- find_holder: a malformed pid must not blind the batched start-time read (one ps on macOS) --


@pytest.mark.parametrize("bad_pid", [0, -1, True, 2**31])
def test_find_holder_ignores_a_malformed_pid_entry_next_to_a_live_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_pid: object
) -> None:
    home, live_pid = tmp_path / "home", 4242
    (home / "sessions").mkdir(parents=True)
    live = {
        "pid": live_pid,
        "sessionId": SID,
        "cwd": "/vault",
        "entrypoint": "claude-vscode",
        "procStart": "X",
    }
    (home / "sessions" / f"{live_pid}.json").write_text(json.dumps(live), encoding="utf-8")
    bad = {
        "pid": bad_pid,
        "sessionId": SID,
        "cwd": "/vault",
        "entrypoint": "claude-vscode",
        "procStart": "X",
    }
    (home / "sessions" / "bad.json").write_text(json.dumps(bad), encoding="utf-8")

    def fake_process_start_times(pids: object) -> dict[int, int | str]:
        # Answers only for the live pid: a leaked pid 0 would make this miss it too.
        return {live_pid: "X"} if set(pids) == {live_pid} else {}  # type: ignore[arg-type]

    monkeypatch.setattr(winproc, "process_start_times", fake_process_start_times)

    holder = session.find_holder(home, SID, set())

    assert holder is not None and holder.pid == live_pid


@pytest.mark.skipif(
    sys.platform == "win32", reason="the real start-time read (ps on macOS, /proc on Linux)"
)
def test_find_holder_reads_real_start_times_but_a_malformed_pid_is_dropped_first(
    tmp_path: Path,
) -> None:
    assert sys.platform != "win32"
    home, me = tmp_path / "home", os.getpid()
    (home / "sessions").mkdir(parents=True)
    live = {
        "pid": me,
        "sessionId": SID,
        "cwd": "/vault",
        "entrypoint": "claude-vscode",
        "procStart": str(winproc.process_start_time(me)),
    }
    (home / "sessions" / f"{me}.json").write_text(json.dumps(live), encoding="utf-8")
    bad = {
        "pid": 0,
        "sessionId": SID,
        "cwd": "/vault",
        "entrypoint": "claude-vscode",
        "procStart": "X",
    }
    (home / "sessions" / "bad.json").write_text(json.dumps(bad), encoding="utf-8")

    holder = session.find_holder(home, SID, set())

    assert holder is not None and holder.pid == me


# --- project_dir: POSIX only --------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink resolution")
def test_project_dir_resolves_a_symlinked_vault_to_its_target(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    target = tmp_path / "real_vault"
    target.mkdir()
    link = tmp_path / "link_vault"
    link.symlink_to(target)
    home = tmp_path / "home"

    assert catalog.project_dir(home, link) == catalog.project_dir(home, target)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS NFD/NFC normalization")
def test_project_dir_normalizes_an_nfd_vault_name_to_nfc(tmp_path: Path) -> None:
    assert sys.platform == "darwin"
    nfd_name = unicodedata.normalize("NFD", "café")
    nfc_name = unicodedata.normalize("NFC", "café")
    home = tmp_path / "home"
    vault = tmp_path / nfd_name
    expected = home / "projects" / catalog._SANITIZE_RE.sub("-", str(tmp_path / nfc_name))

    assert catalog.project_dir(home, vault) == expected


@pytest.mark.skipif(sys.platform != "linux", reason="the Linux CLI's raw-realpath transcript slug")
def test_project_dir_keeps_an_nfd_vault_name_raw_on_linux(tmp_path: Path) -> None:
    assert sys.platform == "linux"
    nfd_name = unicodedata.normalize("NFD", "café")
    home = tmp_path / "home"
    vault = tmp_path / nfd_name
    expected = home / "projects" / catalog._SANITIZE_RE.sub("-", str(tmp_path / nfd_name))

    assert catalog.project_dir(home, vault) == expected


# --- registry liveness with a real process: POSIX only ------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX registry liveness (ps on macOS, /proc on Linux)"
)
def test_a_registry_entry_needs_our_real_posix_start_time(tmp_path: Path) -> None:
    assert sys.platform != "win32"
    home, vault, me = tmp_path / "home", tmp_path / "vault", os.getpid()
    (home / "sessions").mkdir(parents=True)
    entry = {
        "pid": me,
        "sessionId": SID,
        "cwd": str(vault),
        "entrypoint": "claude-vscode",
        "procStart": str(winproc.process_start_time(me)),
    }
    registry = home / "sessions" / f"{me}.json"
    registry.write_text(json.dumps(entry), encoding="utf-8")

    assert session.find_holder(home, SID, set()) is not None

    entry["procStart"] = "not-a-real-start-time"
    registry.write_text(json.dumps(entry), encoding="utf-8")

    assert session.find_holder(home, SID, set()) is None


@pytest.mark.skipif(sys.platform != "linux", reason="the Linux CLI's /proc starttime")
def test_a_registry_entry_written_the_linux_cli_way_is_alive(tmp_path: Path) -> None:
    # procStart as the bundled Linux CLI writes it (field 22 of /proc/<pid>/stat, after the last
    # ")"), read here without the app's own reader, so a format mismatch cannot pass unseen.
    home, vault, me = tmp_path / "home", tmp_path / "vault", os.getpid()
    (home / "sessions").mkdir(parents=True)
    stat = Path("/proc/self/stat").read_bytes()
    entry = {
        "pid": me,
        "sessionId": SID,
        "cwd": str(vault),
        "entrypoint": "sdk-cli",
        "procStart": stat.rsplit(b")", 1)[1].split()[19].decode("ascii"),
    }
    (home / "sessions" / f"{me}.json").write_text(json.dumps(entry), encoding="utf-8")

    holder = session.find_holder(home, SID, set())

    assert holder is not None and holder.pid == me
    assert [o.pid for o in session.vault_sessions(home, vault, set())] == [me]


# --- the registry's pidDomain: Linux only -------------------------------------------------------

DEAD_PID = 0x7FFFFFF0  # above every kernel's pid_max, so never a running process


def _cli_pid_domain() -> str:
    """The Linux CLI's own pid domain, built from the same two files as it builds it."""
    try:
        with open("/etc/machine-id", encoding="utf-8", errors="replace") as f:
            machine = f.read().strip()
    except OSError:
        machine = ""
    try:
        namespace = os.readlink("/proc/self/ns/pid")
    except OSError:
        namespace = ""
    return f"linux:{machine}:{namespace}"


def _register(home: Path, pid: int, vault: Path, **fields: object) -> None:
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    entry = {"pid": pid, "sessionId": SID, "cwd": str(vault), "entrypoint": "sdk-cli", **fields}
    (home / "sessions" / f"{pid}.json").write_text(json.dumps(entry), encoding="utf-8")


@pytest.mark.skipif(sys.platform != "linux", reason="the Linux CLI's pidDomain")
def test_our_pid_domain_is_built_from_the_machine_id_and_the_pid_namespace() -> None:
    # A hand copy of the CLI's build; the live test compares it with what the CLI itself wrote.
    assert session._pid_domain() == _cli_pid_domain()


@pytest.mark.skipif(sys.platform != "linux", reason="the Linux CLI's pidDomain")
def test_an_entry_from_another_pid_domain_is_live_whatever_its_pid(tmp_path: Path) -> None:
    # A claude in a sandbox's own pid namespace registers a pid this process cannot see.
    home, vault = tmp_path / "home", tmp_path / "vault"
    _register(home, DEAD_PID, vault, procStart="1", pidDomain=_cli_pid_domain() + "-elsewhere")

    for own_pids in (set(), {DEAD_PID}):
        holder = session.find_holder(home, SID, own_pids)
        assert holder is not None and holder.pid == DEAD_PID
        assert [o.pid for o in session.vault_sessions(home, vault, own_pids)] == [DEAD_PID]


@pytest.mark.skipif(sys.platform != "linux", reason="the Linux CLI's pidDomain")
@pytest.mark.parametrize("domain", ["ours", "absent", None, 7])
def test_an_entry_from_our_pid_domain_or_without_a_string_one_keeps_the_start_time_rule(
    tmp_path: Path, domain: object
) -> None:
    # The CLI's reader drops a pidDomain that is not a string, so a null or a number is no
    # domain at all.
    home, vault, me = tmp_path / "home", tmp_path / "vault", os.getpid()
    fields: dict[str, object] = {} if domain == "absent" else {"pidDomain": domain}
    if domain == "ours":
        fields["pidDomain"] = _cli_pid_domain()
    started = Path("/proc/self/stat").read_bytes().rsplit(b")", 1)[1].split()[19].decode("ascii")

    _register(home, me, vault, procStart=started, **fields)
    assert session.find_holder(home, SID, set()) is not None
    assert [o.pid for o in session.vault_sessions(home, vault, set())] == [me]

    _register(home, me, vault, procStart=started + "0", **fields)
    assert session.find_holder(home, SID, set()) is None
    assert session.vault_sessions(home, vault, set()) == []


@pytest.mark.skipif(sys.platform == "linux", reason="macOS and Windows ignore pidDomain")
def test_off_linux_a_differing_pid_domain_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, vault = tmp_path / "home", tmp_path / "vault"
    _register(home, DEAD_PID, vault, procStart="1", pidDomain="linux:elsewhere")
    monkeypatch.setattr(winproc, "process_start_times", lambda pids: {})

    assert session.find_holder(home, SID, set()) is None
    assert session.vault_sessions(home, vault, set()) == []


# --- the bundled CLI itself: opt-in (-m live), POSIX only ---------------------------------------

# Guards the real CLI's start-up, which normally writes its registry entry within seconds.
HANG_GUARD_S = 30


@pytest.mark.live
@pytest.mark.skipif(sys.platform == "win32", reason="the bundled POSIX CLI's registry")
async def test_the_bundled_cli_idle_in_a_vault_is_a_live_holder(tmp_path: Path) -> None:
    # A synthetic HOME, so the CLI writes its registry under tmp_path and never reads or writes
    # the real ~/.claude; the environment is built from scratch for the same reason.
    home, vault = tmp_path / "home", (tmp_path / "vault").resolve()
    home.mkdir()
    vault.mkdir()
    # A synthetic USER and TMPDIR too: macOS's CLI names its keychain login by $USER, and the
    # CLI's temporary folders then stay under tmp_path. TZ and LANG force a time zone and locale
    # unlike the runner's own, so a pass here shows the CLI writes its start time under its own
    # forced LC_ALL=C and TZ=UTC rather than inheriting ours: a Mac outside UTC, in another
    # locale, would otherwise record a start time the app cannot match.
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    base = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "USER": "viki-live-test",
        "TMPDIR": str(tmp),
        "TZ": "Asia/Tokyo",
        "LANG": "ja_JP.UTF-8",
    }
    argv = build_argv([str(config.BUNDLED_CLI)], Launch(SID), mode="default", effort="low")
    child = await winproc.spawn(argv, cwd=vault, env=winproc.child_env(config.ENTRYPOINT, base))
    claude_home = home / ".claude"
    try:
        async with asyncio.timeout(HANG_GUARD_S):
            while session.find_holder(claude_home, SID, set()) is None:
                await asyncio.sleep(0.1)  # polls the CLI's real registry
        holder = session.find_holder(claude_home, SID, set())
        assert holder is not None and holder.pid == child.pid
        assert [o.pid for o in session.vault_sessions(claude_home, vault, set())] == [child.pid]
        # Our own claude is ours: a domain built unlike the CLI's would make it read as foreign.
        assert session.find_holder(claude_home, SID, {child.pid}) is None
        assert session.vault_sessions(claude_home, vault, {child.pid}) == []
        if sys.platform == "linux":
            registered = (claude_home / "sessions" / f"{child.pid}.json").read_text("utf-8")
            assert json.loads(registered)["pidDomain"] == session._pid_domain()
    finally:
        child.kill_tree()
        await asyncio.wait_for(child.process.wait(), HANG_GUARD_S)
        child.release()
