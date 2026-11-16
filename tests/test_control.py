"""build_argv is the only source of claude CLI flags; ClaudeControl.send's resolver mapping."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from viki_agent.drivers.base import InventoryDriver
from viki_agent.drivers.claude.control import ClaudeControl, Launch, build_argv
from viki_agent.drivers.claude.transport import ClaudeTransport
from viki_agent.turn_input import Attachment, AttachmentUnavailable, TurnInput

from replay import SID, FakeSpawner, expect

NEW = "22222222-2222-4222-8222-222222222222"
OLD = "33333333-3333-4333-8333-333333333333"
BASE = [
    "claude.exe",
    "-p",
    "--input-format", "stream-json",
    "--output-format", "stream-json",
    "--verbose",
    "--include-partial-messages",
    "--include-hook-events",
    "--thinking-display", "summarized",
    "--permission-prompt-tool", "stdio",
    "--permission-mode", "auto",
    "--effort=low",
]  # fmt: skip
READ_ONLY = ["--tools=Read,Glob,Grep", "--strict-mcp-config"]


def test_new_resume_and_fork_use_app_ids_in_equals_form() -> None:
    assert build_argv(["claude.exe"], Launch(NEW), mode="auto", effort="low") == [
        *BASE,
        f"--session-id={NEW}",
    ]
    assert build_argv(["claude.exe"], Launch(OLD, resume=True), mode="auto", effort="low") == [
        *BASE,
        f"--resume={OLD}",
    ]
    fork = build_argv(["claude.exe"], Launch(NEW, fork_of=OLD), mode="auto", effort="low")
    assert fork == [*BASE, f"--resume={OLD}", "--fork-session", f"--session-id={NEW}"]


def test_model_is_optional_and_last() -> None:
    argv = build_argv(["claude.exe"], Launch(NEW), mode="auto", effort="low", model="sonnet")
    assert argv[-1] == "--model=sonnet"


@pytest.mark.parametrize("mode", ["bypassPermissions", "dontAsk", "", "AUTO"])
def test_unsafe_or_unknown_modes_are_refused(mode: str) -> None:
    with pytest.raises(ValueError):
        build_argv(["claude.exe"], Launch(NEW), mode=mode, effort="low")


def test_unknown_effort_is_refused() -> None:
    with pytest.raises(ValueError):
        build_argv(["claude.exe"], Launch(NEW), mode="auto", effort="turbo")


def test_banned_flags_never_appear() -> None:
    # --mcp-config stays banned here: a launch without a resolved app config never carries it.
    # --tools and --strict-mcp-config too: a launch that is not read-only never carries them.
    banned = (
        "--bare", "--setting-sources", "--system-prompt", "--append-system-prompt",
        "--allowedTools", "--disallowedTools", "--mcp-config", "--add-dir", "--max-turns",
        "--dangerously-skip-permissions", "--permission-prompts", "--tools",
        "--strict-mcp-config", "--agent", "--agents", "--settings",
    )  # fmt: skip
    for launch in (Launch(NEW), Launch(OLD, resume=True), Launch(NEW, fork_of=OLD)):
        for mode in ("default", "manual", "acceptEdits", "auto", "plan"):
            argv = build_argv(["claude.exe"], launch, mode=mode, effort="max", model="opus")
            assert not [a for a in argv if a.split("=")[0] in banned]


def test_an_absolute_mcp_config_is_one_element_before_the_model(tmp_path: Path) -> None:
    path = str(tmp_path / f"{'a' * 64}.json")
    argv = build_argv(
        ["claude.exe"], Launch(NEW, mcp_config=path), mode="auto", effort="low", model="sonnet"
    )
    assert argv == [*BASE, f"--session-id={NEW}", f"--mcp-config={path}", "--model=sonnet"]
    assert [a for a in argv if a.startswith("--mcp-config")] == [f"--mcp-config={path}"]


def test_an_mcp_config_is_last_without_a_model_and_keeps_resume_and_fork(tmp_path: Path) -> None:
    path = str(tmp_path / "config.json")
    element = f"--mcp-config={path}"
    new = build_argv(["claude.exe"], Launch(NEW, mcp_config=path), mode="auto", effort="low")
    assert new == [*BASE, f"--session-id={NEW}", element]
    resume = Launch(OLD, resume=True, mcp_config=path)
    assert build_argv(["claude.exe"], resume, mode="auto", effort="low") == [
        *BASE,
        f"--resume={OLD}",
        element,
    ]
    fork = Launch(NEW, fork_of=OLD, mcp_config=path)
    assert build_argv(["claude.exe"], fork, mode="auto", effort="low") == [
        *BASE,
        f"--resume={OLD}",
        "--fork-session",
        f"--session-id={NEW}",
        element,
    ]


@pytest.mark.parametrize(
    "path",
    [
        "",
        "mcp.json",
        "mcp/config.json",
        "./config.json",
        "C:foo",  # drive-relative on Windows
        "\\foo",  # root-relative on Windows
        '{"mcpServers":{}}',  # a JSON string in place of a path
    ],
)
def test_a_relative_or_empty_mcp_config_is_refused(path: str) -> None:
    with pytest.raises(ValueError, match="mcp config path must be absolute"):
        build_argv(["claude.exe"], Launch(NEW, mcp_config=path), mode="auto", effort="low")


@pytest.mark.parametrize("model", [None, "sonnet"])
def test_a_read_only_launch_keeps_three_file_tools_and_no_mcp_server(model: str | None) -> None:
    """The inbox suggestion run's launch, and its resume after a dropped process, add
    exactly these two elements, after the launch flags and before the model."""
    tail = [f"--model={model}"] if model else []
    new = Launch(NEW, read_only_tools=True)
    assert build_argv(["claude.exe"], new, mode="auto", effort="low", model=model) == [
        *BASE,
        f"--session-id={NEW}",
        *READ_ONLY,
        *tail,
    ]
    resume = Launch(OLD, resume=True, read_only_tools=True)
    assert build_argv(["claude.exe"], resume, mode="auto", effort="low", model=model) == [
        *BASE,
        f"--resume={OLD}",
        *READ_ONLY,
        *tail,
    ]


def test_a_read_only_launch_refuses_an_mcp_config(tmp_path: Path) -> None:
    """--strict-mcp-config keeps the servers --mcp-config names: the two never travel together."""
    launch = Launch(NEW, mcp_config=str(tmp_path / "config.json"), read_only_tools=True)
    with pytest.raises(ValueError, match="a read-only launch takes no mcp config"):
        build_argv(["claude.exe"], launch, mode="auto", effort="low")


def test_a_default_launch_gains_nothing(tmp_path: Path) -> None:
    """Only `read_only_tools` adds an element: every launch shape left at the default builds
    exactly the argv listed here, element for element."""
    path = str(tmp_path / "config.json")
    shapes = [
        (Launch(NEW), [f"--session-id={NEW}"]),
        (Launch(OLD, resume=True), [f"--resume={OLD}"]),
        (Launch(NEW, fork_of=OLD), [f"--resume={OLD}", "--fork-session", f"--session-id={NEW}"]),
        (Launch(NEW, mcp_config=path), [f"--session-id={NEW}", f"--mcp-config={path}"]),
    ]
    for launch, flags in shapes:
        assert launch == replace(launch, read_only_tools=False)
        argv = build_argv(["claude.exe"], launch, mode="auto", effort="low", model="sonnet")
        assert argv == [*BASE, *flags, "--model=sonnet"]


def test_add_dirs_follow_the_session_id_in_equals_form(tmp_path: Path) -> None:
    """Hard rule 4's fourth exception (ADR-050): `Launch.add_dirs` becomes one
    `--add-dir=<path>` element per entry, right after the session-id/resume/fork-session
    elements and before the model."""
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    new = build_argv(["claude.exe"], Launch(NEW, add_dirs=(a, b)), mode="auto", effort="low")
    assert new == [*BASE, f"--session-id={NEW}", f"--add-dir={a}", f"--add-dir={b}"]
    resume = Launch(OLD, resume=True, add_dirs=(a,))
    assert build_argv(["claude.exe"], resume, mode="auto", effort="low") == [
        *BASE,
        f"--resume={OLD}",
        f"--add-dir={a}",
    ]
    fork = Launch(NEW, fork_of=OLD, add_dirs=(a,))
    assert build_argv(["claude.exe"], fork, mode="auto", effort="low") == [
        *BASE,
        f"--resume={OLD}",
        "--fork-session",
        f"--session-id={NEW}",
        f"--add-dir={a}",
    ]
    with_model = build_argv(
        ["claude.exe"], Launch(NEW, add_dirs=(a,)), mode="auto", effort="low", model="sonnet"
    )
    assert with_model == [*BASE, f"--session-id={NEW}", f"--add-dir={a}", "--model=sonnet"]


def test_add_dirs_refuse_more_than_seven(tmp_path: Path) -> None:
    dirs = tuple(str(tmp_path / str(i)) for i in range(8))
    with pytest.raises(ValueError, match="too many"):
        build_argv(["claude.exe"], Launch(NEW, add_dirs=dirs), mode="auto", effort="low")


def test_add_dirs_refuse_a_relative_entry(tmp_path: Path) -> None:
    launch = Launch(NEW, add_dirs=(str(tmp_path / "a"), "relative/dir"))
    with pytest.raises(ValueError, match="absolute"):
        build_argv(["claude.exe"], launch, mode="auto", effort="low")


def test_add_dirs_refuse_beside_an_mcp_config(tmp_path: Path) -> None:
    launch = Launch(NEW, add_dirs=(str(tmp_path / "a"),), mcp_config=str(tmp_path / "config.json"))
    with pytest.raises(ValueError, match="mcp config"):
        build_argv(["claude.exe"], launch, mode="auto", effort="low")


def test_add_dirs_refuse_beside_read_only_tools(tmp_path: Path) -> None:
    launch = Launch(NEW, add_dirs=(str(tmp_path / "a"),), read_only_tools=True)
    with pytest.raises(ValueError, match="read-only"):
        build_argv(["claude.exe"], launch, mode="auto", effort="low")


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("with_mcp", [False, True])
@pytest.mark.parametrize("model", [None, "sonnet"])
def test_bot_code_copy_is_one_equals_element_before_settings_and_mcp(
    tmp_path: Path, resume: bool, with_mcp: bool, model: str | None
) -> None:
    copy = str(tmp_path / "project-worktrees" / NEW)
    settings = str(tmp_path / "bot-memory-settings" / f"{'a' * 64}.json")
    mcp = str(tmp_path / "mcp" / "config.json") if with_mcp else None
    launch = Launch(
        NEW, resume=resume, code_worktree=copy, bot_memory_settings=settings, mcp_config=mcp
    )
    assert build_argv(["claude.exe"], launch, mode="auto", effort="low", model=model) == [
        *BASE,
        f"--resume={NEW}" if resume else f"--session-id={NEW}",
        f"--add-dir={copy}",
        f"--settings={settings}",
        *([f"--mcp-config={mcp}"] if mcp is not None else []),
        *([f"--model={model}"] if model else []),
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"bot_memory_settings": None},
        {"add_dirs": ("extra",)},
        {"read_only_tools": True},
        {"fork_of": OLD},
        {"fork_of": ""},
        {"cwd": Path("session-root")},
        {"cwd": Path(".")},
    ],
)
def test_bot_code_copy_refuses_incompatible_launches(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    launch = Launch(
        NEW,
        code_worktree=str(tmp_path / "project-worktrees" / NEW),
        bot_memory_settings=str(tmp_path / "bot-memory-settings" / f"{'a' * 64}.json"),
    )
    for resume in (False, True):
        with pytest.raises(ValueError, match="invalid Bot code worktree"):
            build_argv(
                ["claude.exe"], replace(launch, resume=resume, **changes), mode="auto", effort="low"
            )


@pytest.mark.parametrize(
    "relative",
    [
        "",
        "project-worktrees",
        f"project-worktrees/{NEW}",
        f"./project-worktrees/{NEW}",
        f"C:project-worktrees/{NEW}",
        f"\\project-worktrees\\{NEW}",
        '{"path":"project-worktrees"}',
    ],
)
def test_bot_code_copy_refuses_non_absolute_paths(tmp_path: Path, relative: str) -> None:
    launch = Launch(
        NEW,
        code_worktree=relative,
        bot_memory_settings=str(tmp_path / "bot-memory-settings" / f"{'a' * 64}.json"),
    )
    with pytest.raises(ValueError, match="invalid Bot code worktree"):
        build_argv(["claude.exe"], launch, mode="auto", effort="low")


@pytest.mark.parametrize(
    "suffix",
    [
        "project-worktrees/not-a-uuid",
        f"project-worktrees/{NEW.replace('-', '')}",
        "project-worktrees/ABCDEFAB-ABCD-4ABC-8ABC-ABCDEFABCDEF",
        f"project-worktrees/{{{NEW}}}",
        f"project-worktrees/urn:uuid:{NEW}",
        f"project-worktrees/{NEW}/extra",
        f"project-worktrees/nested/{NEW}",
        f"project/{NEW}",
        f"project-worktrees-extra/{NEW}",
        f"project-worktrees/{NEW} --add-dir=extra",
        f"project-worktrees/../other-worktrees/{NEW}",
        f"invalid\x00-worktrees/{NEW}",
    ],
)
def test_bot_code_copy_refuses_malformed_or_extra_path_segments(
    tmp_path: Path, suffix: str
) -> None:
    launch = Launch(
        NEW,
        code_worktree=str(tmp_path / suffix),
        bot_memory_settings=str(tmp_path / "bot-memory-settings" / f"{'a' * 64}.json"),
    )
    with pytest.raises(ValueError, match="invalid Bot code worktree"):
        build_argv(["claude.exe"], launch, mode="auto", effort="low")


@pytest.mark.parametrize(
    ("path_type", "root", "foreign"),
    [
        (PurePosixPath, "/app data", f"C:\\app\\project-worktrees\\{NEW}"),
        (PureWindowsPath, "C:/app data", f"/app/project-worktrees/{NEW}"),
        (PureWindowsPath, "//server/share/app data", f"/app/project-worktrees/{NEW}"),
    ],
)
def test_bot_code_copy_checks_host_path_syntax_without_reading_files(
    monkeypatch: pytest.MonkeyPatch, path_type: type[PurePath], root: str, foreign: str
) -> None:
    # Pure paths cannot read files or resolve links: admission belongs to the resolver.
    monkeypatch.setattr("viki_agent.drivers.claude.control.Path", path_type)
    copy = str(path_type(root) / "project-worktrees" / "abcdefab-abcd-4abc-8abc-abcdefabcdef")
    settings = str(path_type(root) / "bot-memory-settings" / f"{'a' * 64}.json")
    launch = Launch(NEW, code_worktree=copy, bot_memory_settings=settings)
    assert build_argv(["claude.exe"], launch, mode="auto", effort="low") == [
        *BASE,
        f"--session-id={NEW}",
        f"--add-dir={copy}",
        f"--settings={settings}",
    ]
    with pytest.raises(ValueError, match="invalid Bot code worktree"):
        build_argv(
            ["claude.exe"], replace(launch, code_worktree=foreign), mode="auto", effort="low"
        )


@pytest.mark.parametrize("with_copy", [False, True])
@pytest.mark.parametrize(
    "settings",
    [
        "",
        "settings.json",
        '{"autoMemoryDirectory":"memory"}',
        "other/" + "a" * 64 + ".json",
        "bot-memory-settings/short.json",
        "bot-memory-settings/" + "A" * 64 + ".json",
        "bot-memory-settings/" + "g" * 64 + ".json",
        "bot-memory-settings/" + "a" * 64 + ".txt",
    ],
)
def test_bot_memory_settings_path_validation_stays_in_force(
    tmp_path: Path, with_copy: bool, settings: str
) -> None:
    # An absolute path still needs the existing generated directory/hash/suffix shape.
    path = str(tmp_path / settings) if "/" in settings else settings
    launch = Launch(
        NEW,
        bot_memory_settings=path,
        code_worktree=str(tmp_path / "project-worktrees" / NEW) if with_copy else None,
    )
    with pytest.raises(ValueError, match="invalid Bot memory settings"):
        build_argv(["claude.exe"], launch, mode="auto", effort="low")


def test_explicit_none_code_copy_keeps_ordinary_argv_byte_equivalent(tmp_path: Path) -> None:
    settings = str(tmp_path / "bot-memory-settings" / f"{'a' * 64}.json")
    mcp = str(tmp_path / "mcp" / "config.json")
    directory = str(tmp_path / "code")
    for resume in (False, True):
        selector = f"--resume={NEW}" if resume else f"--session-id={NEW}"
        shapes = [
            (Launch(NEW, resume=resume), []),
            (Launch(NEW, resume=resume, read_only_tools=True), READ_ONLY),
            (Launch(NEW, resume=resume, add_dirs=(directory,)), [f"--add-dir={directory}"]),
            (Launch(NEW, resume=resume, mcp_config=mcp), [f"--mcp-config={mcp}"]),
            (Launch(NEW, resume=resume, bot_memory_settings=settings), [f"--settings={settings}"]),
            (
                Launch(NEW, resume=resume, bot_memory_settings=settings, mcp_config=mcp),
                [f"--settings={settings}", f"--mcp-config={mcp}"],
            ),
        ]
        for launch, flags in shapes:
            expected = [*BASE, selector, *flags, "--model=sonnet"]
            for variant in (launch, replace(launch, code_worktree=None)):
                actual = build_argv(
                    ["claude.exe"], variant, mode="auto", effort="low", model="sonnet"
                )
                assert [element.encode("utf-8") for element in actual] == [
                    element.encode("utf-8") for element in expected
                ]
    fork = Launch(NEW, fork_of=OLD, mcp_config=mcp)
    assert build_argv(
        ["claude.exe"], replace(fork, code_worktree=None), mode="auto", effort="low"
    ) == [*BASE, f"--resume={OLD}", "--fork-session", f"--session-id={NEW}", f"--mcp-config={mcp}"]


@pytest.mark.parametrize(
    "changes", [{"add_dirs": ("extra",)}, {"read_only_tools": True}, {"fork_of": OLD}]
)
def test_bot_memory_settings_without_a_copy_keep_launch_exclusions(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    launch = Launch(
        NEW, bot_memory_settings=str(tmp_path / "bot-memory-settings" / f"{'a' * 64}.json")
    )
    if "add_dirs" in changes:
        changes = {"add_dirs": (str(tmp_path / "extra"),)}
    with pytest.raises(ValueError, match="invalid Bot memory settings"):
        build_argv(["claude.exe"], replace(launch, **changes), mode="auto", effort="low")


def test_a_bot_system_prompt_adds_no_argv_element(tmp_path: Path) -> None:
    # ADR-084: the transport sends it in initialize, so argv is byte for byte the same.
    settings = str(tmp_path / "bot-memory-settings" / f"{'a' * 64}.json")
    for resume in (False, True):
        launch = Launch(NEW, resume=resume, bot_memory_settings=settings)
        prompted = replace(launch, append_system_prompt="Be brief.")
        assert build_argv(["claude.exe"], prompted, mode="auto", effort="low") == build_argv(
            ["claude.exe"], launch, mode="auto", effort="low"
        )
    assert "Be brief." not in repr(replace(launch, append_system_prompt="Be brief."))


@pytest.mark.parametrize(
    "launch",
    [
        Launch(NEW, append_system_prompt="Be brief."),
        Launch(NEW, read_only_tools=True, append_system_prompt="Be brief."),
        Launch(NEW, fork_of=OLD, append_system_prompt="Be brief."),
    ],
)
def test_a_system_prompt_is_refused_outside_a_bot_session(launch: Launch) -> None:
    with pytest.raises(ValueError, match="only for a Bot session"):
        build_argv(["claude.exe"], launch, mode="auto", effort="low")


async def _allow(
    tool: str, tool_input: dict[str, Any], ctx: ToolPermissionContext
) -> PermissionResultAllow | PermissionResultDeny:
    return PermissionResultAllow()


def _control() -> ClaudeControl:
    """A ClaudeControl over an unstarted transport: enough to exercise send()'s pre-query
    resolver handling, which never reaches the transport."""
    argv = build_argv(["claude.exe"], Launch(SID), mode="auto", effort="low")
    transport = ClaudeTransport(
        argv,
        cwd=Path.cwd(),
        env={},
        sink=lambda frame, size: None,
        spawner=FakeSpawner([]),
        grace_s=0.05,
        poll_s=0.05,
    )
    return ClaudeControl(transport, _allow, control_timeout_s=0.05)


_ATTACHMENT = Attachment(
    "00000000-0000-4000-8000-000000000013", "image/png", 4, 1, 1, "synthetic.png"
)


class _Busy(Exception):
    code = "busy"


async def test_a_busy_resolver_exception_is_transient() -> None:
    async def resolve(_: Attachment) -> bytes:
        raise _Busy("locked")

    with pytest.raises(AttachmentUnavailable) as excinfo:
        await _control().send(TurnInput("describe", (_ATTACHMENT,)), SID, resolve)
    assert excinfo.value.transient is True


class _NotFound(Exception):
    code = "not_found"


@pytest.mark.parametrize("failure", [RuntimeError("plain"), _NotFound("refused")])
async def test_a_non_busy_resolver_exception_is_not_transient(failure: Exception) -> None:
    async def resolve(_: Attachment) -> bytes:
        raise failure

    with pytest.raises(AttachmentUnavailable) as excinfo:
        await _control().send(TurnInput("describe", (_ATTACHMENT,)), SID, resolve)
    assert excinfo.value.transient is False


async def test_no_resolver_and_mismatched_bytes_stay_not_transient() -> None:
    control = _control()
    turn = TurnInput("describe", (_ATTACHMENT,))
    with pytest.raises(AttachmentUnavailable) as missing_resolver:
        await control.send(turn, SID, None)
    assert missing_resolver.value.transient is False

    async def wrong_size(_: Attachment) -> bytes:
        return b"\x00"  # _ATTACHMENT.byte_size is 4

    with pytest.raises(AttachmentUnavailable) as mismatched:
        await control.send(turn, SID, wrong_size)
    assert mismatched.value.transient is False


class _CapturingClient:
    """Stands in for the SDK client so a test can read the exact content blocks `send` built,
    without a real claude.exe: `query`'s public seam is an async iterable of messages."""

    def __init__(self) -> None:
        self.captured: list[dict[str, Any]] = []

    async def query(self, prompt: Any, session_id: str | None = None) -> None:
        async for message in prompt:
            self.captured.append(message)


