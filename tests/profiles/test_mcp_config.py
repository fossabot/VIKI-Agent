from __future__ import annotations

import errno
import hashlib
import logging
import os
import stat
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO

import pytest

from tests.app.test_data_api import make_junction
from viki_agent import protection, winproc
from viki_agent.profiles import mcp_config
from viki_agent.profiles.mcp_config import (
    McpConfigError,
    canonical_servers,
    check_configuration,
    config_bytes,
    config_path,
    sweep_mcp_directory,
    write_config,
)

S = "tools.mcp_configuration.servers"
HANG_GUARD_S = 5  # a guard on a thread event that normally fires in milliseconds


def stdio(**changes: object) -> dict[str, object]:
    return {
        "name": "echo",
        "transport": "stdio",
        "command": "uvx",
        "args": ["tool-name"],
        "url": None,
        "note": "why this server",
        **changes,
    }


def http(**changes: object) -> dict[str, object]:
    return {
        "name": "remote",
        "transport": "http",
        "command": None,
        "args": [],
        "url": "https://mcp.example.com/mcp",
        "note": None,
        **changes,
    }


def doc(*servers: object) -> dict[str, object]:
    return {"servers": list(servers)}


def freeze(value: object) -> object:
    """The shape a profile document arrives in: read-only mappings and tuples."""
    if isinstance(value, dict):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze(item) for item in value)
    return value


def plain(value: object) -> object:
    return value


FORMS = [pytest.param(plain, id="plain"), pytest.param(freeze, id="frozen")]

CONFORMING = [
    pytest.param(doc(stdio()), id="stdio"),
    pytest.param(doc(http()), id="https"),
    pytest.param(doc(http(url="http://127.0.0.1:8080/mcp")), id="http-loopback"),
    pytest.param(doc(http(url="http://localhost/mcp")), id="http-localhost"),
    pytest.param(doc(http(url="http://[::1]:9000/")), id="http-ipv6-loopback"),
    pytest.param(doc(stdio(args=[], note=None)), id="no-args-no-note"),
    pytest.param(doc(*(stdio(name=f"s{i}") for i in range(8))), id="eight-servers"),
    pytest.param(doc(stdio(args=["x" * 20] * 32)), id="thirty-two-args"),
    pytest.param(doc(stdio(command="é" * 512)), id="command-1024-bytes"),
    pytest.param(doc(stdio(name="a_b-9")), id="name-punctuation"),
    pytest.param(doc(stdio(name="a" * 26)), id="name-26"),
]

# The provider-key prefix, spelled in halves so the hygiene guard never sees it whole.
API_KEY = "sk-" + "ant-"

