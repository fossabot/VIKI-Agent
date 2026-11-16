import type { CommandState } from "../../commands";
import { t } from "../../i18n/t";

/** The reason a disabled control is off, in the visually hidden span its `aria-describedby`
 * names and again in `title` (the pattern `PolicyPanel`'s target buttons
 * already use). `null` when the control is enabled — nothing to describe. */
export function ReasonHint({ id, state }: { id: string; state: CommandState }) {
  if (state.enabled || state.reason === null) return null;
  return (
    <span id={id} className="sr-only">
      {t(state.reason)}
    </span>
  );
}

export function describedBy(id: string, state: CommandState): string | undefined {
  return state.enabled || state.reason === null ? undefined : id;
}

export function reasonTitle(state: CommandState): string | undefined {
  return state.enabled || state.reason === null ? undefined : t(state.reason);
}
