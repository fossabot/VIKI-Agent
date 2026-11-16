from __future__ import annotations

import dataclasses
import hashlib
import os
import sys
import types
import uuid
from pathlib import Path

import pytest

from viki_agent.domain import ActiveWorkspace, CanonicalRoot, RootIdentity, Workspace
from viki_agent.profiles import native_assets as native_assets_module
from viki_agent.profiles.native_assets import (
    NativeAssetError,
    NativeAssetInspector,
    claude_user_root,
)
from viki_agent.winproc import run
from viki_agent.workspaces import root_key

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows handle safety")


def _root(path: Path) -> CanonicalRoot:
    resolved = path.resolve()
    info = resolved.stat()
    return CanonicalRoot(
        root_key(resolved),
        str(resolved),
        f"{info.st_dev}:{info.st_ino}",
        False,
    )


def _workspace(path: Path) -> Workspace:
    return Workspace(
        str(uuid.uuid4()),
        "vault",
        _root(path),
        "vault",
        "ordinary",
        2,
        3,
        "created",
        "unknown",
        None,
        RootIdentity((), None, {}, False, None, "2026-09-21T00:00:00.000Z"),
        None,
        "complete",
        True,
        "2026-09-21T00:00:00.000Z",
        "2026-09-21T00:00:00.000Z",
    )


class _Workspaces:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.root_calls = 0
        self.changed_root: CanonicalRoot | None = None
        self.inactive = False

    def resolve_active(self) -> ActiveWorkspace:
        selected = (
            dataclasses.replace(self.workspace, workspace_id=str(uuid.uuid4()))
            if self.inactive
            else self.workspace
        )
        return ActiveWorkspace(
            selected, Path(selected.root.display), selected.workspace_id, "registration", False
        )

    def get(self, workspace_id: str) -> Workspace:
        if workspace_id != self.workspace.workspace_id:
            raise AssertionError("unexpected workspace")
        return self.workspace

    def current_root(self, workspace: Workspace) -> CanonicalRoot | None:
        self.root_calls += 1
        return self.changed_root if self.root_calls > 1 and self.changed_root else workspace.root


def _inspector(path: Path, *, home: Path | None = None) -> tuple[NativeAssetInspector, _Workspaces]:
    owner = _Workspaces(_workspace(path))
    return NativeAssetInspector(owner, claude_home=_root(home) if home else None), owner


def _write(path: Path, body: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body if isinstance(body, bytes) else body.encode())


def test_workspace_allowlist_returns_only_bounded_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent = (
        b"---\nname: Reviewer\ndescription: Checks work\nmodel: sonnet\n"
        b'tools: ["Read", "Grep"]\nfuture: private-value\n---\nDO NOT RETURN THIS BODY'
    )
    _write(workspace / ".claude" / "agents" / "reviewer.md", agent)
    _write(
        workspace / ".claude" / "skills" / "simple" / "SKILL.md",
        "---\nname: Simple\n---\nSECRET BODY",
    )
    _write(workspace / ".claude" / "agents" / ".hidden.md", "hidden")
    _write(workspace / ".claude" / "agents" / "settings.local.md", "settings")
    _write(workspace / ".claude" / "agents" / "nested" / "ignored.md", "nested")
    _write(workspace / ".claude" / "commands" / "ignored.md", "command")
    inspector, owner = _inspector(workspace)

    result = inspector.inspect(f"claude:workspace:{owner.workspace.workspace_id}")

    assert [asset.relative_id for asset in result.assets] == [
        "agents/reviewer.md",
        "skills/simple/SKILL.md",
    ]
    reviewed = result.assets[0]
    assert (reviewed.name, reviewed.description, reviewed.model, reviewed.tools) == (
        "Reviewer",
        "Checks work",
        "sonnet",
        ("Read", "Grep"),
    )
    assert reviewed.digest == hashlib.sha256(agent).hexdigest()
    assert reviewed.unsupported_fields == ("future",)
    assert reviewed.launch_supported is False
    assert "DO NOT RETURN" not in repr(result)
    assert not result.partial


def test_user_source_has_the_same_narrow_relative_allowlist(tmp_path: Path) -> None:
    workspace, home = tmp_path / "workspace", tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    _write(home / "agents" / "user.md", "---\nname: User agent\ndescription: Helps\n---\nbody")
    _write(home / "skills" / "one" / "SKILL.md", "---\nname: User skill\n---\nbody")
    _write(home / "projects" / "transcript.md", "private")
    inspector, _ = _inspector(workspace, home=home)

    result = inspector.inspect("claude:user")

    assert result.source_scope == "user"
    assert [asset.relative_id for asset in result.assets] == [
        "agents/user.md",
        "skills/one/SKILL.md",
    ]
    assert not result.partial


