/**
 * The settings module's error codes and its 5 field codes. `stale_revision` and
 * `command_conflict` are also used by workspaces and data routes; they live here once since a
 * catalog key is global.
 */

import { defineCatalog } from "./defineCatalog";

export const catalog = defineCatalog({
  en: {
    "error.invalid_settings": "Check the highlighted settings.",
    "error.settings_too_large": "The settings document is too large ({current_bytes} bytes).",
    "error.read_only": "Settings are read-only; see the recovery guidance.",
    "error.stale_revision": "Changed elsewhere; review the current values.",
    "error.command_conflict": "This request was already processed with different content.",

    "field.unknown_key": "This setting is not known to this version.",
    "field.not_allowed": "Not one of the allowed values.",
    "field.too_long": "Outside the allowed length.",
    "field.invalid_type": "Wrong kind of value.",
    "field.control_characters": "Control characters are not allowed.",
    "field.avatar_missing":
      "The avatar image is no longer available. Choose another image or reset it.",
  },
});
