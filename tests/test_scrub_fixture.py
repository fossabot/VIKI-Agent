"""Tests with no other home: scrub_fixture.py's joined-delta guard and the four-backslash
home-path form, plus measure_catalog.py's --real promise to print no path.

tests/test_fixtures_clean.py already covers scrub_text/problems/kept_entries against committed
fixtures; this file is for the two scripts' own internals that no fixture exercises.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest

TESTS = Path(__file__).parent
REPO = TESTS.parent
sys.path.insert(0, str(REPO / "scripts"))

import measure_catalog  # noqa: E402
import scrub_fixture  # noqa: E402
from scrub_fixture import OWNER, problems, scrub_text  # noqa: E402
from scrub_fixture import main as scrub_main  # noqa: E402


def _stream_event(*, index: int, event: dict[str, object]) -> str:
    return json.dumps({"dir": "out", "frame": {"type": "stream_event", "event": event}})


def test_scrub_main_refuses_a_name_split_across_streamed_deltas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CLI stream can split one string across consecutive deltas of the same content block,
    as it once did with a maintainer's username in tests/fixtures/claude/subagent-explore.jsonl
    (lines 17-18). Per-line scrubbing never sees the joined string, so `main()` must reassemble
    each block and refuse to write if the rebuilt text is still on the banned list."""
    # A synthetic, non-generic name: the real account can be a CI runner's generic one
    # (GENERIC_ACCOUNTS), which the banned-list check must not skip here.
    name = "someone"
    monkeypatch.setattr(scrub_fixture, "OWNER", name)
    half = len(name) // 2
    raw = tmp_path / "raw.jsonl"
    dest = tmp_path / "out.jsonl"
    lines = [
        json.dumps({"dir": "in", "frame": {"request": {"subtype": "initialize"}}}),
        _stream_event(index=0, event={"type": "content_block_start", "index": 0}),
        _stream_event(
            index=0,
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": name[:half]},
            },
        ),
        _stream_event(
            index=0,
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": name[half:]},
            },
        ),
        _stream_event(index=0, event={"type": "content_block_stop", "index": 0}),
    ]
    raw.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert scrub_main(raw, dest, tmp_path) == 1
    assert not dest.exists()


def test_scrub_main_refuses_a_name_split_across_streamed_thinking_deltas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """joined_block_problems must also reassemble thinking_delta pieces (delta["thinking"]), not
    only text_delta and input_json_delta, so a name split over two thinking frames is caught."""
    name = "someone"
    monkeypatch.setattr(scrub_fixture, "OWNER", name)
    half = len(name) // 2
    raw = tmp_path / "raw.jsonl"
    dest = tmp_path / "out.jsonl"
    lines = [
        json.dumps({"dir": "in", "frame": {"request": {"subtype": "initialize"}}}),
        _stream_event(index=0, event={"type": "content_block_start", "index": 0}),
        _stream_event(
            index=0,
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": name[:half]},
            },
        ),
        _stream_event(
            index=0,
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": name[half:]},
            },
        ),
        _stream_event(index=0, event={"type": "content_block_stop", "index": 0}),
    ]
    raw.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert scrub_main(raw, dest, tmp_path) == 1
    assert not dest.exists()


def test_scrub_main_accepts_the_same_name_whole_in_one_thinking_delta(tmp_path: Path) -> None:
    """The companion case: a banned string already whole on one line is scrubbed by the ordinary
    per-line pass before the joined-block check runs, same as a text_delta frame."""
    raw = tmp_path / "raw.jsonl"
    dest = tmp_path / "out.jsonl"
    lines = [
        json.dumps({"dir": "in", "frame": {"request": {"subtype": "initialize"}}}),
        _stream_event(index=0, event={"type": "content_block_start", "index": 0}),
        _stream_event(
            index=0,
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": f"note about {OWNER}"},
            },
        ),
        _stream_event(index=0, event={"type": "content_block_stop", "index": 0}),
    ]
    raw.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert scrub_main(raw, dest, tmp_path) == 0
    assert dest.exists()


def test_scrub_main_accepts_the_same_name_whole_in_one_delta(tmp_path: Path) -> None:
    """The companion case: nothing about the joined-block pass should refuse a capture whose
    banned string already sits whole on one line, once scrub_line has replaced it."""
    raw = tmp_path / "raw.jsonl"
    dest = tmp_path / "out.jsonl"
    lines = [
        json.dumps({"dir": "in", "frame": {"request": {"subtype": "initialize"}}}),
        _stream_event(index=0, event={"type": "content_block_start", "index": 0}),
        _stream_event(
            index=0,
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": f"note about {OWNER}"},
            },
        ),
        _stream_event(index=0, event={"type": "content_block_stop", "index": 0}),
    ]
    raw.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert scrub_main(raw, dest, tmp_path) == 0
    assert dest.exists()


