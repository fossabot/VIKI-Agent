from __future__ import annotations

import asyncio
import io
from copy import deepcopy

import httpx2
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from viki_agent.llm.magpie import Magpie, MagpieError
from viki_agent.llm.magpie_api import _view, magpie_routes
from viki_agent.window_host import WindowHost

ORIGIN = "http://127.0.0.1:8765"
STATUS = {
    "available": True,
    "running": False,
    "version": "0.1.3357",
    "desktop": False,
    "base_url": "http://127.0.0.1:3426/v1",
}


class Service:
    def __init__(self) -> None:
        self.opened: list[str | None] = []
        self.hidden: list[str | None] = []
        self.views: list[tuple[str | None, dict[str, object]]] = []
        self.closed_views: list[tuple[str | None, str | None]] = []

    async def status(self):
        return STATUS

    async def open(self, ui, view=None):
        self.opened.append(ui)
        if view is not None:
            self.views.append((ui, view))
        return STATUS

    async def hide(self, ui, view_id=None):
        self.hidden.append(ui)
        self.closed_views.append((ui, view_id))
        return STATUS

    async def update_view(self, ui, view):
        self.views.append((ui, view))
        return STATUS


def presentation() -> dict:
    return {
        "id": "b" * 32,
        "bounds": {"x": 240, "y": 49, "width": 900, "height": 650},
        "appearance": {
            "theme": "dark",
            "density": "comfortable",
            "scale": 1,
            "reduce_motion": False,
            "locale": "en",
            "palette": dict.fromkeys(
                (
                    "bg",
                    "panel",
                    "raised",
                    "text",
                    "secondary",
                    "muted",
                    "faint",
                    "line",
                    "hair",
                    "accent",
                    "onAccent",
                    "popover",
                ),
                "#111111",
            ),
        },
        "visible": True,
    }


def test_controls_require_window_auth_origin_and_exact_bounded_body() -> None:
    service = Service()
    app = Starlette(
        routes=magpie_routes(
            service,  # type: ignore[arg-type]
            authorized=lambda r: r.headers.get("authorization") == "Bearer synthetic",
            origins=frozenset({ORIGIN}),
        )
    )
    with TestClient(app) as client:
        path = "/api/v2/llm/magpie"
        assert client.get(path).status_code == 401
        client.headers.update({"authorization": "Bearer synthetic"})
        assert client.get(path).json() == STATUS
        assert client.post(path + "/open", json={"ui": None}).status_code == 403
        client.headers.update({"origin": ORIGIN})
        for body in ({}, {"ui": 1}, {"ui": "x"}, {"ui": None, "url": "https://evil.example"}):
            assert client.post(path + "/open", json=body).status_code == 503
        assert client.post(path + "/open?x=1", json={"ui": None}).status_code == 400
        assert client.post(path + "/open", content=b"x" * 1025).status_code == 503
        assert service.opened == []
        assert client.post(path + "/open", json={"ui": None}).json() == STATUS
        assert client.post(path + "/close", json={"ui": "a" * 32}).json() == STATUS
    assert service.opened == [None] and service.hidden == ["a" * 32]


def test_a_magpie_error_always_names_its_cause() -> None:
    with pytest.raises(TypeError):
        MagpieError()  # type: ignore[call-arg]


@pytest.mark.parametrize("code", ["port_in_use", "missing_bundle", "exited", "invalid_window"])
def test_a_refused_open_answers_unavailable_whatever_its_cause(code: str) -> None:
    """The window reads no cause and shows one sentence for all of them, so the wire carries
    none either."""

    class Refusing(Service):
        async def open(self, ui, view=None):
            raise MagpieError(code)

    app = Starlette(
        routes=magpie_routes(
            Refusing(),  # type: ignore[arg-type]
            authorized=lambda _: True,
            origins=frozenset({ORIGIN}),
        )
    )
    with TestClient(app, headers={"origin": ORIGIN}) as client:
        path = "/api/v2/llm/magpie/open"
        refused = client.post(path, json={"ui": None})
        assert (refused.status_code, refused.json()["error"]["code"]) == (503, "unavailable")
        malformed = client.post(path, json={"ui": None, "url": "https://evil.example"})
        assert (malformed.status_code, malformed.json()["error"]["code"]) == (503, "unavailable")


