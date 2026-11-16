/**
 * In-memory shell fake and the document fixtures every UI test lane builds from.
 *
 * `fakeShellApi()` answers every call at once from `data`, records what was asked in `calls`
 * (so a test can assert the order a form made its requests in), and lets a test script exactly
 * one failure per method with `fail(method, error)`. `deferredShellApi()` hands back a deferred
 * per call for the tests that care which read settles first.
 *
 * The fixtures carry no real user data: every path here is invented (`tests/test_fixtures_clean.py`
 * bans a real profile path in a committed file), and nothing is read from disk.
 */
import type {
  AppDataLocation,
  Applies,
  Capabilities,
  CheckName,
  CheckResult,
  CreateVaultResult,
  DataLocations,
  ExportPreview,
  ExportResult,
  FolderPickerStatus,
  ImportPreview,
  JsonObject,
  KeySpec,
  OpenVaultResult,
  PolicyInspection,
  PolicyKind,
  ProviderReadiness,
  ResetResult,
  SelectResult,
  SettingKind,
  SettingsPatchResult,
  SettingsView,
  ShellApi,
  ShellApiError,
  VaultSuggestion,
  WorkspaceJson,
  WorkspacesListing,
} from "./clients/app/shellApi";

const AT = "2026-09-17T00:00:00.000Z";

export const SCRATCH_WORKSPACE_ID = "165b132f-7fcb-5c95-8c6c-86eff07bfe13";
export const FAKE_WORKSPACE_ID = "22222222-2222-4222-8222-222222222222";
export const FAKE_PREVIEW_ID = "33333333-3333-4333-8333-333333333333";
export const FAKE_VAULT_ROOT = "D:\\VIKI\\Vaults\\My notes";

/** The settings document's fifteen keys. */
const SPECS: Readonly<
  Record<
    string,
    {
      kind: SettingKind;
      allowed: unknown[] | null;
      value: unknown;
      applies: Applies;
      personal: boolean;
    }
  >
> = {
  "appearance.theme": {
    kind: "enum",
    allowed: ["dark", "light", "system"],
    value: "system",
    applies: "now",
    personal: false,
  },
  "appearance.density": {
    kind: "enum",
    allowed: ["comfortable", "compact"],
    value: "comfortable",
    applies: "now",
    personal: false,
  },
  "accessibility.font_scale": {
    kind: "number",
    allowed: [0.85, 1, 1.15, 1.3, 1.5],
    value: 1,
    applies: "now",
    personal: false,
  },
  "accessibility.reduce_motion": {
    kind: "enum",
    allowed: ["allow", "reduce", "system"],
    value: "system",
    applies: "now",
    personal: false,
  },
  "identity.display_name": {
    kind: "string",
    allowed: null,
    value: "",
    applies: "now",
    personal: true,
  },
  "identity.assistant_name": {
    kind: "string",
    allowed: null,
    value: "VIKI",
    applies: "now",
    personal: true,
  },
  "identity.user_avatar_id": {
    kind: "string",
    allowed: null,
    value: null,
    applies: "now",
    personal: true,
  },
  "identity.assistant_avatar_id": {
    kind: "string",
    allowed: null,
    value: null,
    applies: "now",
    personal: true,
  },
  "privacy.hide_previews": {
    kind: "bool",
    allowed: null,
    value: false,
    applies: "now",
    personal: false,
  },
  "privacy.diagnostics_include_paths": {
    kind: "bool",
    allowed: null,
    value: false,
    applies: "now",
    personal: false,
  },
  "chat.default_mode": {
    kind: "enum",
    allowed: ["acceptEdits", "default", "plan"],
    value: null,
    applies: "next_session",
    personal: false,
  },
  "chat.default_effort": {
    kind: "enum",
    allowed: ["high", "low", "medium"],
    value: null,
    applies: "next_session",
    personal: false,
  },
  "chat.default_model": {
    kind: "string",
    allowed: null,
    value: null,
    applies: "next_session",
    personal: false,
  },
  "onboarding.completed": {
    kind: "bool",
    allowed: null,
    value: false,
    applies: "now",
    personal: false,
  },
};

export interface CapabilitiesOverrides {
  app?: Partial<Capabilities["app"]>;
  workspace?: Partial<Capabilities["workspace"]>;
  provider?: Partial<ProviderReadiness>;
  git?: Partial<Capabilities["git"]>;
  storage?: Partial<Capabilities["storage"]>;
  features?: Record<string, boolean>;
  launch?: Partial<Capabilities["launch"]>;
}

