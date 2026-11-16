"""MCP JSON import uses synthetic definitions and does not connect to servers."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from tests.profiles.test_profiles_api import HEADERS, client
from viki_agent.profiles.mcp_config import MAX_IMPORT_BYTES, McpConfigError, import_configuration

URL = "/api/v2/profiles/mcp/import"


@pytest.mark.parametrize("root", ["mcpServers", "servers"])
def test_imports_http_and_stdio_without_changing_names_or_arguments(root: str) -> None:
    result = import_configuration(
        json.dumps(
            {
                root: {
                    "remote": {"type": "http", "url": "https://example.com/mcp"},
                    "echo": {"command": "example-mcp", "args": ["a b", "--stdio", ""]},
                }
            }
        )
    )
    assert result == {
        "servers": [
            {
                "name": "remote",
                "transport": "http",
                "command": None,
                "args": [],
                "url": "https://example.com/mcp",
                "note": None,
            },
            {
                "name": "echo",
                "transport": "stdio",
                "command": "example-mcp",
                "args": ["a b", "--stdio", ""],
                "url": None,
                "note": None,
            },
        ]
    }


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ('{"mcpServers":{"echo":{"command":"a","command":"b"}}}', "invalid_json"),
        ('{"mcpServers":{"echo":{},"echo":{}}}', "invalid_json"),
        ("{", "invalid_json"),
        ("[]", "invalid_type"),
        ('{"mcpServers":{}}', "invalid_count"),
        ('{"mcpServers":{},"servers":{}}', "unknown_key"),
        ('{"servers":{},"inputs":[]}', "unknown_key"),
        ('{"mcpServers":{"bad name":{"command":"a"}}}', "invalid_name"),
        ('{"mcpServers":{"echo":{"type":"sse","url":"https://example.com"}}}', "invalid_transport"),
        ('{"mcpServers":{"echo":{"command":"a","env":{}}}}', "unsupported_import"),
        (
            '{"mcpServers":{"echo":{"url":"https://example.com","headers":{}}}}',
            "unsupported_import",
        ),
        ('{"mcpServers":{"echo":{"command":"a","args":["${EXAMPLE}"]}}}', "forbidden_text"),
        ('{"mcpServers":{"echo":{"url":"https://example.com?token=value"}}}', "secret_value"),
        ('{"mcpServers":{"echo":{"url":"http://remote.example/mcp"}}}', "invalid_url"),
        (
            '{"mcpServers":{"echo":{"command":"a","url":"https://example.com"}}}',
            "invalid_combination",
        ),
        ('{"mcpServers":{"echo":{"command":"a","args":[NaN]}}}', "invalid_type"),
        pytest.param(" " * (MAX_IMPORT_BYTES + 1), "invalid_length", id="too-large"),
        ("\ud800", "forbidden_text"),
    ],
)
def test_refuses_the_whole_import(source: str, reason: str) -> None:
    with pytest.raises(McpConfigError) as caught:
        import_configuration(source)
    assert caught.value.code == reason


def test_import_keeps_the_profile_caps_and_secret_screening() -> None:
    for entries in (
        {f"echo{i}": {"command": "a"} for i in range(9)},
        {"echo": {"command": "a", "args": ["x"] * 33}},
        {"echo": {"command": "a", "args": ["x" * 48]}},
    ):
        with pytest.raises(McpConfigError):
            import_configuration(json.dumps({"mcpServers": entries}))


def test_authenticated_import_is_a_preview_and_errors_do_not_echo_source(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with client(tmp_path) as api:
        response = api.post(
            URL, headers=HEADERS, json={"json": '{"mcpServers":{"echo":{"command":"example-mcp"}}}'}
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["servers"][0]["name"] == "echo"
        refused = api.post(
            URL,
            headers=HEADERS,
            json={"json": '{"servers":{"echo":{"private-example-key":"private-example-value"}}}'},
        )
        assert refused.status_code == 400
        assert refused.json()["error"]["reason"] == "unsupported_import"
        assert "private-example" not in refused.text
        assert api.get("/api/v2/profiles", headers=HEADERS).json() == {"profiles": []}
    assert "private-example" not in caplog.text
    assert "example-mcp" not in caplog.text


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 401),
        ({"Authorization": "Bearer synthetic"}, 403),
        ({**HEADERS, "Origin": "https://untrusted.example"}, 403),
    ],
)
def test_import_requires_token_and_origin(tmp_path: Path, headers: dict, status: int) -> None:
    with client(tmp_path) as api:
        response = api.post(URL, headers=headers, json={"json": "{}"})
        assert response.status_code == status
        assert response.headers["cache-control"] == "no-store"