@pytest.mark.parametrize(
    "source_id", ["codex:user", "claude:workspace:../bad", "claude:workspace:not-a-uuid"]
)
def test_callers_cannot_mint_sources(tmp_path: Path, source_id: str) -> None:
    tmp_path.mkdir(exist_ok=True)
    inspector, _ = _inspector(tmp_path)
    with pytest.raises(NativeAssetError, match="invalid_source"):
        inspector.inspect(source_id)


def test_only_the_active_workspace_source_is_available(tmp_path: Path) -> None:
    inspector, owner = _inspector(tmp_path)
    owner.inactive = True
    with pytest.raises(NativeAssetError, match="source_unavailable"):
        inspector.inspect(f"claude:workspace:{owner.workspace.workspace_id}")


def test_oversized_hardlinked_and_invalid_files_are_partial(tmp_path: Path) -> None:
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    _write(agents / "large.md", b"x" * (128 * 1024 + 1))
    _write(agents / "invalid.md", b"\xff")
    _write(agents / "broken.md", "---\nnot metadata\n---\nbody")
    _write(agents / "original.md", "body")
    os.link(agents / "original.md", agents / "linked.md")
    inspector, owner = _inspector(tmp_path)

    result = inspector.inspect(f"claude:workspace:{owner.workspace.workspace_id}")

    assert set(result.partial_reasons) >= {
        "oversized",
        "invalid_encoding",
        "malformed_metadata",
        "unsafe_entry",
    }
    assert result.scanned_bytes == len(b"\xff") + len(b"---\nnot metadata\n---\nbody")
    assert {asset.relative_id for asset in result.assets} == set()


@pytest.mark.parametrize(
    ("count", "expected_assets", "partial"),
    ((1, 1, False), (256, 256, False), (257, 256, True)),
)
def test_entry_cap_admits_the_exact_boundary_without_false_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int, expected_assets: int, partial: bool
) -> None:
    # the clock stands still so a loaded machine's 2 s deadline never stops the scan at 256 entries
    monkeypatch.setattr(native_assets_module, "time", types.SimpleNamespace(monotonic=lambda: 0.0))
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    for index in range(count):
        _write(
            agents / f"{index:03}.md",
            f"---\nname: Agent {index}\ndescription: Does work\n---\nbody",
        )
    inspector, owner = _inspector(tmp_path)

    result = inspector.inspect(f"claude:workspace:{owner.workspace.workspace_id}")

    assert len(result.assets) == expected_assets
    assert result.scanned_entries == expected_assets
    assert result.partial is partial
    assert ("entry_limit" in result.partial_reasons) is partial
    assert result.omitted == (1 if partial else 0)


def test_total_byte_budget_stops_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    for index in range(5):
        _write(agents / f"{index}.md", b"x" * 100)
    inspector, owner = _inspector(tmp_path)
    source = f"claude:workspace:{owner.workspace.workspace_id}"

    monkeypatch.setattr(native_assets_module, "MAX_TOTAL_BYTES", 250)
    byte_limited = inspector.inspect(source)
    assert byte_limited.scanned_bytes == 200
    assert "byte_limit" in byte_limited.partial_reasons


def test_json_surrogate_tool_is_a_malformed_partial(tmp_path: Path) -> None:
    _write(
        tmp_path / ".claude" / "agents" / "bad.md",
        '---\nname: Bad\ndescription: Checks work\ntools: ["\\ud800"]\n---\nbody',
    )
    inspector, owner = _inspector(tmp_path)

    result = inspector.inspect(f"claude:workspace:{owner.workspace.workspace_id}")

    assert result.assets == ()
    assert result.partial_reasons == ("malformed_metadata",)


def test_deadline_and_root_race_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / ".claude" / "agents" / "one.md", "body")
    inspector, owner = _inspector(tmp_path)
    source = f"claude:workspace:{owner.workspace.workspace_id}"
    calls = iter((10.0, 10.0, 13.0, 13.0))
    monkeypatch.setattr(native_assets_module.time, "monotonic", lambda: next(calls, 13.0))
    assert "deadline" in inspector.inspect(source).partial_reasons

    monkeypatch.undo()
    other = tmp_path / "other"
    other.mkdir()
    owner.root_calls = 0
    owner.changed_root = _root(other)
    with pytest.raises(NativeAssetError, match="source_changed"):
        inspector.inspect(source)


def test_reparse_allowlisted_directory_is_partial_and_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(outside / "secret.md", "secret")
    claude = tmp_path / ".claude"
    claude.mkdir()
    link = claude / "agents"
    created = run(
        [os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(link), str(outside)],
        cwd=tmp_path,
    )
    if created.returncode != 0:
        pytest.skip("directory junctions unavailable")
    inspector, owner = _inspector(tmp_path)

    result = inspector.inspect(f"claude:workspace:{owner.workspace.workspace_id}")

    assert result.assets == ()
    assert "unsafe_entry" in result.partial_reasons
    assert result.omitted == 0


