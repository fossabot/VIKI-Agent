/**
 * One reading of `auto_compact`, shared by ContextMeter and ContextView so a single
 * report never shows two different ways: `{enabled: false, threshold: 167000}` could read
 * "on" in the popover and "off" on the page, and `{enabled: true, threshold: null}` the
 * reverse. Both views classify through here instead of reading `enabled`/`threshold` themselves.
 * Also holds NOT_OBSERVED_TEXT and `failedReportText`: `observed_at: null` alone
 * is not always the respawn case — a first `get_context_usage` read that itself timed out or
 * errored (driver.py's `_context_percent`) answers `failed_report("timeout"/"unavailable")`,
 * same null shape, different reason. `failedReportText` reads the one reason every part shares
 * to pick the sentence both views show in place of the figures.
 */
import type {
  ContextPart,
  ContextReason,
  ContextReport,
} from "../../clients/conversations/contextApi";
import { REASON_TEXT } from "./ContextItems";

type AutoCompact = ContextReport["auto_compact"];

export type AutoCompactRead =
  | { status: "on"; threshold: number }
  | { status: "off" }
  | { status: "not_reported" };

/** "On" (mark shown, "Auto-compact at N tokens") needs both `enabled` and a known threshold.
 * `enabled: false` always reads "off", whatever `threshold` holds. Anything else — a null
 * `auto_compact`, or `enabled: true` with no threshold — is not safe to call "off"; it is simply
 * not reported. */
export function readAutoCompact(autoCompact: AutoCompact): AutoCompactRead {
  if (autoCompact === null) return { status: "not_reported" };
  if (!autoCompact.enabled) return { status: "off" };
  if (autoCompact.threshold === null) return { status: "not_reported" };
  return { status: "on", threshold: autoCompact.threshold };
}

/** The exact strings both views show for the two non-"on" states, so neither can drift from the
 * other by a punctuation mark. "Claude Code did not report this." matches ContextItems'
 * own wording for the same concept elsewhere on the page. */
export const AUTO_COMPACT_OFF_TEXT = "Auto-compact is off";
export const AUTO_COMPACT_NOT_REPORTED_TEXT = "Claude Code did not report this.";

/** A respawned process's report before its first reply ends (`observed_at: null`, every part
 * `not_observed`): matches ContextItems' own wording for that reason, so the popover and
 * the page read it the same way too, instead of each falling back to its own zero or dash. */
export const NOT_OBSERVED_TEXT = "No reply has ended yet in this Claude Code process.";

const PARTS: readonly ContextPart[] = [
  "categories",
  "memory_files",
  "mcp_servers",
  "skills",
  "agents",
];

/** `failed_report` (context_report.py) sets every part in REPORT_PARTS to the identical `reason`,
 * so any one part says why the whole report has no figures; reading `categories` first is no
 * different from reading the rest. */
function sharedReason(report: ContextReport): ContextReason | null {
  for (const part of PARTS) {
    const reason = report.reasons[part];
    if (reason !== undefined) return reason;
  }
  return null;
}

/** The one sentence both views show in place of the figures when `observed_at` is null. Only
 * `not_observed` (a respawn before its first reply ends) reads as NOT_OBSERVED_TEXT; every other
 * reason — the first read itself timing out or erroring — reads as ContextItems' own
 * sentence for that reason instead, e.g. "The request timed out.", never NOT_OBSERVED_TEXT merely
 * because a reply did end. */
export function failedReportText(report: ContextReport): string {
  const reason = sharedReason(report);
  if (reason === null || reason === "not_observed") return NOT_OBSERVED_TEXT;
  return REASON_TEXT[reason];
}
