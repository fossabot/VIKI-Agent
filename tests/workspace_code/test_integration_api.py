"""The review and integration routes over stub services recording each call, and one real review
over a temporary repository.

The gate (Bearer, Origin, path ids, query and body) refuses before any service call; each code a
service raises answers its status with a fixed message; a client that leaves does not cancel an
operation holding the lease.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.workspace_code.test_code_api import ORIGIN, ROOT_NAME
from tests.workspace_code.test_code_git import _git
from tests.workspace_code.test_roots_api import (
    ALPHA,
    AUTH,
    BEARER,
    HANG_GUARD_S,
    JSON,
    _committed,
    _empty_git_environment,  # noqa: F401 (autouse: no developer Git config)
    _rig,
    needs_git,
)
from viki_agent.code import integration_api
from viki_agent.code.code_commit import CommitResult
from viki_agent.code.code_files import CodeError
from viki_agent.code.code_integration import CheckResult, PushResult
from viki_agent.code.code_range import Commit, Integration, RangePath
from viki_agent.code.code_review import CodeReview, Review, ReviewEntry, ReviewView
from viki_agent.code.integration_api import (
    MAX_BODY_BYTES,
    MAX_INTEGRATION_REQUESTS,
    MAX_QUERY_BYTES,
    integration_routes,
)
from viki_agent.storage import StateError

WS = "11111111-1111-4111-8111-111111111111"
WT = "22222222-2222-4222-8222-222222222222"
OID = "a" * 40
NEW_OID = "b" * 64
DIGEST = "d" * 64
BRANCH = "viki/secret-branch"
TARGET = "main"
PATH = "dir/secret-file.txt"
URL = "https://github.com/owner/repo/compare/main...viki/secret-branch?quick_pull=1"
SECRETS = (WS, WT, BRANCH, PATH, URL, "secret")

REVIEW = f"/api/v2/code/{WS}/review"
FILE = f"/api/v2/code/{WS}/review/file"
GETS = {
    "review": f"{REVIEW}?worktree={WT}",
    "view": f"{FILE}?path=dir%2Fsecret-file.txt&worktree={WT}&range=integration",
}
POSTS: dict[str, tuple[str, dict[str, Any]]] = {
    "commit": (
        f"/api/v2/code/{WS}/commit",
        {
            "policy_revision": 7,
            "root_physical_id": "12:345",
            "worktree_id": WT,
            "branch": BRANCH,
            "head_oid": OID,
            "message": "Fix the thing",
            "paths": [{"path": PATH, "digest": DIGEST}],
        },
    ),
    "integrate": (
        f"/api/v2/worktrees/{WT}/integrate",
        {
            "policy_revision": 7,
            "root_physical_id": "12:345",
            "target_branch": TARGET,
            "target_oid": OID,
            "head_oid": NEW_OID,
        },
    ),
    "push": (
        f"/api/v2/code/{WS}/push",
        {
            "policy_revision": 7,
            "root_physical_id": "12:345",
            "remote": "origin",
            "branch": BRANCH,
            "head_oid": OID,
        },
    ),
    "check": (
        f"/api/v2/code/{WS}/push/check",
        {"remote": "origin", "branch": BRANCH, "head_oid": OID},
    ),
}
ROUTES = [*GETS, *POSTS]

SAMPLE_REVIEW = Review(
    workspace_id=WS,
    worktree_id=WT,
    branch=BRANCH,
    head_oid=NEW_OID,
    in_progress=False,
    entries=(
        ReviewEntry(PATH, "modified", "text", DIGEST),
        ReviewEntry("image.png", "added", "binary", None),
    ),
    truncated=False,
    remote="origin",
    remote_state="ready",
    remote_oid=OID,
    pull_request_url=URL,
    integration=Integration(
        target_branch=TARGET,
        target_oid=OID,
        merge_base=OID,
        fast_forward=True,
        ahead=1,
        behind=0,
        commits=(Commit(NEW_OID, "Fix the thing"),),
        commits_truncated=False,
        paths=(RangePath(PATH, "modified", "text"),),
        paths_truncated=False,
    ),
)


class Stub:
    """Stands in for the three services: records each call and answers a well-formed result, or
    raises what it was given. With `gate` an awaited call waits there; `log` records an
    operation's lease, as `held` would hold it around the Git work."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.raises = raises
        self.gate: asyncio.Event | None = None
        self.entered: asyncio.Event | None = None
        self.expect = 1  # `entered` is set by this many calls
        self.log: list[str] = []

    def _record(self, name: str, args: tuple[Any, ...]) -> None:
        self.calls.append((name, args))
        if self.raises is not None:
            raise self.raises

    def review(self, *args: Any) -> Review:
        self._record("review", args)
        return SAMPLE_REVIEW

    def view(
        self, workspace_id: str, path: str, worktree_id: str | None, *, integration: bool
    ) -> ReviewView:
        self._record("view", (workspace_id, path, worktree_id, integration))
        return ReviewView(path, "@@ -1 +1 @@\n-a\n+b\n", None, None)

    async def _operate(self, name: str, args: tuple[Any, ...]) -> None:
        self.calls.append((name, args))
        if self.entered is not None and len(self.calls) == self.expect:
            self.entered.set()
        self.log.append("leased")
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.raises is not None:
                raise self.raises
            self.log.append("finished")
        finally:
            self.log.append("released")

    async def commit(self, *args: Any) -> CommitResult:
        await self._operate("commit", args)
        return CommitResult(NEW_OID, (PATH,), ("other.txt",))

    async def integrate(self, *args: Any) -> str:
        await self._operate("integrate", args)
        return NEW_OID

    async def push(self, *args: Any) -> PushResult:
        await self._operate("push", args)
        return PushResult("pushed", OID)

    async def check(self, *args: Any) -> CheckResult:
        await self._operate("check", args)
        return CheckResult("not_pushed", None)


