/**
 * `dataErrorText`: the data
 * envelope's own 404 `not_found` must never borrow `error.not_found`'s workspaces meaning ("the
 * folder does not exist" — `errors.workspaces.ts`), because that sentence would be actively
 * wrong for a Data-view refusal.
 */
import { expect, test } from "vitest";
import { ShellApiError } from "../../clients/app/shellApi";
import { errorText } from "../../i18n/t";
import { dataErrorText } from "./dataErrorText";

test("a data-route not_found never shows the workspaces sentence", () => {
  const error = new ShellApiError(404, "not_found");
  // Proves the two functions really differ on this code, not just that dataErrorText picked
  // some other sentence.
  expect(errorText(error)).toBe("The folder does not exist.");
  expect(dataErrorText(error)).toBe("Could not complete: not_found");
  expect(dataErrorText(error)).not.toBe(errorText(error));
});

test("every other code renders exactly as errorText renders it", () => {
  const error = new ShellApiError(503, "unavailable");
  expect(dataErrorText(error)).toBe(errorText(error));
});
