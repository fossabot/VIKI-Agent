"""The Look candidates (`looks.py`): which facts list an item, at which thresholds, in which order
and under which key; and the privacy proof: the Decision model gets only each item's type
words (and a waiting chat's kind and wait words), and no Look item ever gets work."""

from __future__ import annotations

import json
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.decision_loop.test_arbiter import random_input
from viki_agent.decision_loop import contracts, words
from viki_agent.decision_loop.arbiter import decide
from viki_agent.decision_loop.contracts import (
    AppLook,
    Candidate,
    CodeFolder,
    CodeLook,
    DecisionSettings,
    DeletedNote,
    HeavyServer,
    SaveRow,
    StoppedBridge,
    UncertainSend,
    VaultLook,
    WaitingChat,
    canonical_json,
    item_key,
)
from viki_agent.decision_loop.looks import app_candidates, code_candidates, vault_candidates
from viki_agent.decision_loop.state import History, Now, Owner, build_state
from viki_agent.vaults.vault_operations import MAX_RECOVERY_BYTES, MAX_RECOVERY_ENTRIES

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
SCOPE = "ws-1"
MINUTE = timedelta(minutes=1)


def vault(**changes: Any) -> VaultLook:
    """A vault look that read every part and found nothing."""
    return replace(VaultLook(saves=(), deleted=(), usage=(0, 0), drift=()), **changes)


def app(**changes: Any) -> AppLook:
    """An app look that read every part and found nothing."""
    return replace(AppLook(waiting=(), heavy=(), sends=(), bridges=()), **changes)


def save(row_id: str, age: timedelta = timedelta(0), *, blocked: bool = False) -> SaveRow:
    return SaveRow(row_id, False, blocked, NOW - age)


def reasons(items: list[Candidate]) -> list[object]:
    return [item.facts["reason"] for item in items]


def wait(session: str, tool: str, age: timedelta, kind: str = "permission") -> WaitingChat:
    return WaitingChat(session, tool, kind, NOW - age)


def send(command: str, session: str, age: timedelta) -> UncertainSend:
    return UncertainSend(command, session, NOW - age)


# ---- vault items -----------------------------------------------------------------------------


def test_vault_recovery_reports_blocked_stopped_expiring_and_full() -> None:
    stopped = vault(saves=(save("s1", 2 * MINUTE), save("s2", MINUTE)))
    blocked = vault(saves=(save("s1", 2 * MINUTE), save("op", MINUTE, blocked=True)))
    expiring = vault(deleted=(DeletedNote("d1", NOW + timedelta(days=2)),))
    full = vault(usage=(231, 0))
    quiet = vault(
        saves=(save("s1", MINUTE),), deleted=(DeletedNote("d1", NOW + timedelta(days=9)),)
    )

    assert reasons(vault_candidates(stopped, SCOPE, "ordinary", NOW)) == ["stopped"]
    # A blocked row reports at once, whatever its age, and the idle save is not listed beside it.
    assert reasons(vault_candidates(blocked, SCOPE, "ordinary", NOW)) == ["blocked"]
    assert reasons(vault_candidates(expiring, SCOPE, "ordinary", NOW)) == ["expiring"]
    assert reasons(vault_candidates(full, SCOPE, "ordinary", NOW)) == ["full"]
    assert vault_candidates(quiet, SCOPE, "ordinary", NOW) == []
    everything = vault(
        saves=blocked.saves, deleted=expiring.deleted, usage=full.usage, drift=("m1",)
    )
    items = vault_candidates(everything, SCOPE, "ordinary", NOW)
    assert [(item.type, item.facts.get("reason")) for item in items] == [
        ("vault_recovery", "blocked"),
        ("vault_recovery", "expiring"),
        ("vault_recovery", "full"),
        ("memory_drift", None),
    ]
    for item in items:
        assert item.facts["workspace_id"] == SCOPE
        assert item.shown_elsewhere == ("vault",)


