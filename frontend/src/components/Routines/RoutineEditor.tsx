/**
 * The routine form: create, or revise with the routine's `current_revision`. Its checks mirror
 * `routines.parse_document`; the server stays the authority. A valid document is previewed 300 ms
 * after the last change. `initial` is read once, so a parent that edits another routine remounts
 * the editor with a `key`.
 */
import { type ReactElement, useEffect, useMemo, useRef, useState } from "react";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import {
  type RoutineDetailData,
  type RoutineDocument,
  type RoutinePreviewData,
  type RoutinesApi,
  RoutinesApiError,
} from "../../clients/automation/routinesApi";
import type { ProfileApi, ProfileSummary } from "../../clients/profiles/profileApi";
import { en, type Key } from "../../i18n";
import { t, tf } from "../../i18n/t";
import { cardSeat, changedFolder, type RoutinesTimers } from "../../state/routines";
import { useReportUnsavedWork } from "../../state/unsavedWork";
import { Loading } from "../Loading/Loading";
import { RoutinePreview } from "./RoutinePreview";
import "./RoutineEditor.css";

export interface RoutineEditorProps {
  api: RoutinesApi;
  initial: RoutineDetailData | null;
  profiles?: ProfileApi;
  workspaces: readonly WorkspaceJson[];
  activeWorkspaceId: string;
  /** New Bot routines keep their selected profile and workspace. Existing routines can move. */
  lockedOwner?: {
    botName: string;
    profileId: string;
    profileName: string;
    workspaceId: string;
    workspaceName: string;
  };
  /** Keep a changed-ownership draft visible without submitting it. */
  saveBlocked?: string;
  /** Keep the object stable between renders: a new one restarts the debounce. */
  timers?: RoutinesTimers;
  onSaved(routineId: string): void;
  onCancel(): void;
}

type Kind = RoutineDocument["schedule"]["kind"];
type Field =
  | "name"
  | "purpose"
  | "prompt"
  | "profile"
  | "workspace"
  | "deadline"
  | "times"
  | "days"
  | "timezone"
  | "start_date"
  | "end_date"
  | "seat"
  | "folder";

interface Form {
  name: string;
  purpose: string;
  prompt: string;
  profileId: string;
  workspaceId: string;
  deadline: string;
  kind: Kind;
  times: string[];
  days: number[];
  timezone: string;
  startDate: string;
  endDate: string;
  runIf: "always" | "card" | "changed";
  seat: string;
  folder: string;
  stop: boolean;
}

const PREVIEW_DELAY_MS = 300;
const MAX_TIMES = 4;
const KINDS: readonly Kind[] = ["manual", "daily", "weekly"];
const WEEKDAYS = [1, 2, 3, 4, 5, 6, 7] as const;
const TIME_PATTERN = /^(?:[01]\d|2[0-3]):[0-5]\d$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const SEAT_PATTERN = /^[a-z][a-z0-9-]{0,31}$/;
const CONTROL_CHARACTER = /\p{Cc}/u;
const RUN_IFS = ["always", "card", "changed"] as const;

const windowTimers: RoutinesTimers = {
  set: (callback, ms) => window.setTimeout(callback, ms),
  clear: (handle) => window.clearTimeout(handle),
};

const browserZone = (): string => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const byteLength = (value: string): number => new TextEncoder().encode(value).length;
const isTime = (value: string): boolean => TIME_PATTERN.test(value);

