"""Detect self-improvement candidates: offer a new skill, suggest one of the
user's own skills for the chat in view, learn a source, call for a retro, tune one of Decision's
own questions, and propose vault upkeep -- link two notes, flag an older note as superseded,
archive a project's ticked tasks.

`learn_source`, `skill_offer`, `skill_suggest`, `note_link`, `note_superseded` and `tasks_ticked`
read one cycle's `Observation` (the same shape `detectors.py` gathers), the way every other
per-request detector does; `detectors.detect` calls them while `decision.watch.improve` is on (the
three vault-change builders also need `settings.limit_changes > 0`, plus `watch_vault_changes`
for the note pair or `watch_tasks` for ticked tasks). The three vault-change builders fan out, one
`Candidate` per pair or per ticked file in `obs.changes` (from `vault_watch.read_changes`), and
read only the `NoteFacts`/`TickedFile` it already read -- never a fresh vault read of their own.
`from_rows` reads the learning rows the engine's beat already reads for `learning.relearn` -- one
shared read, never a second query -- and builds retro_due and question_tune from them instead,
since both judge a window of past decisions rather than the current page.

Pure: no I/O, no clock read (`now` is passed in). Every candidate carries `work_allowed=()` --
none of these eight ever starts work, only prepares a draft -- and a `rules_evidence` of counts
and codes only, never an excerpt or a count derived from one: `rules.decide` and, through
`state.py`, the model itself only ever see how many, not what. `words` stays empty for all eight
(nothing here words a sentence with a number); the user-authored text that does reach a
candidate (a chat title, a capture's own excerpt, a question's wording, a note's opening or
mention line) travels through `facts` or `evidence`, exactly where the existing send-level and
never-send rules already watch it.

skill_suggest follows ADR-061: `pick_skill` matches the chat's title against the vault's own
skills in code and keeps only the matched name, in `facts` (its own docstring has the rest).
"""

from __future__ import annotations

import itertools
import re
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from viki_agent.automation.routines import stamp
from viki_agent.decision_loop import arbiter, calibration, contracts, playbook, rules
from viki_agent.decision_loop.cycle import decode_answers
from viki_agent.decision_loop.stats import LearningRow

if TYPE_CHECKING:
    # Only for the type hint: `detectors.py` imports this module, so importing it back at
    # runtime would cycle. `from __future__ import annotations` keeps every annotation a string,
    # so this name is never looked up except by the type checker.
    from viki_agent.decision_loop.detectors import Observation

_LINK = re.compile(r"https?://")
_RETRO_WINDOW = timedelta(days=14)
_WRONG_OR_MUTE = frozenset({"wrong", "mute"})
_CLOSING = frozenset({"useful", "wrong", "mute"})  # the labels that close an item (service._CLOSES)
_TUNE_MIN_SIDE = 10  # yes_n/no_n floor for the drifting-question branch (i)
_TUNE_DRIFT_GAP = 0.05
_TUNE_WRONG_LABELS = 5  # wrong labels on one topic that call for the worst-topic branch (ii)

# skill_suggest's bar: a title word in a skill's name scores 2 and another one in its
# description 1, so one name word alone, or two description words alone, fall short.
MIN_SCORE = 3
# match_tokens reads at most this many characters of the *folded* text: NFKC can expand one
# input character into several (each U+FDFA becomes 18), so a cut on the caller's input length
# -- the inspector's own 2,048-byte description cut applies before NFKC -- would not bound what
# match_tokens actually scans.
MAX_MATCH_CHARS = 2048
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "does", "for", "from",
    "get", "has", "have", "how", "i", "if", "in", "into", "is", "it", "its", "me", "my", "no",
    "not", "of", "on", "or", "our", "out", "so", "than", "that", "the", "their", "them", "then",
    "there", "these", "they", "this", "to", "up", "us", "use", "used", "uses", "using", "via",
    "was", "we", "what", "when", "where", "which", "while", "who", "why", "will", "with", "you",
    "your", "about", "owner", "user", "says", "say", "asks", "ask", "skill", "skills",
})  # fmt: skip
# Kana (U+3040-U+30FF) and CJK ideographs (U+3400-U+4DBF, U+4E00-U+9FFF), then a pair of
# hiragana (U+3040-U+309F), kept as escapes.
_KANA_HAN = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")
_HIRAGANA_PAIR = re.compile(r"[\u3040-\u309f]{2}")
_SKILL_SUGGEST_TITLE = "A skill for this chat"


