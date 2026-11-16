from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from tests.vaults.test_vault import service
from viki_agent.vaults import vault as module
from viki_agent.vaults.vault import VaultError

if sys.platform == "win32":
    from tests.vaults.test_vault_metadata_windows import native_io as native_io
else:
    from tests.vaults.test_vault_metadata_posix import native_io as native_io


@pytest.mark.parametrize(
    "refs",
    [
        None,
        {},
        "a.md",
        ("a.md",),
        [],
        ["a.md"] * 33,
        ["a.md", "a.md"],
        [1],
        [None],
        [[]],
        [""],
        ["a.txt"],
        ["/a.md"],
        ["a\\b.md"],
        ["C:a.md"],
        ["a//b.md"],
        ["a/./b.md"],
        ["a/../b.md"],
        ["a.md/"],
        ["x" * 510 + ".md"],
        [".git/a.txt"],
        ["good.md", "bad/../a.md"],
        ["x\x7f.md"],
        ["x\x85.md"],
        ["x\ud800.md"],
    ],
)
def test_invalid_batch_is_rejected_before_root_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refs: Any
) -> None:
    vault = service(tmp_path)
    monkeypatch.setattr(vault, "_root", lambda *_: pytest.fail("invalid batch reached root IO"))
    with pytest.raises(VaultError, match="^invalid_metadata_request$"):
        vault.metadata("w", refs)


def test_private_refs_never_open_or_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]]
) -> None:
    vault = service(tmp_path)

    def refuse_open(*_args, **_kwargs):
        pytest.fail("private ref opened")

    if sys.platform == "win32":
        monkeypatch.setattr(module, "_CreateFileW", refuse_open)
    else:
        monkeypatch.setattr(module.os, "open", refuse_open)
    refs = [".git/n.md", "folder/.ENV/n.md", "credentials/n.md", ".claude/n.md"]
    result = vault.metadata("w", refs)
    assert [(item.ref, item.state, item.reason, item.read_bytes) for item in result.items] == [
        (ref, "unreadable", "private_path", 0) for ref in refs
    ]
    assert all(item.display_metadata is None for item in result.items)
    assert (result.partial, result.partial_reasons, result.read_bytes, result.omitted) == (
        True,
        ("unreadable",),
        0,
        0,
    )
    assert native_io == []


def test_native_observations_preserve_order_structure_and_actual_charges(
    tmp_path: Path, native_io: list[tuple[int, int]]
) -> None:
    raw = {
        "japanese.md": "\ufeff---\r\ntitle: 日本語\r\n---\r\n# Body\r\n".encode(),
        "skill.md": b"---\ntitle: SKILL\n---\n# Body\n",
        "empty.md": b"",
        "unsupported.md": b"---\ntitle: 'bad'\ntags: Good\n---\n# Untrusted\n",
        "truncated.md": b"---\ntype: Note\n---\n" + b"x" * 32768,
    }
    for ref, body in raw.items():
        (tmp_path / ref).write_bytes(body)
    refs = [*raw, "missing.md"]
    result = service(tmp_path).metadata("w", refs)
    assert [item.ref for item in result.items] == refs
    assert [(item.state, item.reason) for item in result.items] == [
        ("ready", None),
        ("ready", None),
        ("absent", None),
        ("unsupported", "unsupported_metadata"),
        ("truncated", "prefix_limit"),
        ("unreadable", "missing_note"),
    ]
    projected = [item.display_metadata for item in result.items]
    assert projected[0] is not None and projected[0].display_title == "日本語"
    assert projected[1] is not None and projected[1].display_title == "SKILL"
    assert projected[2] is not None and projected[2].tags == []
    assert projected[3] is not None and projected[3].tags is None
    assert projected[3].display_title is projected[3].type is projected[3].status is None
    assert projected[4] is not None
    assert (projected[4].frontmatter_state, projected[4].heading_state, projected[4].tags) == (
        "ready",
        "truncated",
        [],
    )
    assert projected[4].type == "Note" and projected[5] is None
    expected = [min(len(body), 32768) for body in raw.values()] + [0]
    assert [item.read_bytes for item in result.items] == expected
    assert result.read_bytes == sum(expected) == sum(actual for _, actual in native_io)
    assert all(0 < requested <= 4096 and actual <= requested for requested, actual in native_io)
    assert (result.partial, result.partial_reasons, result.omitted) == (
        True,
        ("unsupported", "truncated", "unreadable"),
        0,
    )
    assert set(asdict(result)) == {"items", "partial", "partial_reasons", "read_bytes", "omitted"}
    assert all(
        set(asdict(item)) == {"ref", "state", "reason", "read_bytes", "display_metadata"}
        for item in result.items
    )