function startingForm(initial: RoutineDetailData | null, workspaceId: string): Form {
  if (!initial) {
    return {
      name: "",
      purpose: "",
      prompt: "",
      profileId: "",
      workspaceId,
      deadline: "30",
      kind: "daily",
      times: ["09:00"],
      days: [],
      timezone: browserZone(),
      startDate: "",
      endDate: "",
      runIf: "always",
      seat: "",
      folder: "",
      stop: false,
    };
  }
  const saved = initial.document;
  const { schedule } = saved;
  return {
    name: saved.name,
    purpose: saved.purpose,
    prompt: saved.prompt,
    profileId: saved.profile_id,
    workspaceId: saved.workspace_id,
    deadline: String(saved.deadline_minutes),
    kind: schedule.kind,
    // A manual routine keeps no times; switching it to daily starts from the default one.
    times: schedule.kind === "manual" ? ["09:00"] : [...schedule.times],
    days: [...schedule.days],
    timezone: schedule.timezone ?? browserZone(),
    startDate: schedule.start_date ?? "",
    endDate: schedule.end_date ?? "",
    runIf: saved.run_only_if?.kind ?? "always",
    seat: cardSeat(saved) ?? "",
    folder: changedFolder(saved) ?? "",
    stop: saved.on_confirmation === "stop",
  };
}

/** What the server is sent: sorted, unique times and days; fields a kind does not use are cleared. */
function toDocument(form: Form): RoutineDocument {
  const manual = form.kind === "manual";
  return {
    name: form.name,
    purpose: form.purpose,
    prompt: form.prompt,
    profile_id: form.profileId,
    workspace_id: form.workspaceId,
    deadline_minutes: /^\d+$/.test(form.deadline) ? Number(form.deadline) : Number.NaN,
    schedule: {
      kind: form.kind,
      times: manual ? [] : [...new Set(form.times)].sort(),
      days: form.kind === "weekly" ? [...new Set(form.days)].sort((a, b) => a - b) : [],
      timezone: manual ? null : form.timezone,
      start_date: manual || !form.startDate ? null : form.startDate,
      end_date: manual || !form.endDate ? null : form.endDate,
      catch_up: "skip",
    },
    ...(manual ? {} : runOnlyIf(form)),
    ...(form.stop ? { on_confirmation: "stop" as const } : {}),
  };
}

/** The scheduled-run condition the form picks; a folder loses its outer spaces and slashes. */
function runOnlyIf(form: Form): Pick<RoutineDocument, "run_only_if"> {
  if (form.runIf === "card") return { run_only_if: { kind: "card", seat: form.seat } };
  if (form.runIf === "changed") {
    const folder = form.folder.trim().replace(/^\/+|\/+$/g, "");
    return { run_only_if: { kind: "changed", folder } };
  }
  return {};
}

/** As `routines._folder`: 1 to 255 characters, no empty or hidden part, no backslash, colon or
 * control character. */
function validFolder(folder: string): boolean {
  const length = [...folder].length;
  return (
    length >= 1 &&
    length <= 255 &&
    !/[\\:]/.test(folder) &&
    !CONTROL_CHARACTER.test(folder) &&
    folder.split("/").every((part) => part !== "" && !part.startsWith("."))
  );
}

/** The first field `parse_document` would refuse; lengths count characters, the prompt bytes. */
function invalidField(doc: RoutineDocument): Field | null {
  const { schedule } = doc;
  const inRange = (value: number, low: number, high: number): boolean =>
    value >= low && value <= high;
  if (!inRange([...doc.name].length, 1, 80) || CONTROL_CHARACTER.test(doc.name)) return "name";
  if ([...doc.purpose].length > 1000) return "purpose";
  if (doc.prompt.includes("\0") || !inRange(byteLength(doc.prompt), 1, 32_768)) return "prompt";
  if (!doc.profile_id) return "profile";
  if (!doc.workspace_id) return "workspace";
  if (!Number.isInteger(doc.deadline_minutes) || !inRange(doc.deadline_minutes, 5, 240))
    return "deadline";
  if (schedule.kind !== "manual") {
    if (!inRange(schedule.times.length, 1, MAX_TIMES) || !schedule.times.every(isTime))
      return "times";
    if (schedule.kind === "weekly" && schedule.days.length === 0) return "days";
    if (!schedule.timezone) return "timezone";
    if (schedule.start_date && !DATE_PATTERN.test(schedule.start_date)) return "start_date";
    if (schedule.end_date && !DATE_PATTERN.test(schedule.end_date)) return "end_date";
    if (schedule.start_date && schedule.end_date && schedule.end_date < schedule.start_date)
      return "end_date";
  }
  const condition = doc.run_only_if;
  if (condition?.kind === "card" && !SEAT_PATTERN.test(condition.seat)) return "seat";
  if (condition?.kind === "changed" && !validFolder(condition.folder)) return "folder";
  return null;
}