def test_a_deeply_nested_tools_value_reads_as_malformed_not_crashed(tmp_path: Path) -> None:
    """A tools value nested this deep still reads as malformed metadata and never crashes the
    inspector."""
    _write(
        tmp_path / ".claude" / "agents" / "deep.md",
        "---\nname: Deep\ndescription: Checks work\ntools: "
        + "[" * 2000
        + "]" * 2000
        + "\n---\nbody",
    )
    inspector, owner = _inspector(tmp_path)

    result = inspector.inspect(f"claude:workspace:{owner.workspace.workspace_id}")

    assert result.assets == ()
    assert result.partial_reasons == ("malformed_metadata",)


def test_a_recursion_error_while_parsing_tools_reads_as_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(
        tmp_path / ".claude" / "agents" / "recursion.md",
        "---\nname: Recursion\ndescription: Checks work\ntools: [a]\n---\nbody",
    )
    inspector, owner = _inspector(tmp_path)

    def loads(value: str) -> object:
        raise RecursionError

    monkeypatch.setattr(native_assets_module, "json", types.SimpleNamespace(loads=loads))

    result = inspector.inspect(f"claude:workspace:{owner.workspace.workspace_id}")

    assert result.assets == ()
    assert result.partial_reasons == ("malformed_metadata",)


_GOOD = "---\nname: Good\ndescription: Checks work\n"