def test_recovery_items_come_only_from_an_ordinary_vault_and_a_read_part() -> None:
    everything = vault(
        saves=(save("op", blocked=True),),
        deleted=(DeletedNote("d1", NOW + timedelta(days=1)),),
        usage=(MAX_RECOVERY_ENTRIES, 0),
        drift=("m1",),
    )
    assert len(vault_candidates(everything, SCOPE, "ordinary", NOW)) == 4
    for policy in ("protected", "readonly", None):
        assert vault_candidates(everything, SCOPE, policy, NOW) == []
    unread = VaultLook(
        saves=None, deleted=None, usage=None, drift=None, failures=("saves_unknown",)
    )
    assert vault_candidates(unread, SCOPE, "ordinary", NOW) == []


def test_stopped_saves_start_at_exactly_two_minutes_and_skip_an_unparsed_stamp() -> None:
    edge = contracts.SAVE_STOPPED_AFTER
    just_under = SaveRow("a", False, False, NOW - edge + timedelta(seconds=1))
    exactly = SaveRow("b", True, False, NOW - edge)
    unparsed = SaveRow("c", False, False, None)
    assert vault_candidates(vault(saves=(just_under,)), SCOPE, "ordinary", NOW) == []
    assert reasons(vault_candidates(vault(saves=(exactly,)), SCOPE, "ordinary", NOW)) == ["stopped"]
    assert vault_candidates(vault(saves=(unparsed,)), SCOPE, "ordinary", NOW) == []
    # A blocked row needs no stamp.
    blocked = SaveRow("d", True, True, None)
    assert reasons(vault_candidates(vault(saves=(blocked,)), SCOPE, "ordinary", NOW)) == ["blocked"]


def test_expiring_covers_the_next_three_days_but_not_an_expired_copy() -> None:
    def listed(expires: datetime | None) -> bool:
        deleted = (DeletedNote("d1", expires),)
        return bool(vault_candidates(vault(deleted=deleted), SCOPE, "ordinary", NOW))

    window = contracts.RECOVERY_EXPIRY_WARNING
    assert window == timedelta(days=3)
    assert listed(NOW + window)
    assert not listed(NOW + window + timedelta(seconds=1))
    assert listed(NOW + timedelta(seconds=1))
    assert not listed(NOW)
    assert not listed(NOW - MINUTE)
    assert not listed(None)


def test_full_starts_at_ninety_percent_of_either_bound() -> None:
    entry_edge = int(contracts.RECOVERY_FULL_SHARE * MAX_RECOVERY_ENTRIES) + 1  # 231 of 256
    byte_edge = int(contracts.RECOVERY_FULL_SHARE * MAX_RECOVERY_BYTES) + 1

    def listed(entries: int, size: int) -> bool:
        return bool(vault_candidates(vault(usage=(entries, size)), SCOPE, "ordinary", NOW))

    assert listed(entry_edge, 0)
    assert not listed(entry_edge - 1, 0)
    assert listed(0, byte_edge)
    assert not listed(0, byte_edge - 1)
    assert not listed(entry_edge - 1, byte_edge - 1)


def test_memory_and_recovery_items_key_by_their_sets() -> None:
    def keys(look: VaultLook, now: datetime = NOW) -> list[str]:
        return [item.item_key for item in vault_candidates(look, SCOPE, "ordinary", now)]

    drift = keys(vault(drift=("m2", "m1")))
    assert drift == [item_key("memory_drift", SCOPE, "m1", "m2")]  # sorted: order never moves a key
    assert keys(vault(drift=("m1", "m2"))) == drift
    assert keys(vault(drift=("m1",))) != drift
    assert keys(vault(drift=("m1", "m3"))) != drift
    assert (
        vault_candidates(vault(drift=("m1", "m2")), SCOPE, "ordinary", NOW)[0].facts["count"] == 2
    )
    other = vault_candidates(vault(drift=("m1", "m2")), "ws-2", "ordinary", NOW)[0]
    assert other.item_key != drift[0]

    stopped = keys(vault(saves=(save("s1", 3 * MINUTE), save("s2", 3 * MINUTE))))
    assert stopped == [item_key("vault_recovery", SCOPE, "stopped", "s1", "s2")]
    assert keys(vault(saves=(save("s1", 3 * MINUTE),))) != stopped
    assert keys(vault(saves=(save("s1", 3 * MINUTE, blocked=True),))) != keys(
        vault(saves=(save("s1", 3 * MINUTE),))
    )
    expiring = vault(deleted=(DeletedNote("d1", NOW + MINUTE),))
    assert keys(expiring) != keys(vault(deleted=(DeletedNote("d2", NOW + MINUTE),)))
    full = vault(usage=(MAX_RECOVERY_ENTRIES, 0))
    assert keys(full) == [item_key("vault_recovery", SCOPE, "full", "2026-10-21")]
    assert keys(full, NOW + timedelta(hours=1)) == keys(full)  # once a day
    assert keys(full, NOW + timedelta(days=1)) != keys(full)