function runIfText(doc: RoutineDocument): string {
  const condition = doc.run_only_if;
  if (condition?.kind === "card") return `${t("routines.editor.run_if_card")}: ${condition.seat}`;
  if (condition?.kind === "changed")
    return `${t("routines.editor.run_if_changed")}: ${condition.folder}`;
  return t("routines.editor.run_if_always");
}

function errorText(error: unknown): string {
  const code = error instanceof RoutinesApiError ? error.code : "generic";
  const key = `routines.error.${code}`;
  return Object.hasOwn(en, key) ? t(key as Key) : t("routines.error.generic");
}

function zoneChoices(current: string): string[] {
  const zones = Intl.supportedValuesOf("timeZone");
  return zones.includes(current) ? zones : [current, ...zones];
}

export function RoutineEditor({
  api,
  initial,
  profiles,
  workspaces,
  activeWorkspaceId,
  lockedOwner,
  saveBlocked,
  timers = windowTimers,
  onSaved,
  onCancel,
}: RoutineEditorProps): ReactElement {
  const owner = initial ? undefined : lockedOwner;
  const offered = workspaces.filter(
    (workspace) => workspace.kind === "vault" && workspace.persistent,
  );
  const workspaceChoices = owner
    ? [{ id: owner.workspaceId, name: owner.workspaceName }]
    : offered.map((workspace) => ({
        id: workspace.workspace_id,
        name: workspace.name,
      }));
  if (initial && !workspaceChoices.some((choice) => choice.id === initial.document.workspace_id))
    workspaceChoices.push({
      id: initial.document.workspace_id,
      name: initial.resolved.workspace?.name ?? initial.document.workspace_id,
    });

  const defaultWorkspaceId = offered.some(
    (workspace) => workspace.workspace_id === activeWorkspaceId,
  )
    ? activeWorkspaceId
    : (offered[0]?.workspace_id ?? "");
  const [form, setForm] = useState<Form>(() => ({
    ...startingForm(initial, owner?.workspaceId ?? defaultWorkspaceId),
    ...(owner ? { profileId: owner.profileId } : {}),
  }));
  const [baseline, setBaseline] = useState<Form>(form);
  const [profileRows, setProfileRows] = useState<ProfileSummary[] | null>(
    profiles && !owner ? null : [],
  );
  const ownerProfile = owner?.profileId;
  const ownerWorkspace = owner?.workspaceId;
  const doc = useMemo(() => {
    const document = toDocument(form);
    return ownerProfile && ownerWorkspace
      ? { ...document, profile_id: ownerProfile, workspace_id: ownerWorkspace }
      : document;
  }, [form, ownerProfile, ownerWorkspace]);
  const [previewAttempt, setPreviewAttempt] = useState(0);
  const [preview, setPreview] = useState<{
    data: RoutinePreviewData;
    document: RoutineDocument;
    attempt: number;
  } | null>(initial ? { data: initial, document: doc, attempt: 0 } : null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [attempted, setAttempted] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [reviewed, setReviewed] = useState<RoutineDetailData | null>(null);
  const command = useRef<{ key: string; id: string } | null>(null);
  const pending = useRef(false);
  const reloadRequest = useRef<AbortController | null>(null);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      reloadRequest.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (!profiles || ownerProfile) return;
    const controller = new AbortController();
    profiles.list(false, controller.signal).then(
      (rows) => {
        if (!controller.signal.aborted) setProfileRows(rows.filter((row) => !row.archived));
      },
      () => {
        if (!controller.signal.aborted) setProfileRows([]);
      },
    );
    return () => controller.abort();
  }, [profiles, ownerProfile]);

  // A new routine starts on the first profile; the baseline follows so that is not an edit.
  useEffect(() => {
    const first = profileRows?.[0]?.profile_id;
    if (!first) return;
    const adopt = (value: Form): Form => (value.profileId ? value : { ...value, profileId: first });
    setForm(adopt);
    setBaseline(adopt);
  }, [profileRows]);

  // A new editor can open before workspace discovery finishes. Fill only an empty workspace;
  // existing routines, locked Bot owners and an explicit selection keep their own workspace.
  useEffect(() => {
    if (initial || owner || !defaultWorkspaceId) return;
    const adopt = (value: Form): Form =>
      value.workspaceId ? value : { ...value, workspaceId: defaultWorkspaceId };
    setForm(adopt);
    setBaseline(adopt);
  }, [initial, owner, defaultWorkspaceId]);

  const invalid = invalidField(doc);

  useEffect(() => {
    if (invalid) {
      setPreview(null);
      setPreviewError(null);
      return;
    }
    const controller = new AbortController();
    setPreviewError(null);
    const handle = timers.set(() => {
      api.preview(doc, controller.signal).then(
        (data) => {
          if (controller.signal.aborted) return;
          setPreview({ data, document: doc, attempt: previewAttempt });
          setPreviewError(null);
        },
        (error) => {
          if (!controller.signal.aborted) setPreviewError(errorText(error));
        },
      );
    }, PREVIEW_DELAY_MS);
    return () => {
      timers.clear(handle);
      controller.abort();
    };
  }, [api, timers, doc, invalid, previewAttempt]);

  const dirty = JSON.stringify(form) !== JSON.stringify(baseline);
  useReportUnsavedWork("routine", dirty);

  const profileChoices = owner
    ? [{ id: owner.profileId, name: owner.profileName }]
    : (profileRows ?? []).map((row) => ({
        id: row.profile_id,
        name: row.identity.name,
      }));
  if (initial && !profileChoices.some((choice) => choice.id === initial.document.profile_id))
    profileChoices.push({
      id: initial.document.profile_id,
      name: initial.resolved.profile?.name ?? initial.document.profile_id,
    });
  const noProfiles = profileRows !== null && profileChoices.length === 0;
  const zones = useMemo(() => zoneChoices(form.timezone), [form.timezone]);

  function change(patch: Partial<Form>): void {
    setForm((value) => ({
      ...value,
      ...patch,
      ...(owner ? { profileId: owner.profileId, workspaceId: owner.workspaceId } : {}),
    }));
  }

  function setTime(index: number, value: string): void {
    change({ times: form.times.map((time, at) => (at === index ? value : time)) });
  }

  function toggleDay(day: number, on: boolean): void {
    change({ days: on ? [...form.days, day] : form.days.filter((item) => item !== day) });
  }

  async function save(): Promise<void> {
    setAttempted(true);
    if (invalid || noProfiles || pending.current || conflict || saveBlocked) return;
    const revision = reviewed?.current_revision ?? initial?.current_revision;
    // The id is reused while the request is unchanged, so a retry after a failure is the same
    // command; any edit, or a success, makes the next one new.
    const key = JSON.stringify([initial?.routine_id ?? null, revision, doc]);
    if (command.current?.key !== key) command.current = { key, id: crypto.randomUUID() };
    const commandId = command.current.id;
    pending.current = true;
    setSaving(true);
    setSaveError(null);
    try {
      const result = initial
        ? await api.revise(initial.routine_id, commandId, revision ?? initial.current_revision, doc)
        : await api.create(commandId, doc);
      if (!alive.current) return;
      command.current = null;
      setBaseline(form);
      onSaved(result.routine_id);
    } catch (error) {
      if (alive.current) {
        setSaveError(errorText(error));
        if (initial && error instanceof RoutinesApiError && error.code === "conflict") {
          setConflict(true);
          setReviewed(null);
        }
      }
    } finally {
      pending.current = false;
      if (alive.current) setSaving(false);
    }
  }

  async function reloadCurrent(): Promise<void> {
    if (!initial || pending.current) return;
    pending.current = true;
    const controller = new AbortController();
    reloadRequest.current = controller;
    setReloading(true);
    setSaveError(null);
    try {
      const current = await api.get(initial.routine_id, controller.signal);
      if (!alive.current || controller.signal.aborted) return;
      setReviewed(current);
      setConflict(false);
      command.current = null;
    } catch (error) {
      if (alive.current && !controller.signal.aborted) setSaveError(errorText(error));
    } finally {
      pending.current = false;
      if (alive.current) setReloading(false);
    }
  }

  const savedFields = reviewed
    ? [
        [t("routines.editor.name"), reviewed.document.name],
        [t("routines.editor.purpose"), reviewed.document.purpose],
        [t("routines.editor.prompt"), reviewed.document.prompt],
        [t("routines.editor.profile"), reviewed.document.profile_id],
        [t("routines.editor.workspace"), reviewed.document.workspace_id],
        [t("routines.editor.deadline"), String(reviewed.document.deadline_minutes)],
        [
          t("routines.editor.schedule"),
          t(`routines.editor.kind_${reviewed.document.schedule.kind}`),
        ],
        [t("routines.editor.times"), reviewed.document.schedule.times.join(", ")],
        [
          t("routines.editor.days"),
          reviewed.document.schedule.days
            .map((day) => t(`routines.editor.day_${day}` as Key))
            .join(", "),
        ],
        [t("routines.editor.timezone"), reviewed.document.schedule.timezone ?? ""],
        [t("routines.editor.start_date"), reviewed.document.schedule.start_date ?? ""],
        [t("routines.editor.end_date"), reviewed.document.schedule.end_date ?? ""],
        [t("routines.editor.run_if"), runIfText(reviewed.document)],
        [
          t("routines.editor.on_confirmation"),
          t(
            reviewed.document.on_confirmation === "stop"
              ? "routines.editor.on_confirmation_stop"
              : "routines.editor.on_confirmation_wait",
          ),
        ],
      ]
    : [];

  const title = t(initial ? "routines.editor.edit" : "routines.editor.create");
  return (
    <section className="routine-editor" aria-label={title}>
      <h2>{title}</h2>
      {saveBlocked ? <p role="alert">{saveBlocked}</p> : null}
      {owner ? <p className="routine-editor-note">{`Bot owner: ${owner.botName}`}</p> : null}
      {initial?.state === "tested" ? (
        <p className="routine-editor-note">{t("routines.editor.retest")}</p>
      ) : null}
      {initial?.state === "enabled" ? (
        <p className="routine-editor-note">{t("routines.editor.enabled_note")}</p>
      ) : null}
      {noProfiles ? <p role="alert">{t("routines.editor.no_profiles")}</p> : null}
      <form
        className="routine-editor-form"
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <fieldset className="routine-editor-fields" disabled={saving}>
          <label className="routine-editor-field">
            <span className="lbl">{t("routines.editor.name")}</span>
            <input value={form.name} onChange={(event) => change({ name: event.target.value })} />
          </label>
          <label className="routine-editor-field">
            <span className="lbl">{t("routines.editor.purpose")}</span>
            <input
              value={form.purpose}
              onChange={(event) => change({ purpose: event.target.value })}
            />
          </label>
          <label className="routine-editor-field">
            <span className="lbl">{t("routines.editor.prompt")}</span>
            <textarea
              rows={6}
              value={form.prompt}
              onChange={(event) => change({ prompt: event.target.value })}
            />
          </label>
          <label className="routine-editor-field">
            <span className="lbl">{t("routines.editor.profile")}</span>
            <select
              value={form.profileId}
              disabled={owner !== undefined}
              onChange={(event) => change({ profileId: event.target.value })}
            >
              {profileChoices.map((choice) => (
                <option key={choice.id} value={choice.id}>
                  {choice.name}
                </option>
              ))}
            </select>
          </label>
          <label className="routine-editor-field">
            <span className="lbl">{t("routines.editor.workspace")}</span>
            <select
              value={form.workspaceId}
              disabled={owner !== undefined}
              onChange={(event) => change({ workspaceId: event.target.value })}
            >
              {workspaceChoices.map((choice) => (
                <option key={choice.id} value={choice.id}>
                  {choice.name}
                </option>
              ))}
            </select>
          </label>
          <label className="routine-editor-field">
            <span className="lbl">{t("routines.editor.deadline")}</span>
            <input
              type="number"
              inputMode="numeric"
              value={form.deadline}
              onChange={(event) => change({ deadline: event.target.value })}
            />
          </label>
          <fieldset className="routine-editor-group">
            <legend className="lbl">{t("routines.editor.schedule")}</legend>
            <div className="routine-editor-kinds">
              {KINDS.map((kind) => (
                <label key={kind}>
                  <input
                    type="radio"
                    name="routine-kind"
                    checked={form.kind === kind}
                    onChange={() => change({ kind })}
                  />{" "}
                  {t(`routines.editor.kind_${kind}`)}
                </label>
              ))}
            </div>
            {form.kind === "manual" ? null : (
              <>
                <div className="routine-editor-field">
                  <span className="lbl">{t("routines.editor.times")}</span>
                  {form.times.map((time, index) => (
                    // biome-ignore lint/suspicious/noArrayIndexKey: rows have no identity beyond their place
                    <div className="routine-editor-time" key={index}>
                      <input
                        type="time"
                        aria-label={`${t("routines.editor.times")} ${index + 1}`}
                        value={time}
                        onChange={(event) => setTime(index, event.target.value)}
                      />
                      <button
                        type="button"
                        className="btn txt sm"
                        disabled={form.times.length === 1}
                        onClick={() =>
                          change({ times: form.times.filter((_, at) => at !== index) })
                        }
                      >
                        {t("routines.editor.remove_time")}
                      </button>
                    </div>
                  ))}
                  <button
                    type="button"
                    className="btn alt sm routine-editor-add"
                    disabled={form.times.length >= MAX_TIMES}
                    onClick={() => change({ times: [...form.times, ""] })}
                  >
                    {t("routines.editor.add_time")}
                  </button>
                </div>
                {form.kind === "weekly" ? (
                  <fieldset className="routine-editor-days">
                    <legend className="lbl">{t("routines.editor.days")}</legend>
                    {WEEKDAYS.map((day) => (
                      <label key={day}>
                        <input
                          type="checkbox"
                          checked={form.days.includes(day)}
                          onChange={(event) => toggleDay(day, event.target.checked)}
                        />{" "}
                        {t(`routines.editor.day_${day}`)}
                      </label>
                    ))}
                  </fieldset>
                ) : null}
                <label className="routine-editor-field">
                  <span className="lbl">{t("routines.editor.timezone")}</span>
                  <select
                    value={form.timezone}
                    onChange={(event) => change({ timezone: event.target.value })}
                  >
                    {zones.map((zone) => (
                      <option key={zone} value={zone}>
                        {zone}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="routine-editor-field">
                  <span className="lbl">{t("routines.editor.start_date")}</span>
                  <input
                    type="date"
                    value={form.startDate}
                    onChange={(event) => change({ startDate: event.target.value })}
                  />
                </label>
                <label className="routine-editor-field">
                  <span className="lbl">{t("routines.editor.end_date")}</span>
                  <input
                    type="date"
                    value={form.endDate}
                    onChange={(event) => change({ endDate: event.target.value })}
                  />
                </label>
                <p className="routine-editor-note">{t("routines.editor.catch_up")}</p>
                <fieldset className="routine-editor-group">
                  <legend className="lbl">{t("routines.editor.run_if")}</legend>
                  <div className="routine-editor-kinds">
                    {RUN_IFS.map((runIf) => (
                      <label key={runIf}>
                        <input
                          type="radio"
                          name="routine-run-if"
                          checked={form.runIf === runIf}
                          onChange={() => change({ runIf })}
                        />{" "}
                        {t(`routines.editor.run_if_${runIf}`)}
                      </label>
                    ))}
                  </div>
                  {form.runIf === "card" ? (
                    <>
                      <label className="routine-editor-field">
                        <span className="lbl">{t("routines.editor.seat")}</span>
                        <input
                          value={form.seat}
                          onChange={(event) => change({ seat: event.target.value })}
                        />
                      </label>
                      <p className="routine-editor-note">{t("routines.editor.seat_note")}</p>
                    </>
                  ) : null}
                  {form.runIf === "changed" ? (
                    <>
                      <label className="routine-editor-field">
                        <span className="lbl">{t("routines.editor.folder")}</span>
                        <input
                          value={form.folder}
                          placeholder="00-inbox"
                          onChange={(event) => change({ folder: event.target.value })}
                        />
                      </label>
                      <p className="routine-editor-note">{t("routines.editor.folder_note")}</p>
                    </>
                  ) : null}
                </fieldset>
              </>
            )}
          </fieldset>
          <fieldset className="routine-editor-group">
            <legend className="lbl">{t("routines.editor.on_confirmation")}</legend>
            <div className="routine-editor-kinds">
              {([false, true] as const).map((stop) => (
                <label key={String(stop)}>
                  <input
                    type="radio"
                    name="routine-on-confirmation"
                    checked={form.stop === stop}
                    onChange={() => change({ stop })}
                  />{" "}
                  {t(`routines.editor.on_confirmation_${stop ? "stop" : "wait"}`)}
                </label>
              ))}
            </div>
            <p className="routine-editor-note">{t("routines.editor.stop_note")}</p>
          </fieldset>
        </fieldset>
        {attempted && invalid ? (
          <p role="alert">
            {tf("routines.editor.invalid", { field: t(`routines.editor.${invalid}`) })}
          </p>
        ) : null}
        {saveError ? <p role="alert">{saveError}</p> : null}
        {conflict ? (
          <button
            type="button"
            className="btn alt"
            disabled={saving || reloading}
            onClick={() => void reloadCurrent()}
          >
            Reload current revision
          </button>
        ) : null}
        {reloading ? <Loading>Loading current revision…</Loading> : null}
        {reviewed ? (
          <section aria-label="Current saved routine">
            <h3>Current saved revision {reviewed.current_revision}</h3>
            <p>Your draft is unchanged. Review this version before saving your draft over it.</p>
            <dl>
              {savedFields.map(([label, value]) => (
                <div key={label}>
                  <dt className="lbl">{label}</dt>
                  <dd style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                    {value || "None"}
                  </dd>
                </div>
              ))}
            </dl>
          </section>
        ) : null}
        <div className="routine-editor-actions">
          <button
            type="submit"
            className="btn go"
            disabled={saving || reloading || conflict || noProfiles || Boolean(saveBlocked)}
          >
            {reviewed && !saving
              ? `Save draft against revision ${reviewed.current_revision}`
              : t(saving ? "routines.editor.saving" : "routines.editor.save")}
          </button>
          <button type="button" className="btn alt" onClick={onCancel}>
            {t("routines.editor.cancel")}
          </button>
        </div>
      </form>
      {previewError ? (
        <>
          <p role="alert">{previewError}</p>
          <button
            type="button"
            className="btn alt"
            disabled={invalid !== null}
            onClick={() => setPreviewAttempt((attempt) => attempt + 1)}
          >
            Retry rehearsal
          </button>
        </>
      ) : null}
      {preview ? (
        <RoutinePreview
          preview={preview.data}
          kind={preview.document.schedule.kind}
          cardSeat={cardSeat(preview.document)}
          changedFolder={changedFolder(preview.document)}
          stopAtDraft={preview.document.on_confirmation === "stop"}
          stale={
            preview.document !== doc || preview.attempt !== previewAttempt || previewError !== null
          }
        />
      ) : null}
    </section>
  );
}
