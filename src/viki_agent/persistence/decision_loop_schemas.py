"""App-owned decision-loop state: episodes, preferences, items, model calls, missions and the key,
and from step 25 the wordings proposed for Decision's questions.

Stamps are 24 characters, ids 36 and hashes 64 lower-case hex. Every vocabulary is spelled out
as a literal, so the DDL a store validates against changes only with a schema step. A candidate
is bounded by its shape alone: a lower-case first letter and 1 to 32 characters.
"""

from __future__ import annotations

DECISION_LOOP_SCHEMA_STEP = 24

# The primary keys alone keep (scope, created_at, episode_id) and (scope, created_at, call_id)
# unique; those UNIQUEs are there for their indexes, which let a read walk one scope newest first
# without sorting it.
DECISION_LOOP_SCHEMAS = {
    "decision_episodes": """CREATE TABLE decision_episodes (
    episode_id TEXT PRIMARY KEY NOT NULL CHECK(length(episode_id) = 36),
    scope TEXT NOT NULL CHECK(length(scope) = 36),
    entry TEXT NOT NULL CHECK(entry IN ('decision','report')),
    created_at TEXT NOT NULL CHECK(length(created_at) = 24),
    day TEXT NOT NULL CHECK(length(day) = 10),
    trigger TEXT NOT NULL CHECK(trigger IN
        ('app_open','new_chat','review','change','launcher','context','heartbeat','mission')),
    candidate TEXT NOT NULL CHECK(length(candidate) BETWEEN 1 AND 32
        AND candidate GLOB '[a-z]*'),
    item_key TEXT CHECK(item_key IS NULL
        OR (length(item_key) = 64 AND item_key NOT GLOB '*[^0-9a-f]*')),
    state_hash TEXT CHECK(state_hash IS NULL
        OR (length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*')),
    evidence TEXT NOT NULL CHECK(length(evidence) BETWEEN 2 AND 2048),
    evidence_key TEXT NOT NULL CHECK(length(evidence_key) = 64
        AND evidence_key NOT GLOB '*[^0-9a-f]*'),
    policy TEXT NOT NULL CHECK(policy IN ('rules','model')),
    policy_version TEXT NOT NULL CHECK(length(policy_version) BETWEEN 1 AND 32),
    model TEXT CHECK(model IS NULL OR length(model) <= 32),
    answers TEXT CHECK(answers IS NULL OR length(answers) BETWEEN 2 AND 2048),
    terms TEXT CHECK(terms IS NULL OR length(terms) BETWEEN 2 AND 512),
    kind TEXT CHECK(kind IS NULL OR kind IN ('inform','ask','suggest','ready','report')),
    work TEXT CHECK(work IS NULL OR work IN ('none','prepare','run','coordinate')),
    level TEXT CHECK(level IS NULL OR level IN ('silent','list','badge','nudge','alert')),
    timing TEXT CHECK(timing IS NULL OR timing IN ('now','at','after_focus','in_context')),
    defer_until TEXT CHECK(defer_until IS NULL OR length(defer_until) = 24),
    reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 64),
    rules_level TEXT CHECK(rules_level IS NULL
        OR rules_level IN ('silent','list','badge','nudge','alert')),
    language TEXT CHECK(language IS NULL OR language IN ('en','ja')),
    latency_ms INTEGER CHECK(latency_ms IS NULL OR latency_ms BETWEEN 0 AND 60000),
    input_tokens INTEGER CHECK(input_tokens IS NULL OR input_tokens BETWEEN 0 AND 1000000),
    cost_micro_usd INTEGER CHECK(cost_micro_usd IS NULL
        OR cost_micro_usd BETWEEN 0 AND 1000000000),
    mission_id TEXT CHECK(mission_id IS NULL OR length(mission_id) = 36),
    exposures INTEGER NOT NULL CHECK(exposures BETWEEN 0 AND 1000000),
    first_exposed_at TEXT CHECK(first_exposed_at IS NULL OR length(first_exposed_at) = 24),
    last_exposed_at TEXT CHECK(last_exposed_at IS NULL OR length(last_exposed_at) = 24),
    opened_at TEXT CHECK(opened_at IS NULL OR length(opened_at) = 24),
    acted_at TEXT CHECK(acted_at IS NULL OR length(acted_at) = 24),
    reviewed_at TEXT CHECK(reviewed_at IS NULL OR length(reviewed_at) = 24),
    observed_until TEXT NOT NULL CHECK(length(observed_until) = 24),
    feedback TEXT CHECK(feedback IS NULL
        OR feedback IN ('useful','wrong','later','mute','missed','right_to_hold')),
    feedback_reason TEXT CHECK(feedback_reason IS NULL
        OR feedback_reason IN ('out_of_date','already_knew','wrong_topic','bad_timing')),
    feedback_source TEXT CHECK(feedback_source IS NULL
        OR feedback_source IN ('brief','rail','review','launcher','page')),
    feedback_revision INTEGER NOT NULL CHECK(feedback_revision BETWEEN 0 AND 1000000),
    feedback_at TEXT CHECK(feedback_at IS NULL OR length(feedback_at) = 24),
    dedupe_key TEXT NOT NULL CHECK(length(dedupe_key) = 64 AND dedupe_key NOT GLOB '*[^0-9a-f]*'),
    UNIQUE(scope, dedupe_key),
    UNIQUE(scope, created_at, episode_id),
    CHECK((entry = 'decision') = (level IS NOT NULL)),
    CHECK((level IS NULL) = (work IS NULL) AND (level IS NULL) = (timing IS NULL)),
    CHECK((timing IS 'at') = (defer_until IS NOT NULL)),
    CHECK((feedback IS NULL) = (feedback_at IS NULL)
        AND (feedback IS NULL) = (feedback_source IS NULL)),
    CHECK(feedback_reason IS NULL OR feedback IS NOT NULL)
) STRICT""",
    "decision_preferences": """CREATE TABLE decision_preferences (
    scope TEXT NOT NULL CHECK(length(scope) = 36),
    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 96),
    value TEXT NOT NULL CHECK(length(value) <= 8192),
    revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 1000000),
    updated_at TEXT NOT NULL CHECK(length(updated_at) = 24),
    PRIMARY KEY(scope, name)
) STRICT""",
    "decision_items": """CREATE TABLE decision_items (
    scope TEXT NOT NULL CHECK(length(scope) = 36),
    item_key TEXT NOT NULL CHECK(length(item_key) = 64 AND item_key NOT GLOB '*[^0-9a-f]*'),
    candidate TEXT NOT NULL CHECK(length(candidate) BETWEEN 1 AND 32
        AND candidate GLOB '[a-z]*'),
    state TEXT NOT NULL CHECK(state IN ('open','acted','dismissed','stale','done')),
    level TEXT NOT NULL CHECK(level IN ('silent','list','badge','nudge','alert')),
    timing TEXT NOT NULL CHECK(timing IN ('now','at','after_focus','in_context')),
    defer_until TEXT CHECK(defer_until IS NULL OR length(defer_until) = 24),
    episode_id TEXT NOT NULL CHECK(length(episode_id) = 36),
    state_hash TEXT CHECK(state_hash IS NULL
        OR (length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*')),
    answers TEXT CHECK(answers IS NULL OR length(answers) BETWEEN 2 AND 2048),
    body BLOB NOT NULL CHECK(length(body) BETWEEN 2 AND 65536),
    revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 1000000000),
    created_at TEXT NOT NULL CHECK(length(created_at) = 24),
    updated_at TEXT NOT NULL CHECK(length(updated_at) = 24),
    shown_at TEXT CHECK(shown_at IS NULL OR length(shown_at) = 24),
    opened_at TEXT CHECK(opened_at IS NULL OR length(opened_at) = 24),
    PRIMARY KEY(scope, item_key)
) STRICT""",
    "decision_calls": """CREATE TABLE decision_calls (
    call_id TEXT PRIMARY KEY NOT NULL CHECK(length(call_id) = 36),
    scope TEXT NOT NULL CHECK(length(scope) = 36),
    created_at TEXT NOT NULL CHECK(length(created_at) = 24),
    item_key TEXT NOT NULL CHECK(length(item_key) = 64 AND item_key NOT GLOB '*[^0-9a-f]*'),
    episode_id TEXT CHECK(episode_id IS NULL OR length(episode_id) = 36),
    session_id TEXT CHECK(session_id IS NULL OR length(session_id) = 36),
    model TEXT CHECK(model IS NULL OR length(model) <= 32),
    outcome TEXT NOT NULL CHECK(outcome IN ('answered','failed')),
    failure TEXT CHECK(failure IS NULL OR length(failure) <= 32),
    latency_ms INTEGER CHECK(latency_ms IS NULL OR latency_ms BETWEEN 0 AND 60000),
    input_tokens INTEGER CHECK(input_tokens IS NULL OR input_tokens BETWEEN 0 AND 1000000),
    cost_micro_usd INTEGER CHECK(cost_micro_usd IS NULL
        OR cost_micro_usd BETWEEN 0 AND 1000000000),
    body BLOB NOT NULL CHECK(length(body) BETWEEN 2 AND 262144),
    UNIQUE(scope, created_at, call_id)
) STRICT""",
    "decision_missions": """CREATE TABLE decision_missions (
    mission_id TEXT PRIMARY KEY NOT NULL CHECK(length(mission_id) = 36),
    scope TEXT NOT NULL CHECK(length(scope) = 36),
    session_id TEXT NOT NULL UNIQUE CHECK(length(session_id) = 36),
    item_key TEXT NOT NULL CHECK(length(item_key) = 64 AND item_key NOT GLOB '*[^0-9a-f]*'),
    work TEXT NOT NULL CHECK(work IN ('prepare','run')),
    state_hash TEXT CHECK(state_hash IS NULL
        OR (length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*')),
    state TEXT NOT NULL CHECK(state IN
        ('starting','running','done','failed','stopped','unknown')),
    reason TEXT NOT NULL CHECK(length(reason) <= 32),
    body BLOB NOT NULL CHECK(length(body) BETWEEN 2 AND 65536),
    created_at TEXT NOT NULL CHECK(length(created_at) = 24),
    updated_at TEXT NOT NULL CHECK(length(updated_at) = 24),
    deadline_at TEXT NOT NULL CHECK(length(deadline_at) = 24),
    finished_at TEXT CHECK(finished_at IS NULL OR length(finished_at) = 24),
    CHECK((state IN ('done','failed','stopped','unknown')) = (finished_at IS NOT NULL))
) STRICT""",
    "decision_key": """CREATE TABLE decision_key (
    slot TEXT PRIMARY KEY NOT NULL CHECK(slot = 'typesafe'),
    format INTEGER NOT NULL CHECK(format = 1),
    body BLOB NOT NULL CHECK(length(body) BETWEEN 2 AND 8192),
    updated_at TEXT NOT NULL CHECK(length(updated_at) = 24)
) STRICT""",
}