# ---- app items -------------------------------------------------------------------------------


def test_chats_waiting_oldest_first_past_five_minutes() -> None:
    edge = contracts.CHAT_WAIT
    assert edge == timedelta(minutes=5)
    look = app(
        waiting=(
            wait("young", "t0", edge - timedelta(seconds=1)),
            wait("b", "t2", timedelta(hours=2), "plan"),
            wait("a", "t1", edge, "question"),
            wait("c", "t3", timedelta(hours=3)),
            wait("mine", "t4", timedelta(hours=9)),
        )
    )
    items = app_candidates(look, SCOPE, NOW, exclude={"mine"})
    assert [item.facts["session_id"] for item in items] == ["c", "b", "a"]
    assert [item.facts["kind"] for item in items] == ["permission", "plan", "question"]
    assert [item.words for item in items] == [
        {"kind": "permission", "wait": words.wait_words(NOW - timedelta(hours=3), NOW)},
        {"kind": "plan", "wait": words.wait_words(NOW - timedelta(hours=2), NOW)},
        {"kind": "question", "wait": words.wait_words(NOW - edge, NOW)},
    ]
    assert items[0].item_key == item_key("chat_waiting", "c", "t3")
    assert items[0].shown_elsewhere == ("chat",)
    assert all(item.facts["workspace_id"] == SCOPE for item in items)


def test_waiting_chats_are_capped_and_keep_the_oldest() -> None:
    many = tuple(
        wait(f"s{n}", "t", timedelta(hours=n + 1)) for n in range(contracts.MAX_CHATS_WAITING + 2)
    )
    items = app_candidates(app(waiting=many), SCOPE, NOW)
    assert len(items) == contracts.MAX_CHATS_WAITING == 6
    assert items[0].facts["session_id"] == f"s{contracts.MAX_CHATS_WAITING + 1}"


def test_a_card_of_an_unknown_kind_lists_nothing() -> None:
    odd = (wait("s1", "t1", timedelta(hours=1), "notes/private.md"),)
    assert app_candidates(app(waiting=odd), SCOPE, NOW) == []


def test_uncertain_sends_one_per_chat() -> None:
    look = app(
        sends=(
            send("c1", "s1", timedelta(hours=5)),
            send("c2", "s1", timedelta(hours=1)),  # newest of s1
            send("c3", "s2", timedelta(hours=3)),
            send("c4", "s3", timedelta(hours=2)),
        )
    )
    items = app_candidates(look, SCOPE, NOW)
    assert [item.facts["command_id"] for item in items] == ["c2", "c4", "c3"]
    assert [item.facts["session_id"] for item in items] == ["s1", "s3", "s2"]
    assert items[0].item_key == item_key("send_uncertain", "c2")
    assert items[0].shown_elsewhere == ("chat",)
    assert all(item.words == {} for item in items)

    many = tuple(send(f"c{n}", f"s{n}", timedelta(hours=n + 1)) for n in range(8))
    capped = app_candidates(app(sends=many), SCOPE, NOW)
    assert [item.facts["command_id"] for item in capped] == ["c0", "c1", "c2", "c3", "c4"]