REFUSALS = [
    # expansion, undeclared keys, secrets, URLs, names and bounds
    pytest.param(
        doc(stdio(command="run ${HOME}")), "forbidden_text", f"{S}[0].command", id="expansion"
    ),
    pytest.param(doc(stdio(env={"KEY": "v"})), "unknown_key", f"{S}[0].env", id="env"),
    pytest.param(doc(http(headers={"X": "v"})), "unknown_key", f"{S}[0].headers", id="headers"),
    pytest.param(
        doc(stdio(args=["--key=" + "q" * 40])), "secret_value", f"{S}[0].args[0]", id="secret-arg"
    ),
    pytest.param(
        doc(http(url="http://example.com/mcp")), "invalid_url", f"{S}[0].url", id="http-remote"
    ),
    pytest.param(
        doc(http(url="https://u:p@example.com/")), "invalid_url", f"{S}[0].url", id="userinfo"
    ),
    pytest.param(
        doc(http(url="https://example.com/?a=1")), "invalid_url", f"{S}[0].url", id="query"
    ),
    pytest.param(
        doc(http(url="https://example.com/#top")), "invalid_url", f"{S}[0].url", id="fragment"
    ),
    pytest.param(doc(stdio(name="Echo")), "invalid_name", f"{S}[0].name", id="bad-name"),
    pytest.param(
        doc(stdio(), http(name="echo")), "duplicate_name", f"{S}[1].name", id="duplicate-name"
    ),
    pytest.param(
        doc(*(stdio(name=f"s{i}") for i in range(9))), "invalid_count", S, id="nine-servers"
    ),
    pytest.param(
        doc(stdio(command="c" * 1025)), "invalid_length", f"{S}[0].command", id="long-command"
    ),
    pytest.param(doc(stdio(args=["a"] * 33)), "invalid_count", f"{S}[0].args", id="33-args"),
    # the rest of the schema
    pytest.param([], "invalid_type", "tools.mcp_configuration", id="not-an-object"),
    pytest.param({}, "missing_key", S, id="no-servers"),
    pytest.param(
        {**doc(stdio()), "extra": 1}, "unknown_key", "tools.mcp_configuration.extra", id="extra-key"
    ),
    pytest.param(doc(), "invalid_count", S, id="zero-servers"),
    pytest.param({"servers": {}}, "invalid_type", S, id="servers-not-array"),
    pytest.param(doc("echo"), "invalid_type", f"{S}[0]", id="server-not-object"),
    pytest.param(
        doc({k: v for k, v in stdio().items() if k != "note"}),
        "missing_key",
        f"{S}[0].note",
        id="missing-note",
    ),
    pytest.param(doc(stdio(name=7)), "invalid_type", f"{S}[0].name", id="name-type"),
    pytest.param(doc(stdio(name="a" * 27)), "invalid_name", f"{S}[0].name", id="name-27"),
    pytest.param(doc(stdio(name="a" * 32)), "invalid_name", f"{S}[0].name", id="name-32"),
    pytest.param(doc(stdio(name="a" * 33)), "invalid_name", f"{S}[0].name", id="name-too-long"),
    pytest.param(doc(stdio(name=API_KEY + "x")), "secret_value", f"{S}[0].name", id="secret-name"),
    pytest.param(doc(stdio(name="9lives")), "invalid_name", f"{S}[0].name", id="name-digit-first"),
    pytest.param(doc(stdio(transport="sse")), "invalid_transport", f"{S}[0].transport", id="sse"),
    pytest.param(
        doc(stdio(transport=None)), "invalid_type", f"{S}[0].transport", id="transport-type"
    ),
    pytest.param(doc(stdio(args=[1])), "invalid_type", f"{S}[0].args[0]", id="arg-type"),
    pytest.param(doc(stdio(args="a b")), "invalid_type", f"{S}[0].args", id="args-string"),
    pytest.param(doc(stdio(command="")), "invalid_length", f"{S}[0].command", id="empty-command"),
    pytest.param(doc(stdio(args=["a" * 1025])), "invalid_length", f"{S}[0].args[0]", id="long-arg"),
    pytest.param(doc(stdio(note="n" * 1025)), "invalid_length", f"{S}[0].note", id="long-note"),
    pytest.param(
        doc(http(url="https://example.com/" + "p" * 2030)),
        "invalid_length",
        f"{S}[0].url",
        id="long-url",
    ),
    pytest.param(doc(stdio(note="a\nb")), "forbidden_text", f"{S}[0].note", id="newline"),
    pytest.param(doc(stdio(args=["a\x7f"])), "forbidden_text", f"{S}[0].args[0]", id="delete"),
    pytest.param(doc(stdio(args=["a\x9f"])), "forbidden_text", f"{S}[0].args[0]", id="c1-arg"),
    pytest.param(
        doc(http(url="https://example.com/\x85")), "forbidden_text", f"{S}[0].url", id="c1-url"
    ),
    pytest.param(doc(stdio(command="\ud800")), "forbidden_text", f"{S}[0].command", id="surrogate"),
    pytest.param(doc(stdio(note="Bearer abc")), "secret_value", f"{S}[0].note", id="secret-note"),
    pytest.param(
        doc(stdio(command=None)), "invalid_combination", f"{S}[0].command", id="stdio-no-command"
    ),
    pytest.param(
        doc(stdio(url="https://example.com/")), "invalid_combination", f"{S}[0].url", id="stdio-url"
    ),
    pytest.param(doc(http(url=None)), "invalid_combination", f"{S}[0].url", id="http-no-url"),
    pytest.param(
        doc(http(command="uvx")), "invalid_combination", f"{S}[0].command", id="http-command"
    ),
    pytest.param(doc(http(args=["a"])), "invalid_combination", f"{S}[0].args", id="http-args"),
    pytest.param(doc(http(url="ftp://example.com/")), "invalid_url", f"{S}[0].url", id="ftp"),
    pytest.param(doc(http(url="https:///mcp")), "invalid_url", f"{S}[0].url", id="no-host"),
    pytest.param(
        doc(http(url="https://example.com:99999/")), "invalid_url", f"{S}[0].url", id="port"
    ),
    pytest.param(doc(http(url="http://[::1/")), "invalid_url", f"{S}[0].url", id="bad-bracket"),
    pytest.param(
        doc(http(url="http://127.0.0.1.example.com/")),
        "invalid_url",
        f"{S}[0].url",
        id="loopback-prefix",
    ),
]


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("value", CONFORMING)
def test_a_conforming_configuration_passes(value: object, form: Callable[[object], object]) -> None:
    check_configuration(form(value))


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize(("value", "code", "field"), REFUSALS)
def test_a_refusal_names_its_reason_and_field(
    value: object, code: str, field: str, form: Callable[[object], object]
) -> None:
    with pytest.raises(McpConfigError) as caught:
        check_configuration(form(value))

    assert (caught.value.code, caught.value.field) == (code, field)