def test_all_controls_and_surrogates_are_refused_before_root_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = service(tmp_path)
    monkeypatch.setattr(vault, "_root", lambda *_: pytest.fail("control reached root IO"))
    for code in (*range(32), *range(127, 160), *range(0xD800, 0xE000)):
        with pytest.raises(VaultError, match="^invalid_metadata_request$"):
            vault.metadata("w", [f"x{chr(code)}.md"])


def test_32_actual_prefixes_hit_the_batch_cap_without_extra_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]]
) -> None:
    monkeypatch.setattr(module.time, "monotonic", lambda: 0.0)
    refs = [f"{index:02}.md" for index in range(32)]
    for ref in refs:
        (tmp_path / ref).write_bytes(b"x" * 32768)
    result = service(tmp_path).metadata("w", refs)
    assert [item.ref for item in result.items] == refs
    assert [item.read_bytes for item in result.items] == [32768] * 32
    assert all(item.state == "absent" and item.reason is None for item in result.items)
    assert result.read_bytes == 1024 * 1024 == sum(actual for _, actual in native_io)
    assert native_io == [(4096, 4096)] * 256
    assert (result.partial, result.partial_reasons, result.omitted) == (False, (), 0)


def test_all_ready_and_absent_items_are_complete_coverage(
    tmp_path: Path, native_io: list[tuple[int, int]]
) -> None:
    (tmp_path / "empty.md").write_bytes(b"")
    (tmp_path / "ready.md").write_bytes(b"# A\r\n")
    result = service(tmp_path).metadata("w", ["empty.md", "ready.md"])
    assert [(item.state, item.reason, item.read_bytes) for item in result.items] == [
        ("absent", None, 0),
        ("ready", None, 5),
    ]
    assert (result.partial, result.partial_reasons, result.read_bytes, result.omitted) == (
        False,
        (),
        5,
        0,
    )
    assert native_io == [(5, 5)]


@pytest.mark.parametrize("reason", ["deadline", "byte_limit"])
def test_a_charged_partial_read_leaves_current_and_remainder_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]], reason: str
) -> None:
    (tmp_path / "a.md").write_bytes(b"x" * 6000)
    vault, now, called = service(tmp_path), [0.0], []
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    if reason == "deadline":
        target, name = (module, "_ReadFile") if sys.platform == "win32" else (module.os, "read")
        read = getattr(target, name)

        def expire_after_read(*args, **kwargs):
            result = read(*args, **kwargs)
            now[0] = 1.0
            return result

        monkeypatch.setattr(target, name, expire_after_read)
    else:
        monkeypatch.setattr(module, "MAX_METADATA_BATCH_BYTES", 5000)
    prefix = vault._read_prefix

    def observed(root, ref, budget):
        called.append(ref)
        return prefix(root, ref, budget)

    monkeypatch.setattr(vault, "_read_prefix", observed)
    result = vault.metadata("w", ["a.md", "next.md", ".git/n.md"])
    charged = 4096 if reason == "deadline" else 5000
    assert [(item.state, item.reason, item.read_bytes) for item in result.items] == [
        ("unknown", reason, charged),
        ("unknown", reason, 0),
        ("unknown", reason, 0),
    ]
    assert all(item.display_metadata is None for item in result.items)
    assert (result.partial, result.partial_reasons, result.read_bytes, result.omitted) == (
        True,
        (reason,),
        charged,
        3,
    )
    assert called == ["a.md"]
    expected = [(4096, 4096)] if reason == "deadline" else [(4096, 4096), (904, 904)]
    assert native_io == expected


@pytest.mark.parametrize("boundary", ["admission", "between-items"])
def test_deadline_counts_admission_and_precedes_the_next_prefix_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]], boundary: str
) -> None:
    (tmp_path / "a.md").write_bytes(b"# A\n")
    vault, now, called = service(tmp_path), [0.0], []
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    root, prefix = vault._root, vault._read_prefix

    def admitted(workspace_id):
        current = root(workspace_id)
        if boundary == "admission":
            now[0] = 1.0
        return current

    def observed(root, ref, budget):
        called.append(ref)
        result = prefix(root, ref, budget)
        now[0] = 1.0
        return result

    monkeypatch.setattr(vault, "_root", admitted)
    monkeypatch.setattr(vault, "_read_prefix", observed)
    result = vault.metadata("w", ["a.md", "next.md"])
    expected = (
        [("unknown", "deadline", 0)] * 2
        if boundary == "admission"
        else [("ready", None, 4), ("unknown", "deadline", 0)]
    )
    assert [(item.state, item.reason, item.read_bytes) for item in result.items] == expected
    assert called == ([] if boundary == "admission" else ["a.md"])
    assert native_io == ([] if boundary == "admission" else [(4, 4)])
    assert result.omitted == (2 if boundary == "admission" else 1)
    assert result.partial and result.partial_reasons == ("deadline",)