/** The default is the launch the user sees on a clean install: normal mode, the shared scratch
 * active, no registrations, a provider that can start and a writable store. */
export function capabilitiesFixture(overrides: CapabilitiesOverrides = {}): Capabilities {
  return {
    app: { mode: "normal", ...overrides.app },
    workspace: {
      active: {
        id: SCRATCH_WORKSPACE_ID,
        kind: "scratch",
        name: "scratch",
        policy: "ordinary",
        layout: "scratch",
        source: "builtin",
        aliased: false,
        root_available: true,
      },
      chat_only: true,
      selected_id: null,
      selected_by: "builtin",
      restart_required: false,
      shared_scratch: true,
      ...overrides.workspace,
    },
    provider: {
      claude: {
        cli: "ready",
        version: "1.0.0",
        login: "signed_in",
        billing: "clear",
        last_checked_at: AT,
        can_start: true,
        problems: [],
        source: "installed",
        version_status: "tested",
        tested: ["2.1.281", "2.1.282"],
        ...overrides.provider,
      },
    },
    git: { state: "ready", version: "2.45.0", ...overrides.git },
    storage: { state: "ready", reason: null, private: true, ...overrides.storage },
    features: { ...overrides.features },
    launch: { id: "44444444-4444-4444-8444-444444444444", ...overrides.launch },
  };
}

function keySpec(key: string): KeySpec {
  const spec = SPECS[key];
  if (spec === undefined) throw new Error(`unknown settings key: ${key}`);
  return {
    scope: "app",
    applies: spec.applies,
    kind: spec.kind,
    allowed: spec.allowed,
    default: spec.value,
    personal: spec.personal,
  };
}

/** `values` are the stored values; every other key shows its built-in default. */
export function settingsFixture(
  values: Record<string, unknown> = {},
  overrides: Partial<SettingsView> = {},
): SettingsView {
  const effective: Record<string, unknown> = {};
  const sources: Record<string, SettingsView["sources"][string]> = {};
  const keys: Record<string, KeySpec> = {};
  for (const key of Object.keys(SPECS)) {
    keys[key] = keySpec(key);
    const stored = key in values;
    effective[key] = stored ? values[key] : keys[key].default;
    sources[key] = stored ? "setting" : "default";
  }
  return {
    version: 1,
    revision: 3,
    read_only: false,
    reason: null,
    values: { ...values },
    effective,
    sources,
    keys,
    ...overrides,
  };
}

export function workspaceFixture(overrides: Partial<WorkspaceJson> = {}): WorkspaceJson {
  return {
    workspace_id: FAKE_WORKSPACE_ID,
    kind: "vault",
    root: FAKE_VAULT_ROOT,
    root_available: true,
    aliased: false,
    name: "My notes",
    policy: "ordinary",
    policy_revision: 1,
    revision: 1,
    source: "created",
    layout: "unknown",
    portable_id: null,
    identity: {
      established: false,
      git_root_oids: 0,
      inherits_from: null,
      probed_at: AT,
    },
    face_dir: null,
    bootstrap: "complete",
    persistent: true,
    created_at: AT,
    updated_at: AT,
    ...overrides,
  };
}

export function listingFixture(workspaces: WorkspaceJson[] = []): WorkspacesListing {
  return {
    workspaces,
    active_id: SCRATCH_WORKSPACE_ID,
    selected_id: null,
    selected_by: "builtin",
    chat_only: true,
    scratch: { workspace_id: SCRATCH_WORKSPACE_ID, kind: "scratch", shared: true },
  };
}

export function inspectionFixture(overrides: Partial<PolicyInspection> = {}): PolicyInspection {
  return {
    workspace_id: FAKE_WORKSPACE_ID,
    policy: "existing_readonly",
    policy_revision: 1,
    root_available: true,
    aliased: false,
    markers: {
      claude_md: false,
      agents_md: false,
      claude_dir: false,
      hooks: false,
      git: false,
      viki_manifest: false,
    },
    instructions: [],
    identity: { established: false },
    overlaps: [],
    admissible: { ordinary: true, protected: true, reason: null },
    excluded_from_writes: [],
    acknowledgements_required: [],
    ...overrides,
  };
}

