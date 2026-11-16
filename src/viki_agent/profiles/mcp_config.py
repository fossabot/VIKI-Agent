"""App MCP servers: the closed schema of a profile's `tools.mcp_configuration`, the
generated `--mcp-config` file and its sweep.

A profile document may name at most eight stdio or http servers, and nothing else: the schema
refuses an environment, headers, variable expansion and secret-shaped text (as
`diagnostics._looks_secret` defines it). This module never logs a server name, a path or a
definition.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import itertools
import json
import logging
import os
import re
import stat
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

from viki_agent import osfs, protection
from viki_agent.api_json import unique_object
from viki_agent.app.diagnostics import _looks_secret

logger = logging.getLogger(__name__)

SERVER_PREFIX = "viki_"
MAX_SERVERS = 8
MAX_ARGS = 32
MAX_TEXT_BYTES = 1024
MAX_URL_BYTES = 2048
MAX_IMPORT_BYTES = 64 << 10
MAX_SWEEP_ENTRIES = 1024

_ROOT = "tools.mcp_configuration"
_CONFIG_KEYS = ("servers",)
_SERVER_KEYS = ("name", "transport", "command", "args", "url", "note")
_TRANSPORTS = ("stdio", "http")
# At most 26 characters, so the live name `viki_<name>` stays below the 32-character run that
# `_looks_secret` refuses.
_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,25}")
# `${` would be expanded by the CLI from its environment; control characters (C0, DEL and C1)
# could split or hide a value in the generated file, the preview or the editor.
_FORBIDDEN_RE = re.compile(r"\$\{|[\x00-\x1f\x7f-\x9f]")
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_GENERATED_NAME_RE = re.compile(r"[0-9a-f]{64}\.json")
# One process writes the folder (the app is single-instance); the lock keeps a start from
# reading a file another start is still writing.
_WRITE_LOCK = threading.Lock()


class McpConfigError(Exception):
    """A value outside the schema: `code` is the reason, `field` the dotted path to it."""

    def __init__(self, code: str, field: str) -> None:
        super().__init__(f"{code} at {field}")
        self.code = code
        self.field = field


def import_configuration(source: object) -> dict[str, object]:
    """Convert Claude or VS Code JSON into a checked profile configuration, without I/O.

    Reject the whole import when any entry is unsupported; dropping an environment or a header
    would change what the server runs. Duplicate JSON keys are refused before conversion.
    """
    if not isinstance(source, str):
        raise McpConfigError("invalid_type", _ROOT)
    try:
        size = len(source.encode("utf-8"))
    except UnicodeEncodeError:
        raise McpConfigError("forbidden_text", _ROOT) from None
    if not 1 <= size <= MAX_IMPORT_BYTES:
        raise McpConfigError("invalid_length", _ROOT)
    try:
        value = json.loads(source, object_pairs_hook=unique_object)
    except ValueError, RecursionError:
        raise McpConfigError("invalid_json", _ROOT) from None
    config = _object(value, _ROOT)
    if set(config) == {"mcpServers"}:
        raw_servers = config["mcpServers"]
    elif set(config) == {"servers"}:
        raw_servers = config["servers"]
    else:
        raise McpConfigError("unknown_key", _ROOT)
    entries = _object(raw_servers, _ROOT)
    if not 1 <= len(entries) <= MAX_SERVERS:
        raise McpConfigError("invalid_count", f"{_ROOT}.servers")
    servers: list[dict[str, object]] = []
    for index, (name, raw) in enumerate(entries.items()):
        path = f"{_ROOT}.servers[{index}]"
        entry = _object(raw, path)
        if set(entry) - {"type", "command", "args", "url"}:
            # Do not echo an unknown key: source JSON can contain credentials as keys too.
            raise McpConfigError("unsupported_import", path)
        transport = entry.get("type", "http" if "url" in entry else "stdio")
        servers.append(
            {
                "name": name,
                "transport": transport,
                "command": entry.get("command"),
                "args": entry.get("args", []),
                "url": entry.get("url"),
                "note": None,
            }
        )
    result: dict[str, object] = {"servers": servers}
    check_configuration(result)
    return result


def check_configuration(value: object) -> None:
    """Raise `McpConfigError` unless `value` is a conforming `tools.mcp_configuration`. Any
    Mapping reads as an object and any list or tuple as an array, so a frozen document checks
    the same as its plain form."""
    config = _object(value, _ROOT)
    _require_keys(config, _CONFIG_KEYS, _ROOT)
    path = f"{_ROOT}.servers"
    servers = _array(config["servers"], path)
    if not 1 <= len(servers) <= MAX_SERVERS:
        raise McpConfigError("invalid_count", path)
    names: set[str] = set()
    for index, server in enumerate(servers):
        _check_server(server, f"{path}[{index}]", names)


def canonical_servers(value: object) -> tuple[bytes, ...]:
    """The whole server object's canonical JSON bytes, in document order. The bytes identify
    one server for revocation: any change to its name, transport, command, args, url or note
    gives different bytes. Raises `McpConfigError` first, as `check_configuration`."""
    check_configuration(value)
    config = _object(value, _ROOT)
    servers = _array(config["servers"], f"{_ROOT}.servers")
    canonical: list[bytes] = []
    for raw in servers:
        server = _object(raw, _ROOT)
        whole = {key: server[key] for key in _SERVER_KEYS}
        whole["args"] = list(_array(server["args"], _ROOT))
        canonical.append(_canonical_bytes(whole))
    return tuple(canonical)


def config_bytes(value: object) -> bytes:
    """The generated `--mcp-config` file's bytes: `{"mcpServers": {"viki_<name>": ...}}`, `note`
    absent. Raises `McpConfigError` first, as `check_configuration`."""
    check_configuration(value)
    config = _object(value, _ROOT)
    servers = _array(config["servers"], f"{_ROOT}.servers")
    mcp_servers: dict[str, object] = {}
    for server in servers:
        obj = _object(server, _ROOT)
        mcp_servers[f"{SERVER_PREFIX}{obj['name']}"] = _render_server(obj)
    return _canonical_bytes({"mcpServers": mcp_servers})


def config_path(directory: Path, data: bytes) -> Path:
    """The file's content-addressed name, so equal configurations share one file."""
    return directory.absolute() / f"{hashlib.sha256(data).hexdigest()}.json"