@pytest.mark.parametrize("code", ["unknown_workspace", "root_unavailable"])
def test_admission_failures_abort_instead_of_becoming_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]], code: str
) -> None:
    vault = service(tmp_path)
    if code == "unknown_workspace":

        def unknown(_workspace_id):
            raise module.WorkspaceError("unknown_workspace")

        monkeypatch.setattr(vault._workspaces, "get", unknown)
    else:
        monkeypatch.setattr(vault._workspaces, "current_root", lambda _: None)
    with pytest.raises(VaultError, match=f"^{code}$"):
        vault.metadata("w", ["a.md", "b.md"])
    assert native_io == []


def test_a_mid_batch_root_replacement_aborts_after_the_closed_first_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]]
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "a.md").write_bytes(b"a")
    (root / "b.md").write_bytes(b"b")
    vault, called = service(root), []
    prefix = vault._read_prefix

    def replace_after_first(root_arg, ref, budget):
        called.append(ref)
        result = prefix(root_arg, ref, budget)
        root.rename(tmp_path / "moved")
        root.mkdir()
        return result

    monkeypatch.setattr(vault, "_read_prefix", replace_after_first)
    with pytest.raises(VaultError, match="^root_changed$"):
        vault.metadata("w", ["a.md", "b.md"])
    assert called == ["a.md", "b.md"]
    assert native_io == [(1, 1)]


@pytest.fixture
def per_item_handles(monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]]):
    target = module if sys.platform == "win32" else module.os
    open_name, close_name = (
        ("_CreateFileW", "_CloseHandle") if sys.platform == "win32" else ("open", "close")
    )
    real_open, real_close = getattr(target, open_name), getattr(target, close_name)
    active: set[int] = set()

    def open_handle(*args, **kwargs):
        handle = real_open(*args, **kwargs)
        if sys.platform == "win32" and handle == module._INVALID_HANDLE_VALUE:
            return handle
        active.add(handle)
        return handle

    def close_handle(handle):
        result = real_close(handle)
        active.remove(handle)
        return result

    monkeypatch.setattr(target, open_name, open_handle)
    monkeypatch.setattr(target, close_name, close_handle)
    prefix = module.VaultService._read_prefix

    def closed_after_item(self, *args):
        try:
            return prefix(self, *args)
        finally:
            assert active == set()

    monkeypatch.setattr(module.VaultService, "_read_prefix", closed_after_item)
    yield active
    assert active == set()


@pytest.mark.parametrize(
    "code",
    [
        "missing_note",
        "private_path",
        "unsafe_path",
        "note_too_large",
        "target_busy",
        "read_failed",
        "invalid_encoding",
        "unexpected_internal_code",
    ],
)
def test_only_approved_unreadable_codes_escape_the_reader_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    vault = service(tmp_path)

    def refused(_root, _ref, budget):
        budget.charge(9)
        raise VaultError(code, synthetic_detail="not a wire field")

    monkeypatch.setattr(vault, "_read_prefix", refused)
    result = vault.metadata("w", ["a.md", "b.md"])
    expected = "read_failed" if code == "unexpected_internal_code" else code
    assert [
        (item.state, item.reason, item.read_bytes, item.display_metadata) for item in result.items
    ] == [("unreadable", expected, 9, None), ("unreadable", expected, 9, None)]
    assert (result.partial, result.partial_reasons, result.read_bytes, result.omitted) == (
        True,
        ("unreadable",),
        18,
        0,
    )
    assert "synthetic_detail" not in str(asdict(result))


def test_native_changed_observation_discards_projection_but_keeps_charge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_io: list[tuple[int, int]],
    per_item_handles,
) -> None:
    (tmp_path / "changed.md").write_bytes(b"# Bad\n")
    (tmp_path / "good.md").write_bytes(b"# Good\n")
    name = "_prefix_stamp" if sys.platform == "win32" else "_posix_prefix_stamp"
    original, observed = getattr(module, name), [0]

    def stamp(value, *, directory=False):
        result = original(value, directory=directory)
        if not directory:
            observed[0] += 1
            if observed[0] == 2:
                return (*result[:-1], result[-1] + 1)
        return result

    monkeypatch.setattr(module, name, stamp)
    result = service(tmp_path).metadata("w", ["changed.md", "good.md"])
    assert [(item.state, item.reason, item.read_bytes) for item in result.items] == [
        ("changed", "changed", 6),
        ("ready", None, 7),
    ]
    assert result.items[0].display_metadata is None
    assert result.items[1].display_metadata is not None
    assert result.items[1].display_metadata.display_title == "Good"
    assert result.read_bytes == 13 == sum(actual for _, actual in native_io)
    assert result.partial_reasons == ("changed",) and result.omitted == 0
    assert per_item_handles == set()