def _app(
    review: Any, committer: Any, integration: Any, workers: set[asyncio.Task[Any]] | None = None
) -> Starlette:
    return Starlette(
        routes=integration_routes(
            review,
            committer,
            integration,
            authorized=lambda request: request.headers.get("authorization") == "Bearer secret",
            origins=frozenset({ORIGIN}),
            workers=set() if workers is None else workers,
        )
    )


def _stubbed(stub: Stub, workers: set[asyncio.Task[Any]] | None = None) -> Starlette:
    return _app(stub, stub, stub, workers)


def post(client: TestClient, url: str, fields: Any = None, **kwargs: Any) -> Any:
    headers = {**JSON, **kwargs.pop("headers", {})}
    content = kwargs.pop("content", None)
    return client.post(
        url, content=json.dumps(fields) if content is None else content, headers=headers
    )


def call(client: TestClient, route: str, **headers: str) -> Any:
    """The route's well-formed request, with `headers` replacing the defaults."""
    if route in GETS:
        return client.get(GETS[route], headers=headers if headers else AUTH)
    url, fields = POSTS[route]
    return client.post(url, content=json.dumps(fields), headers=headers if headers else JSON)


def _refused(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert list(body) == ["error"]
    error = body["error"]
    assert set(error) == {"code", "message"}
    assert error["code"] == code and error["message"] and error["message"].isascii()
    for secret in SECRETS:
        assert secret not in response.text and secret not in error["message"]
    assert response.content.isascii()
    return error


def _answer(response: Any) -> Any:
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


# --- the six answers ----------------------------------------------------------------------------


def test_review_route_answers_the_contract() -> None:
    stub = Stub()
    with TestClient(_stubbed(stub)) as client:
        body = _answer(client.get(GETS["review"], headers=AUTH))
        plain = _answer(client.get(REVIEW, headers=BEARER))
    assert list(body) == [
        "workspace_id",
        "worktree_id",
        "branch",
        "head_oid",
        "in_progress",
        "entries",
        "truncated",
        "remote",
        "remote_state",
        "remote_oid",
        "pull_request_url",
        "integration",
    ]
    assert body == {
        "workspace_id": WS,
        "worktree_id": WT,
        "branch": BRANCH,
        "head_oid": NEW_OID,
        "in_progress": False,
        "entries": [
            {"path": PATH, "change": "modified", "kind": "text", "digest": DIGEST},
            {"path": "image.png", "change": "added", "kind": "binary", "digest": None},
        ],
        "truncated": False,
        "remote": "origin",
        "remote_state": "ready",
        "remote_oid": OID,
        "pull_request_url": URL,
        "integration": {
            "target_branch": TARGET,
            "target_oid": OID,
            "merge_base": OID,
            "fast_forward": True,
            "ahead": 1,
            "behind": 0,
            "commits": [{"oid": NEW_OID, "subject": "Fix the thing"}],
            "commits_truncated": False,
            "paths": [{"path": PATH, "change": "modified", "kind": "text"}],
            "paths_truncated": False,
        },
    }
    assert plain == body
    assert stub.calls == [("review", (WS, WT)), ("review", (WS, None))]


def test_view_route_answers_the_contract_and_passes_the_range() -> None:
    stub = Stub()
    with TestClient(_stubbed(stub)) as client:
        ranged = _answer(client.get(GETS["view"], headers=AUTH))
        plain = _answer(client.get(f"{FILE}?path=a+b%E3%81%82.txt", headers=AUTH))
        in_worktree = _answer(client.get(f"{FILE}?worktree={WT}&path=x", headers=AUTH))
    assert list(ranged) == ["path", "diff", "content", "reason"]
    assert ranged == {
        "path": PATH,
        "diff": "@@ -1 +1 @@\n-a\n+b\n",
        "content": None,
        "reason": None,
    }
    assert plain["path"] == "a bあ.txt" and in_worktree["path"] == "x"
    assert stub.calls == [
        ("view", (WS, PATH, WT, True)),
        ("view", (WS, "a bあ.txt", None, False)),
        ("view", (WS, "x", WT, False)),
    ]


def test_each_post_answers_the_contract_and_passes_the_body_as_sent() -> None:
    stub = Stub()
    with TestClient(_stubbed(stub)) as client:
        answers = {route: _answer(call(client, route)) for route in POSTS}
    assert answers == {
        "commit": {
            "outcome": "committed",
            "head_oid": NEW_OID,
            "hook_changed": [PATH],
            "unexpected": ["other.txt"],
        },
        "integrate": {"outcome": "integrated", "head_oid": NEW_OID},
        "push": {"outcome": "pushed", "remote_oid": OID},
        "check": {"outcome": "not_pushed", "remote_oid": None},
    }
    assert [list(answer) for answer in answers.values()] == [
        ["outcome", "head_oid", "hook_changed", "unexpected"],
        ["outcome", "head_oid"],
        ["outcome", "remote_oid"],
        ["outcome", "remote_oid"],
    ]
    assert stub.calls == [
        ("commit", (WS, WT, 7, "12:345", BRANCH, OID, "Fix the thing", [(PATH, DIGEST)])),
        ("integrate", (WT, 7, "12:345", TARGET, OID, NEW_OID)),
        ("push", (WS, 7, "12:345", "origin", BRANCH, OID)),
        ("check", (WS, "origin", BRANCH, OID)),
    ]


def test_a_folder_commit_passes_a_null_worktree() -> None:
    url, fields = POSTS["commit"]
    stub = Stub()
    with TestClient(_stubbed(stub)) as client:
        _answer(post(client, url, {**fields, "worktree_id": None}))
    assert stub.calls[0][1][1] is None


# --- the gate: Bearer, Origin, path ids ---------------------------------------------------------


@pytest.mark.parametrize("route", ROUTES)
def test_every_route_needs_bearer(route: str) -> None:
    stub = Stub()
    extra = {"Content-Type": "application/json"} if route in POSTS else {}
    with TestClient(_stubbed(stub)) as client:
        for bearer in ({}, {"Authorization": "Bearer other"}):
            _refused(call(client, route, Origin=ORIGIN, **bearer, **extra), 401, "unauthorized")
    assert stub.calls == []


@pytest.mark.parametrize("route", POSTS)
def test_a_post_needs_a_listed_origin(route: str) -> None:
    stub = Stub()
    with TestClient(_stubbed(stub)) as client:
        _refused(
            call(client, route, **BEARER, **{"Content-Type": "application/json"}),
            403,
            "forbidden_origin",
        )
        foreign = {**JSON, "Origin": "https://evil.invalid"}
        _refused(call(client, route, **foreign), 403, "forbidden_origin")
        assert stub.calls == []
        _answer(call(client, route))
    assert len(stub.calls) == 1


@pytest.mark.parametrize("route", GETS)
def test_a_get_may_omit_origin_but_not_name_a_foreign_one(route: str) -> None:
    stub = Stub()
    with TestClient(_stubbed(stub)) as client:
        _answer(call(client, route, **BEARER))
        _answer(call(client, route))
        foreign = {**BEARER, "Origin": "https://evil.invalid"}
        _refused(call(client, route, **foreign), 403, "forbidden_origin")
    assert len(stub.calls) == 2


@pytest.mark.parametrize("route", ROUTES)
def test_a_non_canonical_path_id_is_404(route: str) -> None:
    template = GETS[route] if route in GETS else POSTS[route][0]
    unknown = "unknown_worktree" if route == "integrate" else "unknown_workspace"
    real = WT if route == "integrate" else WS
    stub = Stub()
    with TestClient(_stubbed(stub)) as client:
        for raw in (
            ALPHA.upper(),
            f"{{{ALPHA}}}",
            ALPHA.replace("-", ""),
            f"urn:uuid:{ALPHA}",
            "w",
        ):
            url = template.replace(real, raw, 1)
            if route in GETS:
                response = client.get(url, headers=AUTH)
            else:
                response = post(client, url, POSTS[route][1])
            _refused(response, 404, unknown)
    assert stub.calls == []


def test_another_method_is_405() -> None:
    stub = Stub()
    with TestClient(_stubbed(stub)) as client:
        for route in GETS:
            assert client.post(GETS[route], headers=JSON).status_code == 405
        for route in POSTS:
            assert client.get(POSTS[route][0], headers=AUTH).status_code == 405
    assert stub.calls == []


# --- the query ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        f"worktree={WT}&worktree={WT}",
        "x=1",
        "worktree",
        f"worktree={WT}&",
        "worktree=not-a-uuid",
        f"worktree={ALPHA.upper()}",
        "worktree=",
        "range=integration",
        f"worktree={WT}&range=integration",
        "path=a",
        "=",
        "worktree=%zz",
    ],
)
def test_a_review_query_of_any_other_shape_is_400_invalid_query(query: str) -> None:
    stub = Stub()
    with TestClient(_stubbed(stub)) as client:
        _refused(client.get(f"{REVIEW}?{query}", headers=AUTH), 400, "invalid_query")
    assert stub.calls == []