def test_embedded_view_metadata_is_bounded_and_keeps_admin_credentials_private() -> None:
    service = Service()
    app = Starlette(
        routes=magpie_routes(
            service,  # type: ignore[arg-type]
            authorized=lambda _: True,
            origins=frozenset({ORIGIN}),
        )
    )
    view = presentation()
    with TestClient(app, headers={"origin": ORIGIN}) as client:
        path = "/api/v2/llm/magpie"
        for suffix in ("/open", "/view"):
            response = client.post(path + suffix, json={"ui": "a" * 32, "view": view})
            assert response.json() == STATUS
        assert service.views == [("a" * 32, view), ("a" * 32, view)]
        assert (
            client.post(path + "/close", json={"ui": "a" * 32, "view_id": view["id"]}).json()
            == STATUS
        )
        assert service.closed_views == [("a" * 32, view["id"])]
        bad = deepcopy(view)
        bad["appearance"]["css"] = "body { display:none }"
        assert client.post(path + "/view", json={"ui": "a" * 32, "view": bad}).status_code == 503
        assert (
            client.post(
                path + "/view", json={"ui": "a" * 32, "view": view, "url": ORIGIN}
            ).status_code
            == 503
        )
        assert len(service.views) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("x", -1),
        ("width", True),
        ("height", 32769),
        ("y", float("nan")),
        ("x", float("inf")),
        ("width", 10**500),
    ],
)
def test_view_geometry_rejects_nonfinite_unbounded_and_boolean_values(field, value) -> None:
    view = presentation()
    view["bounds"][field] = value
    with pytest.raises(ValueError):
        _view(view)


@pytest.mark.parametrize(
    "field,value",
    [
        ("scale", True),
        ("scale", 0.74),
        ("scale", 2.01),
        ("scale", 10**500),
        ("theme", "system"),
        ("density", "large"),
        ("locale", "../../private"),
        ("locale", "ja"),
        ("reduce_motion", 1),
        ("palette", {"bg": "url(https://example.com)"}),
    ],
)
def test_view_appearance_accepts_no_arbitrary_css_or_unknown_values(field, value) -> None:
    view = presentation()
    view["appearance"][field] = value
    with pytest.raises(ValueError):
        _view(view)


async def test_view_close_can_cancel_an_open_while_startup_is_pending() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Starting(Service):
        async def open(self, ui, view=None):
            entered.set()
            await release.wait()
            return await super().open(ui, view)

    service = Starting()
    app = Starlette(
        routes=magpie_routes(
            service,  # type: ignore[arg-type]
            authorized=lambda _: True,
            origins=frozenset({ORIGIN}),
        )
    )
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=ORIGIN, headers={"origin": ORIGIN}
    ) as client:
        path = "/api/v2/llm/magpie"
        pending = asyncio.create_task(
            client.post(path + "/open", json={"ui": "a" * 32, "view": presentation()})
        )
        try:
            async with asyncio.timeout(0.05):
                await entered.wait()
            response = await client.post(
                path + "/close", json={"ui": "a" * 32, "view_id": "b" * 32}
            )
            assert response.status_code == 200
            assert service.closed_views == [("a" * 32, "b" * 32)]
        finally:
            release.set()
            await pending


async def test_navigation_during_startup_prevents_the_late_dashboard_overlay(
    tmp_path, monkeypatch
) -> None:
    sent = io.BytesIO()
    host = WindowHost(io.BytesIO(), sent, host_pid=1, wake=lambda: None, gone=lambda: None)
    host._uis[1] = "a" * 32
    service = Magpie(tmp_path, host=host)
    entered, release = asyncio.Event(), asyncio.Event()

    async def start(*, explicit=False):
        assert explicit
        entered.set()
        await release.wait()

    monkeypatch.setattr(service, "start", start)
    pending = asyncio.create_task(service.open("a" * 32, presentation()))
    try:
        async with asyncio.timeout(0.05):
            await entered.wait()
        await service.hide("a" * 32, "b" * 32)
    finally:
        release.set()
        await pending
    assert sent.getvalue() == (b'{"type":"magpie_close","id":1,"view_id":"' + b"b" * 32 + b'"}\n')
