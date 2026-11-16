import type { ReactNode } from "react";
import "./StatusBar.css";

export interface VaultCounts {
  knowledge: number;
  sources: number;
  inbox: number;
}

export interface StatusBarProps {
  vaultCounts?: VaultCounts;
  /** The git chip (components/StatusBar/GitChip.tsx), rendered first. */
  git?: ReactNode;
  /** 0-100. Renders the 64x3 usage bar from the design. */
  contextPercent?: number;
  notice?: string;
  /** The plan-limit meter (`components/StatusBar/PlanLimitMeter.tsx`), after the notice. */
  planLimit?: ReactNode;
  /** The context meter + popover. Falls back to the static context span below when absent. */
  contextMeter?: ReactNode;
}

/** The bottom status bar. Every field is optional; anything absent is simply omitted
 * (no placeholder dashes) rather than guessed at. */
export function StatusBar({
  vaultCounts,
  git,
  contextPercent,
  notice,
  planLimit,
  contextMeter,
}: StatusBarProps) {
  return (
    <footer className="statusbar mono" data-region="statusbar">
      {git}
      {vaultCounts ? (
        <span>
          {vaultCounts.knowledge} knowledge · {vaultCounts.sources} sources · inbox{" "}
          {vaultCounts.inbox}
        </span>
      ) : null}
      {notice ? <span>{notice}</span> : null}
      {planLimit}
      {contextMeter ??
        (contextPercent !== undefined ? (
          <span className="statusbar-context" title="Estimated from the last reply's token usage">
            context {contextPercent}%
            <span className="statusbar-context-track">
              <span
                className="statusbar-context-fill"
                style={{ width: `${(contextPercent / 100) * 64}px` }}
              />
            </span>
          </span>
        ) : null)}
    </footer>
  );
}
