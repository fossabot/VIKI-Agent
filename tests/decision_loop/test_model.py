"""The Decision model's TypeSafe client against synthetic transports only: one allowlisted path,
statuses as fixed codes, one transport call per call, strictly checked replies, and a key that
travels only in its own request's Authorization header."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import ssl
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any

import httpx2
import pytest

from viki_agent.decision_loop import playbook
from viki_agent.decision_loop.contracts import (
    ChoiceAnswer,
    ModelError,
    ModelReply,
    ModelRequest,
    NoulAnswer,
    ScoreAnswer,
)
from viki_agent.decision_loop.model import DecisionModel, TypeSafeHttp
from viki_agent.messaging import mattermost_http
from viki_agent.messaging.mattermost_http import FixedHttpError

KEY = "ts-synthetic-secret-key"
BASE = "https://api.typesafe.ai"
QUESTIONS: dict[str, dict[str, object]] = {
    "goal_link": {
        "type": "noul",
        "instructions": "Is it linked to a goal?",
        "criteria": {"true": "Linked", "false": "Unrelated"},
    },
    "kind": {
        "type": "choice",
        "instructions": "What kind of message?",
        "criteria": {"inform": "Tell a fact", "ask": "Ask the owner"},
    },
    "importance": {
        "type": "score",
        "instructions": "How much does it matter?",
        "criteria": ["Not at all", "Somewhat", "Very much"],
    },
}
REQUEST = ModelRequest("jev-1.14.0", {"candidate": {"title": "Synthetic title"}}, QUESTIONS)
REPLY: dict[str, Any] = {
    "model": "jev-1.14.0",
    "answers": {
        "goal_link": {"type": "noul", "noul": 0.75},
        "kind": {
            "type": "choice",
            "choice": "ask",
            "confidence": 0.6,
            "probabilities": {"inform": 0.4, "ask": 0.6},
        },
        "importance": {
            "type": "score",
            "score": 1.5,
            "confidence": 0.5,
            "legend": {"0": "Not at all", "1": "Somewhat", "2": "Very much"},
            "probabilities": {"0": 0.1, "1": 0.3, "2": 0.6},
        },
    },
    "usage": {"input_tokens": 120, "output_tokens": 12},
}
DROP = object()
BAD_KEYS: list[Any] = [
    "",
    " ",
    "two words",
    "x" * 4097,
    "caf" + chr(0xE9),
    "tab" + chr(9),
    "line" + chr(10),
    "del" + chr(0x7F),
    None,
    b"bytes-key",
    123,
]


def changed(path: str, value: object) -> dict[str, Any]:
    """A copy of `REPLY` with the value at the dotted `path` set to `value`, or removed for
    `DROP`."""
    reply = copy.deepcopy(REPLY)
    *parents, last = path.split(".")
    target = reply
    for part in parents:
        target = target[part]
    if value is DROP:
        del target[last]
    else:
        target[last] = value
    return reply


INVALID_REPLIES: dict[str, object] = {
    "not-an-object": [REPLY],
    "null": None,
    "model-missing": changed("model", DROP),
    "model-not-text": changed("model", 1),
    "model-empty": changed("model", ""),
    "model-33-chars": changed("model", "m" * 33),
    "model-surrogate": changed("model", "\ud800"),
    "model-space": changed("model", " "),
    "answers-missing": changed("answers", DROP),
    "answers-not-an-object": changed("answers", [REPLY["answers"]["goal_link"]]),
    "answer-missing": changed("answers.kind", DROP),
    "answer-unasked": changed("answers.other", {"type": "noul", "noul": 0.5}),
    "answer-not-an-object": changed("answers.goal_link", 0.5),
    "usage-missing": changed("usage", DROP),
    "usage-not-an-object": changed("usage", [120, 12]),
    "input-tokens-missing": changed("usage.input_tokens", DROP),
    "input-tokens-negative": changed("usage.input_tokens", -1),
    "input-tokens-fraction": changed("usage.input_tokens", 1.5),
    "input-tokens-bool": changed("usage.input_tokens", True),
    "input-tokens-above-cap": changed("usage.input_tokens", 1_000_001),
    "output-tokens-missing": changed("usage.output_tokens", DROP),
    "output-tokens-text": changed("usage.output_tokens", "12"),
    "output-tokens-above-cap": changed("usage.output_tokens", 1_000_001),
    "noul-other-type": changed("answers.goal_link.type", "score"),
    "noul-missing": changed("answers.goal_link.noul", DROP),
    "noul-above-one": changed("answers.goal_link.noul", 1.01),
    "noul-below-zero": changed("answers.goal_link.noul", -0.01),
    "noul-bool": changed("answers.goal_link.noul", True),
    "noul-text": changed("answers.goal_link.noul", "0.75"),
    "choice-type-missing": changed("answers.kind.type", DROP),
    "choice-unasked-option": changed("answers.kind.choice", "suggest"),
    "choice-not-text": changed("answers.kind.choice", ["ask"]),
    "choice-confidence-missing": changed("answers.kind.confidence", DROP),
    "choice-confidence-above-one": changed("answers.kind.confidence", 1.5),
    "choice-probabilities-missing": changed("answers.kind.probabilities", DROP),
    "choice-probabilities-not-an-object": changed("answers.kind.probabilities", [0.4, 0.6]),
    "choice-probability-missing": changed("answers.kind.probabilities.inform", DROP),
    "choice-probability-unasked": changed("answers.kind.probabilities.suggest", 0.0),
    "choice-probability-above-one": changed("answers.kind.probabilities.ask", 1.2),
    "choice-probability-text": changed("answers.kind.probabilities.ask", "0.6"),
    "score-missing": changed("answers.importance.score", DROP),
    "score-above-top": changed("answers.importance.score", 2.01),
    "score-below-zero": changed("answers.importance.score", -0.5),
    "score-confidence-below-zero": changed("answers.importance.confidence", -0.1),
    "score-level-above-top": changed("answers.importance.probabilities.3", 0.0),
    "score-level-padded": changed("answers.importance.probabilities.01", 0.0),
    "score-level-negative": changed("answers.importance.probabilities.-1", 0.0),
    "score-level-word": changed("answers.importance.probabilities.one", 0.0),
    "score-probability-above-one": changed("answers.importance.probabilities.2", 1.5),
    "score-probabilities-not-an-object": changed(
        "answers.importance.probabilities", [0.1, 0.3, 0.6]
    ),
}


class Stream(httpx2.AsyncByteStream):
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.raw

    async def aclose(self) -> None:
        self.closed = True


class Server:
    """A synthetic TypeSafe. It keeps every request and answers `status` with the body for the
    request's method, or fails as `failure` says: "connect", "read-timeout", or "hang" (no
    answer at all)."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.bodies: dict[str, object] = {"POST": REPLY}
        self.status = 200
        self.headers: dict[str, str] = {}
        self.failure: str | None = None

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.failure == "hang":
            await asyncio.Event().wait()
        if self.failure is not None:
            error = httpx2.ConnectError if self.failure == "connect" else httpx2.ReadTimeout
            raise error(KEY + " synthetic transport details", request=request)
        body = self.bodies[request.method]
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return httpx2.Response(self.status, headers=self.headers, stream=Stream(raw))


