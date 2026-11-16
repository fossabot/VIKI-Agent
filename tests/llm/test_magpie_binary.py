"""Opt-in compatibility run of the packaged binary against a local synthetic model."""

from __future__ import annotations

import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from scripts.prepare_magpie import stage
from viki_agent.llm import magpie
from viki_agent.llm.connections import parse_connection
from viki_agent.llm.gateway import Gateway

pytestmark = pytest.mark.skipif(
    os.environ.get("VIKI_TEST_MAGPIE") != "1",
    reason="set VIKI_TEST_MAGPIE=1 for binary compatibility",
)


@pytest.mark.parametrize("packaged", [False, True], ids=["source", "installed-gzip"])
async def test_pinned_magpie_discovery_streaming_and_tool_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, packaged: bool
) -> None:
    binary = magpie.verified_bundle(magpie.bundle_path())
    assert binary.is_file()
    if packaged:
        installed = tmp_path / "installed"
        installed.mkdir()
        stage(magpie.compiled.INSTALL_DIR, installed)
        monkeypatch.setattr(magpie.compiled, "INSTALL_DIR", installed)
        monkeypatch.setattr(magpie.compiled, "COMPILED", True)
        binary = magpie.verified_bundle(magpie.bundle_path())
        assert binary.name == "magpie.gz"
    try:
        magpie._ports_free()
    except magpie.MagpieError:
        pytest.skip("dedicated Magpie ports are in use")

    seen: list[dict[str, Any]] = []

    class Model(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            payload = json.dumps({"object": "list", "data": [{"id": "model"}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:
            seen.append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
            if len(seen) == 1:
                delta = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_synthetic",
                            "type": "function",
                            "function": {"name": "Read", "arguments": '{"file_path":"example.md"}'},
                        }
                    ],
                }
                finish = "tool_calls"
            else:
                delta, finish = {"role": "assistant", "content": "Synthetic complete."}, "stop"
            payload = (
                "data: "
                + json.dumps(
                    {
                        "id": "synthetic-completion",
                        "object": "chat.completion.chunk",
                        "model": "model",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                        "usage": {"prompt_tokens": 12, "completion_tokens": 8},
                    }
                )
                + "\n\ndata: [DONE]\n\n"
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # A closed local proxy prevents catalog/update lookups from leaving this test.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        proxy_port = probe.getsockname()[1]
    proxy = f"http://127.0.0.1:{proxy_port}"
    synthetic_home = tmp_path / "home"
    synthetic_home.mkdir()
    environment = {
        key: os.environ[key] for key in ("SystemRoot", "WINDIR", "COMSPEC") if key in os.environ
    }
    environment.update(
        HOME=str(synthetic_home),
        USERPROFILE=str(synthetic_home),
        APPDATA=str(synthetic_home / "roaming"),
        LOCALAPPDATA=str(synthetic_home / "local"),
        TEMP=str(tmp_path),
        TMP=str(tmp_path),
        TMPDIR=str(tmp_path),
        PATH=str(Path(environment.get("SystemRoot", "/")) / "System32"),
        HTTP_PROXY=proxy,
        HTTPS_PROXY=proxy,
        ALL_PROXY=proxy,
        NO_PROXY="127.0.0.1,localhost",
        http_proxy=proxy,
        https_proxy=proxy,
        all_proxy=proxy,
        no_proxy="127.0.0.1,localhost",
    )
    monkeypatch.setattr(os, "environ", environment)
    make_environment = magpie._environment

    def isolated_environment(root: Path, key: str) -> dict[str, str]:
        env = make_environment(root, key)
        env["MAGPIE_NO_ACCOUNTS"] = "1"
        config = root / "config" / "magpie"
        (config / "settings.json").write_text(
            json.dumps(
                {
                    "noAutoUpdate": True,
                    "noAutoDownload": True,
                    "autoRestart": False,
                    "noAccountDiscovery": True,
                    "noStats": True,
                    "noUsageStats": True,
                    "requestArchive": False,
                    "requestArchiveLocal": False,
                    "gatewayConversations": False,
                }
            ),
            encoding="utf-8",
        )
        (config / "providers.json").write_text(
            json.dumps(
                {
                    "providers": [
                        {
                            "id": "synthetic",
                            "name": "Synthetic",
                            "key": "synthetic-key",
                            "chat": f"http://127.0.0.1:{server.server_port}/v1",
                            "models": ["model"],
                        }
                    ],
                    "noAutoGroups": True,
                }
            ),
            encoding="utf-8",
        )
        return env

    monkeypatch.setattr(magpie, "_environment", isolated_environment)
    app_data = tmp_path / "app"
    app_data.mkdir()
    service = magpie.Magpie(app_data)
    spawned: list[str] = []
    original_spawn = magpie.winproc.spawn

    async def capture(argv, **kwargs):
        spawned.append(str(argv[0]))
        return await original_spawn(argv, **kwargs)

    monkeypatch.setattr(magpie.winproc, "spawn", capture)
    gateway = Gateway(before_request=service.ensure_connection)
    connection = parse_connection(
        {
            "name": "Synthetic Magpie",
            "base_url": magpie.BASE_URL,
            "api_mode": "anthropic",
            "model": "synthetic/model",
            "api_key": "magpie-viki-agent",
        }
    )
    try:
        assert "synthetic/model" in await gateway.models(connection)
        assert len(spawned) == 1
        if packaged:
            assert Path(spawned[0]).is_relative_to(service.root / "runtime")
            assert Path(spawned[0]).read_bytes() != binary.read_bytes()
        else:
            assert spawned[0] == str(binary)
        messages: list[dict[str, Any]] = [{"role": "user", "content": "Synthetic request."}]
        body = {
            "messages": messages,
            "max_tokens": 128,
            "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
        }
        first = b"".join([part async for part in gateway.generate(connection, body)])
        events = [json.loads(line[6:]) for line in first.splitlines() if line.startswith(b"data: ")]
        tool = next(
            row["content_block"]
            for row in events
            if row.get("content_block", {}).get("type") == "tool_use"
        )
        assert tool["name"] == "Read"
        assert events[-1]["type"] == "message_stop"
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": [{**tool, "input": {"file_path": "example.md"}}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool["id"],
                            "content": "Synthetic data.",
                        }
                    ],
                },
            ]
        )
        second = b"".join([part async for part in gateway.generate(connection, body)])
        assert b"Synthetic complete." in second
        assert len(seen) == 2
        assert seen[0]["model"] == "model"
        assert any(message.get("role") == "tool" for message in seen[1]["messages"])
    finally:
        await service.close()
        server.shutdown()
        server.server_close()
        thread.join()
