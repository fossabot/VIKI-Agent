"""The Look candidates: items for what VIKI sees without a chat in view, built from the facts
`vault_look` and `app_look` read. Pure: no I/O, no clock read (`now` is passed in).

Every item's title is its type words, with no evidence, related chat or ref, and no work
(`work_allowed=()`), so `build_state` sends the model the kind of item and nothing it could name.
The refs, ids, sessions, commands, servers and providers it reports ride only in `facts`,
which `templates` reads for the user's own sentence; `words` is empty except for a waiting chat's
card kind and how long it has waited. Keys hash what an item reports, so a changed set is a
new item; a part that is None (unread) lists nothing.

The code look (`code_candidates`) is the one Look with work: `work_allowed=("prepare",)`, which the
cycle holds back from the arbiter and cuts to its grant. Its key names the folder and its newest
change, not the scope, so one change is looked at once whichever vault is open. The folder's name
and id ride only in `facts`; its title and `words` stay the type words and empty.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from datetime import datetime

from viki_agent.decision_loop import contracts, words
from viki_agent.decision_loop.contracts import Candidate, item_key
from viki_agent.vaults.vault_operations import MAX_RECOVERY_BYTES, MAX_RECOVERY_ENTRIES

# The only card kinds that may become a word for the model; anything else lists no item rather
# than send an unexpected string.
_WAIT_KINDS = ("permission", "plan", "question")


def vault_candidates(
    look: contracts.VaultLook, scope: str, policy: str | None, now: datetime
) -> list[Candidate]:
    """The Look items of one vault: `vault_recovery` (blocked, else stopped; expiring; full) then
    `memory_drift`, only in an ordinary vault. "blocked" lists the rows needing recovery and wins
    over "stopped", the rows idle for `SAVE_STOPPED_AFTER`; "expiring" lists deleted notes whose
    copy goes within `RECOVERY_EXPIRY_WARNING`; "full" is keyed by the day, so it reports once a
    day. `facts`: "workspace_id" and, for `vault_recovery`, "reason"; for `memory_drift`,
    "count"."""
    if policy != "ordinary":
        return []
    found: list[Candidate] = []

    def recovery(reason: str, *ids: str) -> None:
        found.append(
            _look(
                "vault_recovery",
                item_key("vault_recovery", scope, reason, *sorted(ids)),
                {"workspace_id": scope, "reason": reason},
                ("vault",),
            )
        )

    saves = look.saves or ()
    blocked = [row.row_id for row in saves if row.blocked]
    stopped = [
        row.row_id
        for row in saves
        if row.updated is not None and now - row.updated >= contracts.SAVE_STOPPED_AFTER
    ]
    if blocked:
        recovery("blocked", *blocked)
    elif stopped:
        recovery("stopped", *stopped)
    expiring = [
        note.recovery_id
        for note in look.deleted or ()
        if note.expires is not None
        and now < note.expires <= now + contracts.RECOVERY_EXPIRY_WARNING
    ]
    if expiring:
        recovery("expiring", *expiring)
    if look.usage is not None and _recovery_full(*look.usage):
        found.append(
            _look(
                "vault_recovery",
                item_key("vault_recovery", scope, "full", now.date().isoformat()),
                {"workspace_id": scope, "reason": "full"},
                ("vault",),
            )
        )
    if look.drift:
        found.append(
            _look(
                "memory_drift",
                item_key("memory_drift", scope, *sorted(look.drift)),
                {"workspace_id": scope, "count": len(look.drift)},
                ("vault",),
            )
        )
    return found


def app_candidates(
    look: contracts.AppLook,
    scope: str,
    now: datetime,
    *,
    exclude: Collection[str] = frozenset(),
) -> list[Candidate]:
    """The Look items of the live app, in the order bridges, waiting chats, uncertain sends, heavy
    MCP servers. `exclude` holds sessions already answered elsewhere (a mission's chat, a seat
    waiting for an answer); their cards list nothing. A waiting chat lists once its card is
    `CHAT_WAIT` old, oldest first, at most `MAX_CHATS_WAITING`; sends are the newest per chat,
    newest first, at most `MAX_UNCERTAIN_SENDS`; servers are sorted by chat and name, at most
    `MAX_MCP_CONTEXT`. `facts`: "workspace_id" on all; "provider", "restart" and "reason"
    (bridge_stopped); "session_id" and "kind" (chat_waiting); "session_id" and "command_id"
    (send_uncertain); "session_id" and "server" (mcp_context). `words`: "kind" and "wait" of a
    waiting chat only."""
    day = now.date().isoformat()
    found = [
        _look(
            "bridge_stopped",
            item_key(
                "bridge_stopped",
                bridge.provider,
                "restart" if bridge.restart else bridge.reason or "stopped",
                day,
            ),
            {
                "workspace_id": scope,
                "provider": bridge.provider,
                "restart": bridge.restart,
                "reason": bridge.reason,
            },
            ("messaging",),
        )
        for bridge in look.bridges or ()
    ]
    waiting = sorted(
        (
            chat
            for chat in look.waiting or ()
            if chat.session_id not in exclude
            and chat.kind in _WAIT_KINDS
            and now - chat.since >= contracts.CHAT_WAIT
        ),
        key=lambda chat: (chat.since, chat.session_id, chat.tool_use_id),
    )
    found += [
        _look(
            "chat_waiting",
            item_key("chat_waiting", chat.session_id, chat.tool_use_id),
            {"workspace_id": scope, "session_id": chat.session_id, "kind": chat.kind},
            ("chat",),
            {"kind": chat.kind, "wait": words.wait_words(chat.since, now)},
        )
        for chat in waiting[: contracts.MAX_CHATS_WAITING]
    ]
    found += [
        _look(
            "send_uncertain",
            item_key("send_uncertain", send.command_id),
            {"workspace_id": scope, "session_id": send.session_id, "command_id": send.command_id},
            ("chat",),
        )
        for send in _newest_per_chat(look.sends or ())[: contracts.MAX_UNCERTAIN_SENDS]
    ]
    heavy = sorted({(server.session_id, server.server) for server in look.heavy or ()})
    found += [
        _look(
            "mcp_context",
            item_key("mcp_context", session_id, server),
            {"workspace_id": scope, "session_id": session_id, "server": server},
            ("chat",),
        )
        for session_id, server in heavy[: contracts.MAX_MCP_CONTEXT]
    ]
    return found


def code_candidates(look: contracts.CodeLook, scope: str, now: datetime) -> list[Candidate]:
    """The `code_look` items: folders whose newest change is `CODE_LOOK_IDLE` old, most recently
    changed first (ties by folder id), at most `MAX_CODE_LOOKS`. `facts`: "workspace_id" (the
    scope), "folder_id" and "folder" (its name). The item's one action opens the folder in Code."""
    idle = sorted(
        (
            folder
            for folder in look.folders or ()
            if now - folder.newest >= contracts.CODE_LOOK_IDLE
        ),
        key=lambda folder: (-folder.newest.timestamp(), folder.workspace_id),
    )
    return [
        _look(
            "code_look",
            item_key("code_look", folder.workspace_id, folder.newest.isoformat()),
            {"workspace_id": scope, "folder_id": folder.workspace_id, "folder": folder.name},
            ("code",),
            work_allowed=("prepare",),
        )
        for folder in idle[: contracts.MAX_CODE_LOOKS]
    ]