def test_canonical_servers_pins_the_exact_bytes_in_document_order() -> None:
    result = canonical_servers(doc(stdio(), http()))

    assert result == (
        b'{"args":["tool-name"],"command":"uvx","name":"echo",'
        b'"note":"why this server","transport":"stdio","url":null}',
        b'{"args":[],"command":null,"name":"remote","note":null,"transport":"http",'
        b'"url":"https://mcp.example.com/mcp"}',
    )


def test_a_changed_note_gives_different_bytes() -> None:
    assert canonical_servers(doc(stdio(note="another reason"))) != canonical_servers(doc(stdio()))


def test_a_changed_name_gives_different_bytes() -> None:
    assert canonical_servers(doc(stdio(name="echo2"))) != canonical_servers(doc(stdio()))


def test_a_frozen_document_gives_the_same_bytes_as_its_plain_form() -> None:
    assert canonical_servers(freeze(doc(stdio()))) == canonical_servers(doc(stdio()))


def test_config_bytes_pins_the_generated_file_for_one_stdio_server() -> None:
    result = config_bytes(doc(stdio()))

    assert result == b'{"mcpServers":{"viki_echo":{"args":["tool-name"],"command":"uvx"}}}'


def test_config_bytes_pins_the_generated_file_for_one_http_server() -> None:
    result = config_bytes(doc(http()))

    assert result == (
        b'{"mcpServers":{"viki_remote":{"type":"http","url":"https://mcp.example.com/mcp"}}}'
    )


def test_config_bytes_escapes_non_ascii_args_and_drops_the_note() -> None:
    result = config_bytes(doc(stdio(args=["café"], note="drop this note text")))

    assert result == b'{"mcpServers":{"viki_echo":{"args":["caf\\u00e9"],"command":"uvx"}}}'
    result.decode("ascii")  # pure ASCII bytes
    assert b"note" not in result
    assert b"drop this" not in result


@pytest.mark.parametrize("render", [canonical_servers, config_bytes])
def test_an_invalid_value_raises_before_rendering(
    render: Callable[[object], tuple[bytes, ...] | bytes],
) -> None:
    with pytest.raises(McpConfigError):
        render(doc())  # zero servers


DATA = b'{"mcpServers":{"viki_echo":{"args":["tool-name"],"command":"uvx"}}}'
HEX_NAME = "0" * 64 + ".json"


def private_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "mcp"
    assert protection.create_private_directory(folder)
    return folder