def answer_with(server: Server, monkeypatch: pytest.MonkeyPatch, outcome: int | str) -> None:
    """Make `server` answer `outcome`: an HTTP status; "not-json", "oversize" or "invalid" for a
    200 whose body is unusable; or a transport failure ("hang" under a 10 ms deadline)."""
    if isinstance(outcome, int):
        server.status = outcome
    elif outcome in ("not-json", "oversize"):
        raw = b"<html>synthetic</html>" if outcome == "not-json" else b" " * (128 * 1024 + 1)
        server.bodies = {"POST": raw}
    elif outcome == "invalid":
        server.bodies = {"POST": changed("usage", DROP)}
    else:
        server.failure = outcome
        if outcome == "hang":
            monkeypatch.setattr(mattermost_http, "REQUEST_DEADLINE_S", 0.01)


@asynccontextmanager
async def typesafe(
    handler: Callable[[httpx2.Request], Any], **options: Any
) -> AsyncIterator[DecisionModel]:
    model = DecisionModel(TypeSafeHttp(transport=httpx2.MockTransport(handler)), **options)
    try:
        yield model
    finally:
        await model.close()


def top_answer(question: dict[str, object]) -> dict[str, object]:
    """An answer to a playbook `question` at the top of each range, in JSON integers."""
    criteria = question["criteria"]
    if question["type"] == "noul":
        return {"type": "noul", "noul": 1}
    if isinstance(criteria, dict):
        last = list(criteria)[-1]
        probabilities = {option: int(option == last) for option in criteria}
        return {"type": "choice", "choice": last, "confidence": 1, "probabilities": probabilities}
    assert isinstance(criteria, list)
    top = len(criteria) - 1
    return {"type": "score", "score": top, "confidence": 0, "probabilities": {str(top): 1}}