def _recovery_full(entries: int, size: int) -> bool:
    """Whether the recovery store holds `RECOVERY_FULL_SHARE` of either bound."""
    return (
        entries >= contracts.RECOVERY_FULL_SHARE * MAX_RECOVERY_ENTRIES
        or size >= contracts.RECOVERY_FULL_SHARE * MAX_RECOVERY_BYTES
    )


def _newest_per_chat(sends: Iterable[contracts.UncertainSend]) -> list[contracts.UncertainSend]:
    """Each chat's newest send, newest first (ties by command id, so the order is fixed)."""
    ordered = sorted(sends, key=lambda send: (-send.updated.timestamp(), send.command_id))
    seen: set[str] = set()
    newest = []
    for send in ordered:
        if send.session_id not in seen:
            seen.add(send.session_id)
            newest.append(send)
    return newest


def _look(
    candidate_type: str,
    key: str,
    facts: Mapping[str, object],
    shown_elsewhere: tuple[str, ...],
    word_forms: Mapping[str, str] | None = None,
    *,
    work_allowed: tuple[str, ...] = (),
) -> Candidate:
    title = words.type_words(candidate_type)
    return Candidate(
        item_key=key,
        type=candidate_type,
        title=title,
        facts=facts,
        words=word_forms or {},
        evidence=(),
        related_chat=None,
        related_ref=None,
        deadline=None,
        shown_elsewhere=shown_elsewhere,
        rules_evidence={},
        origin_session=None,
        work_allowed=work_allowed,
    )