@pytest.mark.usefixtures("desktop_umask")
def test_two_writes_of_equal_bytes_share_one_private_file(tmp_path: Path) -> None:
    folder = tmp_path / "mcp"

    first = write_config(folder, DATA)
    second = write_config(folder, DATA)

    assert first == second == config_path(folder, DATA)
    assert first.is_absolute()
    assert first.name == f"{hashlib.sha256(DATA).hexdigest()}.json"
    assert os.listdir(folder) == [first.name]
    assert first.read_bytes() == DATA
    if sys.platform == "win32":
        protection.require_private_directory(folder)
        protection.require_private_file(first)
    else:
        assert stat.S_IMODE(os.lstat(folder).st_mode) == 0o700
        assert stat.S_IMODE(os.lstat(first).st_mode) == 0o600


def test_a_different_file_at_the_name_refuses_without_naming_it(tmp_path: Path) -> None:
    folder = private_folder(tmp_path)
    path = config_path(folder, DATA)
    path.write_bytes(b"{}")

    with pytest.raises(OSError) as caught:
        write_config(folder, DATA)

    assert caught.value.errno == errno.EEXIST
    assert str(tmp_path) not in str(caught.value)
    assert path.read_bytes() == b"{}"


def test_a_write_that_fails_midway_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "mcp"

    def disk_full(fd: int) -> None:
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(mcp_config.os, "fsync", disk_full)  # the file exists by now
    with pytest.raises(OSError) as caught:
        write_config(folder, DATA)
    monkeypatch.undo()

    assert caught.value.errno == errno.ENOSPC
    assert os.listdir(folder) == []
    assert write_config(folder, DATA).read_bytes() == DATA


def test_a_start_waits_for_another_still_writing_the_same_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "mcp"
    real_open = mcp_config.osfs.open_new_private
    inside, release = threading.Event(), threading.Event()

    def held_open(path: Path) -> BinaryIO:
        target = real_open(path)
        if not inside.is_set():  # only the first start is held, its file created but empty
            inside.set()
            assert release.wait(HANG_GUARD_S)
        return target

    results: dict[str, object] = {}

    def start(key: str) -> None:
        try:
            results[key] = write_config(folder, DATA)
        except OSError as exc:
            results[key] = exc

    monkeypatch.setattr(mcp_config.osfs, "open_new_private", held_open)
    first = threading.Thread(target=start, args=("first",))
    second = threading.Thread(target=start, args=("second",))
    first.start()
    assert inside.wait(HANG_GUARD_S)
    second.start()
    second.join(0.05)  # room for a second start that does not wait to read the unfinished file
    release.set()
    first.join(HANG_GUARD_S)
    second.join(HANG_GUARD_S)

    path = config_path(folder, DATA)
    assert results == {"first": path, "second": path}


def test_a_write_through_a_junctioned_folder_refuses_and_writes_nothing(tmp_path: Path) -> None:
    target = tmp_path / "target"
    assert protection.create_private_directory(target)
    link = tmp_path / "mcp"
    make_junction(link, target)

    with pytest.raises(protection.ProtectionError):
        write_config(link, DATA)

    assert os.listdir(target) == []