async def test_two_image_content_blocks_are_unchanged_by_the_threaded_encode() -> None:
    """The off-loaded encode must build the same content blocks (types, order, media types, and
    base64 data equal to the stdlib encoding of the bytes) as the inline encode did."""
    control = _control()
    client = _CapturingClient()
    control._client = client  # type: ignore[assignment]

    first = bytes(range(256)) * 16
    second = bytes(range(255, -1, -1)) * 16
    attachment_a = Attachment(
        "00000000-0000-4000-8000-000000000021", "image/png", len(first), 1, 1, "a.png"
    )
    attachment_b = Attachment(
        "00000000-0000-4000-8000-000000000022", "image/jpeg", len(second), 1, 1, "b.jpg"
    )

    async def resolve(attachment: Attachment) -> bytes:
        return first if attachment.asset_id == attachment_a.asset_id else second

    turn = TurnInput("two images", (attachment_a, attachment_b))
    await control.send(turn, SID, resolve)

    assert len(client.captured) == 1
    content = client.captured[0]["message"]["content"]
    assert [block["type"] for block in content] == ["text", "image", "image"]
    assert content[0]["text"] == "two images"
    assert content[1]["source"]["type"] == "base64"
    assert content[1]["source"]["media_type"] == "image/png"
    assert content[1]["source"]["data"] == base64.b64encode(first).decode("ascii")
    assert content[2]["source"]["type"] == "base64"
    assert content[2]["source"]["media_type"] == "image/jpeg"
    assert content[2]["source"]["data"] == base64.b64encode(second).decode("ascii")


