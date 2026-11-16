"""diagnostics_api.py routes over a bare Starlette app (no server.py wiring, no port): Bearer,
Origin, the one-check-at-a-time rule and the provider_cli spawn gate."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette

from viki_agent.app import diagnostics
from viki_agent.app.diagnostics import ProviderChecks, Readiness
from viki_agent.app.diagnostics_api import (
    MAX_CHECK_BODY_BYTES,
    MAX_CHECK_REQUESTS,
    diagnostics_routes,
)
from viki_agent.domain import Capabilities, ProviderProblem, StorageReadiness

BASE = "http://127.0.0.1:8765"
TOKEN = "diagnostics-test-token"
AUTH = {"authorization": f"Bearer {TOKEN}"}
MUTATION = {**AUTH, "origin": BASE}
PATH = "/api/v2/diagnostics/checks"


@dataclass
class _FakeConfig:
    cli: Path | None
    cli_problem: ProviderProblem | None = None
    claude_config_dir_set: bool = False
    browser_kind: str = ""
    cli_source: str = ""


_EMPTY_STORAGE = StorageReadiness(state="ready", reason=None, private=True)


def _app(readiness: Readiness, checks: ProviderChecks) -> Starlette:
    def capabilities() -> Capabilities:
        return readiness.capabilities(None, _EMPTY_STORAGE)

    def document() -> dict[str, object]:
        return {"provider": "stub"}

    workers: set[asyncio.Task[object]] = set()
    routes = diagnostics_routes(
        checks,
        capabilities=capabilities,
        document=document,
        authorized=lambda request: request.headers.get("authorization") == f"Bearer {TOKEN}",
        origins=frozenset({BASE}),
        workers=workers,
    )
    return Starlette(routes=routes)


async def test_checks_run_one_at_a_time_and_never_spawn_under_config_dir_override() -> None:
    import threading

    # -- one check at a time -------------------------------------------------------------
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l1", git_available=lambda: True
    )
    release = threading.Event()

    def slow_git() -> str:
        release.wait(2)
        return "2.40.0"

    checks = ProviderChecks(readiness=readiness, cli_version=lambda: "1.0.0", git_version=slow_git)
    app = _app(readiness, checks)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        first = asyncio.create_task(
            client.post(PATH, headers=MUTATION, json={"command_id": "c1", "check": "git"})
        )
        await asyncio.sleep(0.01)  # let the first request reach checks.run() before the second
        second = await client.post(
            PATH, headers=MUTATION, json={"command_id": "c2", "check": "git"}
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "check_in_progress"
        release.set()
        first_response = await first
        assert first_response.status_code == 200
        assert first_response.json()["status"] == "ok"

    # -- never spawns while CLAUDE_CONFIG_DIR is set -------------------------------------
    spawn_calls: list[str] = []
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe"), claude_config_dir_set=True),
        launch_id="l2",
        git_available=lambda: True,
    )
    checks = ProviderChecks(
        readiness=readiness,
        cli_version=lambda: spawn_calls.append("cli") or "9.9.9",
        git_version=lambda: "2.40.0",
    )
    app = _app(readiness, checks)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        response = await client.post(
            PATH, headers=MUTATION, json={"command_id": "c3", "check": "provider_cli"}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "unsupported"
        assert spawn_calls == []  # the probe that would have run claude.exe --version never ran


async def test_unknown_check_name_is_422_unsupported() -> None:
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l", git_available=lambda: True
    )
    checks = ProviderChecks(
        readiness=readiness, cli_version=lambda: "1.0.0", git_version=lambda: "2.40.0"
    )
    app = _app(readiness, checks)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        response = await client.post(
            PATH, headers=MUTATION, json={"command_id": "c1", "check": "bogus"}
        )
        assert response.status_code == 422


async def test_checks_require_bearer_and_origin() -> None:
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l", git_available=lambda: True
    )
    checks = ProviderChecks(
        readiness=readiness, cli_version=lambda: "1.0.0", git_version=lambda: "2.40.0"
    )
    app = _app(readiness, checks)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        no_auth = await client.post(PATH, json={"command_id": "c1", "check": "git"})
        assert no_auth.status_code == 401
        bad_origin = await client.post(
            PATH,
            headers={**AUTH, "origin": "http://evil.example"},
            json={"command_id": "c1", "check": "git"},
        )
        assert bad_origin.status_code == 403
        # a POST with a valid Bearer but no Origin header at all must refuse exactly like a
        # wrong one, not fall through as if no Origin meant "same-origin, allow it"
        missing_origin = await client.post(
            PATH, headers=AUTH, json={"command_id": "c1", "check": "git"}
        )
        assert missing_origin.status_code == 403


async def test_checks_reject_bad_content_type_and_oversize_body() -> None:
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l", git_available=lambda: True
    )
    checks = ProviderChecks(
        readiness=readiness, cli_version=lambda: "1.0.0", git_version=lambda: "2.40.0"
    )
    app = _app(readiness, checks)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        wrong_type = await client.post(
            PATH, headers={**MUTATION, "content-type": "text/plain"}, content=b"{}"
        )
        assert wrong_type.status_code == 415

        too_large = await client.post(
            PATH,
            headers=MUTATION,
            json={"command_id": "x" * (MAX_CHECK_BODY_BYTES + 1), "check": "git"},
        )
        assert too_large.status_code == 413


async def test_checks_time_out_the_intake_and_recover(monkeypatch: pytest.MonkeyPatch) -> None:
    import viki_agent.app.diagnostics_api as diagnostics_api_module

    monkeypatch.setattr(diagnostics_api_module, "CHECK_BODY_TIMEOUT_S", 0.005)
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l", git_available=lambda: True
    )
    checks = ProviderChecks(
        readiness=readiness, cli_version=lambda: "1.0.0", git_version=lambda: "2.40.0"
    )
    app = _app(readiness, checks)

    async def slow_body():
        yield b'{"command_id": "c1", '
        await asyncio.sleep(0.02)  # well past the 5ms intake deadline above
        yield b'"check": "git"}'

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        timed_out = await client.post(
            PATH, headers={**MUTATION, "content-type": "application/json"}, content=slow_body()
        )
        assert timed_out.status_code == 408
        # the endpoint recovers: an ordinary request right after still works
        ok = await client.post(PATH, headers=MUTATION, json={"command_id": "c2", "check": "git"})
        assert ok.status_code == 200


async def test_checks_return_429_when_intake_capacity_is_exhausted() -> None:
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l", git_available=lambda: True
    )
    checks = ProviderChecks(
        readiness=readiness, cli_version=lambda: "1.0.0", git_version=lambda: "2.40.0"
    )
    app = _app(readiness, checks)

    async def slow_body(command_id: str):
        yield f'{{"command_id": "{command_id}", '.encode()
        await asyncio.sleep(0.02)  # holds this request's capacity permit while it "streams"
        yield b'"check": "git"}'

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        slow = [
            asyncio.create_task(
                client.post(
                    PATH,
                    headers={**MUTATION, "content-type": "application/json"},
                    content=slow_body(f"slow-{i}"),
                )
            )
            for i in range(MAX_CHECK_REQUESTS)
        ]
        await asyncio.sleep(0.005)  # let all of them acquire their permit and start streaming
        overloaded = await client.post(
            PATH, headers=MUTATION, json={"command_id": "fast", "check": "git"}
        )
        assert overloaded.status_code == 429
        assert overloaded.json()["error"]["code"] == "overloaded"
        for task in slow:
            await task  # drain: each finishes ok or check_in_progress, either is fine here


async def test_a_probe_exception_is_reported_failed_not_500_and_the_route_recovers() -> None:
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l", git_available=lambda: True
    )
    calls = 0

    def flaky_git() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FileNotFoundError("git.exe not found")
        return "2.40.0"

    checks = ProviderChecks(readiness=readiness, cli_version=lambda: "1.0.0", git_version=flaky_git)
    app = _app(readiness, checks)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        first = await client.post(PATH, headers=MUTATION, json={"command_id": "c1", "check": "git"})
        assert first.status_code == 200
        assert first.json()["status"] == "failed"
        # the check that raised did not leave the route (or ProviderChecks) stuck: the next
        # request runs normally
        second = await client.post(
            PATH, headers=MUTATION, json={"command_id": "c2", "check": "git"}
        )
        assert second.status_code == 200
        assert second.json()["status"] == "ok"


async def test_capabilities_and_diagnostics_get_routes_are_authenticated() -> None:
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l", git_available=lambda: True
    )
    checks = ProviderChecks(
        readiness=readiness, cli_version=lambda: "1.0.0", git_version=lambda: "2.40.0"
    )
    app = _app(readiness, checks)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        unauth = await client.get("/api/v2/capabilities")
        assert unauth.status_code == 401
        caps = await client.get("/api/v2/capabilities", headers=AUTH)
        assert caps.status_code == 200
        body = caps.json()
        assert body["provider"]["claude"]["cli"] == "ready"
        assert body["git"] == {"state": "ready", "version": ""}
        doc = await client.get("/api/v2/diagnostics", headers=AUTH)
        assert doc.status_code == 200
        assert doc.json() == {"provider": "stub"}


async def test_capabilities_carry_version_fields() -> None:
    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe"), cli_source="installed"),
        launch_id="l",
        git_available=lambda: True,
    )
    readiness.observe_version("2.1.281")
    checks = ProviderChecks(
        readiness=readiness, cli_version=lambda: "1.0.0", git_version=lambda: "2.40.0"
    )
    app = _app(readiness, checks)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        caps = await client.get("/api/v2/capabilities", headers=AUTH)
        assert caps.status_code == 200
        provider = caps.json()["provider"]["claude"]
    assert provider["source"] == "installed"
    assert provider["version_status"] == "tested"
    assert provider["tested"] == [diagnostics.CLI_TESTED_MIN, diagnostics.CLI_TESTED_MAX]


async def test_capabilities_and_document_getters_run_off_the_event_loop_thread() -> None:
    import threading

    loop_thread = threading.get_ident()
    capability_threads: list[int] = []
    document_threads: list[int] = []

    def capabilities() -> Capabilities:
        capability_threads.append(threading.get_ident())
        return Readiness(
            config=_FakeConfig(cli=Path("claude.exe")), launch_id="l", git_available=lambda: True
        ).capabilities(None, _EMPTY_STORAGE)

    def document() -> dict[str, object]:
        document_threads.append(threading.get_ident())
        return {"provider": "stub"}

    readiness = Readiness(
        config=_FakeConfig(cli=Path("claude.exe")), launch_id="l", git_available=lambda: True
    )
    checks = ProviderChecks(
        readiness=readiness, cli_version=lambda: "1.0.0", git_version=lambda: "2.40.0"
    )
    workers: set[asyncio.Task[object]] = set()
    routes = diagnostics_routes(
        checks,
        capabilities=capabilities,
        document=document,
        authorized=lambda request: request.headers.get("authorization") == f"Bearer {TOKEN}",
        origins=frozenset({BASE}),
        workers=workers,
    )
    app = Starlette(routes=routes)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        caps = await client.get("/api/v2/capabilities", headers=AUTH)
        assert caps.status_code == 200
        doc = await client.get("/api/v2/diagnostics", headers=AUTH)
        assert doc.status_code == 200

    assert capability_threads and capability_threads[0] != loop_thread
    assert document_threads and document_threads[0] != loop_thread