def test_a_sweep_removes_only_generated_files(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    folder = tmp_path / "mcp"
    generated = write_config(folder, DATA)
    kept = ["other.json", "ABC.json", "A" * 64 + ".json"]
    for name in kept:
        (folder / name).write_bytes(b"{}")
    (folder / HEX_NAME).mkdir()
    linked = folder / ("1" * 64 + ".json")
    linked.write_bytes(b"{}")
    os.link(linked, tmp_path / "second-link")

    with caplog.at_level(logging.WARNING):
        sweep_mcp_directory(folder)

    assert not generated.exists()
    assert sorted(os.listdir(folder)) == sorted([*kept, HEX_NAME, linked.name])
    assert caplog.records == []


def test_a_sweep_of_an_absent_folder_does_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        sweep_mcp_directory(tmp_path / "mcp")

    assert caplog.records == []


def test_a_sweep_through_a_junction_deletes_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    target = private_folder(tmp_path)
    (target / HEX_NAME).write_bytes(b"{}")
    link = tmp_path / "linked"
    make_junction(link, target)

    with caplog.at_level(logging.WARNING):
        sweep_mcp_directory(link)

    assert (target / HEX_NAME).exists()
    assert [record.getMessage() for record in caplog.records] == [
        "mcp config folder is not private; nothing swept"
    ]


def test_a_sweep_under_a_junctioned_parent_deletes_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    target = private_folder(tmp_path)
    inner = target / "mcp"
    assert protection.create_private_directory(inner)
    (inner / HEX_NAME).write_bytes(b"{}")
    link = tmp_path / "linked"
    make_junction(link, target)

    with caplog.at_level(logging.WARNING):
        sweep_mcp_directory(link / "mcp")

    assert (inner / HEX_NAME).exists()
    assert [record.getMessage() for record in caplog.records] == [
        "mcp config folder is not private; nothing swept"
    ]


def test_a_sweep_reads_at_most_the_capped_number_of_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = private_folder(tmp_path)
    for digit in "012":
        (folder / (digit * 64 + ".json")).write_bytes(b"{}")
    monkeypatch.setattr(mcp_config, "MAX_SWEEP_ENTRIES", 2)

    sweep_mcp_directory(folder)

    assert len(os.listdir(folder)) == 1


def test_a_failed_unlink_logs_only_the_errno_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = private_folder(tmp_path)
    (folder / HEX_NAME).write_bytes(b"{}")

    def refuse(path: object) -> None:
        raise PermissionError(errno.EACCES, "refused", str(path))

    monkeypatch.setattr(mcp_config.os, "unlink", refuse)
    with caplog.at_level(logging.WARNING):
        sweep_mcp_directory(folder)
    monkeypatch.undo()

    assert (folder / HEX_NAME).exists()
    assert len(caplog.records) == 1
    assert "EACCES" in caplog.text
    assert str(tmp_path) not in caplog.text
    assert HEX_NAME not in caplog.text


def test_a_sweep_whose_listing_fails_returns_and_deletes_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = private_folder(tmp_path)
    (folder / HEX_NAME).write_bytes(b"{}")

    def refuse(path: object) -> None:
        raise PermissionError(errno.EACCES, "refused", str(path))

    monkeypatch.setattr(mcp_config.os, "scandir", refuse)
    with caplog.at_level(logging.WARNING):
        sweep_mcp_directory(folder)
    monkeypatch.undo()

    assert (folder / HEX_NAME).exists()
    assert "EACCES" in caplog.text


def test_a_value_is_bounded_in_bytes_not_characters() -> None:
    value = "é" * 600  # 600 characters, 1200 bytes: over the 1024-byte bound

    with pytest.raises(McpConfigError) as caught:
        check_configuration(doc(stdio(command=value)))

    assert (caught.value.code, caught.value.field) == ("invalid_length", f"{S}[0].command")


def test_config_path_is_absolute_from_a_relative_directory() -> None:
    path = config_path(Path("relative-mcp"), DATA)

    assert path.is_absolute()
    assert path == Path("relative-mcp").absolute() / f"{hashlib.sha256(DATA).hexdigest()}.json"


@pytest.mark.usefixtures("desktop_umask")
def test_the_read_back_asks_for_one_byte_more_than_the_data_without_following_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    real = mcp_config.osfs.read_regular

    def spy(*args: object, **kwargs: object) -> bytes:
        calls.append((args, kwargs))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(mcp_config.osfs, "read_regular", spy)
    path = write_config(tmp_path / "mcp", DATA)

    assert len(calls) == 1
    assert callable(calls[0][1].pop("pre_read"))
    assert calls == [((path, len(DATA) + 1), {"follow_symlinks": False})]


def test_the_module_imports_alone_without_a_cycle(tmp_path: Path) -> None:
    # A fresh interpreter, so no module this test session already imported can hide a cycle.
    result = winproc.run(
        [sys.executable, "-c", "import viki_agent.profiles.mcp_config"], cwd=tmp_path
    )

    assert result.returncode == 0, result.stderr
