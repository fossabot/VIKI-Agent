from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.llm.test_connections import AUTH, BASE, FORM, service
from tests.llm.test_gateway_lifecycle import Stream
from viki_agent.llm import api as api_module
from viki_agent.llm import gateway as gateway_module
from viki_agent.llm.api import connection_routes
from viki_agent.llm.connections import Connections, parse_connection
from viki_agent.llm.gateway import Gateway


def app(connections: Connections, gateway: Gateway) -> Starlette:
    return Starlette(
        routes=connection_routes(
            connections,
            gateway,
            authorized=lambda _: True,
            origins=frozenset({BASE}),
        )
    )


def test_key_renewal_preserves_identity_seals_key_and_keeps_active_snapshot(tmp_path: Path) -> None:
    settings, connections = service(tmp_path)
    row = parse_connection(FORM)
    connections.write(0, [row])
    gateway = Gateway()
    runtime = gateway.issue(row)
    key = "synthetic-renewed-key"
    with TestClient(app(connections, gateway), base_url=BASE) as client:
        path = "/api/v2/llm/connections/" + row.id
        result = client.patch(path, headers=AUTH, json={"expected_revision": 1, "api_key": key})
        assert result.status_code == 200
        renewed = connections.resolve(row.ref)
        assert renewed.id == row.id and renewed.ref == row.ref
        assert renewed.public() == row.public() and renewed.api_key == key
        assert runtime.connection.api_key == FORM["api_key"]
        assert gateway.issue(renewed).connection.api_key == key
        assert key not in result.text and key not in json.dumps(settings.export_values())
        stale = client.patch(path, headers=AUTH, json={"expected_revision": 1, "api_key": "other"})
        assert stale.status_code == 409 and connections.resolve(row.ref).api_key == key
        extra = client.patch(
            path,
            headers=AUTH,
            json={
                "expected_revision": result.json()["revision"],
                "api_key": "other",
                "base_url": "https://elsewhere.example",
            },
        )
        assert extra.status_code == 400 and connections.resolve(row.ref).api_key == key


@pytest.mark.parametrize("method", ["DELETE", "PATCH"])
@pytest.mark.parametrize("operation", ["test", "models"])
def test_connection_id_methods_cannot_dispatch_setup_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, operation: str
) -> None:
    _, connections = service(tmp_path)
    gateway = Gateway()
    calls: list[object] = []

    async def remote(connection: object) -> list[str]:
        calls.append(connection)
        return ["example/model"]

    monkeypatch.setattr(gateway, "test", remote)
    monkeypatch.setattr(gateway, "models", remote)
    with TestClient(app(connections, gateway), base_url=BASE) as client:
        response = client.request(
            method,
            "/api/v2/llm/connections/" + operation,
            headers=AUTH,
            json={"connection": FORM},
        )
        assert response.status_code == 400
    assert calls == []


@pytest.mark.parametrize("operation", ["test", "models"])
def test_setup_operations_still_dispatch_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    _, connections = service(tmp_path)
    gateway = Gateway()
    calls: list[object] = []

    async def remote(connection: object) -> list[str]:
        calls.append(connection)
        return ["example/model"]

    monkeypatch.setattr(gateway, "test", remote)
    monkeypatch.setattr(gateway, "models", remote)
    with TestClient(app(connections, gateway), base_url=BASE) as client:
        response = client.post(
            "/api/v2/llm/connections/" + operation,
            headers=AUTH,
            json={"connection": FORM},
        )
        assert response.status_code == 200
    assert len(calls) == 1


@pytest.mark.parametrize("operation", ["models", "test"])
async def test_setup_disconnect_cancels_and_awaits_owned_upstream(
    tmp_path: Path, operation: str
) -> None:
    _, connections = service(tmp_path)
    upstream = Stream([], block=True)
    gateway = Gateway(
        transport=httpx2.MockTransport(
            lambda _: httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=upstream,
            )
        )
    )
    disconnected = asyncio.Event()
    first = True

    async def receive() -> dict[str, Any]:
        nonlocal first
        if first:
            first = False
            return {
                "type": "http.request",
                "body": json.dumps({"connection": FORM}).encode(),
                "more_body": False,
            }
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(_: Any) -> None:
        pass

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v2/llm/connections/" + operation,
        "query_string": b"",
        "root_path": "",
        "server": ("127.0.0.1", 8765),
        "client": ("127.0.0.1", 1),
        "headers": [(b"origin", BASE.encode())],
    }
    existing_tasks = asyncio.all_tasks()
    task = asyncio.create_task(app(connections, gateway)(scope, receive, send))
    await upstream.entered.wait()
    disconnected.set()
    await asyncio.wait_for(task, 0.05)
    assert upstream.closed.is_set() and gateway._admitted == 0
    assert not (asyncio.all_tasks() - existing_tasks)


@pytest.mark.parametrize("operation", ["models", "test"])
async def test_setup_admits_before_intake_and_times_out_partial_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    _, connections = service(tmp_path)
    gateway = Gateway()
    release = gateway.admit()
    monkeypatch.setattr(gateway_module, "MAX_REQUESTS", 1)
    entered = asyncio.Event()
    messages: list[Any] = []

    async def receive() -> dict[str, Any]:
        entered.set()
        await asyncio.Event().wait()
        return {}

    async def send(message: Any) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v2/llm/connections/" + operation,
        "query_string": b"",
        "root_path": "",
        "server": ("127.0.0.1", 8765),
        "client": ("127.0.0.1", 1),
        "headers": [(b"origin", BASE.encode())],
    }
    await app(connections, gateway)(scope, receive, send)
    assert not entered.is_set() and messages[0]["status"] == 503
    release()
    messages.clear()
    monkeypatch.setattr(api_module, "INTAKE_S", 0.001)
    await app(connections, gateway)(scope, receive, send)
    assert entered.is_set() and messages[0]["status"] == 503
    assert gateway._admitted == 0