REFUSED_AGENTS = (
    pytest.param(
        "---\ndescription: Checks work\n---\nbody",
        id="no_name",
    ),
    pytest.param(
        "---\nname:\ndescription: Checks work\n---\nbody",
        id="empty_name_refused_by_the_plain_check",
    ),
    pytest.param(
        "---\nname: -bad\ndescription: Checks work\n---\nbody",
        id="name_starts_with_dash",
    ),
    pytest.param(
        "---\nname: bad:name\ndescription: Checks work\n---\nbody",
        id="name_holds_colon",
    ),
    pytest.param(
        "---\nname: Good\n---\nbody",
        id="no_description",
    ),
    pytest.param(
        "---\nname: Good\ndescription:\n---\nbody",
        id="empty_description_refused_by_the_plain_check",
    ),
    pytest.param(
        "---\nname: Good\ndescription: Checks work\n  and more\n---\nbody",
        id="continuation_under_description",
    ),
    pytest.param(
        "---\n  - x\nname: Good\ndescription: Checks work\n---\nbody",
        id="continuation_before_any_key",
    ),
    pytest.param(
        "---\nName: Good\ndescription: Checks work\n---\nbody",
        id="name_key_in_another_case",
    ),
    pytest.param(
        "---\nname: Good\nDescription: Checks work\n---\nbody",
        id="description_key_in_another_case",
    ),
    pytest.param(
        "---\nname: Good\nname: Again\ndescription: Checks work\n---\nbody",
        id="name_key_repeated_exactly",
    ),
    pytest.param(
        "---\nname: bad\uff1aname\ndescription: Checks work\n---\nbody",
        id="name_holds_a_full_width_colon",
    ),
    pytest.param(
        "---\nname: Good\n\u3000\ndescription: Checks work\n---\nbody",
        id="full_width_space_line",
    ),
    pytest.param(
        "---\nname: Good\n\u00a0\ndescription: Checks work\n---\nbody",
        id="no_break_space_line",
    ),
    pytest.param(
        "---\nname: Good\n\u3000# note\ndescription: Checks work\n---\nbody",
        id="full_width_space_before_a_comment",
    ),
    pytest.param(
        "---\nname: Good\ndescription: Checks work\nhooks:\n\tpre: run\n---\nbody",
        id="tab_indented_line_under_an_unsupported_key",
    ),
    pytest.param(
        "---\nname: Good\ndescription: Checks work\nhooks:\n  - x\n---\nbody",
        id="list_under_an_unsupported_key",
    ),
    pytest.param(
        "---\nname: Good\ndescription: Checks work\nhooks:\n- x\n- y\n---\nbody",
        id="dash_lines_at_column_zero_under_an_unsupported_key",
    ),
    pytest.param(
        "---\nname: Good\ndescription: Checks work\nhooks:\n  pre: run\n  post:\n    - y\n---\n",
        id="mapping_and_dash_lines_under_an_unsupported_key",
    ),
    pytest.param(
        "---\nname: Good\ndescription: Checks work\nnotes:\n- ---\ntools: Read\n---\nbody",
        id="dash_line_holding_the_closing_dashes",
    ),
    pytest.param(
        "---\nname: Good\ndescription: Checks work\n# limits\ftools: Read\n---\nbody",
        id="form_feed_inside_a_comment",
    ),
    pytest.param(
        "---\nname: Good\ndescription: Checks work\nnotes:\n- x\u2028tools: Read\n---\nbody",
        id="line_separator_inside_a_body_line",
    ),
    pytest.param(
        '---\nname: Good\ndescription: Checks work\nnotes:\n  "open\ntools: Read\n---\nbody',
        id="unterminated_quote_in_a_body_line",
    ),
    # a value its key line leaves open: YAML reads the lines below into it, comments included
    *(
        pytest.param(_GOOD + body + "\n---\nbody", id=case)
        for case, body in (
            ("tools_double_quote_closed_in_an_indented_comment", 'tools: "Read\n  # *"'),
            ("tools_single_quote_closed_in_an_indented_comment", "tools: 'Read, Grep\n  # *'"),
            ("model_double_quote_closed_in_an_indented_comment", 'model: "sonnet\n  # "'),
            ("tools_double_quote_ending_in_an_escaped_quote", 'tools: "Read\\"\n  # *"'),
            ("tools_single_quote_ending_in_a_doubled_quote", "tools: 'Read, Grep''\n  # *'"),
            ("model_double_quote_ending_in_an_escaped_quote", 'model: "opus\\"\n  # x"'),
            ("tag_before_an_open_double_quote", 'tools: !!str "Read\n  # *"'),
            ("anchor_before_an_open_single_quote", "tools: &a 'Read\n  # *'"),
            ("verbatim_tag_before_an_open_quote", 'tools: !<tag:yaml.org,2002:str> "Read\n  # *"'),
            ("tag_before_a_literal_block", "tools: !!str |\n  # *"),
            ("folded_block_with_a_header_comment", "tools: > # Read\n  # *"),
            ("literal_block_with_strip", "tools: |-\n  # *"),
            ("folded_block_with_indent_and_keep", "tools: >2+\n  # *"),
            ("literal_block_with_a_blank_body", "tools: |\n"),
            ("open_quote_closed_on_a_key_line", 'tools: "Read\nx: *"'),
            ("open_quote_closed_on_a_key_line_after_a_blank", 'tools: "Read\n\nx: *"'),
            ("open_quote_closed_in_a_column_zero_comment", 'tools: "Read\n# *"'),
            ("open_quote_closed_in_a_tab_led_comment", 'tools: "Read\n\t# *"'),
            ("quote_inside_a_flow_value", 'model: [a, "]\n  # x"]'),
            ("apostrophes_inside_a_flow_value", "model: [a'b, 'c]\n  # x']"),
            ("flow_value_closed_inside_a_comment", "model: [opus # ]\nnotes: x]"),
            ("open_quote_under_an_unsupported_key", 'notes: "x\n# y\ntools: Read"'),
            ("open_quote_under_an_unsupported_key_at_the_end", 'tools: Read\nnotes: "x\n# y'),
        )
    ),
    pytest.param(
        '---\nname: "Go\n  # :od"\ndescription: Checks work\ntools: Read\n---\nbody',
        id="open_quote_in_the_name",
    ),
    pytest.param(
        '---\nname: Good\ndescription: "Checks --- work"\ntools: Read\n---\nbody',
        id="quoted_description_holding_the_closing_dashes",
    ),
    pytest.param(
        "---\nname: Good\ndescription: |\ntools: Read\n---\nbody",
        id="empty_literal_block_description",
    ),
    pytest.param(_GOOD + "tools:x: Read\n---\nbody", id="colon_inside_the_key"),
    pytest.param(_GOOD + "tools : Read\n---\nbody", id="space_before_the_colon"),
    pytest.param(
        "---\nname:Good\ndescription: Checks work\n---\nbody", id="no_space_after_the_colon"
    ),
    pytest.param(_GOOD + "tools\u00a0: Read\n---\nbody", id="no_break_space_before_the_colon"),
    pytest.param(_GOOD + "hooks:\n  tools: Read\n---\nbody", id="indented_key_under_another_key"),
    *(
        pytest.param(
            _GOOD + f"# limits{char}tools: Read\n---\nbody", id=f"u{ord(char):04x}_in_a_comment"
        )
        for char in ("\x01", "\x0b", "\x1b", "\x7f", "\x85", "\x9f", "\u2028", "\u2029")
    ),
    pytest.param(_GOOD + "tools: Read\n", id="no_closing_dashes"),
    pytest.param(
        " ---\nname: Good\ndescription: Checks work\n---\nbody", id="opening_after_a_space"
    ),
    pytest.param(
        "---\rname: Good\rdescription: Checks work\r---\rbody", id="no_line_feed_so_no_frontmatter"
    ),
    # a line ends only at a line feed: the CLI's retry splits lines there alone
    pytest.param(
        "---\r\r\nname: Good\r\r\ndescription: Checks work\r\r\ntools: Read\r\r\n---\r\r\nbody",
        id="carriage_returns_doubled_before_line_feeds",
    ),
    pytest.param(
        "---\nname: Good\rdescription: Checks work\rtools: Read\r---\rbody",
        id="lone_carriage_returns_after_the_opening",
    ),
    pytest.param(_GOOD + "# limits\rtools: Read\n---\nbody", id="carriage_return_ending_a_comment"),
    pytest.param(
        _GOOD + "tools:\rmodel: haiku\n---\nbody", id="carriage_return_after_an_empty_key"
    ),
    *(
        pytest.param(f"---\nname: Good\ndescription: {trigger}\n{lines}\n---\nbody", id=case)
        for case, trigger, lines in (
            (
                "carriage_return_after_an_empty_key_under_a_retry",
                "Use when: reviewing code",
                "# limits\nnotes:\rtools: Read",
            ),
            (
                "carriage_return_after_an_empty_key_under_an_alias",
                "*Reviews* code",
                "notes:\rtools: Read",
            ),
            (
                "two_carriage_returns_after_an_empty_key_under_an_alias",
                "*Reviews* code",
                "notes:\r\rtools: Read",
            ),
        )
    ),
    # a value lists only when YAML reads it as written on its line
    *(
        pytest.param(_GOOD + body + "\n---\nbody", id=case)
        for case, body in (
            ("anchor_glued_to_an_open_flow_value", 'tools: &a[Read,"x\n  # ","*"]'),
            ("verbatim_tag_glued_to_an_open_quote", 'tools: !<!>"Read\n  # *"'),
            ("verbatim_tag_glued_to_a_block_header", "tools: !<!>|\n  # *"),
            ("hash_after_a_comma_in_a_flow_value", "# c\nnotes: [a,#b]\ntools: Read, Grep]"),
            ("open_flow_value_under_a_key_with_a_digit", "n1: [a,\ntools: Read"),
            ("flow_value_closed_on_the_next_key_line", "notes: [x,\ntools: Read]"),
            ("flow_map_closed_after_a_comment", "metadata: {author: x,\n# c\ntools: Read}"),
            ("tab_led_comment_inside_a_flow_value", "notes: [a,\t# ]\ntools: Read]"),
            ("tag_then_a_tab_then_an_open_quote", 'notes: !!str\t"x\ntools: Read\ny: z"'),
            ("tag_and_anchor_before_an_open_quote", 'tools: !!str &x "Read\n  # *"'),
            ("comment_after_the_tools", "tools: Read # Grep"),
            ("comment_after_the_model", "model: haiku # opus"),
            ("comment_only_tools", "tools: # c"),
            ("anchor_then_an_alias_as_the_tools", "tools: &a *"),
            ("quote_closed_then_text", 'model: ""opus"'),
            ("closed_quote_followed_by_text", 'tools: "Read" Grep'),
            ("space_after_a_closed_quote", 'model: "opus" '),
            ("space_after_a_json_list", 'tools: ["Read"] '),
            ("doubled_single_quote", "model: 'op''us'"),
            ("dash_led_tools", "tools: - Read"),
            ("plain_flow_list", "skills: [a, b]"),
            ("flow_map", "metadata: {author: x}"),
            ("json_list_of_objects", 'mcpServers: [{"github": {"type": "http"}}]'),
            ("flow_list_then_a_comment", "skills: [a, [b, c]] # two"),
            ("full_width_space_before_a_quote", 'tools: \u3000"Read\n# *"'),
            ("full_width_space_before_the_model", "model: \u3000opus"),
            ("byte_order_mark_after_the_model", "model: opus\ufeff"),
            ("colon_space_under_a_key_with_a_digit", "note2: Use when: reviewing"),
        )
    ),
    *(
        pytest.param(f"---\nname: Good\ndescription: {value}\n---\nbody", id=case)
        for case, value in (
            ("escaped_quote_in_the_description", '"Say \\"hi\\""'),
            ("single_quote_closed_inside_the_description", "'can't'"),
            ("comment_after_the_description", "Checks work # note"),
            ("description_ending_in_a_colon", "Use this agent when:"),
            ("one_line_folded_description", "> Checks work"),
            ("one_line_literal_description", "| Checks work"),
            ("noncharacter_in_the_description", "Checks \ufffe work"),
        )
    ),
    pytest.param('---\nname: #"Good"\ndescription: Checks work\n---\nbody', id="comment_as_name"),
    pytest.param(
        '---\nname: "Go"od\ndescription: Checks work\n---\nbody', id="quoted_name_then_text"
    ),
    pytest.param(
        "---\r\nname: Good\r\ndescription: Use when: reviewing\r\n---\r\nbody",
        id="colon_space_in_a_file_with_crlf_line_ends",
    ),
    # two keys, 254 comment lines and the empty line before the closing dashes: 257 lines
    pytest.param(_GOOD + "#\n" * 254 + "---\nbody", id="block_of_257_lines"),
    *(
        pytest.param(
            f"---\nname: {value}\ndescription: Checks work\n---\nbody",
            id=f"name_is_the_yaml_scalar_{value}",
        )
        for value in ("~", "null", "Null", "true", "FALSE", "42", "+7", "0o17", "0x1F", ".NaN")
    ),
    *(
        pytest.param(
            f"---\nname: Good\ndescription: {value}\n---\nbody",
            id=f"description_is_the_yaml_scalar_{value}",
        )
        for value in ("42", ".inf", ".NaN", "1.5e3", "-2.", ".5")
    ),
)

