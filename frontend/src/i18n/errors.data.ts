/**
 * The data module's error codes: the export/import/reset codes, the reset-reason rows
 * `resetReasonText` falls back to, and the one import-unsupported reason.
 */

import { defineCatalog } from "./defineCatalog";

export const catalog = defineCatalog({
  en: {
    "error.invalid_kind": "Unknown export kind.",
    "error.invalid_include": "Unknown export option.",
    "error.denied_destination":
      "Exports cannot be written inside a vault, the app data folder, ~/.claude or ~/.codex.",
    "error.preview_expired": "The preview expired; run it again.",
    "error.exists": "A file already exists at that path.",
    "error.invalid_format": "Not a VIKI settings export.",
    "error.unsupported_format": "This export was written by a newer version.",
    "error.invalid_selection": 'Only rows marked "apply" can be selected.',
    "error.invalid_id": "Unknown preview.",
    "error.invalid_confirmation": "Type RESET exactly.",
    "error.invalid_categories": "Unknown reset category.",

    "error.reset.in_use": "In use while VIKI runs; skipped.",
    "error.reset.read_only": "Settings are read-only; skipped.",
    "error.reset.unknown_category": "Unknown category; skipped.",
    "error.reset.generic": "Skipped: {code}.",

    "error.import.newer_format": "Written by a newer version; cannot be applied.",

    "error.backup.denied_destination":
      "Backups cannot go inside a vault, the app data folder, ~/.claude or ~/.codex.",
    "error.backup.invalid_path":
      "Enter the full path of an existing folder. Relative, device and network paths are not accepted.",
    "error.backup.too_large": "The vault is too large to back up.",
    "error.backup.invalid_name": "That folder name cannot be used.",
    "error.backup.invalid_backup": "That folder is not a usable VIKI backup.",
    "error.backup.recovery_required":
      "A vault has an unfinished save or note operation, so a restore cannot register the copy yet. Resolve it in that vault's Vault view, then restore again.",
  },
});
