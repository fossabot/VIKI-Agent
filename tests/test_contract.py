"""events.py and frontend/src/protocol.ts describe the same contract."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Literal, get_args, get_origin, get_type_hints

from viki_agent import events, session
from viki_agent.conversations.artifacts import ArtifactDetailRef, ArtifactResult, ArtifactStart
from viki_agent.turn_input import Attachment

PROTOCOL = (Path(__file__).parents[1] / "frontend" / "src" / "protocol.ts").read_text(
    encoding="utf-8"
)
UNLOAD = (Path(__file__).parents[1] / "frontend" / "src" / "state" / "unload.ts").read_text(
    encoding="utf-8"
)
NESTED = (
    events.CodeCopy,
    ArtifactStart,
    ArtifactDetailRef,
    ArtifactResult,
    events.Command,
    events.ModelOption,
    events.QuestionOption,
    events.Question,
    events.ToolRow,
    events.Usage,
    events.OtherSession,
    events.SessionRow,
    events.SearchHit,
    events.SessionsResult,
    events.SearchResult,
    events.AgentInfo,
    events.GateRow,
    events.Routine,
    events.VaultCounts,
    events.GitChip,
    events.Rails,
)


def _ts_interfaces() -> dict[str, set[str]]:
    found = {}
    for name, body in re.findall(r"export interface (\w+) \{([^{}]*)\}", PROTOCOL):
        found[name] = set(re.findall(r"^\s*(\w+)\??:", body, flags=re.M))
    return found


def _ts_event_map() -> dict[str, str]:
    body = re.search(r"export interface EventMap \{([^{}]*)\}", PROTOCOL)
    assert body, "EventMap not found"
    return dict(re.findall(r'^\s*"?([\w.]+)"?: (\w+);', body.group(1), flags=re.M))


def _fields(cls: type) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


def test_every_event_type_has_the_same_data_fields() -> None:
    interfaces = _ts_interfaces()
    event_map = _ts_event_map()
    assert set(event_map) == set(events.EVENTS)
    for event_type, cls in events.EVENTS.items():
        assert interfaces[event_map[event_type]] == _fields(cls), event_type


def test_attachment_fields_match_and_text_only_wire_is_unchanged() -> None:
    text = (Path(__file__).parents[1] / "frontend/src/turnInput.ts").read_text(encoding="utf-8")
    body = re.search(r"export interface Attachment \{([^{}]*)\}", text)
    assert body is not None
    assert set(re.findall(r"^\s*(\w+)\??:", body.group(1), flags=re.M)) == _fields(Attachment)
    assert events.UserMessage(text="hello").data() == {"text": "hello", "command": "", "at": ""}
    attachment = Attachment(
        asset_id="44444444-4444-4444-8444-444444444444",
        media_type="image/png",
        byte_size=100,
        width=1,
        height=1,
        name="screen.png",
    )
    assert events.UserMessage(text="", attachments=(attachment,)).data()["attachments"] == [
        attachment.data()
    ]


def test_nested_shapes_match() -> None:
    interfaces = _ts_interfaces()
    for cls in NESTED:
        assert interfaces[cls.__name__] == _fields(cls), cls.__name__


def _ts_literals(text: str) -> set[str]:
    """Every string literal in a type, the empty one included (`reason: "" | "time_budget"`)."""
    return set(re.findall(r'"([\w.]*)"', text))


def _ts_aliases() -> dict[str, set[str]]:
    """`export type X = "a" | "b";` → its string literals, for every alias made only of string
    literals (doc comments allowed). Other aliases are not literal unions and are left out, so
    a field typed with one is compared by name only."""
    found = {}
    for name, body in re.findall(r"export type (\w+) =([^;]*);", PROTOCOL):
        bare = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        if re.fullmatch(r'\s*(\|?\s*"[\w.]*"\s*)+', bare):
            found[name] = _ts_literals(bare)
    return found


def _ts_literal_fields() -> dict[str, dict[str, set[str]]]:
    """Per interface, the fields typed as string literals (inline, or through an alias)."""
    aliases = _ts_aliases()
    found: dict[str, dict[str, set[str]]] = {}
    for name, body in re.findall(r"export interface (\w+) \{([^{}]*)\}", PROTOCOL):
        fields = {}
        for field_name, field_type in re.findall(r"^\s*(\w+)\??: ([^;]+);", body, flags=re.M):
            if '"' in field_type:
                fields[field_name] = _ts_literals(field_type)
            elif field_type.strip() in aliases:
                fields[field_name] = aliases[field_type.strip()]
        found[name] = fields
    return found


def _py_literal_fields(cls: type) -> dict[str, set[str]]:
    hints = get_type_hints(cls)
    return {
        name: set(get_args(hint)) for name, hint in hints.items() if get_origin(hint) is Literal
    }


def test_literal_unions_match() -> None:
    """ErrorCode, TurnStatus and every inline literal field carry the same values on both sides
    (the field-name checks above would not notice a value added or dropped on one side)."""
    aliases = _ts_aliases()
    assert aliases["TurnStatus"] == set(get_args(events.TurnStatus))
    assert aliases["ErrorCode"] == set(get_args(events.ErrorCode))
    literal_fields = _ts_literal_fields()
    event_map = _ts_event_map()
    for event_type, cls in events.EVENTS.items():
        assert literal_fields[event_map[event_type]] == _py_literal_fields(cls), event_type
    for cls in NESTED:
        assert literal_fields[cls.__name__] == _py_literal_fields(cls), cls.__name__


def test_client_messages_match() -> None:
    body = PROTOCOL.split("export type ClientMessage =", 1)[1]
    ts: dict[str, set[str]] = {}
    for variant in re.findall(r"\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", body):
        kind = re.search(r'type: "([\w.]+)"', variant)
        if kind:
            fields = re.findall(r"(\w+)(\??):", variant)
            ts[kind.group(1)] = {
                ("?" if opt else "") + name for name, opt in fields if name != "type"
            }
    expected = {kind: set(keys) for kind, keys in events.CLIENT_MESSAGES.items()}
    assert ts == expected


def test_the_write_capable_tools_match() -> None:
    """The retry review names the vault-changing tools in TypeScript (unload.ts); session.py
    names them in Python. A tool added to one list and not the other would let a retry skip
    its warning about changed files."""
    block = re.search(r"WRITE_TOOLS: readonly string\[\] = \[([^\]]*)\]", UNLOAD)
    assert block, "WRITE_TOOLS not found in unload.ts"
    assert set(re.findall(r'"(\w+)"', block.group(1))) == set(session.WRITE_TOOLS)


def test_envelopes_serialise() -> None:
    event = events.ToolStarted(
        block_id="toolu_1", tool="Read", row=events.ToolRow(head="Read", rest="a.md")
    )
    assert event.envelope("s1", 7, "generation-1") == {
        "v": 2,
        "generation": "generation-1",
        "type": "tool.started",
        "session_id": "s1",
        "seq": 7,
        "data": {
            "block_id": "toolu_1",
            "tool": "Read",
            "row": {"head": "Read", "rest": "a.md", "tail": ""},
            "parent_id": "",
        },
    }


def test_steer_events_serialise() -> None:
    assert events.SteerPending(steer_id="st_1", text="go faster").data() == {
        "steer_id": "st_1",
        "text": "go faster",
        "command": "",
        "images": 0,
        "at": "",
    }
    assert events.SteerEnded(steer_id="st_1", outcome="started").data() == {
        "steer_id": "st_1",
        "outcome": "started",
    }