# what lists: blank and comment lines between key lines, every value read as written, and only
# what comes before the first `---` after the opening line, where the CLI ends the frontmatter
LISTED_AGENTS = (
    pytest.param(
        "---\nname: Good\n\n# a comment\n  # an indented comment\ndescription: Checks work\n---\n",
        (),
        (),
        id="blank_and_comment_lines",
    ),
    pytest.param(
        "---\nname: Good\ndescription: Checks work\nfuture: " + "x" * 5000 + "\n---\nbody",
        ("future",),
        (),
        id="unsupported_value_is_never_bounded",
    ),
    pytest.param(
        "---\nName: Other\nname: Good\ndescription: Checks work\n---\nbody",
        ("Name",),
        (),
        id="key_in_another_case_is_unsupported",
    ),
    pytest.param(
        "---\nname: Good\ndescription: Checks work\ntools: Read\nnotes: a --- b\n---\nbody",
        ("notes",),
        ("Read",),
        id="dashes_inside_a_value_after_the_tools",
    ),
    pytest.param(
        "---\nname: Good\ndescription: Checks work---\ntools: Read\n---\nbody",
        (),
        (),
        id="dashes_ending_the_description",
    ),
    pytest.param(
        "---\nname: Good\ndescription: Checks work\n# --- limits ---\ntools: Read, Grep\n"
        "model: opus\n---\nbody",
        (),
        (),
        id="dashes_inside_a_comment_before_the_tools_and_model",
    ),
    pytest.param(
        "---\r\nname: Good\r\ndescription: Checks work\r\ntools: Read\r\n---\r\nbody",
        (),
        ("Read",),
        id="carriage_return_line_ends",
    ),
    pytest.param(
        "---\t \r\nname: Good\r\ndescription: Checks work\r\n---\r\nbody",
        (),
        (),
        id="opening_with_a_tab_a_space_and_a_carriage_return",
    ),
    pytest.param(_GOOD + "tools: Read, Grep\n---\nbody", (), ("Read", "Grep"), id="plain_tools"),
    pytest.param(
        _GOOD + "tools: Bash(git status:*), Read\n---\nbody",
        (),
        ("Bash(git status:*)", "Read"),
        id="plain_tools_holding_a_colon",
    ),
    pytest.param(_GOOD + "tools:\tRead\n---\nbody", (), ("Read",), id="tab_after_the_colon"),
    pytest.param(_GOOD + "notes:\ntools: Read\n---\nbody", ("notes",), ("Read",), id="empty_value"),
    pytest.param(
        _GOOD + 'skills: ["a", "b"]\n---\nbody', ("skills",), (), id="json_list_of_strings"
    ),
    pytest.param(
        _GOOD + 'tools: "Read, Grep"\n# note\n---\nbody',
        (),
        ("Read", "Grep"),
        id="closed_double_quote",
    ),
    pytest.param(
        _GOOD + 'tools: ["Read", "Grep"]\n# note\n---\nbody',
        (),
        ("Read", "Grep"),
        id="json_tools",
    ),
    pytest.param(
        _GOOD + "tools: Read#1, Grep\n---\nbody",
        (),
        ("Read#1", "Grep"),
        id="plain_value_holding_a_hash",
    ),
    # two keys, 253 comment lines and the empty line before the closing dashes: 256 lines
    pytest.param(_GOOD + "#\n" * 253 + "---\nbody", (), (), id="block_of_256_lines"),
)