@pytest.fixture
def server() -> Server:
    return Server()


@pytest.fixture
async def model(server: Server) -> AsyncIterator[DecisionModel]:
    async with typesafe(server) as client:
        yield client


async def test_only_one_path_reaches_the_transport(model: DecisionModel, server: Server) -> None:
    await model.ask(REQUEST, key=KEY)
    assert [(request.method, str(request.url)) for request in server.requests] == [
        ("POST", BASE + "/v1/systemone"),
    ]
    (post,) = server.requests
    assert json.loads(post.content) == {
        "model": "jev-1.14.0",
        "state": {"candidate": {"title": "Synthetic title"}},
        "questions": QUESTIONS,
    }
    assert post.headers["content-type"] == "application/json"
    for request in server.requests:
        assert request.headers["user-agent"] == "VIKI Agent"
        assert request.headers["accept"] == "application/json"
        assert request.headers["accept-encoding"] == "identity"
        assert "cookie" not in request.headers
    http = TypeSafeHttp(transport=httpx2.MockTransport(server))
    try:
        for method, path in [
            ("GET", "/v1/models"),
            ("GET", "/v1/systemone"),
            ("POST", "/v1/models"),
            ("post", "/v1/systemone"),
            ("PUT", "/v1/systemone"),
            ("DELETE", "/v1/models"),
            ("POST", "/v1/systemone/"),
            ("POST", "/v1/SystemOne"),
            ("POST", "/v1/systemone?model=jev-latest"),
            ("GET", "/v1/models/jev-latest"),
            ("POST", "/v1/../v1/systemone"),
            ("POST", "//other.example.test/v1/systemone"),
            ("POST", "@other.example.test/v1/systemone"),
            ("POST", ""),
        ]:
            with pytest.raises(FixedHttpError, match="^messaging_http_failed$"):
                await http.request(method, path, {}, authorization="Bearer " + KEY)
    finally:
        await http.close()
    assert len(server.requests) == 1


async def test_set_cookie_is_dropped_from_a_successful_reply(
    model: DecisionModel, server: Server
) -> None:
    # TypeSafeHttp opts into set_cookie="drop" (test_mattermost_http.py's
    # test_set_cookie_drop_is_opt_in covers the mechanism itself): a reply that sets a cookie
    # still answers, and the cookie never lands in the client's jar.
    server.headers = {"set-cookie": "synthetic=secret; Path=/"}
    reply = await model.ask(REQUEST, key=KEY)
    assert isinstance(reply, ModelReply)
    assert len(model._http._client.cookies) == 0


