import { describe, expect, it, vi } from "vitest";
import { ShellApiError } from "../clients/app/shellApi";
import { en, type Key, modules } from "./index";
import { errorText, t, tf } from "./t";

/** Every code that gets an `error.<code>` row: HTTP-bearing failures, the client codes, and the
 * readiness fact. `not_found` is deliberately listed once (see `errors.workspaces.ts`'s header
 * comment: the data routes' second `not_found` collides on the same flat code and has no row). */
const SERVER_ERROR_CODES = [
  "unauthorized",
  "forbidden_origin",
  "invalid_representation",
  "invalid_body",
  "too_large",
  "intake_timeout",
  "disconnected",
  "overloaded",
  "invalid_input",
  "busy",
  "unavailable",
  "corrupt",
  "recovery_required",
  "incompatible",
  "unsafe_path",
  "insecure_permissions",
  "timeout",
  "network_error",
  "invalid_response",
  "http_error",
  "invalid_settings",
  "settings_too_large",
  "read_only",
  "stale_revision",
  "command_conflict",
  "invalid_path",
  "invalid_name",
  "not_found",
  "not_directory",
  "invalid_transition",
  "denied_location",
  "protected_overlap",
  "protected_root",
  "unknown_workspace",
  "destination_not_empty",
  "overlaps_registration",
  "case_collision",
  "acknowledgement_required",
  "root_unavailable",
  "not_app_created",
  "unsupported_location",
  "unsupported",
  "check_in_progress",
  "invalid_kind",
  "invalid_include",
  "denied_destination",
  "preview_expired",
  "exists",
  "invalid_format",
  "unsupported_format",
  "invalid_selection",
  "invalid_id",
  "invalid_confirmation",
  "invalid_categories",
] as const;

const RESET_REASON_CODES = ["in_use", "read_only", "unknown_category", "generic"] as const;

/** The 5 settings + 8 workspaces field codes. */
const FIELD_CODES = [
  "unknown_key",
  "not_allowed",
  "too_long",
  "invalid_type",
  "control_characters",
  "invalid_path",
  "invalid_name",
  "denied_location",
  "unsupported_location",
  "overlaps_registration",
  "protected_overlap",
  "case_collision",
  "destination_not_empty",
] as const;

const UNSUPPORTED_LOCATION_REASONS = [
  "mount_point",
  "mount_point_posix",
  "cloud_sync",
  "drive_type",
  "no_identity",
  "network",
];

/** Every `reason.*` `commandFor` can return. */
const COMMAND_FOR_REASONS = [
  "reason.provider_not_ready",
  "reason.recovery",
  "reason.recovery_no_export",
  "reason.vault_search_in_view",
  "reason.storage_not_ready",
  "reason.no_workspaces",
  "reason.check_running",
  "reason.cli_not_ready",
];

describe("the catalog is English only and the fallback is visible", () => {
  it("every module holds one English table and nothing else", () => {
    for (const [name, catalog] of Object.entries(modules)) {
      expect(Object.keys(catalog), name).toEqual(["en"]);
    }
  });

  it("t falls back to the key itself for a key with no row", () => {
    // A `Key`-typed call can never bypass the catalog; this simulates the one case the design
    // calls "never in typed code" by going around the type system, exactly like a corrupted or
    // future server code would.
    expect(t("nothing.like.a.real.key" as Key)).toBe("nothing.like.a.real.key");
  });

  it("t reads the English table", () => {
    expect(t("common.button.close" as Key)).toBe(en["common.button.close" as Key]);
  });
});

describe("no duplicate key across modules", () => {
  it("the merged catalog has as many keys as the sum of every module's own keys", () => {
    const sum = Object.values(modules).reduce(
      (total, catalog) => total + Object.keys(catalog.en).length,
      0,
    );
    expect(Object.keys(en)).toHaveLength(sum);
  });
});

describe("every server error code resolves", () => {
  it.each(SERVER_ERROR_CODES)("error.%s has a row", (code) => {
    const key = `error.${code}` as Key;
    expect(en[key]).toBeTruthy();
  });

  it.each(RESET_REASON_CODES)("error.reset.%s has a row", (reason) => {
    const key = `error.reset.${reason}` as Key;
    expect(en[key]).toBeTruthy();
  });

  it("error.import.newer_format has a row", () => {
    expect(en["error.import.newer_format"]).toBeTruthy();
  });

  it.each(FIELD_CODES)("field.%s has a row", (code) => {
    const key = `field.${code}` as Key;
    expect(en[key]).toBeTruthy();
  });

  it.each(UNSUPPORTED_LOCATION_REASONS)("field.unsupported_location.%s has a row", (reason) => {
    const key = `field.unsupported_location.${reason}` as Key;
    expect(en[key]).toBeTruthy();
  });

  it("the two admissible reasons have a row", () => {
    for (const reason of ["protected_root", "root_unavailable"]) {
      const key = `workspaces.admissible.${reason}` as Key;
      expect(en[key]).toBeTruthy();
    }
  });

  it("all seven provider problems have a row", () => {
    for (const code of [
      "not_found",
      "batch_shim",
      "config_dir_override",
      "api_key_helper",
      "billing_env_key",
      "federation_profile",
      "other",
    ]) {
      const key = `providers.problem.${code}` as Key;
      expect(en[key]).toBeTruthy();
    }
  });
});

describe("every reason.* commandFor can return exists", () => {
  it.each(COMMAND_FOR_REASONS)("%s has a row", (key) => {
    expect(en[key as Key]).toBeTruthy();
  });
});