# the CLI's opening is `---`, any JavaScript white space, then a line feed
LISTED_WITH_A_MODEL = (
    *(
        pytest.param(
            kind, f"---{space}\n{_GOOD[4:]}model: opus\n---\nbody", "Good", "opus", id=case
        )
        for kind in ("agent", "skill")
        for case, space in (
            (f"{kind}_opening_with_a_no_break_space", "\u00a0"),
            (f"{kind}_opening_with_a_full_width_space", "\u3000"),
        )
    ),
    *(
        pytest.param(
            "skill", f"---{space}\n{_GOOD[4:]}model: opus\n---\nbody", "Good", "opus", id=case
        )
        for case, space in (
            ("skill_opening_with_a_vertical_tab", "\v"),
            ("skill_opening_with_a_form_feed", "\f"),
            ("skill_opening_with_u1680", "\u1680"),
            ("skill_opening_with_u2000", "\u2000"),
            ("skill_opening_with_u200a", "\u200a"),
            ("skill_opening_with_a_line_separator", "\u2028"),
            ("skill_opening_with_a_paragraph_separator", "\u2029"),
            ("skill_opening_with_u202f", "\u202f"),
            ("skill_opening_with_u205f", "\u205f"),
            ("skill_opening_with_a_byte_order_mark", "\ufeff"),
            ("skill_opening_with_a_full_width_space_line", "\n\u3000"),
        )
    ),
    pytest.param(
        "skill",
        "---\rname: Good\rmodel: opus\r---\rbody",
        "one",
        None,
        id="skill_with_no_line_feed_has_no_frontmatter",
    ),
)