# ---- per-request: the client's own page --------------------------------------------------------


def learn_source(obs: Observation) -> contracts.Candidate | None:
    """One aggregate candidate over the vault's oldest kept inbox captures (already at most
    `brief.MAX_INBOX_ITEMS`, oldest first) whose excerpt holds a link -- one suggestion per batch
    of links, never one per capture. `evidence` holds those captures themselves (read for their
    refs alone: `templates._learn_source_draft` never quotes an excerpt); `related_ref` and the
    item key use the oldest of them. Vault scopes only; None with no vault or no link."""
    if obs.kind != "vault" or obs.vault is None:
        return None
    captures = [item for item in obs.vault.inbox.items if _LINK.search(item.excerpt)]
    if not captures:
        return None
    oldest = captures[0]
    evidence = tuple(
        contracts.Evidence(item.ref, item.excerpt, False, item.tags) for item in captures
    )
    title = "Sources to learn"
    return contracts.Candidate(
        item_key=contracts.item_key("learn_source", oldest.ref),
        type="learn_source",
        title=title,
        facts={"workspace_id": obs.scope},
        words={},
        evidence=evidence,
        related_chat=None,
        related_ref=oldest.ref,
        deadline=None,
        shown_elsewhere=(),
        rules_evidence={"links": len(captures)},
        origin_session=None,
        work_allowed=(),
    )


def skill_offer(obs: Observation) -> contracts.Candidate | None:
    """One candidate carrying the client's own skill hint (`obs.page.skill`) through unchanged.
    `evidence` holds the *other* chats' titles only, `Evidence(None, title, False, ())` so the
    send level and the inline never-send tags still apply to them; the chat in view never
    appears in its own evidence, only as `title` and `related_chat`. Vault scopes only -- the
    client itself mounts the hint for a vault workspace alone, but this detector does not take
    that on trust. None without a hint."""
    if obs.kind != "vault" or obs.page is None or obs.page.skill is None:
        return None
    hint = obs.page.skill
    other_titles = hint.titles[1:]
    evidence = tuple(contracts.Evidence(None, title, False, ()) for title in other_titles)
    title = hint.titles[0] if hint.titles and hint.titles[0] else "This chat"
    return contracts.Candidate(
        item_key=contracts.item_key("skill_offer", hint.trigger, *sorted(hint.sessions)),
        type="skill_offer",
        title=title,
        facts={"trigger": hint.trigger, "sessions": hint.sessions, "titles": hint.titles},
        words={},
        evidence=evidence,
        related_chat=hint.sessions[0] if hint.sessions else None,
        related_ref=None,
        deadline=None,
        shown_elsewhere=(),
        rules_evidence={"chats": len(hint.sessions), "trigger": hint.trigger},
        origin_session=None,
        work_allowed=(),
    )


def match_tokens(text: str) -> frozenset[str]:
    """`text`'s words for matching a skill: folded by NFKC, then casefolded, cut to at
    most `MAX_MATCH_CHARS` characters of that *folded* text, and split into runs of letters and
    numbers (Unicode categories L* and N*), a run also breaking between a kana/Han character
    (`_KANA_HAN`) and any other. A kana/Han run gives each of its overlapping two-character pairs
    but an all-hiragana one; any other run gives itself when it has two characters or more, is
    not all numerals and is not one of `_STOPWORDS`."""
    tokens: set[str] = set()
    folded = unicodedata.normalize("NFKC", text).casefold()[:MAX_MATCH_CHARS]
    for kind, chars in itertools.groupby(folded, key=_run_kind):
        run = "".join(chars)
        if kind == "kana_han":
            pairs = (run[at : at + 2] for at in range(len(run) - 1))
            tokens.update(pair for pair in pairs if _HIRAGANA_PAIR.fullmatch(pair) is None)
        elif (
            kind == "word"
            and len(run) >= 2
            and not all(unicodedata.category(ch).startswith("N") for ch in run)
            and run not in _STOPWORDS
        ):
            tokens.add(run)
    return frozenset(tokens)


def _run_kind(ch: str) -> str:
    """The run `ch` belongs to in `match_tokens`: "kana_han", "word" for any other letter or
    number, or "none"."""
    if unicodedata.category(ch)[0] not in "LN":
        return "none"
    return "kana_han" if _KANA_HAN.match(ch) else "word"