export function locationsFixture(): DataLocations {
  const appData: AppDataLocation[] = [
    { kind: "database", path: "D:\\AppData\\viki-agent\\app.sqlite3", bytes: 65_536, schema: 4 },
    { kind: "backups", path: "D:\\AppData\\viki-agent", bytes: 0, count: 0 },
    { kind: "logs", path: "D:\\AppData\\viki-agent\\logs", bytes: 1_024 },
    { kind: "edge_profile", path: "D:\\AppData\\viki-agent\\edge", bytes: 2_048, partial: false },
    {
      kind: "scratch",
      path: "D:\\AppData\\viki-agent\\scratch",
      bytes: 0,
      in_use: true,
      shared: true,
    },
    { kind: "legacy_state", path: "D:\\AppData\\viki-agent\\state.json", bytes: 0 },
  ];
  return {
    categories: [
      { id: "vaults", items: [] },
      { id: "app_data", items: appData },
      {
        id: "provider",
        items: [
          {
            provider: "claude",
            kind: "home",
            path: "D:\\Profile\\.claude",
            contains: ["transcripts", "session_registry", "settings"],
            read_by_app: true,
            bytes: 4_096,
            partial: false,
          },
          { provider: "claude", kind: "credentials", path: null, read_by_app: false },
        ],
      },
      { id: "indexes", items: [] },
      { id: "assets", items: [] },
    ],
  };
}

export function exportPreviewFixture(overrides: Partial<ExportPreview> = {}): ExportPreview {
  return {
    preview_id: FAKE_PREVIEW_ID,
    kind: "diagnostics",
    files: [{ name: "viki-diagnostics.json", bytes: 512, redactions: 3 }],
    content: { "viki-diagnostics.json": '{"app":{"mode":"normal"}}' },
    expires_at: AT,
    ...overrides,
  };
}

export function importPreviewFixture(overrides: Partial<ImportPreview> = {}): ImportPreview {
  return {
    preview_id: FAKE_PREVIEW_ID,
    content_hash: "0".repeat(64),
    settings_revision: 3,
    format_version: 1,
    supported: true,
    unsupported_reason: null,
    provenance: { exported_at: AT },
    changes: [
      {
        key: "appearance.density",
        current: "comfortable",
        incoming: "compact",
        action: "apply",
      },
    ],
    workspaces: [],
    executable_definitions: [],
    duplicate_ids: [],
    ...overrides,
  };
}

export interface FakeShellData {
  capabilities: Capabilities;
  diagnostics: JsonObject;
  settings: SettingsView;
  workspaces: WorkspacesListing;
  suggestion: VaultSuggestion;
  picker: FolderPickerStatus;
  picked: string[];
  workspace: WorkspaceJson;
  createdFiles: string[];
  inspection: PolicyInspection;
  locations: DataLocations;
  exportPreview: ExportPreview;
  exportFile: { path: string; bytes: number; sha256: string };
  importPreview: ImportPreview;
  reset: ResetResult;
}

export interface FakeCall {
  method: string;
  args: unknown[];
}

export interface FakeShellApi extends ShellApi {
  /** The documents every call answers from; a test may edit this between calls. */
  data: FakeShellData;
  calls: FakeCall[];
  /** Script one failure: the next call of `method` rejects with `error`, then the fake is
   * itself again, so a form's retry can succeed. */
  fail(method: keyof ShellApi, error: ShellApiError): void;
}

function defaultData(): FakeShellData {
  return {
    capabilities: capabilitiesFixture(),
    diagnostics: {},
    settings: settingsFixture(),
    workspaces: listingFixture(),
    suggestion: { parent: "D:\\VIKI\\Vaults", folder: "My notes", path: FAKE_VAULT_ROOT },
    picker: { available: false, multiple: false, reason: "unsupported" },
    picked: [],
    workspace: workspaceFixture(),
    createdFiles: ["Welcome.md"],
    inspection: inspectionFixture(),
    // The default is an empty locations document; `locationsFixture()` is what a test
    // that wants rows to render passes in.
    locations: {
      categories: [
        { id: "vaults", items: [] },
        { id: "app_data", items: [] },
        { id: "provider", items: [] },
        { id: "indexes", items: [] },
        { id: "assets", items: [] },
      ],
    },
    exportPreview: exportPreviewFixture(),
    exportFile: { path: "D:\\Exports\\viki-diagnostics.json", bytes: 512, sha256: "1".repeat(64) },
    importPreview: importPreviewFixture(),
    reset: { scope_of: SCRATCH_WORKSPACE_ID, results: {} },
  };
}

/** The document a patch or an import apply produces: the stored values move, everything else
 * falls back to its spec default, and the revision advances by one. */