describe("every CommandId has a cmd.<id> row", () => {
  const COMMAND_IDS = [
    "session.new",
    "search.conversations",
    "search.vault",
    "command.run",
    "workspace.create",
    "workspace.open",
    "workspace.later",
    "workspace.select",
    "settings.open",
    "settings.search",
    "diagnostics.open",
    "diagnostics.check",
    "data.open",
    "data.export_diagnostics",
    "data.import_settings",
    "data.reset",
    "provider.setup",
  ];

  it.each(COMMAND_IDS)("cmd.%s has a row", (id) => {
    const key = `cmd.${id}` as Key;
    expect(en[key]).toBeTruthy();
  });
});

describe("tf substitutes", () => {
  it("keeps interpolated text in another script, or shaped like markup, exactly as given", () => {
    const content = "日本語 <b>そのまま</b>";
    expect(tf("error.unknown", { code: content })).toBe(`Could not complete: ${content}`);
  });

  it("replaces a {name} placeholder with the given value", () => {
    expect(tf("error.unknown" as Key, { code: "weird_code" })).toBe(
      "Could not complete: weird_code",
    );
  });

  it("leaves an unmatched placeholder untouched rather than guessing", () => {
    expect(tf("error.acknowledgement_required" as Key, {})).toContain("{required}");
  });
});

describe("errorText", () => {
  it("renders the catalog row for a known code", () => {
    const error = new ShellApiError(503, "busy");
    expect(errorText(error)).toBe(t("error.busy" as Key));
  });

  it("renders the generic fallback, with the code substituted in, for an unknown code", () => {
    const error = new ShellApiError(500, "a_code_with_no_catalog_row");
    expect(errorText(error)).toBe("Could not complete: a_code_with_no_catalog_row");
  });

  it("fills in {current_bytes} from extra for a 413 settings_too_large, no braces left", () => {
    const error = new ShellApiError(413, "settings_too_large", { extra: { current_bytes: 9001 } });
    const text = errorText(error);
    expect(text).toBe("The settings document is too large (9001 bytes).");
    expect(text).not.toContain("{");
    expect(text).not.toContain("}");
  });

  it("fills in {required} from extra for a 409 acknowledgement_required, no braces left", () => {
    const error = new ShellApiError(409, "acknowledgement_required", {
      extra: { required: ["instructions_present", "hooks_present"] },
    });
    const text = errorText(error);
    expect(text).toContain("instructions_present");
    expect(text).toContain("hooks_present");
    expect(text).not.toContain("{");
    expect(text).not.toContain("}");
  });

  it("a row with no placeholder ignores extra entirely", () => {
    const error = new ShellApiError(503, "busy", { extra: { unrelated: "value" } });
    expect(errorText(error)).toBe(t("error.busy" as Key));
  });
});

describe("the two storage recovery codes", () => {
  it("invalid_legacy and invalid_preferences resolve through errorText", () => {
    for (const code of ["invalid_legacy", "invalid_preferences"]) {
      const key = `error.${code}` as Key;
      expect(en[key]).toBeTruthy();
      expect(errorText(new ShellApiError(503, code))).toBe(en[key]);
    }
  });
});

describe("the module graph has no cycle", () => {
  it("loading a catalog module before index.ts does not throw a TDZ ReferenceError", async () => {
    // A fresh module graph whose first import is a catalog module, not index.ts or t.ts -
    // exactly how a view's own component reaches its catalog. `errors.common.ts` stands for
    // every catalog module: like them, it imports `defineCatalog` from the dependency-free
    // `./defineCatalog`.
    vi.resetModules();
    const direct = await import("./errors.common");
    expect(Object.keys(direct.catalog.en).length).toBeGreaterThan(0);
    const indexModule = await import("./index");
    expect(indexModule.en["error.busy" as Key]).toBe(direct.catalog.en["error.busy"]);
  });

  it("defineCatalog.ts, the shared dependency, imports nothing", async () => {
    vi.resetModules();
    const mod = await import("./defineCatalog");
    expect(typeof mod.defineCatalog).toBe("function");
  });
});

describe("no catalog value contains a path or a token-shaped string", () => {
  // `field.example_path` and its per-OS siblings are the deliberate exceptions: fixed,
  // non-profile example paths.
  const PATH_LIKE = /[a-zA-Z]:\\|\/[a-z]+\//;
  const TOKEN_LIKE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
  // Built from parts so this file never spells the profile prefix the hygiene guard bans.
  const PROFILE_PREFIX = new RegExp(["c:", "users"].join("\\\\"), "i");

  const EXAMPLE_PATH_KEYS = [
    "field.example_path",
    "field.example_path_mac",
    "field.example_path_linux",
  ];

  it("has no path-shaped or token-shaped value outside the one named exception", () => {
    for (const [key, value] of Object.entries(en)) {
      if (EXAMPLE_PATH_KEYS.includes(key)) continue;
      expect(PATH_LIKE.test(value), `en["${key}"] looks like a path: ${value}`).toBe(false);
      expect(TOKEN_LIKE.test(value), `en["${key}"] looks like a token: ${value}`).toBe(false);
      expect(PROFILE_PREFIX.test(value), `en["${key}"] names a user's profile: ${value}`).toBe(
        false,
      );
    }
  });

  it("the one named exception is the fixed, non-profile example path", () => {
    expect(en["field.example_path" as Key]).toBe("D:\\VIKI\\Vaults\\My notes");
    expect(en["field.example_path_mac" as Key]).toBe("/Users/you/VIKI/Vaults/My notes");
    expect(en["field.example_path_linux" as Key]).toBe("/home/you/VIKI/Vaults/My notes");
  });
});
