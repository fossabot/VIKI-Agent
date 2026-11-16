import { type ReactElement, useContext, useEffect, useRef, useState } from "react";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import type { BotsApi } from "../../clients/messaging/botsApi";
import {
  ACCENTS,
  blankProfileDocument,
  type ProfileApi,
  ProfileApiError,
  type ProfileComparison,
  type ProfileDefinition,
  type ProfileDocumentV1,
  type ProfileSummary,
} from "../../clients/profiles/profileApi";
import {
  type ProfileLaunchApi,
  ProfileLaunchApiError,
  type ProfilePreview,
  type ProfileSessionIdentity,
  type ProviderCatalogView,
  type ProviderModelView,
} from "../../clients/profiles/profileLaunchApi";
import { t, tf } from "../../i18n/t";
import { ACCENT_LABELS } from "../../state/accentLabels";
import { useReportUnsavedWork } from "../../state/unsavedWork";
import { Avatar } from "../Avatar/Avatar";
import { AvatarField } from "../Avatar/AvatarField";
import { BotAvatar } from "../Bots/BotAvatar";
import { BotHuesContext } from "../Bots/botHues";
import { seatProfile } from "../Bots/seatBots";
import { TEAM } from "../Bots/team";
import { ConfirmDialog } from "../Dialog/ConfirmDialog";
import { LineDiff } from "../Diff/LineDiff";
import { Field } from "../Field/Field";
import { Loading } from "../Loading/Loading";
import { BotCodeFolder } from "./BotCodeFolder";
import { BotHueField } from "./BotHueField";
import { BotLookField } from "./BotLookField";
import { McpServers, type McpServersError } from "./McpServers";
import { modelLabel, useConnectionLabels } from "./modelLabel";
import { ProfileBotLink } from "./ProfileBotLink";
import { ProfileModelField } from "./ProfileModelField";
import { problemText } from "./profileCopy";
import { SEAT_STARTERS, type SeatStarter, starterDocument } from "./seatStarters";
import "./ProfilesView.css";

type Phase = "loading" | "ready" | "error";
type Owner = { api: ProfileApi; archived: boolean; reload: number };
type DetailOwner = { api: ProfileApi; profileId: string; reload: number };
type LookSnapshot = {
  api: ProfileApi;
  profileId: string;
  document: ProfileDocumentV1;
  baseline: ProfileDocumentV1;
  archived: boolean;
};
type LookFailure = { api: ProfileApi; profileId: string; operation: number };
type DiscardAction =
  | { kind: "choose"; profileId: string }
  | { kind: "new" }
  | { kind: "starter"; starter: SeatStarter };

const ERRORS: Record<string, string> = {
  cancelled: "The request was cancelled.",
  invalid_input: "Check the profile fields.",
  invalid_document: "The profile fields are not valid.",
  invalid_response: "The profile service returned an invalid response.",
  response_too_large: "The profile response is too large.",
  not_found: "The profile or revision was not found.",
  conflict: "The profile changed. Reload it before trying again.",
  command_conflict: "This retry no longer matches the original change.",
  capacity: "The profile limit has been reached.",
  newer_version: "This profile was created by a newer version.",
  unauthorized: "Authentication is required.",
  forbidden_origin: "This window cannot change profiles.",
  avatar_missing: "The avatar is no longer available. Replace or reset it.",
  overloaded: "Profile operations are busy. Try again.",
  intake_timeout: "The profile request timed out.",
  unavailable: "Profiles are unavailable.",
};

function errorMessage(error: unknown): string {
  const code = error instanceof ProfileApiError ? error.code : "unavailable";
  return ERRORS[code] ?? "Profiles are unavailable.";
}

function launchErrorMessage(error: unknown): string {
  if (!(error instanceof ProfileLaunchApiError)) return "Profile launch review is unavailable.";
  const messages: Partial<Record<ProfileLaunchApiError["code"], string>> = {
    workspace_restart_required: "Restart in the selected workspace before creating this draft.",
    workspace_unavailable: "The active workspace is unavailable.",
    provider_unavailable: "The provider is unavailable.",
    settings_unavailable: "Settings are unavailable.",
    capability_unverified: "Provider capability evidence is incomplete.",
    conflict: "The profile or workspace changed. Reload before trying again.",
    command_conflict: "This retry no longer matches the original draft request.",
    session_exists: "That draft session already exists.",
    capacity: "The local draft limit has been reached.",
    cancelled: "The launch review was cancelled.",
  };
  return messages[error.code] ?? "Profile launch review is unavailable.";
}