function applyChanges(view: SettingsView, changes: Record<string, unknown>): SettingsView {
  const values: Record<string, unknown> = { ...view.values };
  const effective: Record<string, unknown> = { ...view.effective };
  const sources: Record<string, SettingsView["sources"][string]> = { ...view.sources };
  for (const [key, value] of Object.entries(changes)) {
    const spec = view.keys[key];
    if (value === null) {
      delete values[key];
      if (spec !== undefined) {
        effective[key] = spec.default;
        sources[key] = "default";
      }
    } else {
      values[key] = value;
      effective[key] = value;
      sources[key] = "setting";
    }
  }
  return { ...view, revision: view.revision + 1, values, effective, sources };
}

export function fakeShellApi(overrides: Partial<FakeShellData> = {}): FakeShellApi {
  const data: FakeShellData = { ...defaultData(), ...overrides };
  const calls: FakeCall[] = [];
  const failures = new Map<string, ShellApiError>();

  function record(method: string, ...args: unknown[]): void {
    calls.push({ method, args });
    const scripted = failures.get(method);
    if (scripted !== undefined) {
      failures.delete(method);
      throw scripted;
    }
  }

  function patched(commandId: string, changes: Record<string, unknown>): SettingsPatchResult {
    data.settings = applyChanges(data.settings, changes);
    return { ...data.settings, command_id: commandId, replayed: false };
  }

  return {
    data,
    calls,
    fail(method, error) {
      failures.set(method, error);
    },
    async capabilities() {
      record("capabilities");
      return data.capabilities;
    },
    async diagnostics() {
      record("diagnostics");
      return data.diagnostics;
    },
    async runCheck(commandId: string, check: CheckName): Promise<CheckResult> {
      record("runCheck", commandId, check);
      return { check, status: "ok", result: { version: "1.0.0" }, checked_at: AT, duration_ms: 12 };
    },
    async settings() {
      record("settings");
      return data.settings;
    },
    async patchSettings(commandId, expectedRevision, changes) {
      record("patchSettings", commandId, expectedRevision, changes);
      return patched(commandId, changes);
    },
    async workspaces() {
      record("workspaces");
      return data.workspaces;
    },
    async selectWorkspace(commandId, workspaceId): Promise<SelectResult> {
      record("selectWorkspace", commandId, workspaceId);
      const active = data.capabilities.workspace.active;
      // `_workspace_json`'s rule: the implicit scratch fallback needs no restart while nothing is
      // selected, a vault active without its own selection already does (src/viki_agent/app/diagnostics.py:239-248).
      const restart =
        active?.kind === "scratch"
          ? workspaceId !== null && workspaceId !== active.id
          : workspaceId !== (active?.id ?? null);
      data.workspaces = { ...data.workspaces, selected_id: workspaceId };
      // The launch keeps the workspace it resolved, so `active` and `chat_only` do not move; the
      // selection and the restart flag do, which is what the onboarding forms then render.
      data.capabilities = {
        ...data.capabilities,
        workspace: {
          ...data.capabilities.workspace,
          selected_id: workspaceId,
          selected_by: workspaceId === null ? "builtin" : "registration",
          restart_required: restart,
        },
      };
      return {
        selected_id: workspaceId,
        active_id: active?.id ?? data.workspaces.active_id,
        restart_required: restart,
      };
    },
    async vaultSuggestion(name): Promise<VaultSuggestion> {
      record("vaultSuggestion", name);
      // The folder follows the name, as the real service's suggestion does, so a form test can
      // tell a prefill from a stale value.
      const folder = name === "" ? data.suggestion.folder : name;
      return {
        parent: data.suggestion.parent,
        folder,
        path: `${data.suggestion.parent}\\${folder}`,
      };
    },
    async createVault(commandId, input): Promise<CreateVaultResult> {
      record("createVault", commandId, input);
      return { workspace: data.workspace, created_files: data.createdFiles, created: true };
    },
    async openVault(commandId, path): Promise<OpenVaultResult> {
      record("openVault", commandId, path);
      return { workspace: data.workspace, inspection: data.inspection, created: true };
    },
    async openCodeFolder(commandId, path): Promise<OpenVaultResult> {
      record("openCodeFolder", commandId, path);
      return { workspace: data.workspace, inspection: data.inspection, created: true };
    },
    async cancelBootstrap(commandId, workspaceId) {
      record("cancelBootstrap", commandId, workspaceId);
      return { workspace_id: workspaceId, cancelled: true };
    },
    async removeWorkspace(commandId, workspaceId) {
      record("removeWorkspace", commandId, workspaceId);
      data.workspaces = {
        ...data.workspaces,
        workspaces: data.workspaces.workspaces.filter(
          (workspace) => workspace.workspace_id !== workspaceId,
        ),
      };
      return { workspace_id: workspaceId, removed: true };
    },
    async folderPickerStatus() {
      record("folderPickerStatus");
      return data.picker;
    },
    async pickFolders(multiple, title) {
      record("pickFolders", multiple, title);
      return data.picked;
    },
    async policy(workspaceId) {
      record("policy", workspaceId);
      return data.inspection;
    },
    async setPolicy(
      commandId,
      workspaceId,
      expectedPolicyRevision,
      policy: PolicyKind,
      acknowledged,
    ) {
      record("setPolicy", commandId, workspaceId, expectedPolicyRevision, policy, acknowledged);
      data.inspection = { ...data.inspection, policy, policy_revision: expectedPolicyRevision + 1 };
      const noteWrite =
        data.workspace.kind === "vault" &&
        policy === "ordinary" &&
        data.workspace.persistent &&
        data.workspace.bootstrap === "complete";
      return {
        policy,
        policy_revision: expectedPolicyRevision + 1,
        revision: data.workspace.revision + 1,
        note_write: noteWrite,
      };
    },
    async dataLocations() {
      record("dataLocations");
      return data.locations;
    },
    async exportPreview(kind, include) {
      record("exportPreview", kind, include);
      return { ...data.exportPreview, kind };
    },
    async exportWrite(commandId, previewId, destination): Promise<ExportResult> {
      record("exportWrite", commandId, previewId, destination);
      return { ...data.exportFile, command_id: commandId, replayed: false };
    },
    async importPreview(content) {
      record("importPreview", content);
      return data.importPreview;
    },
    async importApply(previewId, commandId, expectedRevision, keys) {
      record("importApply", previewId, commandId, expectedRevision, keys);
      const changes: Record<string, unknown> = {};
      for (const change of data.importPreview.changes)
        if (keys.includes(change.key)) changes[change.key] = change.incoming;
      return patched(commandId, changes);
    },
    async reset(commandId, categories) {
      record("reset", commandId, categories);
      return data.reset;
    },
  };
}