# `: ` inside a plain value: the CLI's retry quotes the line, so the agent loads with this text
LISTED_DESCRIPTIONS = (
    pytest.param(
        "---\nname: Good\ndescription: Use when: reviewing\n---\nbody",
        "Use when: reviewing",
        id="colon_space_in_the_description",
    ),
    pytest.param(
        "---\nname: Good\ndescription: Use when: reviewing\nnotes:\r\ntools: Read\n---\nbody",
        "Use when: reviewing",
        id="colon_space_on_a_line_ending_in_a_line_feed_alone",
    ),
    pytest.param(
        "---\nname: Good\ndescription: Use this agent when the user asks for a review. Examples: "
        '<example>Context: the user wrote code. user: "Review this" assistant: "I will use the '
        'Good agent"</example>\n---\nbody',
        "Use this agent when the user asks for a review. Examples: <example>Context: the user "
        'wrote code. user: "Review this" assistant: "I will use the Good agent"</example>',
        id="generated_agent_description",
    ),
)

REFUSED_SKILLS = (
    pytest.param(
        "---\nname: one\ndescription: *Reviews* code\rmodel: opus\n---\nbody",
        id="skill_carriage_return_after_an_alias",
    ),
)


@pytest.mark.parametrize("frontmatter", REFUSED_AGENTS)
def test_a_refused_agent_is_malformed_metadata(tmp_path: Path, frontmatter: str) -> None:
    _write(tmp_path / ".claude" / "agents" / "one.md", frontmatter)
    inspector, owner = _inspector(tmp_path)

    result = inspector.inspect(f"claude:workspace:{owner.workspace.workspace_id}")

    assert result.assets == ()
    assert result.partial_reasons == ("malformed_metadata",)


def test_an_agent_with_name_and_description_still_lists(tmp_path: Path) -> None:
    _write(
        tmp_path / ".claude" / "agents" / "one.md",
        "---\nname: Good\ndescription: Checks work\n---\nbody",
    )
    inspector, owner = _inspector(tmp_path)

    result = inspector.inspect(f"claude:workspace:{owner.workspace.workspace_id}")

    assert [asset.name for asset in result.assets] == ["Good"]
    assert result.assets[0].description == "Checks work"


@pytest.mark.parametrize(("frontmatter", "unsupported", "tools"), LISTED_AGENTS)
def test_an_agent_the_cli_would_read_lists(
    tmp_path: Path, frontmatter: str, unsupported: tuple[str, ...], tools: tuple[str, ...]
) -> None:
    _write(tmp_path / ".claude" / "agents" / "one.md", frontmatter)
    inspector, owner = _inspector(tmp_path)

    result = inspector.inspect(f"claude:workspace:{owner.workspace.workspace_id}")

    assert [asset.name for asset in result.assets] == ["Good"]
    assert result.assets[0].description == "Checks work"
    assert result.assets[0].model is None
    assert result.assets[0].tools == tools
    assert result.assets[0].unsupported_fields == unsupported
    assert not result.partial


@pytest.mark.parametrize(("kind", "text", "name", "model"), LISTED_WITH_A_MODEL)
def test_an_entry_lists_with_the_model_the_cli_applies(
    tmp_path: Path, kind: str, text: str, name: str, model: str | None
) -> None:
    _write(
        tmp_path / ".claude" / ("agents/one.md" if kind == "agent" else "skills/one/SKILL.md"),
        text,
    )
    inspector, owner = _inspector(tmp_path)

    result = inspector.inspect(f"claude:workspace:{owner.workspace.workspace_id}")

    assert [(asset.name, asset.model) for asset in result.assets] == [(name, model)]
    assert not result.partial


@pytest.mark.parametrize(("frontmatter", "description"), LISTED_DESCRIPTIONS)
def test_an_agent_lists_with_the_description_the_cli_reads(
    tmp_path: Path, frontmatter: str, description: str
) -> None:
    _write(tmp_path / ".claude" / "agents" / "one.md", frontmatter)
    inspector, owner = _inspector(tmp_path)

    result = inspector.inspect(f"claude:workspace:{owner.workspace.workspace_id}")

    assert [(asset.name, asset.description) for asset in result.assets] == [("Good", description)]
    assert not result.partial