@dataclass(frozen=True, slots=True)
class SkillWords:
    """One of the vault's own skills, tokenized once -- `skill_words`, called from `gather` in
    its worker thread -- so that `pick_skill`, reached from `detect` on the server's event
    loop, never tokenizes a skill's own name or description there. `name` is the skill's own
    name, read back for `facts["skill"]` and matched against `used`; `name_tokens` is
    `match_tokens` of the name with "-" read as a space, `description_tokens` of the
    description."""

    name: str
    name_tokens: frozenset[str]
    description_tokens: frozenset[str]


def skill_words(name: str, description: str) -> SkillWords:
    """`SkillWords` for one of the vault's own skills, *name* and *description* as the catalog
    reads them."""
    return SkillWords(name, match_tokens(name.replace("-", " ")), match_tokens(description))


def pick_skill(
    title: str, skills: Sequence[SkillWords], used: Collection[str]
) -> tuple[str, int] | None:
    """The one of `skills` that best fits a chat's `title`, as (name, score); None below
    `MIN_SCORE`. Tokenizes only `title` -- each skill's own tokens are `skill_words`'
    work, already done off the event loop. Of the title's `match_tokens`, each one in a
    skill's name scores 2 and each other one in its description 1. A skill named in `used` is
    skipped, but scored first all the same: if it still scores `MIN_SCORE` or more for this
    title, the chat already has a fitting skill, and this returns None for every skill, not
    only that one, rather than fall back to the next best -- "at most once a chat".
    Otherwise the highest score wins, then the most name words, then the name that sorts first,
    so the order of `skills` does not matter."""
    wanted = match_tokens(title)
    if not wanted:
        return None
    scored: list[tuple[int, int, str]] = []
    for skill in skills:
        named = wanted & skill.name_tokens
        described = (wanted - named) & skill.description_tokens
        score = 2 * len(named) + len(described)
        if skill.name in used:
            if score >= MIN_SCORE:
                return None
            continue
        scored.append((score, len(named), skill.name))
    if not scored:
        return None
    score, _named, name = min(scored, key=lambda entry: (-entry[0], -entry[1], entry[2]))
    return (name, score) if score >= MIN_SCORE else None


def skill_suggest(obs: Observation) -> contracts.Candidate | None:
    """One candidate for the vault chat in view (`obs.page.suggest`) when `pick_skill` finds one
    of the vault's own skills (`obs.skills`) for its title, among those not used there yet.
    Vault scopes only, like `skill_offer`. The chat's title and its used names go no further:
    `title` is the fixed "A skill for this chat", `words` and `evidence` are empty, and the
    matched name goes only into `facts["skill"]`, read by `templates.py` for the item's chip and
    its "/<name>" draft into the chat `facts["session"]`. The key is the chat's alone, so a chat
    has one item: Useful, Not useful or Stop these close it (`service._CLOSES`; Use this skill
    alone does not), and `pick_skill` offers no other there once one of the chat's own
    skills already fits its title and is in `used`. None without a pick."""
    if obs.kind != "vault" or obs.page is None or obs.page.suggest is None:
        return None
    hint = obs.page.suggest
    pick = pick_skill(hint.title, obs.skills, hint.used)
    if pick is None:
        return None
    name, score = pick
    return contracts.Candidate(
        item_key=contracts.item_key("skill_suggest", hint.session),
        type="skill_suggest",
        title=_SKILL_SUGGEST_TITLE,
        facts={"session": hint.session, "skill": name},
        words={},
        evidence=(),
        related_chat=hint.session,
        related_ref=None,
        deadline=None,
        shown_elsewhere=(),
        rules_evidence={"score": score},
        origin_session=None,
        work_allowed=(),
    )


# ---- per-request: vault changes (`vault_watch.read_changes`) ----------------------------------


