---
name: lint
description: Vault health check — dead links, orphans, schema violations, duplicates, stale facts, inbox age — written to system/metrics.md. Use when the owner says "lint", "vault health", "check the vault", or before a weekly review.
---

# /lint

Measure, report, and fix only what's mechanical. Judgment calls become proposals.

Scope for ALL checks below: content notes only — exclude `_templates/`, `.claude/`, `_raw/`, `00-inbox/`, `system/`, and per-project `tasks.md` from link extraction, the basename inventory, and schema validation (these are the schema-exempt classes in `AGENTS.md`, plus `.claude/` and `system/`; inbox has its own age metric instead). Ignore wikilink targets containing `<`, `>`, or `...` — they are placeholders or documentation examples.

1. **Collect metrics:**
   - Note counts per top-level folder; total vault size
   - **Dead links:** `[[targets]]` with no matching basename (`rg -o '\[\[[^\]|#]+' -r '$0'` vs `Glob **/*.md` basenames). Distinguish *breadcrumbs* (intentional, listed under a MOC's Open questions) from *breaks* (typo'd or renamed targets).
   - **Orphans:** notes in `30-knowledge/`/`40-sources/` with no incoming links and no `up:` hub
   - **Schema violations:** missing/invalid `type:`, `status:` (per the type-specific vocabularies in AGENTS.md), `created:`, unquoted wikilinks in YAML, `type: source` without `source:`, duplicate basenames among linkable notes
   - **Duplicate candidates:** near-identical titles or heavily overlapping content
   - **Staleness:** "as of" dates older than 6 months; `00-inbox/` item count + oldest age; `status: draft` notes older than a month (except `type: thinking` — unfinished thinking notes may stay draft indefinitely)
2. **Auto-fix (mechanical only):** obvious wikilink typos where exactly one basename is a clear match, missing `modified:` fields, unquoted YAML wikilinks. One commit for lint fixes when the vault is a Git repository.
3. **Propose (never auto-apply):** merges for duplicates, `up:` assignments for orphans, archiving for stale drafts.
4. **Write** a dated snapshot to the top of `system/metrics.md` (keep prior snapshots — the trend is the point), append to `system/log.md`, and give the owner a 5-line summary with the worst finding first.