@pytest.mark.parametrize(
    "query",
    [
        "",
        f"worktree={WT}",
        f"path=a&worktree={WT}&worktree={WT}",
        "path=a&path=b",
        "path=a&x=1",
        "path=a&worktree",
        "path",
        f"path=a&worktree={WT}&range=other",
        f"path=a&worktree={WT}&range=",
        "path=a&range=integration",
        "path=a&worktree=bad",
        f"path=a&worktree={WT.upper().replace('-', '')}",
        "path=%ff",
        "path=a%2",
    ],
)
def test_a_view_query_of_any_other_shape_is_400_invalid_query(query: str) -> None:
    stub = Stub()
    with TestClient(_stubbed(stub)) as client:
        _refused(client.get(f"{FILE}?{query}", headers=AUTH), 400, "invalid_query")
    assert stub.calls == []


def test_a_query_over_16_kib_is_400_and_exactly_16_kib_passes() -> None:
    assert MAX_QUERY_BYTES == 16_384
    stub = Stub()
    exact = "path=" + "a" * (MAX_QUERY_BYTES - len("path="))
    with TestClient(_stubbed(stub)) as client:
        _refused(client.get(f"{FILE}?{exact}a", headers=AUTH), 400, "invalid_query")
        assert stub.calls == []
        _answer(client.get(f"{FILE}?{exact}", headers=AUTH))
    assert len(stub.calls) == 1