def test_problems_flags_the_double_escaped_home_path() -> None:
    # partial_json is itself JSON text embedded in the (also JSON) capture line, so a raw path
    # backslash surfaces as four backslash characters, not the one or two the other BANNED
    # entries cover (subagent-explore.jsonl lines 17/39 once slipped past on this gap alone).
    four = "\\" * 4
    text = f'"partial_json": "C:{four}Users{four}"'
    assert problems(text) != []


def test_problems_does_not_flag_a_single_escaped_home_path_as_double() -> None:
    two = "\\" * 2
    text = f'"cwd": "C:{two}Users{two}owner"'
    # already scrubbed to two backslashes; the four-backslash rule must not over-match it
    assert "c:" + "\\" * 4 + "users" not in problems(text)


def test_scrub_text_leaves_a_generic_ci_account_name_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    # A CI runner's account (root, runner, ...) is ordinary English text, not personal data.
    monkeypatch.setattr(scrub_fixture, "OWNER", "runner")
    text = scrub_text(json.dumps({"who": "runner started this build"}), Path.home())
    assert json.loads(text)["who"] == "runner started this build"
    assert problems(text) == []


def test_problems_still_flags_a_non_generic_account_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scrub_fixture, "OWNER", "someone")
    assert problems('"who": "someone built this"') == ["someone"]


def test_path_forms_on_posix_has_no_backslash_variants() -> None:
    # Windows-hosted test exercising the POSIX branch: a path with no backslash to escape.
    home = PurePosixPath("/home/someone")
    assert scrub_fixture._path_forms(home, "HOME", "linux") == [  # noqa: SLF001 - the fn under test
        ("/home/someone", "%HOME%"),
        ("-home-someone", "%HOME-SLUG%"),
    ]


def test_slug_path_posix_flags_a_project_slug_naming_another_account() -> None:
    # Built from parts, never as one literal, so the guard scanning this file does not trip on it.
    other = "projects/" + "-home-" + "someone-notes"
    assert problems(f'"{other}"') != []


def test_slug_path_posix_passes_the_owner_slug() -> None:
    owned = "projects/" + "-home-" + "owner-notes"
    assert problems(f'"{owned}"') == []


# Made-up ids, joined at run time so the guard scanning this file never sees a synced folder whole.
SYNCED_IDS = "12345678-1234-4123-8123-123456789abc" + "_" + "87654321-4321-4321-8321-cba987654321"


def _synced_path(separator: str) -> str:
    return separator.join(["%HOME%", ".claude", "plugins", "syn" + "ced", SYNCED_IDS, "a-plugin"])


@pytest.mark.parametrize("separator", ["\\" * 2, "\\" * 4, "/"])
def test_scrub_text_replaces_a_synced_plugin_folders_account_ids(separator: str) -> None:
    # Claude Code names a claude.ai-synced plugin's folder after the account's ids (2.1.281's
    # system/init `plugins` list): the same identity BY_KEY replaces in the account block.
    text = scrub_text(json.dumps({"path": "x"}).replace("x", _synced_path(separator)), Path.home())
    assert SYNCED_IDS not in text
    assert scrub_fixture.SYNCED_PLACEHOLDER in text
    assert problems(text) == []


def test_problems_flags_an_unscrubbed_synced_plugin_folder() -> None:
    assert problems('"path": "' + _synced_path("\\" * 2) + '"') != []


def test_scrub_main_redacts_mcp_status_env_headers_and_a_foreign_url(tmp_path: Path) -> None:
    """A synthetic `mcp_status` reply: a stdio server's `config.env` (2.1.281's stdio rows carry
    `command` and `args`, never `env`), an http server's `config.headers` and that server's
    `config.url` all carry secret-shaped values that scrub_line's per-key pass never sees (they are
    not in BY_KEY). An app server (`source: dynamic`, name starting `viki_`) keeps its url; the
    other servers are redacted (the next tests cover an app server's args and the two ways a
    server can fail to be an app one)."""
    token = "ghp_" + "Z" * 36
    bearer = "Bearer " + "q" * 50  # long enough to trip BEARER_TOKEN's 20-char floor
    foreign_url = "https://mcp.example.com/mcp"
    app_url = "https://app-owned.example.com/keep"
    status_reply = {
        "dir": "out",
        "frame": {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": "req_6_x",
                "response": {
                    "mcpServers": [
                        {
                            "name": "viki_echo",
                            "status": "connected",
                            "source": "dynamic",
                            "config": {"type": "http", "url": app_url},
                        },
                        {
                            "name": "github",
                            "status": "connected",
                            "config": {
                                "type": "stdio",
                                "command": "npx",
                                "args": ["--yes", token],
                                "env": {"TOKEN": token},
                            },
                        },
                        {
                            "name": "remote",
                            "status": "connected",
                            "config": {
                                "type": "http",
                                "url": foreign_url,
                                "headers": {"Authorization": bearer},
                            },
                        },
                    ]
                },
            },
        },
    }
    raw = tmp_path / "raw.jsonl"
    dest = tmp_path / "out.jsonl"
    lines = [
        json.dumps({"dir": "in", "frame": {"request": {"subtype": "initialize"}}}),
        json.dumps(status_reply),
    ]
    raw.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert scrub_main(raw, dest, tmp_path) == 0
    text = dest.read_text(encoding="utf-8")
    assert token not in text  # neither the env value nor the args entry that repeats it
    assert bearer not in text
    assert foreign_url not in text
    assert app_url in text  # the app server's url is kept: source dynamic, name viki_
    assert problems(json.dumps(status_reply)) != []


