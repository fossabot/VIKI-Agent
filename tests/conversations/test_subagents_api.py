"""The subagent transcript route: a run is found only through the meta file naming its tool_use
id, its own lines are mapped, its reads and its answer are bounded, refusals come in order, and
nothing is written or logged."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import queue
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.test_server import BEARER, running
from viki_agent.catalog import Catalog, project_dir
from viki_agent.conversations import subagents_api
from viki_agent.conversations.subagents_api import MAX_META_BYTES, MAX_REQUESTS, subagent_routes
from viki_agent.drivers.claude.mapper import Mapper

TOKEN = "route-token-7f3a"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ORIGIN = "http://127.0.0.1:8765"
HANG_GUARD_S = 5  # a guard on an event that normally fires in milliseconds
# A vault path that is never created: the transcripts folder's name embeds the vault's whole
# path, and pytest's own tmp_path there would push a run's files past Windows' MAX_PATH.
VAULT = Path("C:/Vault")
SID = "aaaaaaaa-1111-4111-8111-111111111111"
FORK = "bbbbbbbb-2222-4222-8222-222222222222"
TOOL = "toolu_01RunA"
WIDTH = 256  # bytes per line, newline included, in the read-bound transcripts


def _ts(n: int) -> str:
    return f"2026-10-08T{n // 3600 % 24:02d}:{n // 60 % 60:02d}:{n % 60:02d}.000Z"


def _user(text: str, n: int, **extra: Any) -> dict[str, Any]:
    message = {"role": "user", "content": text}
    return {"type": "user", "isSidechain": True, "timestamp": _ts(n), "message": message, **extra}


def _assistant(block: dict[str, Any], n: int, **extra: Any) -> dict[str, Any]:
    message: dict[str, Any] = {"id": f"msg_{n:04d}", "role": "assistant", "content": [block]}
    if "usage" in extra:
        message["usage"] = extra.pop("usage")
    return {
        "type": "assistant",
        "isSidechain": True,
        "timestamp": _ts(n),
        "message": message,
        "apiBlockIndex": 0,
        **extra,
    }


def _text(text: str, n: int, **extra: Any) -> dict[str, Any]:
    return _assistant({"type": "text", "text": text}, n, **extra)


def _thinking(n: int, **extra: Any) -> dict[str, Any]:
    return _assistant({"type": "thinking", "thinking": "hmm"}, n, **extra)


def _tool(tool_id: str, name: str, tool_input: dict[str, Any], n: int) -> dict[str, Any]:
    return _assistant({"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}, n)


def _result(tool_id: str, content: str, n: int, *, is_error: bool = False) -> dict[str, Any]:
    block = {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": content,
        "is_error": is_error,
    }
    return {
        "type": "user",
        "isSidechain": True,
        "timestamp": _ts(n),
        "message": {"role": "user", "content": [block]},
    }


def _runs(home: Path, session_id: str = SID) -> Path:
    """The chat's transcript (written once) and its subagents folder."""
    project = project_dir(home, VAULT)
    project.mkdir(parents=True, exist_ok=True)
    main = project / f"{session_id}.jsonl"
    if not main.exists():
        main.write_text(json.dumps(_user("the chat", 0)) + "\n", encoding="utf-8")
    folder = project / session_id / "subagents"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _lines(lines: list[Any]) -> bytes:
    return "".join(
        f"{line if isinstance(line, str) else json.dumps(line)}\n" for line in lines
    ).encode()


def _run(
    home: Path,
    tool_use_id: str,
    lines: list[Any],
    *,
    session_id: str = SID,
    name: str = "a1b2c3",
    under: str = "",
) -> Path:
    """One run: `agent-<name>.meta.json` naming `tool_use_id` and its transcript beside it."""
    folder = _runs(home, session_id) / under
    folder.mkdir(parents=True, exist_ok=True)
    meta = {"agentType": "researcher", "description": "a run", "toolUseId": tool_use_id}
    (folder / f"agent-{name}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    transcript = folder / f"agent-{name}.jsonl"
    transcript.write_bytes(_lines(lines))
    return transcript


@dataclass
class Actor:
    history_source: str


class Sessions:
    def __init__(self, actors: dict[str, Actor]) -> None:
        self.actors = actors
        self.asked: list[str] = []

    def get(self, session_id: str) -> Actor | None:
        self.asked.append(session_id)
        return self.actors.get(session_id)


class Gated:
    """A Catalog whose transcript lookups wait while `gate` is clear, announcing each wait."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.gate = threading.Event()
        self.gate.set()
        self.waiting: queue.Queue[str] = queue.Queue()

    def transcript_path(self, session_id: str) -> Path | None:
        if not self.gate.is_set():
            self.waiting.put(session_id)
            self.gate.wait(HANG_GUARD_S)
        return self.catalog.transcript_path(session_id)

    def root_for(self, session_id: str) -> Path:
        return self.catalog.root_for(session_id)


def _app(
    transcripts: Catalog | Gated,
    sessions: Sessions | None = None,
    workers: set[asyncio.Task[Any]] | None = None,
) -> Starlette:
    routes = subagent_routes(
        transcripts,
        Sessions({}) if sessions is None else sessions,
        authorized=lambda request: request.headers.get("authorization") == f"Bearer {TOKEN}",
        origins=frozenset({ORIGIN}),
        workers=set() if workers is None else workers,
    )
    return Starlette(routes=routes)


def _path(tool_use_id: str, session_id: str = SID) -> str:
    return f"/api/v2/sessions/{session_id}/subagents/{tool_use_id}"


def _size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")))


def _units(value: str) -> int:
    """`value`'s length as the browser counts it, in UTF-16 units."""
    return len(value.encode("utf-16-le", "surrogatepass")) // 2


def _taken(body: dict[str, Any]) -> bool:
    """Whether the browser's shape check (frontend/src/clients/conversations/subagentsApi.ts)
    takes `body`'s values: at most 1,000 lines; the prompt and a line's text within 65,536 UTF-16
    units, the start within 64 and every other string within 4,096; `tokens` null or an integer a
    browser's number holds."""
    strings = [(key, value) for line in body["lines"] for key, value in line.items()]
    tokens = body["tokens"]
    return (
        len(body["lines"]) <= 1_000
        and _units(body["prompt"]) <= 65_536
        and _units(body["started_at"]) <= 64
        and all(
            _units(value) <= (65_536 if key == "text" else 4_096)
            for key, value in strings
            if isinstance(value, str)
        )
        and (tokens is None or 0 <= tokens <= 2**53 - 1)
    )


def _tree(root: Path) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.is_file() else None for path in root.rglob("*")}