def note_link(obs: Observation) -> list[contracts.Candidate]:
    """One candidate per `obs.changes.links` pair: evidence is (A's
    mention line, A's tags) then (B's opening, B's tags), built only from the `NoteFacts`
    already read -- never a fresh read of either note. `related_ref` is A (`pair.source.ref`),
    so a never-send rule on the mentioning note itself still holds the pair back;
    `rules_evidence["pairs"]` is always 1, since this candidate exists only when `vault_watch`
    already found one. No `obs.changes` (outside a vault cycle, or one that could not read it)
    yields no candidates."""
    if obs.changes is None:
        return []
    candidates: list[contracts.Candidate] = []
    for pair in obs.changes.links:
        a, b = pair.source, pair.target
        evidence = (
            contracts.Evidence(a.ref, pair.mention, False, a.tags),
            contracts.Evidence(b.ref, b.opening, False, b.tags),
        )
        title = "Notes to link"
        candidates.append(
            contracts.Candidate(
                item_key=contracts.item_key("note_link", a.ref, b.ref),
                type="note_link",
                title=title,
                facts={"workspace_id": obs.scope},
                words={},
                evidence=evidence,
                related_chat=None,
                related_ref=a.ref,
                deadline=None,
                shown_elsewhere=(),
                rules_evidence={"pairs": 1},
                origin_session=None,
                work_allowed=(),
            )
        )
    return candidates


def note_superseded(obs: Observation) -> list[contracts.Candidate]:
    """One candidate per `obs.changes.superseded` pair: evidence is O (the older note)'s own
    opening and tags, then A (the newer note)'s -- again only the `NoteFacts` already read.
    `related_ref` is O, so a never-send rule on the older note still holds the pair back."""
    if obs.changes is None:
        return []
    candidates: list[contracts.Candidate] = []
    for pair in obs.changes.superseded:
        older, newer = pair.older, pair.newer
        evidence = (
            contracts.Evidence(older.ref, older.opening, False, older.tags),
            contracts.Evidence(newer.ref, newer.opening, False, newer.tags),
        )
        title = "Superseded note"
        candidates.append(
            contracts.Candidate(
                item_key=contracts.item_key("note_superseded", older.ref, newer.ref),
                type="note_superseded",
                title=title,
                facts={"workspace_id": obs.scope},
                words={},
                evidence=evidence,
                related_chat=None,
                related_ref=older.ref,
                deadline=None,
                shown_elsewhere=(),
                rules_evidence={"pairs": 1},
                origin_session=None,
                work_allowed=(),
            )
        )
    return candidates


def tasks_ticked(obs: Observation) -> list[contracts.Candidate]:
    """One candidate per ticked-tasks file that already has a usable archive target
    (`obs.changes.archive`; `vault_watch`'s own `MIN_TICKED` and archive-folder gate already
    filtered the rest). Evidence carries the file's own ref and tags, never an excerpt -- the
    ticked lines' text never travels past this module. `related_ref` is that same ref; the
    archive target itself is read back later, from `obs.changes.archive`, only once the item is
    actually filed."""
    if obs.changes is None or obs.vault is None:
        return []
    candidates: list[contracts.Candidate] = []
    for tf in obs.vault.tasks.ticked:
        if tf.ref not in obs.changes.archive:
            continue
        title = "Ticked tasks"
        candidates.append(
            contracts.Candidate(
                item_key=contracts.item_key("tasks_ticked", tf.ref, tf.digest),
                type="tasks_ticked",
                title=title,
                facts={"workspace_id": obs.scope},
                words={},
                evidence=(contracts.Evidence(tf.ref, "", False, tf.tags),),
                related_chat=None,
                related_ref=tf.ref,
                deadline=None,
                shown_elsewhere=(),
                rules_evidence={"ticked": tf.count},
                origin_session=None,
                work_allowed=(),
            )
        )
    return candidates


# ---- beat-cached: a window of past rows --------------------------------------------------------


def _reset_stamp(rows: Sequence[LearningRow], candidate_type: str) -> datetime | None:
    """The newest `acted_at`, or `feedback_at` of a useful, wrong or mute label, among *rows* of
    `candidate_type`: once the user acts on, or closes with a label, an earlier item of that
    same self-improvement type, the next count starts fresh after it rather than recount the
    labels that already led to one. Not now ("later") only defers that same item, and a held-back
    label closes none, so their `feedback_at` moves neither the count nor the key. None when
    *rows* holds no such row yet."""
    newest: datetime | None = None
    for row in rows:
        if row.candidate != candidate_type:
            continue
        closed = row.feedback_at if row.feedback in _CLOSING else None
        for at in (row.acted_at, closed):
            if at is not None and (newest is None or at > newest):
                newest = at
    return newest