@pytest.mark.parametrize("text", REFUSED_SKILLS)
def test_a_refused_skill_is_malformed_metadata(tmp_path: Path, text: str) -> None:
    _write(tmp_path / ".claude" / "skills" / "one" / "SKILL.md", text)
    inspector, owner = _inspector(tmp_path)

    result = inspector.inspect(f"claude:workspace:{owner.workspace.workspace_id}")

    assert result.assets == ()
    assert result.partial_reasons == ("malformed_metadata",)


def test_a_folder_that_changed_is_left_out_without_a_count(tmp_path: Path) -> None:
    (tmp_path / "agents").mkdir()
    budget = native_assets_module._Budget(expires_at=1e12)

    NativeAssetInspector._directory_failure(_root(tmp_path), ("agents",), budget)

    assert budget.reasons == {"changed"}
    assert budget.omitted == 0


def test_a_folder_that_cannot_be_read_is_left_out_without_a_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "agents").mkdir()
    root = _root(tmp_path)
    budget = native_assets_module._Budget(expires_at=1e12)

    def lstat(self: Path) -> os.stat_result:
        raise PermissionError

    monkeypatch.setattr(Path, "lstat", lstat)

    NativeAssetInspector._directory_failure(root, ("agents",), budget)

    assert budget.reasons == {"unreadable"}
    assert budget.omitted == 0


@pytest.mark.parametrize(
    "value", ('"42"', "'true'", "nullable", "1.2.3", "0x", "1_000", "null value")
)
def test_a_value_only_shaped_like_a_yaml_scalar_still_lists(tmp_path: Path, value: str) -> None:
    _write(
        tmp_path / ".claude" / "agents" / "one.md",
        f"---\nname: {value}\ndescription: {value}\n---\nbody",
    )
    inspector, owner = _inspector(tmp_path)

    result = inspector.inspect(f"claude:workspace:{owner.workspace.workspace_id}")

    assert [asset.name for asset in result.assets] == [value.strip("'\"")]
    assert not result.partial


def test_a_skill_named_like_a_yaml_scalar_still_lists(tmp_path: Path) -> None:
    _write(tmp_path / ".claude" / "skills" / "one" / "SKILL.md", "---\nname: 42\n---\nbody")
    inspector, owner = _inspector(tmp_path)

    result = inspector.inspect(f"claude:workspace:{owner.workspace.workspace_id}")

    assert [asset.name for asset in result.assets] == ["42"]
    assert not result.partial


def test_a_skill_key_in_another_case_is_unsupported_and_the_name_is_the_folder(
    tmp_path: Path,
) -> None:
    _write(tmp_path / ".claude" / "skills" / "one" / "SKILL.md", "---\nName: Loud\n---\nbody")
    inspector, owner = _inspector(tmp_path)

    result = inspector.inspect(f"claude:workspace:{owner.workspace.workspace_id}")

    assert [asset.name for asset in result.assets] == ["one"]
    assert result.assets[0].unsupported_fields == ("Name",)


@pytest.mark.parametrize(
    ("description", "cut_to"),
    (
        ("d" * 3000, "d" * 2048),
        ("あ" * 1000, "あ" * 682),
        ("d" * 2048, "d" * 2048),
    ),
    ids=("ascii", "cut_inside_a_multibyte_character", "exact_bound_is_not_cut"),
)
@pytest.mark.parametrize("kind", ("agent", "skill"))
def test_an_overlong_description_is_cut_and_listed(
    tmp_path: Path, kind: str, description: str, cut_to: str
) -> None:
    text = f"---\nname: Good\ndescription: {description}\n---\nbody"
    _write(
        tmp_path / ".claude" / ("agents/one.md" if kind == "agent" else "skills/one/SKILL.md"),
        text,
    )
    inspector, owner = _inspector(tmp_path)

    result = inspector.inspect(f"claude:workspace:{owner.workspace.workspace_id}")

    assert [asset.description for asset in result.assets] == [cut_to]
    assert len(cut_to.encode("utf-8")) <= 2048
    was_cut = len(description.encode("utf-8")) > 2048
    assert result.partial is was_cut
    assert result.partial_reasons == (("oversized",) if was_cut else ())
    assert result.omitted == 0
    assert result.assets[0].digest == hashlib.sha256(text.encode()).hexdigest()


def test_claude_user_root_reads_only_when_the_cli_would_read_it(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    resolved = claude_user_root(home, config_dir_set=False)

    assert resolved is not None
    assert resolved.display == str(home.resolve())
    assert claude_user_root(home, config_dir_set=True) is None
    assert claude_user_root(tmp_path / "missing", config_dir_set=False) is None
    file_path = tmp_path / "not-a-folder"
    file_path.write_text("x", encoding="utf-8")
    assert claude_user_root(file_path, config_dir_set=False) is None