def _link_dir(target: Path, link: Path) -> None:
    """A directory link: a junction on Windows, where a symlink needs a privilege."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_a_run_answers_its_task_lines_start_and_tokens(tmp_path: Path) -> None:
    home = tmp_path / "home"
    newest = {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_creation_input_tokens": 3,
        "cache_read_input_tokens": True,  # not a count, so not in the total
    }
    _run(
        home,
        TOOL,
        [
            _user("Find the daily template", 0),
            _thinking(1),
            _text("Looking.", 2, usage={"input_tokens": 1, "output_tokens": 1}),
            _tool("toolu_read", "Read", {"file_path": "C:/Vault/notes/a.md"}, 3),
            _result("toolu_read", "a", 4),
            _tool("toolu_bash", "Bash", {"command": "ls missing"}, 5),
            _result("toolu_bash", "Exit code 2\nls: missing: No such file", 6, is_error=True),
            _tool("toolu_nested", "Agent", {"subagent_type": "Explore", "description": "Look"}, 7),
            _result("toolu_nested", "found it", 8),
            _tool("toolu_open", "Agent", {"subagent_type": "researcher", "description": "Web"}, 9),
            _user("Also check the weekly one", 10),
            _tool("toolu_grep", "Grep", {"pattern": "TODO"}, 11),
            _text("Still looking.", 12, usage=newest),
            # Newer usages that are not counts leave the last real one standing.
            _thinking(13, usage={"input_tokens": False, "output_tokens": 5}),
            _thinking(14, usage={"input_tokens": 7, "output_tokens": -1}),
        ],
    )
    before = _tree(home)
    with TestClient(_app(Catalog(home, VAULT))) as client:
        response = client.get(_path(TOOL), headers=AUTH)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "prompt": "Find the daily template",
        "lines": [
            {"kind": "text", "id": "msg_0002:0", "text": "Looking."},
            {
                "kind": "tool",
                "id": "toolu_read",
                "head": "Read",
                "rest": "notes/a.md",  # relative to the chat's folder
                "tail": "",
                "done": True,
                "error": False,
            },
            {
                "kind": "tool",
                "id": "toolu_bash",
                "head": "ls",
                "rest": "missing",
                "tail": "→ exit 2",
                "done": True,
                "error": True,
            },
            {
                "kind": "agent",
                "id": "toolu_nested",
                "agent": "Explore",
                "description": "Look",
                "status": "completed",
            },
            {
                "kind": "agent",
                "id": "toolu_open",
                "agent": "researcher",
                "description": "Web",
                "status": "running",
            },
            {"kind": "message", "id": "m1", "text": "Also check the weekly one"},
            {
                "kind": "tool",
                "id": "toolu_grep",
                "head": "Grep",
                "rest": "TODO",
                "tail": "",
                "done": False,
                "error": False,
            },
            {"kind": "text", "id": "msg_0012:0", "text": "Still looking."},
        ],
        "omitted": 0,
        "partial": False,
        "started_at": "2026-10-08T00:00:00.000Z",
        "tokens": 123,
    }
    assert _tree(home) == before  # read-only


def test_only_the_meta_naming_the_tool_use_id_maps_the_run(tmp_path: Path) -> None:
    home = tmp_path / "home"
    folder = _runs(home)

    def meta(name: str, raw: str, *, transcript: bool = True) -> None:
        (folder / f"{name}.meta.json").write_text(raw, encoding="utf-8")
        if transcript:
            (folder / f"{name}.jsonl").write_bytes(_lines([_user(name, 0)]))

    def padded(tool_use_id: str, size: int) -> str:
        raw = json.dumps({"toolUseId": tool_use_id, "pad": ""})
        return raw.replace('""', '"' + "x" * (size - len(raw)) + '"')

    meta("agent-good", json.dumps({"toolUseId": "toolu_good"}))
    meta("agent-fits", padded("toolu_fits", MAX_META_BYTES))
    meta("agent-big", padded("toolu_big", MAX_META_BYTES + 1))
    meta("agent-bad", '{"toolUseId": "toolu_bad"')
    meta("agent-list", json.dumps(["toolUseId", "toolu_list"]))
    meta("agent-none", json.dumps({"agentId": "toolu_none"}))
    meta("agent-int", json.dumps({"toolUseId": 12345}))
    meta("agent-lost", json.dumps({"toolUseId": "toolu_lost"}), transcript=False)
    meta("run-x", json.dumps({"toolUseId": "toolu_name"}))  # not a run's meta file by name
    # A transcript name that links to a folder elsewhere is no transcript, not a failed read.
    meta("agent-dir", json.dumps({"toolUseId": "toolu_dir"}), transcript=False)
    (tmp_path / "elsewhere").mkdir()
    _link_dir(tmp_path / "elsewhere", folder / "agent-dir.jsonl")
    assert len((folder / "agent-fits.meta.json").read_bytes()) == MAX_META_BYTES
    with TestClient(_app(Catalog(home, VAULT))) as client:
        for tool_use_id, prompt in (("toolu_good", "agent-good"), ("toolu_fits", "agent-fits")):
            response = client.get(_path(tool_use_id), headers=AUTH)
            assert response.status_code == 200 and response.json()["prompt"] == prompt
        for tool_use_id in (
            "toolu_big",
            "toolu_bad",
            "toolu_list",
            "toolu_none",
            "12345",
            "toolu_lost",
            "toolu_dir",
            "toolu_name",
            "TOOLU_GOOD",
            "toolu_unknown",
        ):
            response = client.get(_path(tool_use_id), headers=AUTH)
            assert (response.status_code, response.json()["error"]["code"]) == (
                404,
                "not_found",
            ), tool_use_id
            assert response.headers["cache-control"] == "no-store"


def test_a_link_out_of_the_folder_is_not_followed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    folder, outside = _runs(home), tmp_path / "outside"
    store = folder / "store"
    outside.mkdir()
    store.mkdir()
    # A meta file that links out, beside a transcript inside.
    (outside / "out.json").write_text('{"toolUseId": "toolu_out"}', encoding="utf-8")
    (folder / "agent-out.jsonl").write_bytes(_lines([_user("inside", 0)]))
    # A meta file inside, beside a transcript that links out.
    (folder / "agent-in.meta.json").write_text('{"toolUseId": "toolu_in"}', encoding="utf-8")
    (outside / "in.jsonl").write_bytes(_lines([_user("outside", 0)]))
    # Links that stay inside; store/ holds no run file by name, so only the link names the id.
    (store / "real.json").write_text('{"toolUseId": "toolu_alias"}', encoding="utf-8")
    (store / "real.jsonl").write_bytes(_lines([_user("inside", 0)]))
    try:
        (folder / "agent-out.meta.json").symlink_to(outside / "out.json")
        (folder / "agent-in.jsonl").symlink_to(outside / "in.jsonl")
        (folder / "agent-alias.meta.json").symlink_to(store / "real.json")
        (folder / "agent-alias.jsonl").symlink_to(store / "real.jsonl")
    except OSError:
        pytest.skip("this account may not make file links (Windows needs a privilege)")
    with TestClient(_app(Catalog(home, VAULT))) as client:
        assert client.get(_path("toolu_out"), headers=AUTH).status_code == 404
        assert client.get(_path("toolu_in"), headers=AUTH).status_code == 404
        inside = client.get(_path("toolu_alias"), headers=AUTH)
        assert inside.status_code == 200 and inside.json()["prompt"] == "inside"


def test_a_meta_or_transcript_that_resolves_outside_the_folder_is_refused_however_it_got_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """File links need a privilege on Windows, so test_a_link_out_of_the_folder_is_not_followed
    skips itself there. This fakes a link's effect on Path.resolve for two chosen paths instead,
    so it runs on every platform and still pins _inside's own containment check."""
    home = tmp_path / "home"
    folder, outside = _runs(home), tmp_path / "outside"
    outside.mkdir()
    # A meta file inside the folder, as if linked out: _inside must refuse it before _names ever
    # opens it, keeping its matching tool_use_id from reaching the plain transcript beside it.
    (folder / "agent-out.meta.json").write_text(
        json.dumps({"toolUseId": "toolu_out"}), encoding="utf-8"
    )
    (outside / "out.json").write_text(json.dumps({"toolUseId": "toolu_out"}), encoding="utf-8")
    (folder / "agent-out.jsonl").write_bytes(_lines([_user("leaked via meta", 0)]))
    # A meta file plainly inside names the run, but its transcript is as if linked out: _inside
    # must refuse the transcript even though the meta file matched cleanly.
    (folder / "agent-in.meta.json").write_text(
        json.dumps({"toolUseId": "toolu_in"}), encoding="utf-8"
    )
    (folder / "agent-in.jsonl").write_bytes(_lines([_user("leaked via transcript", 0)]))
    (outside / "in.jsonl").write_bytes(_lines([_user("outside", 0)]))
    # A control run whose meta file and transcript both resolve normally.
    _run(home, "toolu_ok", [_user("ok", 0)], name="ok")

    original = Path.resolve
    escapes = {
        original(folder / "agent-out.meta.json"): outside / "out.json",
        original(folder / "agent-in.jsonl"): outside / "in.jsonl",
    }

    def fake(path: Path, strict: bool = False) -> Path:
        real = original(path, strict)
        return escapes.get(real, real)

    monkeypatch.setattr(Path, "resolve", fake)
    with TestClient(_app(Catalog(home, VAULT))) as client:
        assert client.get(_path("toolu_out"), headers=AUTH).status_code == 404
        assert client.get(_path("toolu_in"), headers=AUTH).status_code == 404
        control = client.get(_path("toolu_ok"), headers=AUTH)
    assert control.status_code == 200 and control.json()["prompt"] == "ok"