DECISION_PROPOSALS_SCHEMA_STEP = 25

# Step 25: one row per proposal. The primary key alone keeps (scope, created_at, proposal_id)
# unique; that UNIQUE is there for its index, as above. A row may carry both stamps (a rejection
# after an approval); a reason comes only with a rejection.
DECISION_PROPOSAL_SCHEMAS = {
    "decision_proposals": """CREATE TABLE decision_proposals (
    proposal_id TEXT PRIMARY KEY NOT NULL CHECK(length(proposal_id) = 36),
    scope TEXT NOT NULL CHECK(length(scope) = 36),
    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 32 AND name NOT GLOB '*[^a-z_]*'),
    proposed_sha256 TEXT NOT NULL CHECK(length(proposed_sha256) = 64
        AND proposed_sha256 NOT GLOB '*[^0-9a-f]*'),
    body BLOB NOT NULL CHECK(length(body) BETWEEN 2 AND 8192),
    created_at TEXT NOT NULL CHECK(length(created_at) = 24),
    updated_at TEXT NOT NULL CHECK(length(updated_at) = 24),
    approved_at TEXT CHECK(approved_at IS NULL OR length(approved_at) = 24),
    rejected_at TEXT CHECK(rejected_at IS NULL OR length(rejected_at) = 24),
    reason TEXT CHECK(reason IS NULL OR length(reason) BETWEEN 1 AND 200),
    UNIQUE(scope, created_at, proposal_id),
    CHECK(reason IS NULL OR rejected_at IS NOT NULL)
) STRICT""",
}