export interface PendingCall {
  method: string;
  args: unknown[];
  resolve(value: unknown): void;
  reject(error: unknown): void;
}

export interface DeferredShellApi extends ShellApi {
  /** Every call, in the order it was made; a test settles each one by hand. */
  pending: PendingCall[];
}

/** Each call hands back a deferred, so a test can land a slower earlier read after a faster
 * later one and watch which of them the store lets commit. */
export function deferredShellApi(): DeferredShellApi {
  const pending: PendingCall[] = [];

  function defer<T>(method: string, ...args: unknown[]): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      pending.push({ method, args, resolve: resolve as (value: unknown) => void, reject });
    });
  }

  return {
    pending,
    capabilities: () => defer("capabilities"),
    diagnostics: () => defer("diagnostics"),
    runCheck: (commandId, check) => defer("runCheck", commandId, check),
    settings: () => defer("settings"),
    patchSettings: (commandId, expectedRevision, changes) =>
      defer("patchSettings", commandId, expectedRevision, changes),
    workspaces: () => defer("workspaces"),
    selectWorkspace: (commandId, workspaceId) => defer("selectWorkspace", commandId, workspaceId),
    vaultSuggestion: (name) => defer("vaultSuggestion", name),
    createVault: (commandId, input) => defer("createVault", commandId, input),
    openVault: (commandId, path) => defer("openVault", commandId, path),
    openCodeFolder: (commandId, path) => defer("openCodeFolder", commandId, path),
    cancelBootstrap: (commandId, workspaceId) => defer("cancelBootstrap", commandId, workspaceId),
    removeWorkspace: (commandId, workspaceId) => defer("removeWorkspace", commandId, workspaceId),
    folderPickerStatus: () => defer("folderPickerStatus"),
    pickFolders: (multiple, title) => defer("pickFolders", multiple, title),
    policy: (workspaceId) => defer("policy", workspaceId),
    setPolicy: (commandId, workspaceId, expectedPolicyRevision, policy, acknowledged) =>
      defer("setPolicy", commandId, workspaceId, expectedPolicyRevision, policy, acknowledged),
    dataLocations: () => defer("dataLocations"),
    exportPreview: (kind, include) => defer("exportPreview", kind, include),
    exportWrite: (commandId, previewId, destination) =>
      defer("exportWrite", commandId, previewId, destination),
    importPreview: (content) => defer("importPreview", content),
    importApply: (previewId, commandId, expectedRevision, keys) =>
      defer("importApply", previewId, commandId, expectedRevision, keys),
    reset: (commandId, categories) => defer("reset", commandId, categories),
  };
}