@pytest.mark.parametrize("query", ["?x=1", f"?worktree={WT}", "?a", "?="])
@pytest.mark.parametrize("route", POSTS)
def test_any_query_on_a_post_is_400_invalid_query(route: str, query: str) -> None:
    url, fields = POSTS[route]
    stub = Stub()
    with TestClient(_stubbed(stub)) as client:
        _refused(post(client, url + query, fields), 400, "invalid_query")
    assert stub.calls == []


# --- the body -----------------------------------------------------------------------------------

DROP = object()
OIDS_BAD: list[Any] = ["a" * 39, "a" * 41, "A" * 40, "g" * 40, "a" * 63, "", 5, None]
PATHS_BAD: list[Any] = [
    [],
    [{"path": f"f{index}", "digest": DIGEST} for index in range(201)],
    [{"path": "a", "digest": DIGEST}, {"path": "a", "digest": "e" * 64}],
    [{"path": "a", "digest": "d" * 63}],
    [{"path": "a", "digest": "D" * 64}],
    [{"path": "a", "digest": "d" * 65}],
    [{"path": "a"}],
    [{"digest": DIGEST}],
    [{"path": "a", "digest": DIGEST, "extra": 1}],
    [{"path": "", "digest": DIGEST}],
    [{"path": "a" * 1025, "digest": DIGEST}],
    [{"path": 5, "digest": DIGEST}],
    [{"path": "a", "digest": None}],
    ["a"],
    [["a", DIGEST]],
    {"path": "a", "digest": DIGEST},
    None,
]
BAD_VALUES: dict[str, list[Any]] = {
    "policy_revision": [0, -1, 9_007_199_254_740_992, True, False, "1", 1.0, None],
    "root_physical_id": ["", "1", "1:", "a:1", "1:" + "2" * 80, "1:2\n", 12, None],
    "worktree_id": ["", "x", ALPHA.upper(), ALPHA.replace("-", ""), 5, True],
    "branch": ["", "a" * 101, 5, None, ["a"]],
    "target_branch": ["", "a" * 101, 5, None],
    "head_oid": OIDS_BAD,
    "target_oid": OIDS_BAD,
    "message": ["", "m" * 4001, 5, None, ["m"]],
    "paths": PATHS_BAD,
    "remote": ["-x", "", ".x", "a" * 101, "a b", "a/b", 5, None],
}