@pytest.mark.parametrize("alias", ["file", "directory"])
def test_a_cached_run_rechecks_containment_after_an_alias_replaces_its_path(
    tmp_path: Path, alias: str
) -> None:
    home, outside = tmp_path / "home", tmp_path / "outside"
    transcript = _run(home, TOOL, [_user("inside", 0)], under="nested")
    outside.mkdir()
    replacement = outside / transcript.name
    replacement.write_bytes(_lines([_user("synthetic-outside-transcript", 0)]))
    with TestClient(_app(Catalog(home, VAULT))) as client:
        first = client.get(_path(TOOL), headers=AUTH)
        assert first.status_code == 200 and first.json()["prompt"] == "inside"
        if alias == "directory":
            directory = transcript.parent
            directory.rename(tmp_path / "retired")
            _link_dir(outside, directory)
        else:
            transcript.unlink()
            try:
                transcript.symlink_to(replacement)
            except OSError:
                pytest.skip("this account may not make file links (Windows needs a privilege)")
        response = client.get(_path(TOOL), headers=AUTH)
    assert response.status_code == 404
    assert "synthetic-outside-transcript" not in response.text


@pytest.mark.parametrize("level", ["session", "subagents"])
@pytest.mark.parametrize("cached", [False, True])
def test_a_redirected_subagent_boundary_does_not_rebase_containment(
    tmp_path: Path, level: str, cached: bool
) -> None:
    home, outside = tmp_path / "home", tmp_path / "outside"
    transcript = _run(home, TOOL, [_user("inside", 0)])
    outside_folder = outside / "subagents" if level == "session" else outside
    outside_folder.mkdir(parents=True)
    (outside_folder / transcript.name).write_bytes(_lines([_user("outside", 0)]))
    if not cached:
        # A cold lookup may not admit an outside run even when it has matching metadata.
        meta = transcript.with_suffix(".meta.json")
        (outside_folder / meta.name).write_bytes(meta.read_bytes())
    replaced = transcript.parent.parent if level == "session" else transcript.parent
    with TestClient(_app(Catalog(home, VAULT))) as client:
        if cached:
            assert client.get(_path(TOOL), headers=AUTH).json()["prompt"] == "inside"
        replaced.rename(tmp_path / "retired")
        _link_dir(outside, replaced)
        try:
            response = client.get(_path(TOOL), headers=AUTH)
        finally:
            if sys.platform == "win32":
                replaced.rmdir()
            else:
                replaced.unlink()
    assert response.status_code == 404
    assert "outside" not in response.text