function json(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

interface ExecutionFieldGroup {
  key: string;
  labelKey:
    | "profiles.execution.context"
    | "profiles.execution.nativeAssets"
    | "profiles.execution.requiredTools"
    | "profiles.execution.deniedTools"
    | "profiles.execution.memory"
    | "profiles.execution.limits";
  value: (document: ProfileDocumentV1) => unknown;
  isSet: (document: ProfileDocumentV1) => boolean;
  clear: (document: ProfileDocumentV1) => ProfileDocumentV1;
  note?: string;
}

const EXECUTION_FIELD_GROUPS: readonly ExecutionFieldGroup[] = [
  {
    key: "context.items",
    labelKey: "profiles.execution.context",
    value: (document) => document.context.items,
    isSet: (document) => document.context.items.length > 0,
    clear: (document) => ({ ...document, context: { items: [] } }),
  },
  {
    key: "native_assets",
    labelKey: "profiles.execution.nativeAssets",
    value: (document) => document.native_assets,
    isSet: (document) => document.native_assets.length > 0,
    clear: (document) => ({ ...document, native_assets: [] }),
  },
  {
    key: "tools.required",
    labelKey: "profiles.execution.requiredTools",
    value: (document) => document.tools.required,
    isSet: (document) => document.tools.required.length > 0,
    clear: (document) => ({ ...document, tools: { ...document.tools, required: [] } }),
    note: "VIKI cannot enforce this, so it blocks launch.",
  },
  {
    key: "tools.denied",
    labelKey: "profiles.execution.deniedTools",
    value: (document) => document.tools.denied,
    isSet: (document) => document.tools.denied.length > 0,
    clear: (document) => ({ ...document, tools: { ...document.tools, denied: [] } }),
    note: "VIKI cannot enforce this, so it blocks launch.",
  },
  {
    key: "memory",
    labelKey: "profiles.execution.memory",
    value: (document) => document.memory,
    isSet: (document) =>
      document.memory.mode !== "provider_inherited" || document.memory.sources.length > 0,
    clear: (document) => ({ ...document, memory: { mode: "provider_inherited", sources: [] } }),
  },
  {
    key: "limits",
    labelKey: "profiles.execution.limits",
    value: (document) => document.limits,
    isSet: (document) => Object.values(document.limits).some((value) => value !== null),
    clear: (document) => ({
      ...document,
      limits: {
        timeout_seconds: null,
        max_root_tasks: null,
        token_budget: null,
        currency_budget: null,
      },
    }),
  },
];

function clearUnsupportedFields(document: ProfileDocumentV1): ProfileDocumentV1 {
  return EXECUTION_FIELD_GROUPS.reduce((current, group) => group.clear(current), document);
}

function profileValue(field: string, value: unknown, label: (model: string) => string): string {
  if (field === "runtime.model") {
    return typeof value === "string" ? label(value) : t("profiles.model.appDefault");
  }
  return json(value);
}

function executionFieldReason(
  key: string,
  preview: ProfilePreview | null,
  permissionMode: string | null,
): string {
  if (!preview) return t("profiles.notEvaluated");
  const rows = preview.fields.filter((row) => row.field === key || row.field.startsWith(`${key}.`));
  if (rows.length === 0) return t("profiles.notEvaluated");
  return rows
    .map((row) => {
      if (row.status === "supported") return t("profiles.available");
      return problemText(row.field, {
        reason: row.reason,
        permissionMode: row.field === "runtime.permission_mode" ? permissionMode : null,
      });
    })
    .join(" ");
}

// Bounds match src/viki_agent/profiles/profile_document.py's byte limits (UTF-8), not the fields' UTF-16 maxLength.
const NAME_MAX_BYTES = 128;
const PURPOSE_MAX_BYTES = 2048;
const INSTRUCTIONS_MAX_BYTES = 65_536;
const EFFORTS = ["low", "medium", "high", "xhigh", "max"] as const;

function byteLength(value: string): number {
  return new TextEncoder().encode(value).length;
}

function oversizedField(document: ProfileDocumentV1): { field: string; limit: number } | null {
  if (byteLength(document.identity.name) > NAME_MAX_BYTES)
    return { field: "Name", limit: NAME_MAX_BYTES };
  if (byteLength(document.identity.purpose) > PURPOSE_MAX_BYTES)
    return { field: "Purpose", limit: PURPOSE_MAX_BYTES };
  if (byteLength(document.instructions) > INSTRUCTIONS_MAX_BYTES)
    return { field: "Instructions", limit: INSTRUCTIONS_MAX_BYTES };
  return null;
}

function sameProviderObservation(
  capabilities: ProviderCatalogView,
  models: ProviderModelView,
): boolean {
  return (
    capabilities.provider === models.provider &&
    capabilities.adapter_version === models.adapter_version &&
    capabilities.native_version === models.native_version &&
    capabilities.observed_at === models.observed_at &&
    capabilities.catalog_key === models.catalog_key
  );
}

export function ProfilesView({
  api,
  launchApi,
  workspaceId,
  workspaces,
  onPrepared,
  initialSelectedId,
  bots,
  onOpenBot,
  botSettings = false,
  title = "Profiles",
  defaultModel,
}: {
  api: ProfileApi;
  title?: string;
  launchApi?: ProfileLaunchApi;
  workspaceId?: string;
  workspaces?: readonly WorkspaceJson[];
  initialSelectedId?: string;
  bots?: BotsApi;
  botSettings?: boolean;
  defaultModel?: string | null;
  onOpenBot?(botId: string): void;
  /** The prepared session and the identity it is bound to, from the prepare answer itself. */
  onPrepared?(sessionId: string, identity: ProfileSessionIdentity | null): void;
}): ReactElement {
  const { set: setBotLook } = useContext(BotHuesContext);
  const [showArchived, setShowArchived] = useState(false);
  const [rows, setRows] = useState<ProfileSummary[]>([]);
  const [listPhase, setListPhase] = useState<Phase>("loading");
  const [listError, setListError] = useState("");
  const [listOwner, setListOwner] = useState<Owner | null>(null);
  const [listReload, setListReload] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(initialSelectedId ?? null);
  const [storedDefinition, setDefinition] = useState<ProfileDefinition | null>(null);
  const [detailOwner, setDetailOwner] = useState<DetailOwner | null>(null);
  const [detailPhase, setDetailPhase] = useState<Phase>("ready");
  const [detailError, setDetailError] = useState("");
  const [detailReload, setDetailReload] = useState(0);
  const [document, setDocument] = useState<ProfileDocumentV1 | null>(null);
  const [baseline, setBaseline] = useState<ProfileDocumentV1 | null>(null);
  const [newId, setNewId] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState("");
  const [lookFailure, setLookFailure] = useState<LookFailure | null>(null);
  const [mcpFieldError, setMcpFieldError] = useState<McpServersError | null>(null);
  const [mutating, setMutating] = useState(false);
  const [pendingDiscard, setPendingDiscard] = useState<DiscardAction | null>(null);
  const [leftRevision, setLeftRevision] = useState("1");
  const [rightRevision, setRightRevision] = useState("1");
  const [comparison, setComparison] = useState<ProfileComparison | null>(null);
  const [compareError, setCompareError] = useState("");
  const [catalog, setCatalog] = useState<ProviderCatalogView | null>(null);
  const [models, setModels] = useState<ProviderModelView | null>(null);
  const [evidencePhase, setEvidencePhase] = useState<"unknown" | "loading" | "ready" | "error">(
    "unknown",
  );
  const [evidenceRetry, setEvidenceRetry] = useState(0);
  const evidenceRetryRef = useRef(0);
  const [preview, setPreview] = useState<ProfilePreview | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [preparing, setPreparing] = useState(false);
  const listGeneration = useRef(0);
  const detailGeneration = useRef(0);
  const mutationGeneration = useRef(0);
  const mutationApi = useRef(api);
  const previewGeneration = useRef(0);
  const previewController = useRef<AbortController | null>(null);
  const launchApiRef = useRef(launchApi);
  const previewIds = useRef<{ key: string; commandId: string; sessionId: string } | null>(null);
  const compareGeneration = useRef(0);
  const compareController = useRef<AbortController | null>(null);
  const detailController = useRef<AbortController | null>(null);
  const command = useRef<{ key: string; id: string } | null>(null);
  const duplicateTarget = useRef<{ key: string; id: string } | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const lookSnapshot = useRef<LookSnapshot | null>(null);
  const lookResetOperation = useRef(0);
  const currentProfileRef = useRef({ api, profileId: selectedId, botSettings });
  const lookDraftGateRef = useRef({ api, profileId: selectedId, disabled: false, botSettings });
  currentProfileRef.current = { api, profileId: selectedId, botSettings };

  // Entry focus is the view's own, as in every non-chat view.
  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  if (mutationApi.current !== api) {
    mutationApi.current = api;
    ++mutationGeneration.current;
    ++compareGeneration.current;
    compareController.current?.abort();
    ++lookResetOperation.current;
  }
  if (launchApiRef.current !== launchApi) {
    launchApiRef.current = launchApi;
    ++previewGeneration.current;
    previewController.current?.abort();
  }

  useEffect(() => {
    const owner = api;
    ++lookResetOperation.current;
    setLookFailure(null);
    setMutating(false);
    setMutationError("");
    setMcpFieldError(null);
    return () => {
      if (mutationApi.current === owner) {
        ++mutationGeneration.current;
        ++compareGeneration.current;
        compareController.current?.abort();
      }
    };
  }, [api]);

  const definition =
    detailOwner?.api === api &&
    detailOwner.profileId === selectedId &&
    detailOwner.reload === detailReload
      ? storedDefinition
      : null;
  const providerName = document?.runtime.provider ?? null;
  const evidenceProfile = definition?.profile_id ?? null;
  const evidenceRevision = definition?.revision ?? null;
  const isConnectionRef = (value: unknown): boolean =>
    typeof value === "string" && value.startsWith("viki-llm/");
  // Names load for Bot choices, and for a saved, draft or compared ref in either view.
  const connections = useConnectionLabels(
    (botSettings && document !== null) ||
      isConnectionRef(definition?.document.runtime.model) ||
      isConnectionRef(document?.runtime.model) ||
      Boolean(
        comparison?.changes.some(
          (change) =>
            change.field === "runtime.model" &&
            (isConnectionRef(change.before) || isConnectionRef(change.after)),
        ),
      ),
    newId ?? selectedId,
  );
  const modelText = (model: string): string =>
    modelLabel(model, models, connections.labels, t("profiles.model.connected"));
  useEffect(() => {
    const retry = evidenceRetry;
    const mine = ++previewGeneration.current;
    previewController.current?.abort();
    setCatalog(null);
    setModels(null);
    setPreview(null);
    setPreviewError("");
    setPreparing(false);
    if (!launchApi || !providerName || !evidenceProfile || !evidenceRevision || !workspaceId) {
      setEvidencePhase("unknown");
      return;
    }
    if (providerName !== "claude") {
      // No adapter evidence exists outside claude; let prepare's own refusal show the blocker.
      setEvidencePhase("ready");
      return;
    }
    const controller = new AbortController();
    previewController.current = controller;
    setEvidencePhase("loading");
    Promise.all([
      launchApi.capabilities(providerName, controller.signal),
      launchApi.models(providerName, controller.signal),
    ]).then(
      ([capabilities, providerModels]) => {
        if (
          mine !== previewGeneration.current ||
          retry !== evidenceRetryRef.current ||
          launchApiRef.current !== launchApi
        )
          return;
        if (!sameProviderObservation(capabilities, providerModels)) {
          setPreviewError(
            "Provider evidence changed while it was being reviewed. Reload the profile.",
          );
          setEvidencePhase("error");
          return;
        }
        setCatalog(capabilities);
        setModels(providerModels);
        setEvidencePhase("ready");
      },
      (error) => {
        if (
          mine !== previewGeneration.current ||
          retry !== evidenceRetryRef.current ||
          controller.signal.aborted
        )
          return;
        setPreviewError(launchErrorMessage(error));
        setEvidencePhase("error");
      },
    );
    return () => {
      controller.abort();
      if (previewGeneration.current === mine) ++previewGeneration.current;
    };
  }, [evidenceProfile, evidenceRevision, evidenceRetry, launchApi, providerName, workspaceId]);

  useEffect(() => {
    const mine = ++listGeneration.current;
    const controller = new AbortController();
    setRows([]);
    setListPhase("loading");
    setListError("");
    setListOwner({ api, archived: showArchived, reload: listReload });
    api.list(showArchived, controller.signal).then(
      (result) => {
        if (mine !== listGeneration.current) return;
        setRows(result);
        setListPhase("ready");
      },
      (error) => {
        if (mine !== listGeneration.current || controller.signal.aborted) return;
        setListError(errorMessage(error));
        setListPhase("error");
      },
    );
    return () => {
      controller.abort();
      if (listGeneration.current === mine) ++listGeneration.current;
    };
  }, [api, showArchived, listReload]);

  useEffect(() => {
    const mine = ++detailGeneration.current;
    detailController.current?.abort();
    if (!selectedId) return;
    const controller = new AbortController();
    detailController.current = controller;
    setDetailOwner({ api, profileId: selectedId, reload: detailReload });
    setDefinition(null);
    setDocument(null);
    setBaseline(null);
    setDetailPhase("loading");
    setDetailError("");
    api.get(selectedId, undefined, controller.signal).then(
      (result) => {
        if (mine !== detailGeneration.current || result.profile_id !== selectedId) return;
        setDefinition(result);
        setDocument(result.document);
        setBaseline(result.document);
        setLeftRevision(String(result.revision));
        setRightRevision(String(result.current_revision));
        setDetailPhase("ready");
      },
      (error) => {
        if (mine !== detailGeneration.current || controller.signal.aborted) return;
        setDetailError(errorMessage(error));
        setDetailPhase("error");
      },
    );
    return () => {
      controller.abort();
      if (detailGeneration.current === mine) ++detailGeneration.current;
    };
  }, [api, detailReload, selectedId]);

  const ownedList =
    listOwner?.api === api &&
    listOwner.archived === showArchived &&
    listOwner.reload === listReload;
  const visibleRows = ownedList ? rows : [];
  const visibleListPhase = ownedList ? listPhase : "loading";
  function requestDiscard(action: DiscardAction): void {
    if (dirty) setPendingDiscard(action);
    else performDiscard(action);
  }

  function performDiscard(action: DiscardAction): void {
    if (action.kind === "choose") choose(action.profileId);
    else if (action.kind === "new") createNew();
    else startFrom(action.starter);
  }

  function choose(profileId: string): void {
    ++lookResetOperation.current;
    setLookFailure(null);
    ++previewGeneration.current;
    previewController.current?.abort();
    ++compareGeneration.current;
    compareController.current?.abort();
    ++mutationGeneration.current;
    setMutating(false);
    setNewId(null);
    setMutationError("");
    setMcpFieldError(null);
    setComparison(null);
    command.current = null;
    if (profileId === selectedId) {
      setDetailReload((value) => value + 1);
      return;
    }
    setSelectedId(profileId);
  }

  function createNew(): void {
    ++lookResetOperation.current;
    setLookFailure(null);
    ++previewGeneration.current;
    previewController.current?.abort();
    ++compareGeneration.current;
    compareController.current?.abort();
    ++mutationGeneration.current;
    setMutating(false);
    ++detailGeneration.current;
    detailController.current?.abort();
    const id = crypto.randomUUID();
    setSelectedId(null);
    setDefinition(null);
    setNewId(id);
    setDocument(blankProfileDocument());
    setBaseline(blankProfileDocument());
    setDetailPhase("ready");
    setDetailError("");
    setMutationError("");
    setMcpFieldError(null);
    setComparison(null);
    command.current = null;
  }

  /** A new profile only: replaces the draft with `starter`'s seat. */
  function startFrom(starter: SeatStarter): void {
    if (!newId) return;
    const next = starterDocument(starter);
    setDocument(next);
    setBaseline(next);
    setMutationError("");
    setMcpFieldError(null);
  }

  function commandId(operation: string, payload: unknown): string {
    const key = json([operation, payload]);
    if (command.current?.key !== key) command.current = { key, id: crypto.randomUUID() };
    return command.current.id;
  }

  function refresh(profileId: string): void {
    ++lookResetOperation.current;
    setLookFailure(null);
    ++compareGeneration.current;
    compareController.current?.abort();
    setNewId(null);
    setSelectedId(profileId);
    setListReload((value) => value + 1);
    setDetailReload((value) => value + 1);
    command.current = null;
    duplicateTarget.current = null;
  }

  async function save(): Promise<void> {
    if (!document || (!newId && !definition)) return;
    const oversized = oversizedField(document);
    if (oversized) {
      setMutationError(`${oversized.field} is over ${oversized.limit} bytes.`);
      return;
    }
    const payload = newId
      ? { profile_id: newId, document }
      : { profile_id: definition?.profile_id, revision: definition?.revision, document };
    const mine = ++mutationGeneration.current;
    setMutating(true);
    setMutationError("");
    setMcpFieldError(null);
    try {
      const id = commandId(newId ? "create" : "revise", payload);
      const receipt = newId
        ? await api.create(newId, id, document)
        : definition
          ? await api.revise(definition.profile_id, id, definition.revision, document)
          : null;
      if (!receipt || mine !== mutationGeneration.current || mutationApi.current !== api) return;
      refresh(receipt.profile_id);
    } catch (error) {
      if (mine === mutationGeneration.current && mutationApi.current === api) {
        setMutationError(errorMessage(error));
        if (
          error instanceof ProfileApiError &&
          error.field?.startsWith("tools.mcp_configuration")
        ) {
          setMcpFieldError({ field: error.field, reason: error.reason });
        }
      }
    } finally {
      if (mine === mutationGeneration.current && mutationApi.current === api) setMutating(false);
    }
  }

  async function toggleArchived(): Promise<void> {
    if (!definition) return;
    const archived = !definition.archived;
    const payload = {
      profile_id: definition.profile_id,
      state_revision: definition.state_revision,
      archived,
    };
    const mine = ++mutationGeneration.current;
    setMutating(true);
    setMutationError("");
    setMcpFieldError(null);
    try {
      const id = commandId("archive", payload);
      await api.archive(definition.profile_id, id, definition.state_revision, archived);
      if (mine === mutationGeneration.current && mutationApi.current === api)
        refresh(definition.profile_id);
    } catch (error) {
      if (mine === mutationGeneration.current && mutationApi.current === api)
        setMutationError(errorMessage(error));
    } finally {
      if (mine === mutationGeneration.current && mutationApi.current === api) setMutating(false);
    }
  }

  async function duplicate(): Promise<void> {
    if (!definition) return;
    const targetKey = json([definition.profile_id, definition.revision, "claude"]);
    if (duplicateTarget.current?.key !== targetKey)
      duplicateTarget.current = { key: targetKey, id: crypto.randomUUID() };
    const targetId = duplicateTarget.current.id;
    const payload = {
      source: definition.profile_id,
      revision: definition.revision,
      targetId,
      duplicateProvider: "claude",
    };
    const mine = ++mutationGeneration.current;
    setMutating(true);
    setMutationError("");
    setMcpFieldError(null);
    try {
      const id = commandId("duplicate", payload);
      const receipt = await api.duplicate(
        definition.profile_id,
        id,
        definition.revision,
        targetId,
        "claude",
      );
      if (mine === mutationGeneration.current && mutationApi.current === api)
        refresh(receipt.profile_id);
    } catch (error) {
      if (mine === mutationGeneration.current && mutationApi.current === api)
        setMutationError(errorMessage(error));
    } finally {
      if (mine === mutationGeneration.current && mutationApi.current === api) setMutating(false);
    }
  }

  async function compare(): Promise<void> {
    if (!definition) return;
    const mine = ++compareGeneration.current;
    compareController.current?.abort();
    const controller = new AbortController();
    compareController.current = controller;
    setCompareError("");
    setComparison(null);
    try {
      const result = await api.compare(
        definition.profile_id,
        Number(leftRevision),
        Number(rightRevision),
        controller.signal,
      );
      if (mine === compareGeneration.current && mutationApi.current === api) setComparison(result);
    } catch (error) {
      if (
        mine === compareGeneration.current &&
        mutationApi.current === api &&
        !controller.signal.aborted
      )
        setCompareError(errorMessage(error));
    }
  }

  async function createDraft(): Promise<void> {
    if (!launchApi || !workspaceId || !definition || definition.archived) return;
    const key = json([definition.profile_id, definition.revision, workspaceId]);
    if (previewIds.current?.key !== key) {
      previewIds.current = {
        key,
        commandId: crypto.randomUUID(),
        sessionId: crypto.randomUUID(),
      };
    }
    const ids = previewIds.current;
    const mine = ++previewGeneration.current;
    previewController.current?.abort();
    const controller = new AbortController();
    previewController.current = controller;
    setPreparing(true);
    setPreview(null);
    setPreviewError("");
    try {
      const result = await launchApi.prepare(
        definition.profile_id,
        ids.commandId,
        ids.sessionId,
        definition.revision,
        workspaceId,
        controller.signal,
      );
      if (mine !== previewGeneration.current || launchApiRef.current !== launchApi) return;
      setPreview(result);
      if ("session_id" in result) {
        previewIds.current = null;
        onPrepared?.(result.session_id, result.identity ?? null);
      }
    } catch (error) {
      if (mine !== previewGeneration.current || controller.signal.aborted) return;
      setPreviewError(launchErrorMessage(error));
    } finally {
      if (mine === previewGeneration.current && launchApiRef.current === launchApi)
        setPreparing(false);
    }
  }

  const profileId = newId ?? definition?.profile_id ?? null;
  const seat =
    definition && !newId ? TEAM.find((s) => s.profileId === definition.profile_id) : undefined;
  const starter = seat?.starter;
  const purposeDiffers = Boolean(
    starter && definition && definition.document.identity.purpose !== starter.purpose,
  );
  const instructionsDiffer = Boolean(
    starter && definition && definition.document.instructions !== starter.instructions,
  );
  const missingSeats =
    bots &&
    !showArchived &&
    visibleListPhase === "ready" &&
    (workspaces ?? []).some(
      (workspace) =>
        workspace.workspace_id === workspaceId &&
        workspace.kind === "vault" &&
        workspace.persistent,
    ) &&
    TEAM.some((s) => seatProfile(visibleRows, s) === null);
  const dirty = Boolean(document && baseline && json(baseline) !== json(document));
  // App asks before leaving the view on the same signal.
  useReportUnsavedWork("profile", dirty);
  // The Workspaces fieldset's own checkbox rows: persistent vaults only. An allowlisted id
  // naming a listed but non-persistent workspace still renders as "Unknown workspace" below,
  // so it can be unchecked.
  const shownWorkspaces = (workspaces ?? []).filter(
    (workspace) => workspace.kind === "vault" && workspace.persistent,
  );
  const modelsForField =
    evidencePhase === "loading" ? "loading" : evidencePhase === "error" ? "failed" : models;
  const permissionMode = document?.runtime.permission_mode ?? null;
  const permissionLabel =
    permissionMode === null
      ? t("profiles.permission.default")
      : permissionMode === "plan"
        ? t("profiles.permission.plan")
        : problemText("runtime.permission_mode", { permissionMode });

  const loadedLook =
    botSettings &&
    !newId &&
    selectedId &&
    definition?.profile_id === selectedId &&
    document &&
    baseline
      ? {
          api,
          profileId: selectedId,
          document,
          baseline,
          archived: definition.archived,
        }
      : null;
  if (loadedLook) lookSnapshot.current = loadedLook;
  const retainedLook =
    botSettings &&
    !newId &&
    selectedId &&
    lookSnapshot.current?.api === api &&
    lookSnapshot.current.profileId === selectedId
      ? lookSnapshot.current
      : null;
  const activeLook = loadedLook ?? retainedLook;
  const sameProfileReloading = Boolean(
    activeLook &&
      selectedId &&
      detailOwner?.api === api &&
      detailOwner.profileId === selectedId &&
      (detailOwner.reload !== detailReload || detailPhase === "loading"),
  );
  const lookDraftActionsDisabled = Boolean(
    activeLook &&
      (mutating || activeLook.archived || sameProfileReloading || detailPhase !== "ready"),
  );
  lookDraftGateRef.current = {
    api,
    profileId: activeLook?.profileId ?? selectedId,
    disabled: lookDraftActionsDisabled,
    botSettings,
  };
  const visibleLookFailure =
    activeLook &&
    lookFailure?.api === api &&
    lookFailure.profileId === activeLook.profileId &&
    lookFailure.operation === lookResetOperation.current;

  function canUpdateLookDraft(ownerApi: ProfileApi, ownerProfileId: string): boolean {
    const current = currentProfileRef.current;
    const gate = lookDraftGateRef.current;
    return (
      current.api === ownerApi &&
      current.profileId === ownerProfileId &&
      current.botSettings &&
      gate.api === ownerApi &&
      gate.profileId === ownerProfileId &&
      gate.botSettings &&
      !gate.disabled
    );
  }

  function resetBotLook(ownerApi: ProfileApi, ownerProfileId: string): void {
    if (!canUpdateLookDraft(ownerApi, ownerProfileId)) return;
    const operation = ++lookResetOperation.current;
    setLookFailure(null);
    setDocument((current) =>
      current ? { ...current, identity: { ...current.identity, avatar_asset_id: null } } : current,
    );
    function failed(): void {
      if (
        operation === lookResetOperation.current &&
        currentProfileRef.current.api === ownerApi &&
        currentProfileRef.current.profileId === ownerProfileId &&
        currentProfileRef.current.botSettings
      )
        setLookFailure({ api: ownerApi, profileId: ownerProfileId, operation });
    }
    if (!setBotLook) {
      failed();
      return;
    }
    void setBotLook(ownerProfileId, { icon: null }).catch(failed);
  }

  const profileOverview =
    document && profileId ? (
      <section className="profile-overview" aria-label="Profile overview">
        <div className="profile-overview-identity">
          {botSettings ? (
            <BotAvatar
              profileId={profileId}
              identity={document.identity}
              name={document.identity.name}
              size={64}
            />
          ) : (
            <Avatar
              name={document.identity.name}
              avatarId={document.identity.avatar_asset_id}
              accent={document.identity.accent}
              shape={document.identity.shape}
              size={40}
            />
          )}
          <div>
            <p className="profile-configuration-label">
              {newId || dirty ? "Draft" : "Saved configuration"}
            </p>
            <h2>{document.identity.name}</h2>
            {document.identity.purpose ? <p>{document.identity.purpose}</p> : null}
          </div>
        </div>
        <dl className="profile-overview-configuration">
          <div>
            <dt>{t("profiles.runtime.runsOn")}</dt>
            <dd>
              {document.runtime.provider === "claude"
                ? t("profiles.runtime.claudeCode")
                : document.runtime.provider}
            </dd>
          </div>
          <div>
            <dt>Model</dt>
            <dd>
              {document.runtime.model
                ? modelText(document.runtime.model)
                : t("profiles.model.appDefault")}
            </dd>
          </div>
          <div>
            <dt>{t("profiles.overview.permissionSetting")}</dt>
            <dd>{permissionLabel}</dd>
          </div>
        </dl>
        {bots && onOpenBot && workspaceId && definition && !newId ? (
          <ProfileBotLink
            api={bots}
            profileId={profileId}
            revision={definition.current_revision}
            workspaceId={workspaceId}
            disabled={dirty || mutating || definition.archived}
            onOpen={onOpenBot}
          />
        ) : null}
      </section>
    ) : null;

  const botLook = activeLook ? (
    <>
      <BotLookField
        profileId={activeLook.profileId}
        name={activeLook.document.identity.name}
        design={activeLook.document.identity}
        avatar={activeLook.document.identity.avatar_asset_id}
        compact={botSettings}
        imageRemovalPending={Boolean(
          activeLook.baseline.identity.avatar_asset_id &&
            !activeLook.document.identity.avatar_asset_id,
        )}
        draftDisabled={lookDraftActionsDisabled}
        onUpload={(avatar) => {
          if (!canUpdateLookDraft(api, activeLook.profileId)) return;
          setDocument((current) =>
            current
              ? { ...current, identity: { ...current.identity, avatar_asset_id: avatar } }
              : current,
          );
        }}
        onReset={() => resetBotLook(api, activeLook.profileId)}
      />
      {visibleLookFailure ? <p role="alert">{t("bots.icon.failed")}</p> : null}
    </>
  ) : null;

  return (
    <section className={`profiles-view${botSettings ? " bot-settings" : ""}`} aria-label="Profiles">
      <header className="page-head">
        <h1 className="page-title" ref={headingRef} tabIndex={-1}>
          {title}
        </h1>
        {!botSettings ? (
          <>
            <p className="page-intro" role="note">
              {t("bots.config.help")}
            </p>
            <div className="page-actions profiles-toolbar">
              <label>
                <input
                  type="checkbox"
                  checked={showArchived}
                  onChange={(event) => setShowArchived(event.target.checked)}
                />{" "}
                Show archived
              </label>
              <button
                type="button"
                className="btn alt sm"
                onClick={() => requestDiscard({ kind: "new" })}
              >
                New profile
              </button>
            </div>
          </>
        ) : null}
      </header>
      {pendingDiscard ? (
        <ConfirmDialog
          label={t("bots.confirm.discard")}
          body="Discard your unsaved changes to this profile?"
          confirmLabel={t("bots.confirm.discardAction")}
          destructive
          irreversible
          onConfirm={() => {
            const action = pendingDiscard;
            setPendingDiscard(null);
            performDiscard(action);
          }}
          onCancel={() => setPendingDiscard(null)}
        />
      ) : null}
      {botSettings ? (
        <>
          {profileOverview || botLook ? (
            <div className="bot-settings-selected">
              {profileOverview}
              {botLook}
            </div>
          ) : null}
          <div className="bot-settings-catalog-head">
            <h2>{t("profiles.catalog.title")}</h2>
            <p className="page-intro" role="note">
              {t("bots.config.help")}
            </p>
            <div className="page-actions profiles-toolbar">
              <label>
                <input
                  type="checkbox"
                  checked={showArchived}
                  onChange={(event) => setShowArchived(event.target.checked)}
                />{" "}
                Show archived
              </label>
              <button
                type="button"
                className="btn alt sm"
                onClick={() => requestDiscard({ kind: "new" })}
              >
                New profile
              </button>
            </div>
          </div>
        </>
      ) : null}
      <div className="profiles-layout">
        <div className="profiles-catalog">
          {missingSeats ? (
            <p className="profiles-note" role="note">
              {t("bots.config.missingSeats")}
            </p>
          ) : null}
          {visibleListPhase === "loading" ? <Loading>Loading profiles…</Loading> : null}
          {visibleListPhase === "error" ? <p role="alert">{listError}</p> : null}
          {visibleListPhase === "ready" && visibleRows.length === 0 ? (
            <p>No profiles found.</p>
          ) : null}
          {visibleRows.length > 0 ? (
            <nav
              className="profiles-list"
              aria-label={showArchived ? "Archived profiles" : "Active profiles"}
            >
              {visibleRows.map((profile) => (
                <button
                  type="button"
                  key={profile.profile_id}
                  className="profile-summary"
                  aria-label={`${profile.identity.name} — ${profile.runtime.provider} — revision ${profile.revision}`}
                  aria-describedby={
                    profile.identity.purpose ? `profile-purpose-${profile.profile_id}` : undefined
                  }
                  aria-current={profile.profile_id === selectedId || undefined}
                  onClick={() => requestDiscard({ kind: "choose", profileId: profile.profile_id })}
                >
                  {botSettings ? (
                    <BotAvatar
                      profileId={profile.profile_id}
                      identity={profile.identity}
                      name={profile.identity.name}
                      size={40}
                    />
                  ) : (
                    <Avatar
                      name={profile.identity.name}
                      avatarId={profile.identity.avatar_asset_id}
                      accent={profile.identity.accent}
                      shape={profile.identity.shape}
                      size={32}
                    />
                  )}
                  <span className="profile-summary-content">
                    <strong className="profile-summary-name">{profile.identity.name}</strong>
                    {profile.identity.purpose ? (
                      <span
                        id={`profile-purpose-${profile.profile_id}`}
                        className="profile-summary-purpose"
                      >
                        {profile.identity.purpose}
                      </span>
                    ) : null}
                    <span className="profile-summary-runtime">
                      {profile.runtime.provider} — revision {profile.revision}
                    </span>
                  </span>
                </button>
              ))}
            </nav>
          ) : null}
          {botSettings && activeLook ? (
            <BotHueField
              profileId={activeLook.profileId}
              name={activeLook.document.identity.name}
            />
          ) : null}
        </div>
        <div className="profile-detail">
          {!botSettings ? botLook : null}
          {detailPhase === "loading" ? <Loading>Loading profile…</Loading> : null}
          {detailPhase === "error" ? <p role="alert">{detailError}</p> : null}
          {!document && detailPhase === "ready" ? (
            <p className="profile-selection-hint">Select a profile to review its configuration.</p>
          ) : null}
          {document && profileId ? (
            <>
              {!botSettings ? profileOverview : null}
              {botSettings && bots && workspaceId && definition && !newId ? (
                <BotCodeFolder
                  api={bots}
                  profileId={profileId}
                  workspaceId={workspaceId}
                  folders={workspaces ?? []}
                  disabled={mutating}
                />
              ) : null}
              <form
                className="profile-editor"
                onSubmit={(event) => {
                  event.preventDefault();
                  void save();
                }}
              >
                <h2>{newId ? "New profile" : "Profile definition"}</h2>
                {seat && starter && definition && (purposeDiffers || instructionsDiffer) ? (
                  <div className="profile-starter-compare">
                    <p>{tf("bots.starter.differs", { name: seat.name })}</p>
                    <details>
                      <summary>{t("bots.starter.compare")}</summary>
                      {purposeDiffers ? (
                        <>
                          <h3>{t("bots.starter.purpose")}</h3>
                          <LineDiff
                            before={definition.document.identity.purpose}
                            after={starter.purpose}
                          />
                        </>
                      ) : null}
                      {instructionsDiffer ? (
                        <>
                          <h3>{t("bots.starter.instructions")}</h3>
                          <LineDiff
                            before={definition.document.instructions}
                            after={starter.instructions}
                          />
                        </>
                      ) : null}
                    </details>
                    <button
                      type="button"
                      className="btn alt sm"
                      disabled={mutating || definition.archived}
                      onClick={() =>
                        setDocument({
                          ...document,
                          identity: { ...document.identity, purpose: starter.purpose },
                          instructions: starter.instructions,
                        })
                      }
                    >
                      {t("bots.starter.use")}
                    </button>
                  </div>
                ) : null}
                {newId ? (
                  <fieldset className="profile-seat-starters" disabled={mutating}>
                    <legend>{t("profiles.startSeat")}</legend>
                    <div className="profile-seat-starter-buttons">
                      {SEAT_STARTERS.map((starter) => (
                        <button
                          type="button"
                          key={starter.name}
                          className="btn alt sm"
                          title={starter.purpose}
                          onClick={() => requestDiscard({ kind: "starter", starter })}
                        >
                          {starter.name}
                        </button>
                      ))}
                    </div>
                    <p className="profile-seat-starter-note">{t("profiles.seatStarterNote")}</p>
                  </fieldset>
                ) : null}
                {definition?.archived ? <p>Unarchive this profile to edit it.</p> : null}
                <fieldset disabled={mutating || Boolean(definition?.archived)}>
                  <legend>{t("profiles.identity")}</legend>
                  <label>
                    Name{" "}
                    <input
                      required
                      maxLength={128}
                      value={document.identity.name}
                      onChange={(event) =>
                        setDocument({
                          ...document,
                          identity: { ...document.identity, name: event.target.value },
                        })
                      }
                    />
                  </label>
                  <label>
                    Purpose{" "}
                    <textarea
                      maxLength={2048}
                      value={document.identity.purpose}
                      onChange={(event) =>
                        setDocument({
                          ...document,
                          identity: { ...document.identity, purpose: event.target.value },
                        })
                      }
                    />
                  </label>
                  {!botSettings ? (
                    <label>
                      Accent{" "}
                      <select
                        value={document.identity.accent}
                        onChange={(event) =>
                          setDocument({
                            ...document,
                            identity: {
                              ...document.identity,
                              accent: event.target.value as ProfileDocumentV1["identity"]["accent"],
                            },
                          })
                        }
                      >
                        {ACCENTS.map((value) => (
                          <option key={value} value={value}>
                            {ACCENT_LABELS[value as ProfileDocumentV1["identity"]["accent"]]}
                          </option>
                        ))}
                      </select>
                    </label>
                  ) : null}
                  <label>
                    Shape{" "}
                    <select
                      value={document.identity.shape}
                      onChange={(event) =>
                        setDocument({
                          ...document,
                          identity: {
                            ...document.identity,
                            shape: event.target.value as ProfileDocumentV1["identity"]["shape"],
                          },
                        })
                      }
                    >
                      {["circle", "rounded", "square"].map((value) => (
                        <option key={value}>{value}</option>
                      ))}
                    </select>
                  </label>
                  {!botSettings ? (
                    <AvatarField
                      label="Avatar"
                      name={document.identity.name}
                      accent={document.identity.accent}
                      shape={document.identity.shape}
                      avatarId={document.identity.avatar_asset_id}
                      disabled={mutating || Boolean(definition?.archived)}
                      onChange={(avatarId) =>
                        setDocument({
                          ...document,
                          identity: { ...document.identity, avatar_asset_id: avatarId },
                        })
                      }
                    />
                  ) : null}
                </fieldset>
                <fieldset disabled={mutating || Boolean(definition?.archived)}>
                  <legend>{t("profiles.instructions")}</legend>
                  <Field
                    label={t("profiles.field.instructions")}
                    hint={t("profiles.hint.instructions")}
                  >
                    {(control) => (
                      <textarea
                        {...control}
                        disabled={mutating || Boolean(definition?.archived)}
                        maxLength={65_536}
                        value={document.instructions}
                        onChange={(event) =>
                          setDocument({ ...document, instructions: event.target.value })
                        }
                      />
                    )}
                  </Field>
                  {botSettings ? (
                    <p className="profiles-note" role="note">
                      {t("profiles.botMemoryRule")}
                    </p>
                  ) : null}
                </fieldset>
                <fieldset disabled={mutating || Boolean(definition?.archived)}>
                  <legend>{t("profiles.modelPermissions")}</legend>
                  <p>
                    {t("profiles.runtime.runsOn")}:{" "}
                    {document.runtime.provider === "claude"
                      ? t("profiles.runtime.claudeCode")
                      : document.runtime.provider}
                  </p>
                  {document.runtime.provider !== "claude" ? (
                    <button
                      type="button"
                      className="btn alt sm"
                      onClick={() =>
                        setDocument({
                          ...document,
                          runtime: { ...document.runtime, provider: "claude" },
                        })
                      }
                    >
                      {t("profiles.runtime.useClaudeCode")}
                    </button>
                  ) : null}
                  <ProfileModelField
                    value={document.runtime.model}
                    models={modelsForField}
                    {...(defaultModel !== undefined ? { defaultModel } : {})}
                    botSettings={botSettings}
                    connections={connections}
                    onChange={(model) =>
                      setDocument({
                        ...document,
                        runtime: { ...document.runtime, model },
                      })
                    }
                    onRetry={() =>
                      setEvidenceRetry((value) => {
                        const next = value + 1;
                        evidenceRetryRef.current = next;
                        return next;
                      })
                    }
                  />
                  <Field label={t("profiles.field.effort")} hint={t("profiles.hint.effort")}>
                    {(control) => (
                      <select
                        {...control}
                        value={document.runtime.effort}
                        onChange={(event) =>
                          setDocument({
                            ...document,
                            runtime: {
                              ...document.runtime,
                              effort: event.target.value as ProfileDocumentV1["runtime"]["effort"],
                            },
                          })
                        }
                      >
                        {EFFORTS.map((value) => (
                          <option key={value} value={value}>
                            {t(`profiles.effort.${value}`)}
                          </option>
                        ))}
                      </select>
                    )}
                  </Field>
                  <Field
                    label={t("profiles.field.permission")}
                    hint={t("profiles.hint.permission")}
                  >
                    {(control) => (
                      <select
                        {...control}
                        value={document.runtime.permission_mode ?? ""}
                        onChange={(event) =>
                          setDocument({
                            ...document,
                            runtime: {
                              ...document.runtime,
                              permission_mode: (event.target.value ||
                                null) as ProfileDocumentV1["runtime"]["permission_mode"],
                            },
                          })
                        }
                      >
                        <option value="">{t("profiles.permission.default")}</option>
                        {permissionMode !== null && permissionMode !== "plan" ? (
                          <option value={permissionMode} disabled>
                            {permissionMode} —{" "}
                            {problemText("runtime.permission_mode", { permissionMode })}
                          </option>
                        ) : null}
                        <option value="plan">{t("profiles.permission.plan")}</option>
                      </select>
                    )}
                  </Field>
                </fieldset>
                <fieldset disabled={mutating || Boolean(definition?.archived)}>
                  <legend>{t("profiles.whatItCanUse")}</legend>
                  <Field label={t("profiles.field.vaults")} hint={t("profiles.hint.vaults")} group>
                    {(control) => (
                      <fieldset {...control}>
                        {document.workspace.allowed_workspace_ids.length === 0 ? (
                          <p>{t("profiles.workspace.anyVault")}</p>
                        ) : null}
                        {shownWorkspaces.map((workspace) => (
                          <label key={workspace.workspace_id}>
                            <input
                              type="checkbox"
                              disabled={mutating || Boolean(definition?.archived)}
                              checked={document.workspace.allowed_workspace_ids.includes(
                                workspace.workspace_id,
                              )}
                              onChange={(event) => {
                                const ids = document.workspace.allowed_workspace_ids;
                                setDocument({
                                  ...document,
                                  workspace: {
                                    allowed_workspace_ids: event.target.checked
                                      ? [...ids, workspace.workspace_id]
                                      : ids.filter((id) => id !== workspace.workspace_id),
                                  },
                                });
                              }}
                            />{" "}
                            {workspace.name}
                          </label>
                        ))}
                        {document.workspace.allowed_workspace_ids
                          .filter(
                            (id) =>
                              !shownWorkspaces.some((workspace) => workspace.workspace_id === id),
                          )
                          .map((id) => (
                            <label key={id}>
                              <input
                                type="checkbox"
                                disabled={mutating || Boolean(definition?.archived)}
                                checked
                                onChange={() =>
                                  setDocument({
                                    ...document,
                                    workspace: {
                                      allowed_workspace_ids:
                                        document.workspace.allowed_workspace_ids.filter(
                                          (allowedId) => allowedId !== id,
                                        ),
                                    },
                                  })
                                }
                              />{" "}
                              {`Unknown workspace (${id.slice(0, 8)})`}
                            </label>
                          ))}
                      </fieldset>
                    )}
                  </Field>
                  <Field label={t("profiles.field.mcp")} hint={t("profiles.hint.mcpSaved")} group>
                    {(control) => (
                      <McpServers
                        groupLabelledBy={control["aria-labelledby"]}
                        groupDescribedBy={control["aria-describedby"]}
                        key={`tools.mcp_configuration:${selectedId ?? newId}`}
                        value={document.tools.mcp_configuration}
                        onChange={(next) => {
                          setMcpFieldError(null);
                          setDocument({
                            ...document,
                            tools: { ...document.tools, mcp_configuration: next },
                          });
                        }}
                        disabled={mutating || Boolean(definition?.archived)}
                        error={mcpFieldError}
                        evaluation={executionFieldReason(
                          "tools.mcp_configuration",
                          preview,
                          permissionMode,
                        )}
                        {...(api.importMcpConfiguration
                          ? { onImport: api.importMcpConfiguration }
                          : {})}
                      />
                    )}
                  </Field>
                </fieldset>
                <button
                  type="submit"
                  disabled={mutating || Boolean(definition?.archived) || !(newId || dirty)}
                >
                  {newId ? "Create profile" : "Save revision"}
                </button>
                {mutationError ? <p role="alert">{mutationError}</p> : null}
              </form>
              <details className="profile-advanced">
                <summary>{t("profiles.advanced")}</summary>
                <p>Profile ID: {profileId}</p>
                <section aria-label="Provenance">
                  <h3>Provenance</h3>
                  <p>Kind: {document.provenance.kind}</p>
                  <pre>{json(document.provenance)}</pre>
                </section>
                {definition ? (
                  <>
                    <section aria-label="Revision">
                      <h3>Revision</h3>
                      <p>
                        Selected {definition.revision}
                        {"; current "}
                        {definition.current_revision}
                        {"; state "}
                        {definition.state_revision}
                      </p>
                      <p>{definition.archived ? "Archived" : "Active"}</p>
                    </section>
                    {dirty ? <p>Save or discard this edit first.</p> : null}
                    <button
                      type="button"
                      disabled={mutating || dirty}
                      onClick={() => void toggleArchived()}
                    >
                      {definition.archived ? "Unarchive" : "Archive"}
                    </button>
                    <button
                      type="button"
                      className="btn alt sm"
                      disabled={mutating || dirty}
                      onClick={() => void duplicate()}
                    >
                      {t("profiles.duplicateAsClaudeCode")}
                    </button>
                    <section aria-label="Compare revisions">
                      <h3>Compare revisions</h3>
                      <label>
                        Left revision{" "}
                        <input
                          type="number"
                          min="1"
                          value={leftRevision}
                          onChange={(event) => setLeftRevision(event.target.value)}
                        />
                      </label>
                      <label>
                        Right revision{" "}
                        <input
                          type="number"
                          min="1"
                          value={rightRevision}
                          onChange={(event) => setRightRevision(event.target.value)}
                        />
                      </label>
                      <button type="button" onClick={() => void compare()}>
                        Compare
                      </button>
                      {compareError ? <p role="alert">{compareError}</p> : null}
                      {comparison?.changes.length === 0 ? <p>No differences found.</p> : null}
                      {comparison?.changes.map((change) => (
                        <div key={change.field}>
                          <h4>{change.field}</h4>
                          <pre>{profileValue(change.field, change.before, modelText)}</pre>
                          <pre>{profileValue(change.field, change.after, modelText)}</pre>
                        </div>
                      ))}
                    </section>
                  </>
                ) : null}
              </details>
              {EXECUTION_FIELD_GROUPS.some((group) => group.isSet(document)) ? (
                <section
                  aria-label={t("profiles.cannotApplyChat")}
                  className="profile-cannot-apply"
                >
                  <h3>{t("profiles.cannotApplyChat")}</h3>
                  {EXECUTION_FIELD_GROUPS.filter((group) => group.isSet(document)).map((group) => (
                    <fieldset key={group.key}>
                      <legend>{t(group.labelKey)}</legend>
                      <p>{json(group.value(document))}</p>
                      {group.note ? <p>{group.note}</p> : null}
                      <p>{executionFieldReason(group.key, preview, permissionMode)}</p>
                      <button
                        type="button"
                        disabled={mutating || Boolean(definition?.archived)}
                        onClick={() => setDocument(group.clear(document))}
                      >
                        {t("profiles.clear")}
                      </button>
                    </fieldset>
                  ))}
                  <button
                    type="button"
                    className="btn alt sm"
                    disabled={mutating || Boolean(definition?.archived)}
                    onClick={() => setDocument(clearUnsupportedFields(document))}
                  >
                    {t("profiles.clearAll")}
                  </button>
                </section>
              ) : null}
              {definition ? (
                <section aria-label={t("profiles.tryChat")} className="profile-try-chat">
                  <h3>{t("profiles.tryChat")}</h3>
                  <p>
                    {tf("profiles.savedRuntimeLine", {
                      provider:
                        definition.document.runtime.provider === "claude"
                          ? t("profiles.runtime.claudeCode")
                          : definition.document.runtime.provider,
                      model: definition.document.runtime.model
                        ? modelText(definition.document.runtime.model)
                        : t("profiles.model.appDefault"),
                      effort: t(`profiles.effort.${definition.document.runtime.effort}`),
                      permission:
                        definition.document.runtime.permission_mode === null
                          ? t("profiles.permission.default")
                          : definition.document.runtime.permission_mode === "plan"
                            ? t("profiles.permission.plan")
                            : problemText("runtime.permission_mode", {
                                permissionMode: definition.document.runtime.permission_mode,
                              }),
                    })}
                  </p>
                  <button
                    type="button"
                    className="btn alt"
                    disabled={
                      preparing ||
                      evidencePhase !== "ready" ||
                      !launchApi ||
                      !workspaceId ||
                      dirty ||
                      definition.archived
                    }
                    onClick={() => void createDraft()}
                  >
                    {preparing ? t("profiles.tryChat.starting") : t("profiles.startChat")}
                  </button>
                  <div className="profile-chat-guidance">
                    {evidencePhase === "loading" ? (
                      <Loading>Loading provider evidence…</Loading>
                    ) : null}
                    {!preview ? <p>{t("profiles.tryChat.notEvaluated")}</p> : null}
                    {!launchApi ? <p>{t("profiles.tryChat.unavailable")}</p> : null}
                    {!workspaceId ? <p>{t("profiles.tryChat.workspaceRequired")}</p> : null}
                    {dirty ? <p>{t("profiles.tryChat.saveFirst")}</p> : null}
                    {definition.archived ? <p>{t("profiles.tryChat.archived")}</p> : null}
                    {previewError ? <p role="alert">{previewError}</p> : null}
                    {preview?.blockers.length ? (
                      <ul aria-label={t("profiles.tryChat.blockersLabel")}>
                        {preview.blockers.map((blocker) => {
                          const field = preview.fields.find((row) => row.field === blocker);
                          return (
                            <li key={blocker}>
                              {problemText(blocker, {
                                reason: field?.reason ?? null,
                                permissionMode:
                                  blocker === "runtime.permission_mode"
                                    ? definition.document.runtime.permission_mode
                                    : null,
                              })}
                            </li>
                          );
                        })}
                      </ul>
                    ) : null}
                  </div>
                  <details>
                    <summary>{t("profiles.technicalEvidence")}</summary>
                    {evidencePhase === "ready" && catalog && models ? (
                      <>
                        <p>
                          Provider evidence: {catalog.native_version ?? "No native version"}
                          {"; observed "}
                          {catalog.observed_at ?? "never"}.
                        </p>
                        <ul aria-label="Provider capabilities">
                          {catalog.capabilities.map((item) => (
                            <li key={item.id}>
                              {item.id}: {item.status}
                              {item.reason ? ` (${item.reason})` : ""}
                            </li>
                          ))}
                        </ul>
                        <p>
                          Models: {models.status};{" "}
                          {models.models.length
                            ? models.models.map((item) => item.label).join(", ")
                            : "none observed"}
                        </p>
                      </>
                    ) : null}
                    {preview ? (
                      <section aria-label="Effective profile fields">
                        <h4>Effective profile fields</h4>
                        <ul>
                          {preview.fields.map((item) => (
                            <li key={item.field}>
                              <strong>{item.field}</strong>: {item.status}; source {item.source}
                              {"; value "}
                              <code>
                                {profileValue(item.field, item.effective_value, modelText)}
                              </code>
                              {item.reason ? `; ${item.reason}` : ""}
                            </li>
                          ))}
                        </ul>
                        {preview.blockers.length ? (
                          <p>
                            {tf("profiles.tryChat.blockerFields", {
                              fields: preview.blockers.join(", "),
                            })}
                          </p>
                        ) : (
                          <p>No capability blockers.</p>
                        )}
                        {"session_id" in preview ? (
                          <p>Local draft created for session {preview.session_id}.</p>
                        ) : null}
                      </section>
                    ) : null}
                  </details>
                </section>
              ) : null}
            </>
          ) : null}
        </div>
      </div>
    </section>
  );
}