def _bad_bodies() -> list[Any]:
    cases = []
    for route, (_, fields) in POSTS.items():
        for key in fields:
            for index, value in enumerate([DROP, *BAD_VALUES[key]]):
                cases.append(pytest.param(route, key, value, id=f"{route}-{key}-{index}"))
        cases.append(pytest.param(route, "extra", 1, id=f"{route}-extra"))
    return cases


@pytest.mark.parametrize(("route", "key", "value"), _bad_bodies())
def test_a_body_of_any_other_shape_is_400_invalid_body(route: str, key: str, value: Any) -> None:
    url, fields = POSTS[route]
    if value is DROP:
        body = {name: item for name, item in fields.items() if name != key}
    else:
        body = {**fields, key: value}
    stub = Stub()
    with TestClient(_stubbed(stub)) as client:
        _refused(post(client, url, body), 400, "invalid_body")
    assert stub.calls == []


def test_the_body_limits_admit_their_bounds() -> None:
    url, fields = POSTS["commit"]
    stub = Stub()
    body = {
        **fields,
        "message": "m" * 4000,
        "paths": [
            {"path": f"{index:04d}".ljust(1024, "x"), "digest": DIGEST} for index in range(200)
        ],
        "head_oid": NEW_OID,
        "branch": "b" * 100,
        "policy_revision": 9_007_199_254_740_991,
    }
    with TestClient(_stubbed(stub)) as client:
        _answer(post(client, url, body))
        _answer(post(client, POSTS["push"][0], {**POSTS["push"][1], "remote": "A" + "z" * 99}))
    assert len(stub.calls) == 2


@pytest.mark.parametrize("route", POSTS)
@pytest.mark.parametrize("raw", ["", "{", "[]", "null", '"x"', "5", "NaN", b"\xff\xfe"])
def test_a_body_that_is_not_one_json_object_is_400_invalid_body(
    route: str, raw: str | bytes
) -> None:
    url, _ = POSTS[route]
    stub = Stub()
    with TestClient(_stubbed(stub)) as client:
        _refused(post(client, url, content=raw), 400, "invalid_body")
    assert stub.calls == []


@pytest.mark.parametrize("route", POSTS)
def test_a_duplicate_key_is_400_invalid_body(route: str) -> None:
    url, fields = POSTS[route]
    first = next(iter(fields))
    raw = json.dumps(fields)[:-1] + "," + json.dumps({first: fields[first]})[1:]
    stub = Stub()
    with TestClient(_stubbed(stub)) as client:
        _refused(post(client, url, content=raw), 400, "invalid_body")
    assert stub.calls == []


@pytest.mark.parametrize("route", POSTS)
@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "text/plain"},
        {"Content-Type": "application/json", "Content-Encoding": "gzip"},
        {"Content-Type": "application/json", "Content-Encoding": "br"},
    ],
)
def test_another_content_type_or_encoding_is_400_invalid_body(
    route: str, headers: dict[str, str]
) -> None:
    url, fields = POSTS[route]
    stub = Stub()
    with TestClient(_stubbed(stub)) as client:
        response = client.post(url, content=json.dumps(fields), headers={**AUTH, **headers})
        _refused(response, 400, "invalid_body")
    assert stub.calls == []