@pytest.mark.parametrize("level", ["home", "project"])
def test_a_provider_parent_alias_keeps_its_subagent_runs(tmp_path: Path, level: str) -> None:
    home = tmp_path / "home"
    transcript = _run(home, TOOL, [_user("inside", 0)])
    if level == "home":
        linked = tmp_path / "linked-home"
        _link_dir(home, linked)
        selected_home = linked
    else:
        linked = transcript.parent.parent.parent
        target = tmp_path / "transcripts"
        linked.rename(target)
        _link_dir(target, linked)
        selected_home = home
    try:
        with TestClient(_app(Catalog(selected_home, VAULT))) as client:
            for _ in range(2):  # cold and remembered, with the same accepted parent alias
                response = client.get(_path(TOOL), headers=AUTH)
                assert response.status_code == 200 and response.json()["prompt"] == "inside"
    finally:
        if sys.platform == "win32":
            linked.rmdir()
        else:
            linked.unlink()


def test_in_folder_file_alias_resolution_remains_admitted_without_symlink_privilege(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    transcript = _run(home, TOOL, [_user("inside", 0)])
    meta = transcript.with_suffix(".meta.json")
    store = transcript.parent / "store"
    store.mkdir()
    (store / "real.jsonl").write_bytes(transcript.read_bytes())
    (store / "real.json").write_bytes(meta.read_bytes())
    original = Path.resolve
    aliases = {original(transcript): store / "real.jsonl", original(meta): store / "real.json"}

    def resolve(path: Path, strict: bool = False) -> Path:
        real = original(path, strict)
        return aliases.get(real, real)

    monkeypatch.setattr(Path, "resolve", resolve)
    with TestClient(_app(Catalog(home, VAULT))) as client:
        for _ in range(2):
            response = client.get(_path(TOOL), headers=AUTH)
            assert response.status_code == 200 and response.json()["prompt"] == "inside"


def test_a_fork_finds_runs_in_its_history_source(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _run(home, "toolu_source", [_user("from the source", 0)])
    forked = Sessions({FORK: Actor(history_source=SID)})
    with TestClient(_app(Catalog(home, VAULT), forked)) as client:
        # The fork's own transcript appears late; until then only its source is read.
        response = client.get(_path("toolu_source", FORK), headers=AUTH)
        assert response.status_code == 200 and response.json()["prompt"] == "from the source"
        _run(home, "toolu_fork", [_user("from the fork", 0)], session_id=FORK)
        for tool_use_id, prompt in (
            ("toolu_fork", "from the fork"),
            ("toolu_source", "from the source"),
        ):
            response = client.get(_path(tool_use_id, FORK), headers=AUTH)
            assert response.status_code == 200 and response.json()["prompt"] == prompt
    # Without its actor a chat is only itself: the source is not read.
    with TestClient(_app(Catalog(home, VAULT))) as client:
        assert client.get(_path("toolu_source", FORK), headers=AUTH).status_code == 404


def test_a_forks_polls_for_a_run_of_its_source_walk_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A miss is not remembered, so a fork's poll for a run of its source, looking in the fork's
    own folder first, read every meta file there again on each poll. Every folder's remembered
    run now comes before any walk."""
    home = tmp_path / "home"
    _run(home, "toolu_source", [_user("from the source", 0)])
    for n in range(3):
        _run(home, f"toolu_fork{n}", [_user("from the fork", 0)], session_id=FORK, name=f"f{n}")
    reads: list[int] = []
    names = subagents_api._names

    def counted(meta: Path, tool_use_id: str) -> bool:
        reads[-1] += 1
        return names(meta, tool_use_id)

    monkeypatch.setattr(subagents_api, "_names", counted)
    forked = Sessions({FORK: Actor(history_source=SID)})
    with TestClient(_app(Catalog(home, VAULT), forked)) as client:
        for _ in range(2):
            reads.append(0)
            response = client.get(_path("toolu_source", FORK), headers=AUTH)
            assert response.status_code == 200 and response.json()["prompt"] == "from the source"
    assert reads == [4, 0]  # the fork's three meta files and the source's one, then none


def _sized(n: int) -> str:
    """Entry `n` as a line of WIDTH - 1 bytes: the task when `n` is 0, else a text `t<n>`."""

    def entry(pad: str) -> str:
        return json.dumps(_user(f"task{pad}", 0) if n == 0 else _text(f"t{n:04d}{pad}", n))

    return entry("." * (WIDTH - 1 - len(entry(""))))


def _ids(body: dict[str, Any]) -> list[str]:
    return [line["text"][:5] for line in body["lines"]]


def test_reads_and_the_answer_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    for count, tool_use_id in ((30, "toolu_gap"), (12, "toolu_adjacent"), (10, "toolu_whole")):
        _run(home, tool_use_id, [_sized(n) for n in range(count)], name=tool_use_id)
    _run(home, "toolu_many", [_user("task", 0)] + [_text(f"t{n:04d}", n) for n in range(1, 1006)])
    with TestClient(_app(Catalog(home, VAULT))) as client:

        def get(tool_use_id: str) -> dict[str, Any]:
            response = client.get(_path(tool_use_id), headers=AUTH)
            assert response.status_code == 200, tool_use_id
            return response.json()

        with monkeypatch.context() as patch:
            patch.setattr(subagents_api, "HEAD_BYTES", 6 * WIDTH + 50)
            patch.setattr(subagents_api, "TAIL_BYTES", 10 * WIDTH + 50)
            # 30 lines: the six whole lines of the head, then the ten that begin in the tail
            # window (its partial first line is dropped); the middle is not read.
            gap = get("toolu_gap")
            assert gap["partial"] and gap["prompt"].startswith("task") and gap["omitted"] == 0
            assert gap["started_at"] == _ts(0)
            assert _ids(gap) == [f"t{n:04d}" for n in [*range(1, 6), *range(20, 30)]]
            # 12 lines: the windows meet, and no line is mapped twice.
            adjacent = get("toolu_adjacent")
            assert adjacent["partial"] and _ids(adjacent) == [f"t{n:04d}" for n in range(1, 12)]
            patch.setattr(subagents_api, "TAIL_BYTES", 10 * WIDTH)
            whole = get("toolu_whole")  # exactly TAIL_BYTES: read whole
            assert not whole["partial"] and _ids(whole) == [f"t{n:04d}" for n in range(1, 10)]

        # The newest MAX_LINES lines, the rest counted.
        many = get("toolu_many")
        assert _ids(many) == [f"t{n:04d}" for n in range(6, 1006)] and many["omitted"] == 5
        with monkeypatch.context() as patch:
            patch.setattr(subagents_api, "MAX_ANSWER_BYTES", 3000)
            response = client.get(_path("toolu_many"), headers=AUTH)
            fitted = response.json()
            kept = len(fitted["lines"])
            assert len(response.content) <= 3000 and 0 < kept < 1000
            assert fitted["lines"] == many["lines"][-kept:] and fitted["omitted"] == 1005 - kept
            # The newest lines that fit, and no fewer: one more would not.
            wider = {**fitted, "lines": many["lines"][-kept - 1 :], "omitted": 1004 - kept}
            assert _size(wider) > 3000

        # A walk stops past MAX_WALK_ENTRIES entries. This chat's folder holds five files and
        # workflows/ (6), workflows/ holds w1/ (7), and w1/ holds the run's two files (9).
        deep = _run(home, "toolu_deep", [_user("deep", 0)], session_id=FORK, under="workflows/w1")
        for n in range(5):
            (_runs(home, FORK) / f"x{n}.txt").write_text("x", encoding="utf-8")

        def deep_prompt() -> str | int:
            response = client.get(_path("toolu_deep", FORK), headers=AUTH)
            return (
                response.json()["prompt"] if response.status_code == 200 else response.status_code
            )

        with monkeypatch.context() as patch:
            patch.setattr(subagents_api, "MAX_WALK_ENTRIES", 8)
            assert deep_prompt() == 404
            patch.setattr(subagents_api, "MAX_WALK_ENTRIES", 9)
            assert deep_prompt() == "deep"
            patch.setattr(subagents_api, "MAX_WALK_ENTRIES", 8)
            assert deep_prompt() == "deep"  # found once, served from memory while it is a file
            deep.unlink()
            deep.with_name(deep.name.replace(".jsonl", ".meta.json")).unlink()
            # The run moves to the top, the eight entries a walk may still read: walked again.
            _run(home, "toolu_deep", [_user("moved", 0)], session_id=FORK, name="moved")
            assert deep_prompt() == "moved"


def test_what_the_head_launched_closes_in_the_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run past TAIL_BYTES is read as a head and a body. With a mapper for each, the body's
    knew nothing the head launched: a nested agent finished there read as running, and a Bash
    tool lost its tail, its name being unknown."""
    home = tmp_path / "home"
    _run(
        home,
        TOOL,
        [
            _user("task", 0),
            _tool("toolu_nested", "Agent", {"subagent_type": "Explore", "description": "Look"}, 1),
            _tool("toolu_bash", "Bash", {"command": "ls missing"}, 2),
            *[_text(f"filler {n} {'x' * 200}", 3 + n) for n in range(40)],
            _result("toolu_nested", "found it", 43),
            _result("toolu_bash", "Exit code 2\nls: missing", 44, is_error=True),
        ],
    )
    monkeypatch.setattr(subagents_api, "HEAD_BYTES", 1_500)
    monkeypatch.setattr(subagents_api, "TAIL_BYTES", 2_000)
    with TestClient(_app(Catalog(home, VAULT))) as client:
        body = client.get(_path(TOOL), headers=AUTH).json()
    assert body["partial"] and body["prompt"] == "task"
    assert [line for line in body["lines"] if line["kind"] != "text"] == [
        {
            "kind": "agent",
            "id": "toolu_nested",
            "agent": "Explore",
            "description": "Look",
            "status": "completed",
        },
        {
            "kind": "tool",
            "id": "toolu_bash",
            "head": "ls",
            "rest": "missing",
            "tail": "→ exit 2",
            "done": True,
            "error": True,
        },
    ]
    # The middle was not read: the body holds only the newest fillers.
    assert "filler 20 " not in json.dumps(body)


def test_a_tail_past_the_browsers_bound_is_cut_to_it(tmp_path: Path) -> None:
    """A Bash tail carries the command's own output: one that prints "Exit code" and 5,000 digits
    made a 5,007-unit tail, and the browser refuses a whole answer over one string past 4,096
    UTF-16 units. An astral digit is two units, so the cut keeps each pair whole."""
    home = tmp_path / "home"
    _run(
        home,
        TOOL,
        [
            _user("task", 0),
            _tool("toolu_digits", "Bash", {"command": "cat notes.txt"}, 1),
            _result("toolu_digits", "Exit code " + "9" * 5_000 + "\nrest", 2),
            _tool("toolu_astral", "PowerShell", {"command": "cat notes.txt"}, 3),
            _result("toolu_astral", "Exit code " + "\U0001d7d7" * 2_100, 4),
        ],
    )
    with TestClient(_app(Catalog(home, VAULT))) as client:
        response = client.get(_path(TOOL), headers=AUTH)
    body = response.json()
    assert response.status_code == 200 and _taken(body)
    tails = [line["tail"] for line in body["lines"]]
    assert tails == [("→ exit " + "9" * 5_000)[:4_096], "→ exit " + "\U0001d7d7" * 2_044]
    assert [_units(tail) for tail in tails] == [4_096, 4_095]


def test_a_string_past_the_browsers_bound_costs_only_its_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ids are the transcript's own text, and one past 4,096 UTF-16 units made the browser refuse
    the whole answer: now it costs its own line, counted in `omitted`. The mapper cuts texts and
    messages short of the browser's 65,536; with that cut off, a longer text or message costs its
    line too, and a longer prompt is cut to the bound."""
    monkeypatch.setattr(subagents_api, "Mapper", functools.partial(Mapper, text_limit=None))
    home = tmp_path / "home"
    long_id = _text("a long message id", 4)
    long_id["message"]["id"] = "m" * 4_095  # 4,097 units with its ":0"
    _run(
        home,
        TOOL,
        [
            _user("\U0001f600" * 32_769, 0),  # 65,538 units
            _text("before", 1),
            _tool("a" * 4_096, "Read", {"file_path": "C:/Vault/a.md"}, 2),
            _tool("b" * 4_097, "Read", {"file_path": "C:/Vault/b.md"}, 3),
            long_id,
            _tool("c" * 4_097, "Agent", {"subagent_type": "Explore", "description": "d"}, 5),
            _text("x" * 65_536, 6),
            _text("y" * 65_537, 7),
            _user("z" * 65_537, 8),
            _text("after", 9),
        ],
    )
    with TestClient(_app(Catalog(home, VAULT))) as client:
        response = client.get(_path(TOOL), headers=AUTH)
    body = response.json()
    assert response.status_code == 200 and _taken(body)
    assert body["prompt"] == "\U0001f600" * 32_768
    assert [(line["kind"], line["id"]) for line in body["lines"]] == [
        ("text", "msg_0001:0"),
        ("tool", "a" * 4_096),
        ("text", "msg_0006:0"),
        ("text", "msg_0009:0"),
    ]
    assert body["omitted"] == 5


def test_the_answer_bound_counts_the_bytes_sent(tmp_path: Path) -> None:
    """The browser refuses a body past 1 MiB of bytes on the wire, and the answer goes out ASCII
    escaped: a Japanese character is six bytes there, so a count of characters would let such an
    answer past the bound."""
    home = tmp_path / "home"
    texts = {n: f"t{n:04d}" + "あ" * 16_000 for n in range(1, 13)}  # about 96 KB each, sent
    _run(home, TOOL, [_user("task", 0)] + [_text(text, n) for n, text in texts.items()])
    lines = [{"kind": "text", "id": f"msg_{n:04d}:0", "text": text} for n, text in texts.items()]
    with TestClient(_app(Catalog(home, VAULT))) as client:
        response = client.get(_path(TOOL), headers=AUTH)
    body = response.json()
    kept = len(body["lines"])
    assert response.status_code == 200 and _size(body) == len(response.content)
    assert len(response.content) <= subagents_api.MAX_ANSWER_BYTES
    assert 0 < kept < 12 and body["lines"] == lines[-kept:] and body["omitted"] == 12 - kept
    # The newest lines that fit, and no fewer: one more would not.
    wider = {**body, "lines": lines[-kept - 1 :], "omitted": 11 - kept}
    assert _size(wider) > subagents_api.MAX_ANSWER_BYTES


def test_the_start_and_the_tokens_are_kept_only_as_the_browser_takes_them(
    tmp_path: Path,
) -> None:
    """The browser takes a start of at most 64 UTF-16 units, and `fromisoformat` takes any one
    character as the separator, so 64 characters can be 65 units. A browser's number holds an
    integer exactly only to 2**53 - 1, reads 10**400 as Infinity, which it refuses, and a total
    past 4,300 digits made `json.dumps` raise. Such a usage is no count."""
    home = tmp_path / "home"
    fits = "2026-10-08\U0001f600" + "00:00:00." + "0" * 43  # 63 characters, 64 units
    over = fits + "0"  # 64 characters, 65 units
    runs: dict[str, tuple[str, dict[str, int]]] = {
        "toolu_fits": (fits, {"input_tokens": 2**53 - 2, "output_tokens": 1}),
        "toolu_over": (over, {"input_tokens": 2**53 - 1, "output_tokens": 1}),
        "toolu_huge": (_ts(0), {"input_tokens": 10**400, "output_tokens": 1}),
        "toolu_digits": (_ts(0), {"input_tokens": 10**4_300 - 1, "output_tokens": 10**4_300 - 1}),
    }
    for tool_use_id, (timestamp, usage) in runs.items():
        _run(
            home,
            tool_use_id,
            [_user("task", 0, timestamp=timestamp), _text("hi", 1, usage=usage)],
            name=tool_use_id,
        )
    bodies: list[dict[str, Any]] = []
    with TestClient(_app(Catalog(home, VAULT))) as client:
        for tool_use_id in runs:
            response = client.get(_path(tool_use_id), headers=AUTH)
            assert response.status_code == 200 and _taken(response.json()), tool_use_id
            bodies.append(response.json())
    assert (len(fits), _units(fits), len(over), _units(over)) == (63, 64, 64, 65)
    assert [(body["started_at"], body["tokens"]) for body in bodies] == [
        (fits, 2**53 - 1),
        ("", None),
        (_ts(0), None),
        (_ts(0), None),
    ]


async def test_refusals(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _run(home, TOOL, [_user("task", 0)])
    gated, sessions = Gated(Catalog(home, VAULT)), Sessions({})
    workers: set[asyncio.Task[Any]] = set()
    foreign = {**AUTH, "Origin": "http://evil.invalid"}
    refused = [
        (_path(TOOL), {}, 401, "unauthorized"),
        (_path("toolu.x") + "?q=1", {"Origin": "http://evil.invalid"}, 401, "unauthorized"),
        (_path(TOOL), {"Authorization": "Bearer other"}, 401, "unauthorized"),
        (_path("toolu.x") + "?q=1", foreign, 403, "forbidden_origin"),
        (_path(TOOL) + "?q=1", AUTH, 400, "invalid_input"),
        (_path(TOOL, SID.upper()), AUTH, 400, "invalid_input"),
        (_path(TOOL, SID.replace("-", "")), AUTH, 400, "invalid_input"),
        (_path(TOOL, "x"), AUTH, 400, "invalid_input"),
        (_path("toolu.x"), AUTH, 400, "invalid_input"),
        (_path("a" * 129), AUTH, 400, "invalid_input"),
        (_path("a" * 128), AUTH, 404, "not_found"),  # the longest id is looked up
    ]
    transport = httpx.ASGITransport(app=_app(gated, sessions, workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for url, headers, status, code in refused:
            response = await client.get(url, headers=headers)
            assert (response.status_code, response.json()["error"]["code"]) == (status, code), url
            assert response.headers["cache-control"] == "no-store"
        for headers in (AUTH, {**AUTH, "Origin": ORIGIN}):  # no Origin, or the app's own
            answer = await client.get(_path(TOOL), headers=headers)
            assert answer.status_code == 200 and answer.headers["cache-control"] == "no-store"
        # Every refusal before the lookup reached no session.
        assert sessions.asked == [SID, SID, SID]

        gated.gate.clear()
        calls = [
            asyncio.create_task(client.get(_path(TOOL), headers=AUTH)) for _ in range(MAX_REQUESTS)
        ]
        for _ in range(MAX_REQUESTS):
            await asyncio.to_thread(gated.waiting.get, timeout=HANG_GUARD_S)
        busy = await client.get(_path(TOOL), headers=AUTH)
        assert (busy.status_code, busy.json()["error"]["code"]) == (429, "overloaded")
        assert busy.headers["cache-control"] == "no-store"
        # A caller that goes away keeps its permit: the shielded read runs on, tracked.
        calls[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await calls[0]
        assert len(workers) == MAX_REQUESTS
        assert (await client.get(_path(TOOL), headers=AUTH)).status_code == 429
        gated.gate.set()
        async with asyncio.timeout(HANG_GUARD_S):
            answers = await asyncio.gather(*calls[1:])
            await asyncio.gather(*list(workers))
        assert [answer.status_code for answer in answers] == [200] * (MAX_REQUESTS - 1)
        await asyncio.sleep(0)  # the done callbacks run on the next loop pass
        assert not workers
        assert (await client.get(_path(TOOL), headers=AUTH)).status_code == 200


def test_off_shape_entries_cost_one_line_and_nothing_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="viki_agent")
    home = tmp_path / "home"
    original = Mapper.entry

    def entry(self: Mapper, entry: dict[str, Any], *, source_revision: str = "") -> list[Any]:
        if entry.get("boom"):
            raise ValueError("a transcript's own words")
        return original(self, entry, source_revision=source_revision)

    monkeypatch.setattr(Mapper, "entry", entry)
    _run(
        home,
        TOOL,
        [
            _user("task", 0, timestamp="yesterday"),
            "{not json",
            "[1, 2]",
            '"a string"',
            _text("kept one", 1),
            _text("lost", 2, boom=True),
            {"type": "assistant", "message": "not an object"},  # the mapper itself raises
            _text("kept two", 3),
        ],
    )
    # An ISO 8601 time that parses, but past 64 characters.
    late = "2026-10-08T01:00:00." + "0" * 50 + "Z"
    _run(home, "toolu_late", [_user("task", 0, timestamp=late)], name="late")
    with TestClient(_app(Catalog(home, VAULT))) as client:
        body = client.get(_path(TOOL), headers=AUTH).json()
        assert client.get(_path("toolu_late"), headers=AUTH).json()["started_at"] == ""
    assert [line["text"] for line in body["lines"]] == ["kept one", "kept two"]
    assert (body["prompt"], body["started_at"], body["omitted"]) == ("task", "", 0)
    assert not caplog.records


def test_the_route_is_registered(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _run(home, TOOL, [_user("task", 0)])
    with running(tmp_path, catalog=Catalog(home, VAULT)) as app:
        response = app.client.get(_path(TOOL), headers=BEARER)
    assert response.status_code == 200 and response.json()["prompt"] == "task"
