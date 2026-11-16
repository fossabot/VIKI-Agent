/**
 * The workspaces module's error codes, the 8 path/name field codes, and the 6
 * `field.unsupported_location.<reason>` rows the create/open form reads from a 422's
 * `extra.reason` (`mount_point_posix` is read only off Windows, by its own per-OS key rather
 * than the reason string). `invalid_path` and `unknown_workspace` are also used by data routes;
 * they live here once since a catalog key is global.
 *
 * `not_found` has two meanings on the server: this row (workspaces, 400, "the folder does not
 * exist" — the one the path field's fallback uses) and a second one (data routes, 404, "unknown
 * operation") that no view calls through `errorText`. A flat `error.<code>` catalog cannot hold
 * two values under one key, so only this row exists.
 */

import { defineCatalog } from "./defineCatalog";

export const catalog = defineCatalog({
  en: {
    "error.invalid_path": "Not a usable path.",
    "error.invalid_name": "Not a usable name.",
    "error.not_found": "The folder does not exist.",
    "error.not_directory": "The path is not a folder.",
    "error.invalid_transition": "That policy change is not allowed from the current policy.",
    "error.denied_location":
      "This location is not allowed for a vault (a temporary, system, cloud-synced or app folder).",
    "error.protected_overlap": "This folder lies inside, or contains, a protected vault.",
    "error.protected_root": "A protected vault's policy cannot be changed.",
    "error.unknown_workspace": "No such registration.",
    "error.destination_not_empty": "The destination already has content.",
    "error.overlaps_registration": "This folder overlaps a registered vault.",
    "error.case_collision": "A folder differing only in letter case is already registered.",
    "error.acknowledgement_required": "Confirm every item first: {required}.",
    "error.root_unavailable": "The folder is not where it was registered.",
    "error.not_app_created": "The folder holds files this app did not create; nothing was removed.",
    "error.unsupported_location": "This volume or mount cannot hold a vault.",
    "error.kind_mismatch": "This folder is already registered as the other kind (vault or code).",
    "error.trust_required": "Trust this folder first.",
    "error.workspace_active":
      "The active folder's trust can't change. Select another workspace and restart first.",

    "field.invalid_path": "Not a usable path.",
    "field.invalid_name": "Not a usable name (1–256 characters).",
    "field.denied_location": "Not allowed here (temporary, system, cloud-synced or app folder).",
    "field.unsupported_location": "This volume cannot hold a vault.",
    "field.overlaps_registration": "Overlaps a registered vault.",
    "field.protected_overlap": "Inside or around a protected vault.",
    "field.case_collision": "Differs only in letter case from a registered folder.",
    "field.destination_not_empty": "The folder already has content.",

    /* `extra.denied` category `install`: the app's own folder (`workspaces.py`'s `refuse_denied`,
     * 403 `denied_location`). The only `field.denied_location.<category>` row today; any other
     * category still falls back to the generic `field.denied_location` row above. */
    "field.denied_location.install":
      "This is VIKI Agent's own folder, so it can't be opened here. To work on VIKI Agent itself, open a Git worktree or a second checkout of it instead.",

    "field.unsupported_location.mount_point":
      "The folder sits on a mounted volume; use a drive-letter path.",
    "field.unsupported_location.mount_point_posix":
      "VIKI cannot tell what kind of volume this folder is on; choose a folder on a local disk.",
    "field.unsupported_location.cloud_sync":
      "Cloud-synced folders cannot hold a vault: OneDrive, and on a Mac iCloud Drive and the other cloud folders under ~/Library.",
    "field.unsupported_location.drive_type":
      "This drive type (network, optical, …) cannot hold a vault.",
    "field.unsupported_location.no_identity":
      "This volume cannot identify folders (FAT32 and the like).",
    "field.unsupported_location.network": "Network folders cannot hold a vault.",

    "error.not_removable": "Only code folders can be removed from this list.",
    "error.workspace_in_use":
      "Chats start in this folder, so it can't be removed. Choose another vault first.",
    "error.worktrees_present":
      "This folder still has worktrees. Remove them in the Code view first.",
    "error.picker_unavailable": "This computer can't show a folder picker. Type the path instead.",
    "error.picker_busy": "A folder picker is already open.",
    "error.picker_failed": "The folder picker couldn't open. Type the path instead.",
  },
});
