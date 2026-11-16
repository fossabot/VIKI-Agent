/**
 * `errorText`'s generic dispatcher resolves `error.not_found` to the workspaces meaning ("the
 * folder does not exist") because that is the only `not_found` row the shared catalog carries.
 * The data envelope's own 404 `not_found` (`src/viki_agent/app/data_api.py`'s unmatched-path fallback, "Operation
 * not found.") is a different failure and must never borrow that sentence (see
 * `errors.workspaces.ts`'s header comment). Every panel in this folder renders a
 * `ShellApiError` through this function instead of `errorText` directly, so a `not_found` from
 * one of *our* routes falls through to the same generic "could not complete" fallback `errorText`
 * already uses for any code with no row of its own.
 */

import type { ShellApiError } from "../../clients/app/shellApi";
import { errorText, tf } from "../../i18n/t";

export function dataErrorText(error: ShellApiError): string {
  if (error.code === "not_found") return tf("error.unknown", { code: error.code });
  return errorText(error);
}