def write_config(directory: Path, data: bytes) -> Path:
    """Write `data` once to its private content-addressed file and return the absolute path.

    An existing file must be private, regular, single-link and hold exactly `data` (a start
    racing another, or an earlier write). Other bytes raise OSError(EEXIST) without a path in
    the message. A write that fails removes the file it created. ProtectionError and OSError
    propagate.
    """
    path = config_path(directory, data)
    with _WRITE_LOCK:
        protection.create_private_directory(path.parent)  # False when it already exists
        protection.require_private_directory(path.parent)  # refuses a link, junction or other owner
        try:
            target = osfs.open_new_private(path)
        except FileExistsError:
            pass  # the comparison below decides
        else:
            try:
                with target:
                    target.write(data)
                    target.flush()
                    os.fsync(target.fileno())
                osfs.fsync_dir(path.parent)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(path)  # a partial file would refuse every later start
                raise
        protection.require_private_file(path)
        checked = os.lstat(path)
        if (
            not stat.S_ISREG(checked.st_mode)
            or checked.st_nlink != 1
            or getattr(checked, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise OSError(errno.EINVAL, "refusing an aliased generated config file")

        def check_opened(fd: int) -> None:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or not os.path.samestat(checked, opened)
            ):
                raise OSError(errno.EINVAL, "generated config file changed before read")

        if (
            osfs.read_regular(path, len(data) + 1, follow_symlinks=False, pre_read=check_opened)
            != data
        ):
            raise OSError(errno.EEXIST, "mcp config file holds other bytes")
    return path


def sweep_mcp_directory(directory: Path) -> None:
    """Delete the generated files an earlier launch left: regular, single-link files with a
    generated name, among at most `MAX_SWEEP_ENTRIES` entries. A folder that is not private, or
    whose parent is not (a link, a junction, another owner), is left whole. Logs an errno name
    only; never raises."""
    try:
        if not os.path.lexists(directory):
            return
        try:
            # The parent too: whoever can rename in it could swap the folder after its check.
            protection.require_private_directory(directory.parent)
            protection.require_private_directory(directory)
        except protection.ProtectionError:
            logger.warning("mcp config folder is not private; nothing swept")
            return
        with os.scandir(directory) as entries:
            names = [entry.name for entry in itertools.islice(entries, MAX_SWEEP_ENTRIES)]
    except OSError as exc:
        _log_os_error(exc)
        return
    for name in names:
        if not _GENERATED_NAME_RE.fullmatch(name):
            continue
        path = directory / name
        try:
            # os.lstat, not DirEntry.stat: on Windows the entry's st_nlink is always zero.
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                os.unlink(path)
        except OSError as exc:
            _log_os_error(exc)


def _log_os_error(exc: OSError) -> None:
    logger.warning("mcp config sweep failed: %s", errno.errorcode.get(exc.errno or 0, "unknown"))


def _render_server(server: Mapping[object, object]) -> dict[str, object]:
    if server["transport"] == "stdio":
        return {"command": server["command"], "args": server["args"]}
    return {"type": "http", "url": server["url"]}


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def _check_server(value: object, path: str, names: set[str]) -> None:
    server = _object(value, path)
    _require_keys(server, _SERVER_KEYS, path)

    name = server["name"]
    if not isinstance(name, str):
        raise McpConfigError("invalid_type", f"{path}.name")
    if not _NAME_RE.fullmatch(name):
        raise McpConfigError("invalid_name", f"{path}.name")
    _screen(name, f"{path}.name")
    if name in names:
        raise McpConfigError("duplicate_name", f"{path}.name")
    names.add(name)

    transport = server["transport"]
    if not isinstance(transport, str):
        raise McpConfigError("invalid_type", f"{path}.transport")
    if transport not in _TRANSPORTS:
        raise McpConfigError("invalid_transport", f"{path}.transport")

    command, url = server["command"], server["url"]
    if command is not None:
        _text(command, f"{path}.command", 1, MAX_TEXT_BYTES)
    if url is not None:
        url = _text(url, f"{path}.url", 0, MAX_URL_BYTES)
    if server["note"] is not None:
        _text(server["note"], f"{path}.note", 0, MAX_TEXT_BYTES)
    args = _array(server["args"], f"{path}.args")
    if len(args) > MAX_ARGS:
        raise McpConfigError("invalid_count", f"{path}.args")
    for index, arg in enumerate(args):
        _text(arg, f"{path}.args[{index}]", 0, MAX_TEXT_BYTES)

    if transport == "stdio":
        if command is None:
            raise McpConfigError("invalid_combination", f"{path}.command")
        if url is not None:
            raise McpConfigError("invalid_combination", f"{path}.url")
        return
    if url is None:
        raise McpConfigError("invalid_combination", f"{path}.url")
    if command is not None:
        raise McpConfigError("invalid_combination", f"{path}.command")
    if args:
        raise McpConfigError("invalid_combination", f"{path}.args")
    _check_url(url, f"{path}.url")


def _check_url(url: str, path: str) -> None:
    """https to any host, or http only to this machine; never userinfo, a query or a fragment."""
    if "?" in url or "#" in url:
        raise McpConfigError("invalid_url", path)
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError for a malformed or out-of-range port
    except ValueError as exc:
        raise McpConfigError("invalid_url", path) from exc
    host = parts.hostname
    allowed = (parts.scheme == "https" and bool(host)) or (
        parts.scheme == "http" and host in _LOCAL_HOSTS
    )
    if not allowed or "@" in parts.netloc:
        raise McpConfigError("invalid_url", path)


def _text(value: object, path: str, min_bytes: int, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise McpConfigError("invalid_type", path)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:  # a lone surrogate has no UTF-8 form
        raise McpConfigError("forbidden_text", path) from exc
    if not min_bytes <= size <= max_bytes:
        raise McpConfigError("invalid_length", path)
    _screen(value, path)
    return value


def _screen(text: str, path: str) -> None:
    if _FORBIDDEN_RE.search(text):
        raise McpConfigError("forbidden_text", path)
    if _looks_secret(text):
        raise McpConfigError("secret_value", path)


def _object(value: object, path: str) -> Mapping[object, object]:
    if not isinstance(value, Mapping):
        raise McpConfigError("invalid_type", path)
    return value


def _array(value: object, path: str) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        raise McpConfigError("invalid_type", path)
    return value


def _require_keys(container: Mapping[object, object], keys: tuple[str, ...], path: str) -> None:
    for key in keys:
        if key not in container:
            raise McpConfigError("missing_key", f"{path}.{key}")
    for key in container:
        if key not in keys:
            raise McpConfigError("unknown_key", f"{path}.{key}")