async def test_default_client_is_typesafe_over_verified_tls(
    monkeypatch: pytest.MonkeyPatch, server: Server
) -> None:
    options: dict[str, Any] = {}

    def transport(**given: Any) -> httpx2.MockTransport:
        options.update(given)
        return httpx2.MockTransport(server)

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", transport)
    model = DecisionModel()
    try:
        assert isinstance(await model.ask(REQUEST, key=KEY), ModelReply)
    finally:
        await model.close()
    context = options["verify"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert options["trust_env"] is False and options["proxy"] is None
    assert options["retries"] == 0 and options["http2"] is False
    assert options["limits"].max_connections == options["limits"].max_keepalive_connections == 2
    assert [str(request.url) for request in server.requests] == [BASE + "/v1/systemone"]


@pytest.mark.parametrize(
    ("outcome", "headers", "code", "retry_after_s"),
    [
        pytest.param(401, {}, "key_rejected", None, id="401"),
        pytest.param(400, {}, "refused", None, id="400"),
        pytest.param(415, {}, "refused", None, id="415"),
        pytest.param(422, {}, "refused", None, id="422"),
        pytest.param(413, {}, "too_large", None, id="413"),
        pytest.param(429, {"retry-after": "7"}, "rate_limited", 7.0, id="429-7s"),
        pytest.param(429, {"retry-after": "1"}, "rate_limited", 1.0, id="429-1s"),
        pytest.param(429, {"retry-after": "600"}, "rate_limited", 600.0, id="429-600s"),
        pytest.param(429, {"retry-after": "601"}, "rate_limited", 60.0, id="429-601s"),
        pytest.param(429, {"retry-after": "0"}, "rate_limited", 60.0, id="429-0s"),
        pytest.param(429, {"retry-after": "1.5"}, "rate_limited", 60.0, id="429-fraction"),
        pytest.param(
            429,
            {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"},
            "rate_limited",
            60.0,
            id="429-date",
        ),
        pytest.param(429, {}, "rate_limited", 60.0, id="429-none"),
        pytest.param(529, {}, "overloaded", 60.0, id="529-none"),
        pytest.param(529, {"retry-after": "30"}, "overloaded", 30.0, id="529-30s"),
        pytest.param(529, {"retry-after": "601"}, "overloaded", 60.0, id="529-601s"),
        pytest.param(403, {}, "failed", None, id="403"),
        pytest.param(404, {}, "failed", None, id="404"),
        pytest.param(500, {"retry-after": "7"}, "failed", None, id="500"),
        pytest.param(503, {}, "failed", None, id="503"),
        pytest.param(
            302, {"location": "https://other.example.test/v1/systemone"}, "failed", None, id="302"
        ),
        pytest.param(201, {}, "failed", None, id="201"),
        pytest.param(200, {"content-encoding": "gzip"}, "failed", None, id="200-compressed"),
        pytest.param("not-json", {}, "failed", None, id="200-not-json"),
        pytest.param("oversize", {}, "failed", None, id="200-oversize"),
        pytest.param("connect", {}, "unreachable", None, id="connect-error"),
        pytest.param("read-timeout", {}, "unreachable", None, id="read-timeout"),
        pytest.param("hang", {}, "unreachable", None, id="deadline"),
    ],
)
async def test_statuses_map_to_codes(
    model: DecisionModel,
    server: Server,
    monkeypatch: pytest.MonkeyPatch,
    outcome: int | str,
    headers: dict[str, str],
    code: str,
    retry_after_s: float | None,
) -> None:
    answer_with(server, monkeypatch, outcome)
    server.headers = headers
    with pytest.raises(ModelError) as raised:
        await model.ask(REQUEST, key=KEY)
    assert (raised.value.code, raised.value.retry_after_s) == (code, retry_after_s)
    assert raised.value.args == (code,)
    assert raised.value.__context__ is None and raised.value.__cause__ is None
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    "outcome", [200, 401, 413, 429, 500, 529, "not-json", "invalid", "connect", "hang"]
)
async def test_one_transport_call_per_ask(
    model: DecisionModel,
    server: Server,
    monkeypatch: pytest.MonkeyPatch,
    outcome: int | str,
) -> None:
    answer_with(server, monkeypatch, outcome)
    server.headers = {"retry-after": "1"}
    for calls in (1, 2):
        with suppress(ModelError):
            await model.ask(REQUEST, key=KEY)
        assert len(server.requests) == calls


async def test_a_key_of_the_wrong_shape_is_no_key_before_any_call(
    model: DecisionModel, server: Server
) -> None:
    for key in BAD_KEYS:
        with pytest.raises(ModelError, match="^no_key$") as raised:
            await model.ask(REQUEST, key=key)
        assert raised.value.retry_after_s is None
    assert server.requests == []
    printable = "".join(map(chr, range(0x21, 0x7F)))
    await model.ask(REQUEST, key="x" * 4096)
    await model.ask(REQUEST, key=printable)
    assert [request.headers["authorization"] for request in server.requests] == [
        "Bearer " + "x" * 4096,
        "Bearer " + printable,
    ]


@pytest.mark.parametrize(
    "body", [pytest.param(body, id=name) for name, body in INVALID_REPLIES.items()]
)
async def test_reply_shapes_are_checked(model: DecisionModel, server: Server, body: object) -> None:
    server.bodies = {"POST": body}
    with pytest.raises(ModelError) as raised:
        await model.ask(REQUEST, key=KEY)
    assert (raised.value.code, raised.value.retry_after_s) == ("invalid_reply", None)
    assert raised.value.__context__ is None and raised.value.__cause__ is None
    assert len(server.requests) == 1


async def test_reply_bounds_match_the_store(model: DecisionModel, server: Server) -> None:
    # A stored call keeps a model of at most 32 characters and at most 1,000,000 tokens; the
    # values one past each bound are in INVALID_REPLIES.
    server.bodies["POST"] = reply = changed("model", "m" * 32)
    reply["usage"] = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    answered = await model.ask(REQUEST, key=KEY)
    assert (answered.model, answered.input_tokens) == ("m" * 32, 1_000_000)


async def test_checked_reply_keeps_the_numbers(server: Server) -> None:
    clock = [100.0]

    async def timed(request: httpx2.Request) -> httpx2.Response:
        clock[0] += 0.25
        return await server(request)

    extras = changed("extra", {"ignored": True})
    extras["answers"]["goal_link"]["extra"] = "ignored"
    extras["answers"]["importance"]["probabilities"] = {"2": 1, "0": 0}
    extras["usage"]["cached_tokens"] = 3
    questions = playbook.questions()
    tops = {
        "model": "jev-latest",
        "answers": {name: top_answer(question) for name, question in questions.items()},
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    async with typesafe(timed, monotonic=lambda: clock[0]) as model:
        server.bodies["POST"] = extras
        assert await model.ask(REQUEST, key=KEY) == ModelReply(
            "jev-1.14.0",
            {
                "goal_link": NoulAnswer(0.75),
                "kind": ChoiceAnswer("ask", 0.6, {"inform": 0.4, "ask": 0.6}),
                "importance": ScoreAnswer(1.5, 0.5, {0: 0.0, 2: 1.0}),
            },
            120,
            250,
        )
        server.bodies["POST"] = tops
        answered = await model.ask(ModelRequest("jev-latest", {}, questions), key=KEY)
    assert answered.answers == {
        **{name: NoulAnswer(1.0) for name, entry in questions.items() if entry["type"] == "noul"},
        "importance": ScoreAnswer(3.0, 0.0, {3: 1.0}),
        "kind": ChoiceAnswer(
            "report",
            1.0,
            {"inform": 0.0, "ask": 0.0, "suggest": 0.0, "ready": 0.0, "report": 1.0},
        ),
        "work": ChoiceAnswer(
            "coordinate", 1.0, {"none": 0.0, "prepare": 0.0, "run": 0.0, "coordinate": 1.0}
        ),
    }
    assert list(answered.answers) == list(questions)
    goal = answered.answers["goal_link"]
    assert isinstance(goal, NoulAnswer) and type(goal.p) is float
    assert (answered.model, answered.input_tokens, answered.latency_ms) == ("jev-latest", 0, 250)


async def test_key_never_in_errors_or_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    seen: list[object] = []
    servers: list[Server] = []
    # "hang" comes last: its 10 ms deadline then holds only for itself.
    for outcome in (200, 401, 429, 500, "not-json", "invalid", "connect", "hang"):
        server = Server()
        servers.append(server)
        answer_with(server, monkeypatch, outcome)
        server.headers = {"retry-after": "7"}
        async with typesafe(server) as model:
            try:
                seen.append(await model.ask(REQUEST, key=KEY))
            except ModelError as error:
                seen.append(error)
            for key in (KEY + " ", "Bearer " + KEY, KEY + chr(10)):
                with pytest.raises(ModelError, match="^no_key$") as raised:
                    await model.ask(REQUEST, key=key)
                seen.append(raised.value)
            assert KEY not in repr(vars(model)) + repr(vars(model._http))
            assert "authorization" not in model._http._client.headers
    assert len(seen) == 8 * 4
    assert sum(not isinstance(item, ModelError) for item in seen) == 1
    for item in seen:
        assert KEY not in str(item) + repr(item)
        if isinstance(item, ModelError):
            assert item.__context__ is None and item.__cause__ is None
    sent = [request for server in servers for request in server.requests]
    assert len(sent) == 8
    for request in sent:
        holding = [name for name, value in request.headers.multi_items() if KEY in value]
        assert holding == ["authorization"]
        assert request.headers["authorization"] == "Bearer " + KEY
        assert KEY not in str(request.url) and KEY.encode() not in request.content
    assert caplog.records == []