def test_heavy_servers_and_bridges() -> None:
    look = app(
        heavy=(
            HeavyServer("s2", "github"),
            HeavyServer("s1", "slackmcp"),
            HeavyServer("s1", "github"),
            HeavyServer("s1", "github"),
            HeavyServer("s3", "wiki"),
        ),
        bridges=(
            StoppedBridge("slack", False, "connection_lost"),
            StoppedBridge("discord", True, None),
        ),
    )
    items = app_candidates(look, SCOPE, NOW)
    assert [item.type for item in items] == ["bridge_stopped"] * 2 + ["mcp_context"] * 3
    slack, discord, *heavy = items
    assert slack.facts == {
        "workspace_id": SCOPE,
        "provider": "slack",
        "restart": False,
        "reason": "connection_lost",
    }
    assert slack.item_key == item_key("bridge_stopped", "slack", "connection_lost", "2026-10-21")
    assert discord.item_key == item_key("bridge_stopped", "discord", "restart", "2026-10-21")
    assert slack.shown_elsewhere == discord.shown_elsewhere == ("messaging",)
    assert [(item.facts["session_id"], item.facts["server"]) for item in heavy] == [
        ("s1", "github"),
        ("s1", "slackmcp"),
        ("s2", "github"),
    ]
    assert heavy[0].item_key == item_key("mcp_context", "s1", "github")
    assert heavy[0].shown_elsewhere == ("chat",)

    no_reason = app_candidates(app(bridges=(StoppedBridge("slack", False, None),)), SCOPE, NOW)
    assert no_reason[0].item_key == item_key("bridge_stopped", "slack", "stopped", "2026-10-21")
    next_day = app_candidates(
        app(bridges=(StoppedBridge("slack", False, None),)), SCOPE, NOW + timedelta(days=1)
    )
    assert next_day[0].item_key != no_reason[0].item_key


def test_an_unread_app_part_lists_nothing() -> None:
    unread = AppLook(
        waiting=None, heavy=None, sends=None, bridges=None, failures=("chats_unknown",)
    )
    assert app_candidates(unread, SCOPE, NOW) == []


# ---- code items ------------------------------------------------------------------------------


def folder(workspace_id: str, age: timedelta, name: str = "api") -> CodeFolder:
    return CodeFolder(workspace_id, name, NOW - age)


def test_code_looks_list_folders_idle_a_day_or_more_newest_first() -> None:
    day = contracts.CODE_LOOK_IDLE
    assert day == timedelta(days=1)
    edge = CodeLook((folder("on-edge", day), folder("just-under", day - timedelta(seconds=1))))
    assert [i.facts["folder_id"] for i in code_candidates(edge, SCOPE, NOW)] == ["on-edge"]

    four = CodeLook(
        (
            folder("w-old", timedelta(days=9)),
            folder("w-b", timedelta(days=3)),
            folder("w-new", timedelta(days=2)),
            folder("w-a", timedelta(days=3)),
        )
    )
    items = code_candidates(four, SCOPE, NOW)
    assert contracts.MAX_CODE_LOOKS == 3
    # The oldest is the one cut; a tie in time falls to the folder id.
    assert [i.facts["folder_id"] for i in items] == ["w-new", "w-a", "w-b"]
    first = items[0]
    assert first.type == "code_look"
    assert first.facts == {"workspace_id": SCOPE, "folder_id": "w-new", "folder": "api"}
    assert first.shown_elsewhere == ("code",)
    assert first.work_allowed == ("prepare",)
    assert all(i.work_allowed == ("prepare",) for i in items)


def test_a_code_looks_key_follows_its_folder_and_change_not_the_scope() -> None:
    look = CodeLook((folder("w1", timedelta(days=2)),))
    item = code_candidates(look, SCOPE, NOW)[0]
    assert item.item_key == item_key("code_look", "w1", (NOW - timedelta(days=2)).isoformat())
    assert code_candidates(look, "ws-2", NOW)[0].item_key == item.item_key
    assert code_candidates(look, SCOPE, NOW + timedelta(hours=5))[0].item_key == item.item_key
    changed = CodeLook((folder("w1", timedelta(days=1, hours=1)),))
    assert code_candidates(changed, SCOPE, NOW)[0].item_key != item.item_key
    other = CodeLook((folder("w2", timedelta(days=2)),))
    assert code_candidates(other, SCOPE, NOW)[0].item_key != item.item_key


def test_an_unread_code_look_lists_nothing() -> None:
    assert code_candidates(CodeLook(None, ("code_unknown",)), SCOPE, NOW) == []
    assert code_candidates(CodeLook(()), SCOPE, NOW) == []