async def test_each_image_is_encoded_off_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The base64 encode must leave the event loop once per image, not run inline in the send
    loop (a stall that a refactor could silently bring back)."""
    control = _control()
    client = _CapturingClient()
    control._client = client  # type: ignore[assignment]

    calls: list[object] = []
    real_to_thread = asyncio.to_thread

    async def counting_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr("viki_agent.drivers.claude.control.asyncio.to_thread", counting_to_thread)

    first = bytes(range(256)) * 16
    second = bytes(range(255, -1, -1)) * 16
    attachment_a = Attachment(
        "00000000-0000-4000-8000-000000000023", "image/png", len(first), 1, 1, "a.png"
    )
    attachment_b = Attachment(
        "00000000-0000-4000-8000-000000000024", "image/jpeg", len(second), 1, 1, "b.jpg"
    )

    async def resolve(attachment: Attachment) -> bytes:
        return first if attachment.asset_id == attachment_a.asset_id else second

    turn = TurnInput("two images", (attachment_a, attachment_b))
    await control.send(turn, SID, resolve)

    assert len(calls) == 2


async def test_mcp_status_returns_the_fake_replayers_default() -> None:
    control = _control()
    await control.start()
    try:
        assert await control.mcp_status() == {"mcpServers": []}
    finally:
        await control.close()


async def test_an_unanswered_mcp_status_call_times_out() -> None:
    script = [expect({"type": "control_request", "request": {"subtype": "mcp_status"}})]
    argv = build_argv(["claude.exe"], Launch(SID), mode="auto", effort="low")
    transport = ClaudeTransport(
        argv,
        cwd=Path.cwd(),
        env={},
        sink=lambda frame, size: None,
        spawner=FakeSpawner(script),
        grace_s=0.05,
        poll_s=0.05,
    )
    control = ClaudeControl(transport, _allow, control_timeout_s=0.05)
    await control.start()
    with pytest.raises(TimeoutError):
        await control.mcp_status()
    await control.close()


def test_inventory_driver_is_runtime_checkable_on_a_stub() -> None:
    """A stub proves the Protocol is runtime-checkable."""

    class _NoInventory:
        pass

    class _HasInventory:
        async def inventory(self) -> dict[str, object]:
            return {}

    assert not isinstance(_NoInventory(), InventoryDriver)
    assert isinstance(_HasInventory(), InventoryDriver)
