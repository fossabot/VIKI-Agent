"""Kanban's folder, read cap and batch size are written twice, in the server's reader and in the
view's listing; they must agree. So do the stopped-moving thresholds, written in the view's model
and in Decision's contracts."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

from viki_agent.automation import kanban
from viki_agent.decision_loop import contracts

REPO = Path(__file__).resolve().parents[1]
CARDS = (REPO / "frontend/src/components/Kanban/kanbanCards.ts").read_text(encoding="utf-8")


def _ts_const(name: str) -> str:
    match = re.search(rf"export const {name} = (\"[^\"]*\"|\d+);", CARDS)
    assert match, name
    return match[1].strip('"')


def test_the_view_and_the_server_agree_on_the_kanban_folder_cap_and_batch() -> None:
    assert _ts_const("KANBAN_FOLDER") == kanban.KANBAN_FOLDER == "kanban"
    assert int(_ts_const("KANBAN_READ_CAP")) == kanban.MAX_KANBAN_NOTES == 128
    assert int(_ts_const("KANBAN_BATCH")) == kanban._BATCH == 32


def test_the_view_and_decision_agree_on_when_a_card_stopped_moving() -> None:
    working = timedelta(days=int(_ts_const("STALE_WORKING_DAYS")))
    todo = timedelta(days=int(_ts_const("STALE_TODO_DAYS")))
    assert working == contracts.CARD_STALE_WORKING
    assert todo == contracts.CARD_STALE_TODO