def test_a_code_look_sends_only_its_kind_to_the_model() -> None:
    # Digit-free, so `build_state` dropping a `words` value with a digit cannot hide a leak.
    name, folder_id, scope = "secret-folder-name", "secret-folder-id", "secret-scope"
    look = CodeLook((CodeFolder(folder_id, name, NOW - timedelta(days=2)),))
    item = code_candidates(look, scope, NOW)[0]
    assert (item.facts["folder"], item.facts["folder_id"]) == (name, folder_id)
    assert item.title == words.type_words("code_look")
    assert item.words == {}
    assert (item.evidence, item.related_chat, item.related_ref) == ((), None, None)

    sent = _build(item)
    text = canonical_json(sent)
    assert words.type_words("code_look") in text
    for private in (name, folder_id, scope, "secret"):
        assert private not in text
    candidate_part = sent["candidate"]
    assert isinstance(candidate_part, dict)
    assert candidate_part["type"] == candidate_part["title"] == words.type_words("code_look")
    assert candidate_part["facts"] == {}


# ---- every item: shape, privacy, no work -----------------------------------------------------

# Digit-free private values: `build_state` drops a `words` value with a digit, so a value with one
# would hide a leak from the test.
REF = "notes-secret-ref"
PRIVATE = (
    REF,
    "secret-row",
    "secret-recovery",
    "secret-memory",
    "secret-session",
    "secret-command",
    "secret-tool",
    "secret-server",
    "secretprovider",
    "secret-reason",
)


def _private_looks() -> dict[str, Candidate]:
    """One item of each Look type, built from facts that hold only private values."""
    long_ago = timedelta(hours=2)
    vault_items = vault_candidates(
        vault(
            saves=(save("secret-row", blocked=True),),
            deleted=(DeletedNote("secret-recovery", NOW + MINUTE),),
            drift=("secret-memory", REF),
        ),
        REF,
        "ordinary",
        NOW,
    )
    app_items = app_candidates(
        AppLook(
            waiting=(wait("secret-session", "secret-tool", long_ago),),
            heavy=(HeavyServer("secret-session", "secret-server"),),
            sends=(send("secret-command", "secret-session", long_ago),),
            bridges=(StoppedBridge("secretprovider", False, "secret-reason"),),
        ),
        REF,
        NOW,
    )
    return {item.type: item for item in (*vault_items, *app_items)}


def test_every_look_type_has_an_item() -> None:
    assert set(_private_looks()) == set(contracts.LOOKS)


def _build(item: Candidate) -> dict[str, object]:
    now = Now(None, False, False, NOW, 0)
    built = build_state(item, Owner(), now, History(), DecisionSettings())
    assert not built.blocked
    return json.loads(canonical_json(built.state))


@pytest.mark.parametrize("candidate_type", contracts.LOOKS)
def test_look_items_send_only_their_kind_to_the_model(candidate_type: str) -> None:
    item = _private_looks()[candidate_type]
    sent = _build(item)
    text = canonical_json(sent)
    assert words.type_words(candidate_type) in text
    for private in PRIVATE:
        assert private not in text, (candidate_type, private)
    # Nothing but the type, its title (the type words again), the card kind and wait of a waiting
    # chat, and where else the user already sees it.
    candidate_part = sent["candidate"]
    assert isinstance(candidate_part, dict)
    assert candidate_part["type"] == candidate_part["title"] == words.type_words(candidate_type)
    assert candidate_part["evidence"] == []
    assert "project" not in candidate_part
    expected = {"kind": "permission", "wait": "over an hour"}
    assert candidate_part["facts"] == (expected if candidate_type == "chat_waiting" else {})
    assert not any(ch.isdigit() for value in item.words.values() for ch in value)
    assert (item.evidence, item.related_chat, item.related_ref, item.origin_session) == (
        (),
        None,
        None,
        None,
    )
    assert (item.rules_evidence, item.work_allowed) == ({}, ())


@pytest.mark.parametrize("candidate_type", contracts.LOOKS)
def test_look_items_never_get_work(candidate_type: str) -> None:
    item = _private_looks()[candidate_type]
    assert item.work_allowed == ()
    for seed in range(200):
        inp = random_input(random.Random(seed))
        inp = replace(inp, candidate=replace(item, deadline=None))
        out = decide(inp)
        assert out.work == "none", (candidate_type, seed)
