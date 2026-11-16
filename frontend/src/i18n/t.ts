/**
 * The one place that turns a catalog key into text. VIKI Agent's screens are in English only.
 * Plain data and functions: no React, no I/O, nothing logged.
 */

import type { ShellApiError } from "../clients/app/shellApi";
import { en, type Key } from "./index";

/** The key itself is the visible, predictable fallback: only reachable when a `Key`-typed call
 * is bypassed (never in typed code, since `Key` is the union of every merged catalog key). */
export function t(key: Key): string {
  return en[key] ?? key;
}

/** Substitutes `{name}` placeholders in the catalog string with `String(vars[name])`. The
 * result is still a plain text node: callers never interpolate raw HTML, and neither does this
 * function. */
export function tf(key: Key, vars: Readonly<Record<string, unknown>>): string {
  return t(key).replace(/\{(\w+)\}/g, (whole, name: string) =>
    name in vars ? String(vars[name]) : whole,
  );
}

/**
 * `tf("error.<code>", error.extra)` when the catalog has a row for that code; otherwise the
 * generic fallback with the code substituted in. `error.extra` is always a frozen object
 * (`ShellApiError`'s constructor, `src/clients/app/shellApi.ts`), so it is passed straight
 * through with no guard: a row with no `{name}` placeholder ignores it, and a row that has one —
 * a 413 `settings_too_large`'s `{current_bytes}`, a 409 `acknowledgement_required`'s
 * `{required}` — is filled in instead of showing the user literal braces. Returns plain text — a
 * view that wants the code visually set apart (a `mono` span) wraps `error.code` itself around
 * this string's tail; this module has no JSX to do that with.
 */
export function errorText(error: ShellApiError): string {
  const key = `error.${error.code}` as Key;
  return key in en ? tf(key, error.extra) : tf("error.unknown" as Key, { code: error.code });
}