@pytest.mark.parametrize("route", POSTS)
def test_a_body_over_262144_bytes_is_413_too_large_and_exactly_262144_passes(route: str) -> None:
    url, fields = POSTS[route]
    assert MAX_BODY_BYTES == 262_144
    stub = Stub()
    with TestClient(_stubbed(stub)) as client:
        too_big = post(client, url, content=json.dumps(fields).ljust(MAX_BODY_BYTES + 1))
        _refused(too_big, 413, "too_large")
        assert stub.calls == []
        _answer(post(client, url, content=json.dumps(fields).ljust(MAX_BODY_BYTES)))
    assert [name for name, _ in stub.calls] == [route]


# --- every code a service can raise -------------------------------------------------------------

STATUS = {
    code: status
    for status, codes in {
        400: "invalid_body invalid_query invalid_path invalid_message too_many_paths not_a_file "
        "disconnected",
        401: "unauthorized",
        403: "forbidden_origin private_path",
        404: "unknown_workspace unknown_worktree not_found",
        408: "intake_timeout",
        409: "unsupported_branch trust_required workspace_changed root_unavailable "
        "not_repository not_repository_root no_commits worktree_not_ready worktree_moved "
        "unsupported_filesystem git_common_unavailable stale_review stale_base unrelated_staged "
        "in_progress detached_head detached_target identity_missing unsupported_path "
        "unsupported_change commit_refused checkout_busy dirty_target not_fast_forward "
        "up_to_date merge_refused merge_failed no_remote remote_unsupported push_rejected",
        413: "too_large",
        423: "vault_locked",
        429: "overloaded",
        503: "git_unavailable git_failed push_failed unavailable",
    }.items()
    for code in codes.split()
}
MESSAGES = {
    "invalid_path": "Check the file's location.",
    "unsupported_filesystem": "Commit, integrate and push aren't supported in this environment.",
    "stale_review": "This changed since you reviewed it. Review it again.",
    "push_failed": "Couldn't connect to the remote.",
    "trust_required": "Trust this folder, then try again.",  # roots_api's
    "vault_locked": "The folder is in use. Wait a moment, then try again.",  # roots_api's
}


@pytest.mark.parametrize("route", ROUTES)
def test_each_service_code_has_its_status_and_a_fixed_message(
    caplog: pytest.LogCaptureFixture, route: str
) -> None:
    stub = Stub()
    seen: dict[str, str] = {}
    with caplog.at_level(logging.WARNING), TestClient(_stubbed(stub)) as client:
        for code, status in STATUS.items():
            stub.raises = CodeError(code)
            seen[code] = _refused(call(client, route), status, code)["message"]
        for code in ("a_code_nobody_mapped", "not_a_directory", "invalid_branch"):
            stub.raises = CodeError(code)
            _refused(call(client, route), 503, "unavailable")
        stub.raises = StateError("unavailable")
        _refused(call(client, route), 503, "unavailable")
    assert {code: seen[code] for code in MESSAGES} == MESSAGES
    assert len(stub.calls) == len(STATUS) + 4
    assert [r for r in caplog.records if r.name == integration_api.__name__] == []


@pytest.mark.parametrize("route", ROUTES)
def test_a_value_error_is_400_for_the_route_s_input(route: str) -> None:
    stub = Stub(ValueError(f"bad {PATH} {BRANCH}"))
    code = "invalid_body" if route in POSTS else "invalid_query"
    with TestClient(_stubbed(stub)) as client:
        _refused(call(client, route), 400, code)
    assert len(stub.calls) == 1


@pytest.mark.parametrize("route", ROUTES)
def test_anything_else_is_503_unavailable_with_one_warning_naming_the_type(
    caplog: pytest.LogCaptureFixture, route: str
) -> None:
    stub = Stub(OSError(f"C:\\secret\\{PATH} {URL} token"))
    with caplog.at_level(logging.WARNING), TestClient(_stubbed(stub)) as client:
        _refused(call(client, route), 503, "unavailable")
    records = [r for r in caplog.records if r.name == integration_api.__name__]
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.WARNING, "integration route failed (OSError)")
    ]