def _anchor(reset: datetime | None, learn_since: datetime | None) -> str:
    """The item key's anchor for retro_due and question_tune: the type's own *reset*
    (`_reset_stamp`), else the scope's *learn_since*, else the fixed "start". After Reset
    learning *rows* begin at *learn_since*, so they no longer hold the earlier item the user
    closed, and falling back to "start" could take that closed item's key, which
    `agenda.upsert` keeps closed. Delete all deletes the agenda, so no closed item keeps a key
    past it."""
    found = reset if reset is not None else learn_since
    return "start" if found is None else stamp(found)


def _retro_due(
    rows: Sequence[LearningRow], scope: str, now: datetime, learn_since: datetime | None
) -> contracts.Candidate | None:
    """retro_due: `rules.RETRO_LABELS` or more wrong-or-mute-labelled rows, on topics other
    than the four self-improvement types, created at or after the later of `_RETRO_WINDOW` back
    from *now* and the last time the user acted on an earlier retro_due or closed it with a
    label (`_reset_stamp`; so the same pile of labels never drives two suggestions). No evidence:
    the judgement is about a window of history, not any one note or chat.

    The item key anchors on that reset, never on *now*: the counting window's own start
    (`since`) slides forward with *now* whenever there is no earlier retro_due, or the one there
    is has already aged out of `_RETRO_WINDOW`, and a key built from it would then change every
    beat -- the user would see a fresh "A retro" item in place of the one just shown, and could
    be asked again, though nothing the user did caused it. A key must not move between beats on
    its own; with no earlier retro_due the key anchors on *learn_since*, else on the fixed
    "start" (`_anchor`), and a new key forms only once the user has acted on, or closed with a
    label, that earlier item, or has reset learning."""
    floor = now - _RETRO_WINDOW
    reset = _reset_stamp(rows, "retro_due")
    since = max(floor, reset) if reset is not None else floor
    n = sum(
        1
        for row in rows
        if row.candidate not in contracts.IMPROVE
        and row.feedback in _WRONG_OR_MUTE
        and row.created_at >= since
    )
    if n < rules.RETRO_LABELS:
        return None
    anchor = _anchor(reset, learn_since)
    title = "A retro"
    return contracts.Candidate(
        item_key=contracts.item_key("retro_due", scope, anchor),
        type="retro_due",
        title=title,
        facts={},
        words={},
        evidence=(),
        related_chat=None,
        related_ref=None,
        deadline=None,
        shown_elsewhere=(),
        rules_evidence={"labels": n},
        origin_session=None,
        work_allowed=(),
    )


def _drifting_feature(pool: Sequence[LearningRow]) -> str | None:
    """Branch (i): the `arbiter.FEATURES` question whose yes/no usefulness rates (`calibration.
    questions`) sit closest together -- both sides need at least `_TUNE_MIN_SIDE` labelled rows
    and the gap under `_TUNE_DRIFT_GAP` -- since a question that no longer separates useful
    decisions from not-useful ones has stopped tracking the user's own judgement.
    `arbiter.FEATURES`' own order breaks a tie for the smallest gap. None with nothing that
    qualifies."""
    order = {name: index for index, name in enumerate(arbiter.FEATURES)}
    gaps: list[tuple[str, float]] = []
    for rate in calibration.questions(pool):
        if rate.name not in order or rate.yes_rate is None or rate.no_rate is None:
            continue
        if rate.yes_n < _TUNE_MIN_SIDE or rate.no_n < _TUNE_MIN_SIDE:
            continue
        gap = abs(rate.yes_rate - rate.no_rate)
        if gap < _TUNE_DRIFT_GAP:
            gaps.append((rate.name, gap))
    if not gaps:
        return None
    gaps.sort(key=lambda pair: (pair[1], order[pair[0]]))
    return gaps[0][0]


def _highest_mean_p(rows: Sequence[LearningRow]) -> str | None:
    """Each `arbiter.FEATURES` question's mean `p` over *rows* whose answers decode and carry a
    yes/no answer for it, highest first, `arbiter.FEATURES` order breaking a tie. None when no
    row decodes a usable answer for any of them."""
    order = {name: index for index, name in enumerate(arbiter.FEATURES)}
    means: list[tuple[str, float]] = []
    for name in arbiter.FEATURES:
        values: list[float] = []
        for row in rows:
            if row.answers_json is None:
                continue
            try:
                answers, _ = decode_answers(row.answers_json)
            except ValueError:
                continue
            answer = answers.get(name)
            if isinstance(answer, contracts.NoulAnswer):
                values.append(answer.p)
        if values:
            means.append((name, sum(values) / len(values)))
    if not means:
        return None
    means.sort(key=lambda pair: (-pair[1], order[pair[0]]))
    return means[0][0]


