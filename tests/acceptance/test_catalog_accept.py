"""Acceptance (frozen): the transcript reader. Transcripts are built here in the CLI's shape
(keys as surveyed across the vault's transcripts on 2026-09-11; every text is invented), plus
one scrubbed recorded transcript checked against its history golden."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from viki_agent.catalog import (
    HITS_PER_SESSION,
    PAGE_TURNS,
    PROMPT_LEN,
    SNIPPET_LEN,
    TITLE_LEN,
    Catalog,
    project_dir,
)
from viki_agent.events import history_item

TESTS = Path(__file__).parents[1]
VAULT = Path("C:/Vault")
VSC = "11111111-1111-4111-8111-111111111111"  # VS Code: block prompts, a tool, a rewind
CLI = "22222222-2222-4222-8222-222222222222"  # terminal: a compaction, a custom title
SDK = "33333333-3333-4333-8333-333333333333"  # an SDK script: "Other"
LONG = "44444444-4444-4444-8444-444444444444"  # this app: 45 turns
META = "55555555-5555-4555-8555-555555555555"  # metadata only: not a session
SIDE = "66666666-6666-4666-8666-666666666666"  # a sidechain file: not a session
ELSEWHERE = "77777777-7777-4777-8777-777777777777"  # another project's transcript


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Transcript:
    """A synthetic transcript: entries chain through parentUuid, one second apart."""

    def __init__(self, session_id: str, *, entrypoint: str, start: datetime) -> None:
        self.session_id = session_id
        self.entrypoint = entrypoint
        self.version = "2.1.266"
        self.clock = start
        self.lines: list[dict[str, Any] | str] = []
        self.last: str | None = None
        self.times: dict[str, str] = {}
        self._n = 0

    def entry(
        self, kind: str, *, parent: str | None = None, root: bool = False, **fields: Any
    ) -> str:
        self._n += 1
        self.clock += timedelta(seconds=1)
        uid = f"{self.session_id[:8]}-0000-4000-8000-{self._n:012x}"
        self.times[uid] = iso(self.clock)
        self.lines.append(
            {
                "parentUuid": None if root else (parent or self.last),
                "isSidechain": False,
                "userType": "external",
                "cwd": "C:\\Vault",
                "sessionId": self.session_id,
                "version": self.version,
                "entrypoint": self.entrypoint,
                "type": kind,
                "uuid": uid,
                "timestamp": self.times[uid],
                **fields,
            }
        )
        self.last = uid
        return uid

    def user(self, content: Any, **fields: Any) -> str:
        return self.entry("user", message={"role": "user", "content": content}, **fields)

    def reply(self, text: str, **fields: Any) -> str:
        block = {"type": "text", "text": text}
        message = {"id": f"msg_{self._n + 1:04d}", "role": "assistant", "content": [block]}
        return self.entry("assistant", message=message, **fields)

    def turn(self, prompt: str, answer: str) -> str:
        uid = self.user(prompt)
        self.reply(answer)
        return uid

    def tool(self, name: str, tool_input: dict[str, Any], result: str, structured: Any) -> None:
        tool_id = f"toolu_{self._n + 1:04d}"
        use = {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}
        self.entry("assistant", message={"id": f"msg_{self._n + 1:04d}", "role": "assistant",
                                         "content": [use]})  # fmt: skip
        block = {"type": "tool_result", "tool_use_id": tool_id, "content": result}
        self.user([block], toolUseResult=structured)

    def compact(self, summary: str) -> None:
        self.entry(
            "system",
            root=True,
            subtype="compact_boundary",
            content="Conversation compacted",
            logicalParentUuid=self.last,
            compactMetadata={"trigger": "auto", "preTokens": 180000},
        )
        self.user(summary, isCompactSummary=True)

    def meta(self, kind: str, **fields: Any) -> None:
        """A metadata line (ai-title, custom-title, last-prompt, mode, ...): no uuid, no time."""
        self.lines.append({"type": kind, "sessionId": self.session_id, **fields})

    def write(self, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{self.session_id}.jsonl"
        text = "".join(
            (line if isinstance(line, str) else json.dumps(line, ensure_ascii=False)) + "\n"
            for line in self.lines
        )
        path.write_text(text, encoding="utf-8")
        return path


@dataclass
class World:
    catalog: Catalog
    home: Path
    folder: Path
    vsc: Transcript
    cli: Transcript
    long: Transcript
    turn41: str  # uuid of LONG's 41st prompt


def day(d: int) -> datetime:
    return datetime(2026, 9, d, 9, 0, tzinfo=UTC)


@pytest.fixture
def world(tmp_path: Path) -> World:
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)

    vsc = Transcript(VSC, entrypoint="claude-vscode", start=day(10))
    vsc.entry("attachment", attachment={"type": "hook_success"})
    ide = "<ide_opened_file>The user opened 10-projects/x.md in the IDE.</ide_opened_file>"
    vsc.user(
        [{"type": "text", "text": ide}, {"type": "text", "text": "What does the linter check?"}]
    )
    vsc.reply("常駐してリンク切れを探します。")
    vsc.user("Grepで探して")
    vsc.tool("Grep", {"pattern": "patternonly", "path": "40-sources"}, "secretword in 7 files",
             {"numFiles": 7, "filenames": []})  # fmt: skip
    vsc.user("<task-notification><task-id>t1</task-id></task-notification>")  # injected
    fork_point = vsc.reply("Found it in 7 files.")
    vsc.lines.append("{not json")
    vsc.user("古い質問")
    vsc.reply("Old answer")
    vsc.version = "2.1.267"
    vsc.user("新しい質問", parent=fork_point)  # the user rewound and asked again
    vsc.reply("New answer")
    vsc.meta("ai-title", aiTitle="Linter checks worth adopting")
    vsc.meta("last-prompt", lastPrompt="新しい質問", leafUuid=vsc.last)
    vsc.write(folder)

    cli = Transcript(CLI, entrypoint="cli", start=day(9))
    cli.turn("圧縮前の話", "圧縮前の答え")
    cli.compact("This session is being continued. 圧縮した要約。")
    cli.turn("圧縮後の話", "The answer goes on")
    cli.meta("ai-title", aiTitle="A title the AI chose")
    cli.meta("custom-title", customTitle="圧縮のあるセッション")
    cli.write(folder)

    sdk = Transcript(SDK, entrypoint="sdk-py", start=day(8))
    sdk.turn("linter  " * 20 + "\n" + "linter " * 20, "scripted")
    sdk.write(folder)

    long = Transcript(LONG, entrypoint="viki-agent", start=day(11))
    prompts = [long.turn(f"質問 {n}", f"Answer {n}") for n in range(1, 45)]
    prompts.append(long.turn("質問 45", "あ" * 100 + "目印" + "い" * 100))
    long.write(folder)

    meta = Transcript(META, entrypoint="claude-vscode", start=day(7))
    meta.meta("mode", mode="auto")
    meta.meta("queue-operation", operation="enqueue")
    meta.write(folder)

    side = Transcript(SIDE, entrypoint="claude-vscode", start=day(7))
    side.turn("subagent prompt", "subagent answer")
    first = side.lines[0]
    assert isinstance(first, dict)
    first["isSidechain"] = True
    side.write(folder)

    elsewhere = Transcript(ELSEWHERE, entrypoint="claude-vscode", start=day(11))
    elsewhere.turn("linter elsewhere", "not this vault")
    elsewhere.write(home / "projects" / "C--Elsewhere")

    return World(Catalog(home, VAULT), home, folder, vsc, cli, long, prompts[40])


def prompts_of(events: list[Any]) -> list[str]:
    return [e.text for e in events if getattr(e, "TYPE", "") == "user.message"]


# --- where transcripts live -----------------------------------------------------------------------


def test_the_project_folder_follows_the_cli_naming(tmp_path: Path) -> None:
    home = tmp_path / "home"
    folder = project_dir(home, Path("C:/Users/x/Vault Notes"))
    assert folder == home / "projects" / "C--Users-x-Vault-Notes"


def test_exists_looks_only_in_the_vaults_project(world: World) -> None:
    assert world.catalog.exists(VSC)
    assert not world.catalog.exists(ELSEWHERE)
    assert not world.catalog.exists("88888888-8888-4888-8888-888888888888")


def test_transcript_path_names_a_real_file_and_nothing_a_client_could_steer(world: World) -> None:
    """open.transcript (the row menu's "Open transcript in VS Code") hands a client-supplied id
    straight to this. The id must therefore be the only thing it trusts, and the UUID guard the
    only reason a "../.." can never reach the filesystem."""
    found = world.catalog.transcript_path(VSC)
    assert found is not None
    assert found.is_file()
    assert found.name == f"{VSC}.jsonl"
    assert world.catalog.transcript_path(ELSEWHERE) is None
    for hostile in ("../../../../Windows/System32/drivers/etc/hosts", "..", "", f"{VSC}/.."):
        assert world.catalog.transcript_path(hostile) is None


# --- history --------------------------------------------------------------------------------------


def test_pages_walk_back_to_the_start_in_whole_turns(world: World) -> None:
    pages = []
    before: str | None = None
    while True:
        page = world.catalog.history(LONG, before_uuid=before)
        pages.append(page)
        if page.next_before_uuid is None:
            break
        before = page.next_before_uuid
    assert [len(prompts_of(p.events)) for p in pages] == [PAGE_TURNS, PAGE_TURNS, 5]
    everything = [text for page in reversed(pages) for text in prompts_of(page.events)]
    assert everything == [f"質問 {n}" for n in range(1, 46)]
    assert [e.TYPE for e in pages[0].events][:2] == ["user.message", "text.done"]


def test_a_rewound_branch_and_injected_entries_are_not_history(world: World) -> None:
    page = world.catalog.history(VSC)
    assert prompts_of(page.events) == ["What does the linter check?", "Grepで探して", "新しい質問"]
    assert [e.TYPE for e in page.events if e.TYPE.startswith("tool.")] == [
        "tool.started",
        "tool.done",
    ]
    texts = [getattr(e, "text", "") for e in page.events]
    assert "Old answer" not in texts and "New answer" in texts
    assert page.next_before_uuid is None


def test_history_reaches_back_across_a_compaction(world: World) -> None:
    items = [history_item(e) for e in world.catalog.history(CLI).events]
    assert [(i["type"], i["data"].get("text")) for i in items] == [
        ("user.message", "圧縮前の話"),
        ("text.done", "圧縮前の答え"),
        ("compacted", None),
        ("user.message", "圧縮後の話"),
        ("text.done", "The answer goes on"),
    ]
    assert items[2]["data"] == {"trigger": "auto", "pre_tokens": 180000}


def test_history_stops_where_the_live_log_begins(world: World) -> None:
    until = world.long.times[world.turn41]
    page = world.catalog.history(LONG, until=until)
    assert prompts_of(page.events) == [f"質問 {n}" for n in range(21, 41)]
    older = world.catalog.history(LONG, before_uuid=page.next_before_uuid, until=until)
    assert prompts_of(older.events) == [f"質問 {n}" for n in range(1, 21)]
    assert older.next_before_uuid is None


def test_unknown_starting_points_give_an_empty_page(world: World) -> None:
    for page in (
        world.catalog.history(LONG, before_uuid="99999999-9999-4999-8999-999999999999"),
        world.catalog.history("88888888-8888-4888-8888-888888888888"),
    ):
        assert page.events == [] and page.next_before_uuid is None


def test_a_real_transcript_maps_as_its_history_golden(tmp_path: Path) -> None:
    home, vault = tmp_path / "home", Path("%VAULT%")  # the scrubbed fixtures' vault placeholder
    for name in ("bash-hook-block", "subagent-explore"):
        folder = project_dir(home, vault)
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(
            TESTS / "fixtures" / "transcripts" / f"{name}.jsonl", folder / f"{VSC}.jsonl"
        )
        golden = json.loads((TESTS / "contracts" / "events" / f"history-{name}.json").read_text(
            encoding="utf-8"))  # fmt: skip
        page = Catalog(home, vault).history(VSC)
        assert [history_item(e) for e in page.events] == golden["events"], name


def test_a_grown_transcript_is_read_again(world: World) -> None:
    assert prompts_of(world.catalog.history(CLI).events)[-1] == "圧縮後の話"
    world.cli.turn("One more", "Yes")
    world.cli.write(world.folder)
    assert prompts_of(world.catalog.history(CLI).events)[-1] == "One more"


# --- the sessions list ----------------------------------------------------------------------------


def test_sessions_are_listed_newest_first_with_their_titles(world: World) -> None:
    rows = {row.session_id: row for row in world.catalog.sessions()}
    assert list(rows) == [LONG, VSC, CLI, SDK]  # META (no prompt) and SIDE (sidechain) skipped
    vsc = rows[VSC]
    assert (vsc.title, vsc.first_prompt) == (
        "Linter checks worth adopting",
        "What does the linter check?",
    )
    assert (vsc.entrypoint, vsc.other, vsc.cli_version) == ("claude-vscode", False, "2.1.267")
    assert vsc.modified == world.vsc.times[world.vsc.last or ""]
    assert rows[CLI].title == "圧縮のあるセッション"  # a custom title beats the AI title
    assert (rows[LONG].title, rows[LONG].entrypoint) == ("質問 1", "viki-agent")
    sdk = rows[SDK]
    assert sdk.other and sdk.entrypoint == "sdk-py"
    assert len(sdk.title) == TITLE_LEN and sdk.title.endswith("…") and "\n" not in sdk.title
    assert "  " not in sdk.first_prompt and len(sdk.first_prompt) == PROMPT_LEN


# --- search ---------------------------------------------------------------------------------------


def hits(world: World, query: str) -> list[tuple[str, str, str]]:
    return [(h.session_id, h.role, h.snippet) for h in world.catalog.search(query)]


def test_search_matches_english_case_insensitively_in_titles_and_prompts(world: World) -> None:
    assert hits(world, "LINTER") == [
        (VSC, "title", "Linter checks worth adopting"),
        (VSC, "user", "What does the linter check?"),
    ]  # SDK is "Other" and ELSEWHERE is another project: neither is searched


def test_search_finds_japanese_and_normalises_the_query(world: World) -> None:
    (found,) = world.catalog.search("常駐")
    assert (found.session_id, found.role, found.title) == (
        VSC,
        "assistant",
        "Linter checks worth adopting",
    )
    assert "常駐" in found.snippet and found.at.startswith("2026-09-10T")
    assert hits(world, "ＧＲＥＰで") == [(VSC, "user", "Grepで探して")]  # NFKC, then ASCII folding


def test_search_ignores_tool_io_short_queries_and_summaries(world: World) -> None:
    for query in ("secretword", "patternonly", "x", "  ", "要約"):
        assert hits(world, query) == [], query


def test_search_caps_hits_per_session_and_puts_newer_sessions_first(world: World) -> None:
    assert hits(world, "圧縮") == [
        (CLI, "title", "圧縮のあるセッション"),
        (CLI, "user", "圧縮前の話"),
        (CLI, "assistant", "圧縮前の答え"),
    ]
    assert HITS_PER_SESSION == 3
    found = hits(world, "質問")
    assert found[:3] == [
        (LONG, "title", "質問 1"),
        (LONG, "user", "質問 1"),
        (LONG, "user", "質問 2"),
    ]
    assert {session for session, _, _ in found[3:]} == {VSC}


def test_search_finds_a_phrase_the_owner_wrote_across_two_lines(tmp_path: Path) -> None:
    """The prefilter reads the raw JSON line; the matcher reads the same text with its
    whitespace collapsed. A query with a space in it lands on both sides of that difference, so
    it is the one shape that can match downstream and still be thrown away up front."""
    home = tmp_path / "home"
    folder = project_dir(home, VAULT)
    written = Transcript(VSC, entrypoint="claude-vscode", start=day(10))
    written.user("牛乳を買う\n卵も買う")
    written.reply("Bullet one\nand bullet two")
    written.write(folder)
    catalog = Catalog(home, VAULT)

    # Both sides of the break, in one query, in each language. The title hit rides along
    # because an untitled session is titled by its first prompt, which is this very message.
    assert [(h.role, h.snippet) for h in catalog.search("買う 卵も")] == [
        ("title", "牛乳を買う 卵も買う"),
        ("user", "牛乳を買う 卵も買う"),
    ]
    assert [(h.role, h.snippet) for h in catalog.search("one and")] == [
        ("assistant", "Bullet one and bullet two"),
    ]
    # And the prefilter still narrows: a query whose longest word is absent finds nothing.
    assert catalog.search("牛乳 チーズケーキ") == []


def test_a_snippet_is_one_short_line_around_the_match(world: World) -> None:
    (found,) = world.catalog.search("目印")
    assert "目印" in found.snippet and len(found.snippet) <= SNIPPET_LEN
    assert found.snippet.startswith("…") and found.snippet.endswith("…")


# --- read-only ------------------------------------------------------------------------------------


def test_the_reader_never_writes(world: World) -> None:
    def snapshot() -> dict[Path, tuple[int, int]]:
        return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in world.home.rglob("*")}

    before = snapshot()
    world.catalog.sessions()
    world.catalog.search("linter")
    world.catalog.history(VSC)
    world.catalog.exists(CLI)
    assert snapshot() == before
