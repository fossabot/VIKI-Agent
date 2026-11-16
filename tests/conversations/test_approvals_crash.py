"""Real process death around each approval commit, on synthetic files with real DPAPI."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

# The recovery helper lives beside the other storage process-death tests; recovery must be
# performed the same documented way here (a stopped-app copy, never a reset of the source).
from tests.persistence.test_storage_crash import _posix_key_input, _stopped_copy
from viki_agent import storage, winproc
from viki_agent.conversations.approvals import (
    ApprovalError,
    ApprovalService,
    ChoiceInput,
    NativeRequest,
    QuestionChoice,
)
from viki_agent.state import AppState, Prefs

OWNER, NEXT_OWNER, CID, SID, GEN = (
    f"00000000-0000-4000-8000-{index:012d}" for index in range(1, 6)
)
RUNTIME, REQUEST, TURN = (f"00000000-0000-4000-9000-{index:012d}" for index in range(1, 4))
TOOL = "toolu_01SyntheticCrashApproval"
RESPONSE = "synthetic-approval-response-b47c1e"
ANSWER = "synthetic-approval-answer-2d90f5"
DISPLAYED = "synthetic-displayed-request-6ae3c8"
CHOICE = QuestionChoice({"q-1": ANSWER}, RESPONSE)
NATIVE = NativeRequest(RUNTIME, REQUEST, TURN)
INPUT = ChoiceInput(CID, SID, GEN, TOOL, NATIVE, CHOICE)
# A repeat count, not the text: the helper rebuilds the same projection, because a real
# 800 KB card would not fit in a Windows command line.
PADDING = 25_000
ITEM: dict[str, Any] = {
    "type": "question.asked",
    "data": {"tool_use_id": TOOL, "question": DISPLAYED, "padding": DISPLAYED * PADDING},
    "native_request": {"runtime_id": RUNTIME, "request_id": REQUEST, "turn_id": TURN},
}
SPEC = {
    "owner": OWNER,
    "choice_id": CID,
    "session_id": SID,
    "generation": GEN,
    "tool_use_id": TOOL,
    "runtime_id": RUNTIME,
    "request_id": REQUEST,
    "turn_id": TURN,
    "answer": ANSWER,
    "response": RESPONSE,
    "displayed": DISPLAYED,
    "padding": PADDING,
}

_APPROVAL_CRASH = """
import json
import os
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
if sys.platform != 'win32':
    from viki_agent import protection
    key = bytes.fromhex(sys.stdin.readline().strip())
    protection.set_key_source(lambda: key)
from viki_agent.conversations.approvals import (
    ApprovalService, ChoiceInput, NativeRequest, QuestionChoice,
)
from viki_agent.state import AppState, Prefs
spec = json.loads(sys.argv[3])
phase, boundary = sys.argv[4:6]
state = AppState(Path(sys.argv[1]), Prefs())
service = ApprovalService(state, Path(sys.argv[1]).parent / 'vault', owner=spec['owner'])
value = ChoiceInput(
    spec['choice_id'],
    spec['session_id'],
    spec['generation'],
    spec['tool_use_id'],
    NativeRequest(spec['runtime_id'], spec['request_id'], spec['turn_id']),
    QuestionChoice({'q-1': spec['answer']}, spec['response']),
)
item = {
    'type': 'question.asked',
    'data': {
        'tool_use_id': spec['tool_use_id'],
        'question': spec['displayed'],
        'padding': spec['displayed'] * spec['padding'],
    },
    'native_request': {
        'runtime_id': spec['runtime_id'],
        'request_id': spec['request_id'],
        'turn_id': spec['turn_id'],
    },
}
if phase != 'record':
    service.record(value, item)
if phase == 'finish':
    service.mark_delivering(value.choice_id)
original_connect = sqlite3.connect
class Connection(sqlite3.Connection):
    def execute(self, sql, parameters=(), /):
        if sql == 'BEGIN IMMEDIATE':
            super().execute('PRAGMA cache_size=4')
        result = super().execute(sql, parameters)
        mutation = sql.startswith('INSERT INTO approvals') or sql.startswith('UPDATE approvals')
        if (boundary == 'write' and mutation) or (boundary == 'commit' and sql == 'COMMIT'):
            os._exit(73)
        return result
sqlite3.connect = lambda *a, **kw: original_connect(*a, factory=Connection, **kw)
if phase == 'record':
    service.record(value, item)
elif phase == 'intent':
    service.mark_delivering(value.choice_id)
else:
    service.finish(value.choice_id, 'delivered_locally', '')
os._exit(74)
"""

# (phase, boundary) -> the durable row, then what a new launch's owner may claim about it.
_DURABLE = {
    ("record", "commit"): ("recorded", ""),
    ("intent", "write"): ("recorded", ""),
    ("intent", "commit"): ("delivering", ""),
    ("finish", "write"): ("delivering", ""),
    ("finish", "commit"): ("delivered_locally", ""),
}
_PROJECTED = {
    ("recorded", ""): ("not_delivered", "owner_changed"),
    ("delivering", ""): ("unknown", "owner_changed"),
    ("delivered_locally", ""): ("delivered_locally", ""),
}


@pytest.mark.parametrize("phase", ["record", "intent", "finish"])
@pytest.mark.parametrize("boundary", ["write", "commit"])
def test_record_intent_and_finish_survive_real_process_death(
    tmp_path: Path, phase: str, boundary: str
) -> None:
    vault = tmp_path / "vault"
    AppState(tmp_path / "state.json", Prefs())
    result = winproc.run(
        [
            sys.executable,
            "-I",
            "-c",
            _APPROVAL_CRASH,
            str(tmp_path / "state.json"),
            str(Path(storage.__file__).parents[1]),
            json.dumps(SPEC),
            phase,
            boundary,
        ],
        cwd=tmp_path,
        input=_posix_key_input(),
    )
    assert result.returncode == 73, result.stderr
    source = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    for marker in (RESPONSE, ANSWER, DISPLAYED):
        assert all(marker.encode("utf-8") not in data for data in source.values())
        assert all(marker.encode("utf-16le") not in data for data in source.values())

    recovery = _stopped_copy(tmp_path)
    recovered = AppState(recovery / "state.json", Prefs())
    # Keep the original logical vault scope even when app-owned state is recovered elsewhere.
    service = ApprovalService(recovered, vault, owner=NEXT_OWNER)
    if (phase, boundary) == ("record", "write"):
        for missing in (
            lambda: service.get(CID),
            lambda: service.find_request(SID, RUNTIME, REQUEST),
        ):
            with pytest.raises(ApprovalError) as failure:
                missing()
            assert failure.value.code == "not_found"
        assert service.list().approvals == []
        assert service.lookup(INPUT) is None
    else:
        durable = _DURABLE[(phase, boundary)]
        record = service.get(CID)
        assert (record.state, record.reason) == _PROJECTED[durable]
        assert record.choice == CHOICE and record.request == ITEM
        assert service.record(INPUT, ITEM) == (record, False)
        assert service.list().approvals[0].state == record.state
        assert service.find_request(SID, RUNTIME, REQUEST).state == record.state
        # The same-launch projection of an interrupted row belongs to the runner, not here:
        # the same `owner` that wrote the row still reads exactly what was committed.
        same_owner = ApprovalService(recovered, vault, owner=OWNER)
        assert (same_owner.get(CID).state, same_owner.get(CID).reason) == durable
        with contextlib.closing(sqlite3.connect(recovered.database_path)) as connection:
            assert connection.execute("SELECT state,reason FROM approvals").fetchone() == durable
    assert all(path.read_bytes() == data for path, data in source.items())