def _worst_topic_feature(pool: Sequence[LearningRow]) -> str | None:
    """Branch (ii), over *pool* (already the non-IMPROVE rows the window allows): once one topic
    reaches `_TUNE_WRONG_LABELS` wrong labels, the `arbiter.FEATURES` question with the highest
    mean `p` (`_highest_mean_p`) on that topic's own wrong-labelled rows -- the question Decision
    was most confident about on the calls that turned out wrong. The topic with the most wrong
    labels wins; `contracts.CANDIDATES` order breaks a tie there. None with no topic that
    qualifies."""
    by_topic: dict[str, list[LearningRow]] = {}
    for row in pool:
        if row.feedback == "wrong":
            by_topic.setdefault(row.candidate, []).append(row)
    topics = [topic for topic, wrong in by_topic.items() if len(wrong) >= _TUNE_WRONG_LABELS]
    if not topics:
        return None
    topic_order = {name: index for index, name in enumerate(contracts.CANDIDATES)}
    topics.sort(key=lambda topic: (-len(by_topic[topic]), topic_order[topic]))
    return _highest_mean_p(by_topic[topics[0]])


def _question_tune(
    rows: Sequence[LearningRow],
    scope: str,
    wording: Mapping[str, object],
    now: datetime,
    learn_since: datetime | None,
) -> contracts.Candidate | None:
    """question_tune: non-IMPROVE rows created at or after the last time the user acted on
    an earlier question_tune or closed it with a label (`_reset_stamp`; the engine's own row read
    already starts no earlier than its own `learn_since`, so with no earlier question_tune this
    needs no further bound -- *rows* itself is the window). Branch (i) first, else (ii); neither
    found leaves Decision's questions alone this beat. `facts["wording_json"]` is the chosen
    question's *current* wording -- `wording`'s own entry when one is in use, else the
    playbook's.

    The item key anchors on that same reset, never on the pool's oldest row: the oldest row's
    own stamp moves whenever the engine's row cap or retention drops it, and a key built from it
    would then change for the same drifting question though nothing the user did caused it. A
    key must not move between beats on its own; with no earlier question_tune the key anchors on
    *learn_since*, else on the fixed "start" (`_anchor`), and a new key forms only once the user
    has acted on, or closed with a label, that earlier item, or has reset learning."""
    since = _reset_stamp(rows, "question_tune")
    pool = [
        row
        for row in rows
        if row.candidate not in contracts.IMPROVE and (since is None or row.created_at >= since)
    ]
    name = _drifting_feature(pool) or _worst_topic_feature(pool)
    if name is None:
        return None
    current = playbook.questions_for("question_tune", wording)[name]
    wording_json = contracts.canonical_json(current)
    anchor = _anchor(since, learn_since)
    title = "Decision's questions"
    return contracts.Candidate(
        item_key=contracts.item_key("question_tune", scope, name, anchor),
        type="question_tune",
        title=title,
        facts={"name": name, "wording_json": wording_json},
        words={},
        evidence=(),
        related_chat=None,
        related_ref=None,
        deadline=None,
        shown_elsewhere=(),
        rules_evidence={"name": name},
        origin_session=None,
        work_allowed=(),
    )


def from_rows(
    rows: Sequence[LearningRow],
    *,
    scope: str,
    wording: Mapping[str, object],
    trial_open: bool,
    now: datetime,
    learn_since: datetime | None = None,
) -> list[contracts.Candidate]:
    """The two beat-cached self-improvement types, in `contracts.CANDIDATES` order: retro_due,
    then question_tune -- skipped entirely while *trial_open*, since trying a proposed
    wording is itself the user's answer to "does this question need work". Pure: *rows*, *now*
    and *learn_since* (the scope's, which the engine reads *rows* from; None while unset) are its
    only moving parts; the engine reads *rows* once per beat and reuses them for
    `learning.relearn`, so this never opens a second connection."""
    candidates: list[contracts.Candidate] = []
    retro = _retro_due(rows, scope, now, learn_since)
    if retro is not None:
        candidates.append(retro)
    if not trial_open:
        tune = _question_tune(rows, scope, wording, now, learn_since)
        if tune is not None:
            candidates.append(tune)
    return candidates