def test_redact_mcp_entry_keeps_an_app_servers_args() -> None:
    """The committed fixture's one entry (source `dynamic`, name `viki_echo`) is a stdio server
    whose `config.args` must survive a re-scrub unchanged."""
    entry = {
        "name": "viki_echo",
        "source": "dynamic",
        "config": {"type": "stdio", "command": "python", "args": ["echo_server.py"]},
    }
    redacted = cast(dict[str, Any], scrub_fixture._redact_mcp_entry(entry))  # noqa: SLF001
    assert redacted["config"]["args"] == ["echo_server.py"]


def test_redact_mcp_entry_scrubs_a_viki_named_servers_url_and_args_of_the_wrong_source() -> None:
    """The word "app" means `source == "dynamic"` *and* the `viki_` name; a same-named server the
    CLI reports from any other source is redacted like any other server."""
    entry = {
        "name": "viki_x",
        "source": "user",
        "config": {
            "type": "http",
            "url": "https://elsewhere.example.com",
            "args": ["--flag"],
        },
    }
    redacted = cast(dict[str, Any], scrub_fixture._redact_mcp_entry(entry))  # noqa: SLF001
    assert redacted["config"]["url"] == scrub_fixture.MCP_VALUE_PLACEHOLDER
    assert redacted["config"]["args"] == [scrub_fixture.MCP_VALUE_PLACEHOLDER]


def test_redact_mcp_entry_scrubs_a_dynamic_servers_url_and_args_without_the_viki_name() -> None:
    """The other half of the rule: a `dynamic` server the app did not add (a browser's or an
    IDE's) has no `viki_` name, so it is redacted like any other server."""
    entry = {
        "name": "ide",
        "source": "dynamic",
        "config": {
            "type": "http",
            "url": "https://elsewhere.example.com",
            "args": ["--flag"],
        },
    }
    redacted = cast(dict[str, Any], scrub_fixture._redact_mcp_entry(entry))  # noqa: SLF001
    assert redacted["config"]["url"] == scrub_fixture.MCP_VALUE_PLACEHOLDER
    assert redacted["config"]["args"] == [scrub_fixture.MCP_VALUE_PLACEHOLDER]


@pytest.mark.parametrize(
    "token",
    [
        # The JWT, base64 and tilde tokens' first run of [A-Za-z0-9_-] is under 20, so only the
        # widened set reaches the floor; the ghp_ token reaches it either way.
        "x" * 10 + "." + "y" * 10 + "." + "z" * 10,  # JWT-shaped: dot-separated segments
        "ghp_" + "a" * 36,  # a GitHub personal access token shape
        "ab+/" * 6 + "==",  # base64 with +, / and = padding
        "a" * 10 + "~" + "b" * 10,  # a tilde inside the token
    ],
)
def test_problems_flags_the_widened_bearer_shapes(token: str) -> None:
    found = problems(f"Authorization: Bearer {token}")
    assert found != []
    assert all(token not in match for match in found)  # the report never holds the whole token


def test_problems_bearer_floor_is_twenty_characters() -> None:
    assert problems("Authorization: Bearer " + "a" * 19) == []
    assert problems("Authorization: Bearer " + "a" * 20) != []


def test_measure_real_silences_library_loggers_before_printing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """config.py logs a path at WARNING when it skips an unreadable transcript. With no handler
    configured, Python's own fallback prints that straight to stderr — exactly what --real's
    docstring promises never happens. pytest's own logging plugin puts handlers on the root
    logger that would swallow the warning regardless of the fix, masking the exact gap this
    guards against, so the fresh-process state (no handler anywhere) is simulated in full."""
    root_log = logging.getLogger()
    pkg_log = logging.getLogger("viki_agent")
    child_log = logging.getLogger("viki_agent.config")
    loggers = (root_log, pkg_log, child_log)
    saved = [(lg.handlers[:], lg.propagate) for lg in loggers]
    try:
        for lg in loggers:
            lg.handlers.clear()
            lg.propagate = True
        measure_catalog._silence_library_warnings()  # noqa: SLF001 - the guard under test
        child_log.warning("ignoring unreadable %s: %s", "D:/vault/some-transcript.jsonl", "OSError")
        assert capsys.readouterr().err == ""
    finally:
        for lg, (handlers, propagate) in zip(loggers, saved, strict=True):
            lg.handlers[:] = handlers
            lg.propagate = propagate