def test_native_read_failure_keeps_actual_bytes_and_continues_after_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_io: list[tuple[int, int]],
    per_item_handles,
) -> None:
    (tmp_path / "failed.md").write_bytes(b"x" * 6000)
    (tmp_path / "good.md").write_bytes(b"# Good\n")
    target, name = (module, "_ReadFile") if sys.platform == "win32" else (module.os, "read")
    original, calls = getattr(target, name), [0]

    def read(*args, **kwargs):
        calls[0] += 1
        if sys.platform != "win32" and calls[0] == 2:
            raise OSError("synthetic read refusal")
        result = original(*args, **kwargs)
        return False if sys.platform == "win32" and calls[0] == 1 else result

    monkeypatch.setattr(target, name, read)
    result = service(tmp_path).metadata("w", ["failed.md", "good.md"])
    assert [(item.state, item.reason, item.read_bytes) for item in result.items] == [
        ("unreadable", "read_failed", 4096),
        ("ready", None, 7),
    ]
    assert result.items[0].display_metadata is None
    assert result.read_bytes == 4103 == sum(actual for _, actual in native_io)
    assert result.partial_reasons == ("unreadable",) and result.omitted == 0
    assert native_io == [(4096, 4096), (7, 7)]
    assert per_item_handles == set()


def test_native_invalid_utf8_is_charged_and_does_not_supply_projection(
    tmp_path: Path, native_io: list[tuple[int, int]], per_item_handles
) -> None:
    (tmp_path / "bad.md").write_bytes(b"x\xff")
    result = service(tmp_path).metadata("w", ["bad.md"])
    item = result.items[0]
    assert (item.state, item.reason, item.read_bytes, item.display_metadata) == (
        "unreadable",
        "invalid_encoding",
        2,
        None,
    )
    assert (result.partial, result.partial_reasons, result.read_bytes, result.omitted) == (
        True,
        ("unreadable",),
        2,
        0,
    )
    assert native_io == [(2, 2)] and per_item_handles == set()


def test_metadata_reuses_prefix_only_and_preserves_scan_and_selected_note_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_io: list[tuple[int, int]],
    per_item_handles,
) -> None:
    raw = b"\xef\xbb\xbf# Same\r\n" + b"x" * 32769
    (tmp_path / "a.md").write_bytes(raw)
    vault = service(tmp_path)
    listing = vault.scan("w")
    assert listing.scanned_bytes == 0 and native_io == []
    assert listing.entries[0].bytes == len(raw)
    note = vault.read("w", "a.md")
    assert (note.text, note.source, note.byte_size, note.sha256) == (
        raw.decode("utf-8-sig"),
        raw.decode("utf-8"),
        len(raw),
        module.hashlib.sha256(raw).hexdigest(),
    )
    assert sum(actual for _, actual in native_io) == len(raw)
    native_io.clear()

    def forbidden(*_args, **_kwargs):
        pytest.fail("metadata reached full read, scan, links, history, hash or legacy parser")

    for name in (
        "read",
        "scan",
        "_read_stamped",
        "_scan_root",
        "history",
        "backlinks",
        "resolve_links",
    ):
        monkeypatch.setattr(vault, name, forbidden)
    for name in ("_frontmatter", "_link_targets"):
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(module.hashlib, "sha256", forbidden)
    result = vault.metadata("w", ["a.md"])
    assert result.read_bytes == 32768 and native_io == [(4096, 4096)] * 8
    item = result.items[0]
    assert item.display_metadata is not None and item.display_metadata.display_title == "Same"
    assert "sha256" not in asdict(item) and per_item_handles == set()


def test_the_validated_request_snapshot_survives_caller_list_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native_io: list[tuple[int, int]]
) -> None:
    (tmp_path / "a.md").write_bytes(b"# A\n")
    refs, vault = ["a.md"], service(tmp_path)
    original = vault._root

    def admitted(workspace_id):
        refs[:] = ["x\x7f.md", "a.md"]
        return original(workspace_id)

    monkeypatch.setattr(vault, "_root", admitted)
    result = vault.metadata("w", refs)
    assert [(item.ref, item.state, item.reason, item.read_bytes) for item in result.items] == [
        ("a.md", "ready", None, 4)
    ]
    assert native_io == [(4, 4)]
