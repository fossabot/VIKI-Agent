/**
 * The server's request-level and storage error codes, plus the client-side codes
 * `src/clients/app/shellApi.ts` itself produces (`timeout`, `network_error`, `invalid_response`,
 * `http_error`) and the `error.unknown` fallback `errorText` uses for any code with no row
 * anywhere. Codes that more than one server module can send (`invalid_input`, `unavailable`,
 * `unsafe_path`) live here once, since a catalog key is global — `errorText` looks it up by
 * code alone, not by which route produced it.
 *
 * `invalid_legacy` and `invalid_preferences` are two more `StateError` codes from `storage.py`;
 * either can be the recovery refusal `RecoveryView` explains. Own wording, not copied from
 * `storage.py`'s `_MESSAGES`, matching every other row in this file.
 */

import { defineCatalog } from "./defineCatalog";

export const catalog = defineCatalog({
  en: {
    "error.unauthorized": "Not signed in to this window. Reopen VIKI Agent.",
    "error.forbidden_origin": "This window may not perform the operation.",
    "error.invalid_representation": "The server could not read the request.",
    "error.invalid_body": "The request was malformed.",
    "error.too_large": "The request is too large.",
    "error.intake_timeout": "The request did not arrive in time.",
    "error.disconnected": "The request was interrupted.",
    "error.overloaded": "Busy; try again in a moment.",
    "error.invalid_input": "The app store did not accept a value.",

    "error.busy": "The app store is busy.",
    "error.unavailable": "The operation could not be completed.",
    "error.corrupt": "The app store is damaged.",
    "error.recovery_required": "The app store needs recovery.",
    "error.incompatible": "The app store was written by another version.",
    "error.unsafe_path": "The path contains a link that cannot be trusted.",
    "error.insecure_permissions": "The app data folder's permissions are too open.",
    "error.invalid_legacy": "The app's previous saved state could not be read.",
    "error.invalid_preferences":
      "Saved session preferences contain a value that could not be used.",

    "error.timeout": "No answer in time.",
    "error.network_error": "The server could not be reached.",
    "error.invalid_response": "The server's answer had an unexpected shape.",
    "error.http_error": "The server refused the request.",

    "error.unknown": "Could not complete: {code}",
  },
});