# --- capacity and cancellation ------------------------------------------------------------------


async def test_a_fifth_request_is_429_overloaded_and_the_permits_come_back() -> None:
    stub = Stub()
    stub.gate, stub.entered, stub.expect = (
        asyncio.Event(),
        asyncio.Event(),
        MAX_INTEGRATION_REQUESTS,
    )
    workers: set[asyncio.Task[Any]] = set()
    transport = httpx.ASGITransport(app=_stubbed(stub, workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        url, fields = POSTS["integrate"]

        async def integrate() -> httpx.Response:
            return await client.post(url, content=json.dumps(fields), headers=JSON)

        calls = [asyncio.create_task(integrate()) for _ in range(MAX_INTEGRATION_REQUESTS)]
        await asyncio.wait_for(stub.entered.wait(), HANG_GUARD_S)
        assert len(workers) == MAX_INTEGRATION_REQUESTS
        _refused(await integrate(), 429, "overloaded")
        _refused(await client.get(GETS["review"], headers=AUTH), 429, "overloaded")
        stub.gate.set()
        assert all(r.status_code == 200 for r in await asyncio.gather(*calls))
        await asyncio.sleep(0)
        assert not workers
        assert (await client.get(GETS["review"], headers=AUTH)).status_code == 200


@pytest.mark.parametrize("route", ["integrate", "commit", "push"])
async def test_a_client_that_leaves_does_not_cancel_the_operation_or_release_its_lease(
    route: str,
) -> None:
    stub = Stub()
    stub.gate, stub.entered = asyncio.Event(), asyncio.Event()
    workers: set[asyncio.Task[Any]] = set()
    transport = httpx.ASGITransport(app=_stubbed(stub, workers))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        url, fields = POSTS[route]
        request = asyncio.create_task(client.post(url, content=json.dumps(fields), headers=JSON))
        await asyncio.wait_for(stub.entered.wait(), HANG_GUARD_S)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        # The client is gone; the operation still holds its lease and waits on its Git work.
        assert stub.log == ["leased"] and len(workers) == 1
        stub.gate.set()
        await asyncio.wait_for(asyncio.gather(*workers), HANG_GUARD_S)
        await asyncio.sleep(0)
    assert stub.log == ["leased", "finished", "released"] and not workers


# --- one real review ----------------------------------------------------------------------------


@needs_git
def test_a_real_review_and_view_over_a_temporary_repository(tmp_path: Path) -> None:
    rig = _rig(tmp_path, _committed(tmp_path), git=True)
    (rig.root / "a.txt").write_bytes(b"alpha\nmore\n")
    (rig.root / "new.txt").write_bytes(b"new\n")
    head = _git(rig.root, "rev-parse", "HEAD").strip()
    stub = Stub()
    app = _app(CodeReview(rig.store, rig.git), stub, stub)
    review_url = f"/api/v2/code/{rig.workspace_id}/review"
    with TestClient(app) as client:
        body = _answer(client.get(review_url, headers=AUTH))
        shown = _answer(client.get(f"{review_url}/file?path=a.txt", headers=AUTH))
        added = _answer(client.get(f"{review_url}/file?path=new.txt", headers=AUTH))
        unknown = client.get(f"{review_url}?worktree={WT}", headers=AUTH)
    assert (body["workspace_id"], body["worktree_id"], body["head_oid"]) == (
        rig.workspace_id,
        None,
        head,
    )
    assert body["integration"] is None and body["in_progress"] is False
    assert body["remote_state"] == "none" and body["pull_request_url"] is None
    entries = {entry["path"]: entry for entry in body["entries"]}
    assert set(entries) == {"a.txt", "new.txt"}
    assert all(set(entry) == {"path", "change", "kind", "digest"} for entry in entries.values())
    assert (entries["a.txt"]["change"], entries["new.txt"]["change"]) == ("modified", "added")
    assert all(entry["kind"] == "text" and len(entry["digest"]) == 64 for entry in entries.values())
    assert shown["diff"] is not None and "+more" in shown["diff"] and shown["content"] is None
    assert added == {"path": "new.txt", "diff": None, "content": "new\n", "reason": None}
    error = _refused(unknown, 404, "unknown_worktree")
    assert ROOT_NAME not in error["message"] and str(tmp_path) not in unknown.text
    assert stub.calls == []
