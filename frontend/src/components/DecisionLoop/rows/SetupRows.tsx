/**
 * The Decision page's setup rows: Decision model and What leaves this computer — 2 of the page's
 * rows (Proactive VIKI is the card in `ProactiveCard.tsx`). Built from the row kit
 * (`DecisionRow`, `Choice`) and the `dloop-` classes in `../DecisionLoopPage.css`.
 *
 * Two sources feed every row: `page` (the Decision loop's own `GET page` view, typed, refreshed
 * on demand by `onRefresh`) and `values` (the settings document's effective values, `unknown`
 * per key, kept fresh by the caller's own settings subscription once `patch` resolves). A field
 * `page` mirrors (the model's version, the daily cap) is always read from `page`, since it is
 * already typed and `onRefresh` is how it catches up after a change; a field
 * `page` does not carry (send_level, never_send) is read from `values`, and a change to it needs
 * no `onRefresh` — `values` updates on its own once `patch` lands. `patch` is the only way to
 * write any of these: `DecisionLoopApi` has no dedicated route for them.
 *
 * Each row defaults open, unlike Messaging's collapsed-by-default provider rows: the key
 * field and each row's controls are there to see (and to query in a test) without a click
 * first. `open`/`onToggle` let the Decision page control that
 * disclosure instead — the page owns one open record for all its rows — while a row mounted
 * alone, as every test below does, keeps its own state and this same open-by-default behaviour
 * (`useRowOpen`, `../DecisionRow`).
 */
import { useState } from "react";
import type {
  DecisionLoopApi,
  ModelErrorCode,
  PageView,
  ShadowSetting,
  VersionSetting,
} from "../../../clients/decisionLoop/types";
import { t, tf } from "../../../i18n/t";
import { Choice, type ChoiceOption, DecisionRow, useRowOpen } from "../DecisionRow";
import {
  DAILY_CAP_OPTIONS,
  formatMoney,
  formatNumber,
  readShadowSetting,
  SHADOW_OPTIONS,
} from "../wording";
import { ShadowPart } from "./LearningParts";

export interface DecisionRowsProps {
  page: PageView;
  values: Readonly<Record<string, unknown>>;
  writable: boolean;
  patch: (changes: Record<string, unknown>) => Promise<unknown>;
  api: DecisionLoopApi;
  onRefresh: () => void;
  /** Controls this row's disclosure from the page; left undefined, the row keeps its own
   * open/closed state, defaulting open. */
  open?: boolean;
  onToggle?: () => void;
}

export interface SetupRowsProps extends DecisionRowsProps {
  onOpenUrl: (url: string) => void;
  onOpenContext: () => void;
}

/** A disclosure line pairing a short confirm question with its Apply-shaped action; ports
 * VaultBrowser/RecoveryPanel.tsx's own confirm pattern under `.dloop-confirm`. Exported
 * so `rows/LearningRows.tsx`'s own Reset confirmation
 * reuses it rather than re-implementing the same markup. */
export function ConfirmLine({
  text,
  confirmLabel,
  busy,
  onConfirm,
  onCancel,
}: {
  text: string;
  confirmLabel: string;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <span className="dloop-confirm">
      <span>{text}</span>
      <button type="button" disabled={busy} onClick={onConfirm}>
        {confirmLabel}
      </button>
      <button type="button" disabled={busy} onClick={onCancel}>
        Cancel
      </button>
    </span>
  );
}

/** The Test button's result and `page.model.last_error` share this mapping. */
function modelErrorText(code: ModelErrorCode | null): string {
  switch (code) {
    case "key_rejected":
      return "TypeSafe refused the key.";
    case "unreachable":
      return "TypeSafe could not be reached.";
    case "rate_limited":
      return "TypeSafe asked VIKI to slow down.";
    case "overloaded":
      return "TypeSafe is busy. Try again later.";
    case "no_key":
      return "Save a key first.";
    default:
      return "The test failed.";
  }
}

/** "Decision model": TypeSafe, the key field (never filled from the server), the Test button,
 * Spending (daily cap and today's use) and, under Advanced, the version choice and the
 * comparison version. */
export function ModelRow({
  page,
  values,
  writable,
  patch,
  api,
  onOpenUrl,
  onRefresh,
  open: openProp,
  onToggle: onToggleProp,
}: SetupRowsProps) {
  const [open, toggle] = useRowOpen(true, openProp, onToggleProp);
  const [keyInput, setKeyInput] = useState("");
  const [replacing, setReplacing] = useState(false);
  const [keyBusy, setKeyBusy] = useState(false);
  const [keyError, setKeyError] = useState<string | null>(null);
  const [removeConfirming, setRemoveConfirming] = useState(false);
  const [versionBusy, setVersionBusy] = useState(false);
  const [versionError, setVersionError] = useState<string | null>(null);
  const [capBusy, setCapBusy] = useState(false);
  const [capError, setCapError] = useState<string | null>(null);
  const [shadowBusy, setShadowBusy] = useState(false);
  const [shadowError, setShadowError] = useState<string | null>(null);
  const [testBusy, setTestBusy] = useState(false);
  const [testText, setTestText] = useState<string | null>(null);

  const model = page.model;
  const shadow = readShadowSetting(values);
  const dailyCap = DAILY_CAP_OPTIONS.find((option) => option === page.today.cap_usd) ?? 0.5;
  const showKeyInput = model.key === "missing" || replacing;

  async function handleSave(): Promise<void> {
    const key = keyInput.trim();
    if (key === "" || !writable) return;
    setKeyBusy(true);
    setKeyError(null);
    try {
      await api.saveKey(key);
      setKeyInput("");
      setReplacing(false);
      onRefresh();
    } catch {
      setKeyError("Could not save the key. Try again.");
    } finally {
      setKeyBusy(false);
    }
  }

  async function handleRemove(): Promise<void> {
    if (!writable) return;
    setKeyBusy(true);
    setKeyError(null);
    try {
      await api.removeKey();
      setRemoveConfirming(false);
      onRefresh();
    } catch {
      setKeyError("Could not remove the key. Try again.");
    } finally {
      setKeyBusy(false);
    }
  }

  async function handleVersionChange(next: VersionSetting): Promise<void> {
    // `Choice` disables its own radios via the `disabled` prop below; this guard is kept
    // as defence in depth for a caller that renders the row some other way.
    if (!writable) return;
    setVersionBusy(true);
    setVersionError(null);
    try {
      await patch({ "decision.model_version": next });
      onRefresh();
    } catch {
      setVersionError("Could not save. Try again.");
    } finally {
      setVersionBusy(false);
    }
  }

  async function handleCapChange(next: number): Promise<void> {
    if (!writable) return;
    setCapBusy(true);
    setCapError(null);
    try {
      await patch({ "decision.daily_cap_usd": next });
      onRefresh();
    } catch {
      setCapError("Could not save that change. Try again.");
    } finally {
      setCapBusy(false);
    }
  }

  async function handleShadowChange(next: ShadowSetting): Promise<void> {
    if (!writable) return;
    setShadowBusy(true);
    setShadowError(null);
    try {
      await patch({ "decision.shadow": next });
      onRefresh();
    } catch {
      setShadowError("Could not save. Try again.");
    } finally {
      setShadowBusy(false);
    }
  }

  async function handleTest(): Promise<void> {
    if (!writable) return;
    setTestBusy(true);
    try {
      const result = await api.test();
      setTestText(
        result.ok ? `Answered in ${result.latency_ms ?? 0} ms` : modelErrorText(result.error),
      );
    } catch {
      setTestText(modelErrorText(null));
    } finally {
      setTestBusy(false);
      onRefresh();
    }
  }

  const versionOptions: readonly ChoiceOption<VersionSetting>[] = [
    {
      value: "pinned",
      label: `Pinned ${model.version.display}`,
      description: t("decision_page.model.version.pinned"),
    },
    {
      value: "latest",
      label: "Latest stable",
      description: t("decision_page.model.version.latest"),
    },
    {
      value: "preview",
      label: "Preview",
      description: t("decision_page.model.version.preview"),
    },
  ];

  return (
    <DecisionRow
      id="model"
      name="Decision model"
      description={t("decision_page.model.description")}
      status={t(`decision_page.model.state.${model.state}`)}
      open={open}
      onToggle={toggle}
      control={null}
    >
      <p>
        TypeSafe{" "}
        <button
          type="button"
          className="btn txt"
          disabled={!writable}
          onClick={() => onOpenUrl("https://typesafe.ai")}
        >
          Get a key
        </button>
      </p>
      {showKeyInput ? (
        <div className="dloop-field">
          <label htmlFor="model-key">Key</label>
          <input
            id="model-key"
            type="password"
            autoComplete="off"
            value={keyInput}
            disabled={!writable || keyBusy}
            onChange={(event) => setKeyInput(event.target.value)}
          />
          <div className="dloop-button-row">
            <button
              type="button"
              className="btn go"
              disabled={!writable || keyBusy || keyInput.trim() === ""}
              onClick={() => void handleSave()}
            >
              Save
            </button>
          </div>
        </div>
      ) : (
        <div className="dloop-button-row">
          <p>A key is saved.</p>
          {!removeConfirming ? (
            <>
              <button
                type="button"
                className="btn alt"
                disabled={!writable || keyBusy}
                onClick={() => setReplacing(true)}
              >
                Replace
              </button>
              <button
                type="button"
                className="btn txt"
                disabled={!writable || keyBusy}
                onClick={() => setRemoveConfirming(true)}
              >
                Remove
              </button>
            </>
          ) : null}
        </div>
      )}
      {removeConfirming ? (
        <ConfirmLine
          text="Remove the saved key? VIKI will fall back to rules until you save a new one."
          confirmLabel="Remove"
          busy={keyBusy}
          onConfirm={() => void handleRemove()}
          onCancel={() => setRemoveConfirming(false)}
        />
      ) : null}
      {keyError ? (
        <p className="dloop-hint" role="status">
          {keyError}
        </p>
      ) : null}
      <div className="dloop-button-row">
        <button
          type="button"
          className="btn alt"
          disabled={!writable || testBusy}
          onClick={() => void handleTest()}
        >
          Test
        </button>
        {testText ? <span role="status">{testText}</span> : null}
      </div>
      {model.last_error ? (
        <p className="dloop-hint">Last error: {modelErrorText(model.last_error)}</p>
      ) : null}
      <h4 className="dloop-subhead">{t("decision_page.model.spending")}</h4>
      <label className="dloop-field">
        <span className="lbl">Daily cap</span>
        <select
          value={String(dailyCap)}
          aria-describedby="model-cap-hint"
          disabled={!writable || capBusy}
          onChange={(event) => void handleCapChange(Number(event.target.value))}
        >
          {DAILY_CAP_OPTIONS.map((capValue) => (
            <option key={capValue} value={String(capValue)}>
              {formatMoney(capValue)}
            </option>
          ))}
        </select>
      </label>
      <p id="model-cap-hint" className="dloop-hint">
        {t("decision_page.model.cap_hint")}
      </p>
      {capError ? (
        <p className="dloop-hint" role="status">
          {capError}
        </p>
      ) : null}
      <p className="dloop-hint">
        {tf(
          page.today.shadow_calls > 0
            ? "decision_page.model.today_compare"
            : "decision_page.model.today",
          {
            decisions: formatNumber(page.today.decisions),
            requests: formatNumber(page.today.model_calls),
            comparison: formatNumber(page.today.shadow_calls),
            cost: formatMoney(page.today.cost_usd),
          },
        )}
      </p>
      <p className="dloop-hint">{t("decision_page.model.rate")}</p>
      {page.today.capped ? <p className="dloop-hint">{t("decision_page.model.capped")}</p> : null}
      <details className="dloop-details">
        <summary>{t("decision_page.model.advanced")}</summary>
        <Choice
          label="Version"
          options={versionOptions}
          value={model.version.setting}
          disabled={!writable || versionBusy}
          onChange={(next) => void handleVersionChange(next)}
        />
        {versionError ? (
          <p className="dloop-hint" role="status">
            {versionError}
          </p>
        ) : null}
        <Choice
          label="Compare with another model version"
          options={SHADOW_OPTIONS}
          value={shadow}
          disabled={!writable || shadowBusy}
          onChange={(next) => {
            // Choice greys out its own radios via `disabled` above, but jsdom still fires a
            // disabled radio's onChange on a synthetic click, so this guard stays as defence in
            // depth.
            if (!writable || shadowBusy) return;
            void handleShadowChange(next);
          }}
        />
        <p className="dloop-hint">
          Each new decision also goes to this version for comparison. Its answers do not change
          VIKI's decisions. Each comparison is an extra paid request within your daily cap and the
          10-a-minute limit. If TypeSafe rejects your key or says it is busy, VIKI pauses all
          Decision model requests, including requests to the version in use.
        </p>
        {shadow === model.version.setting ? (
          <p className="dloop-hint">
            This is the version already in use, so no comparison requests run.
          </p>
        ) : null}
        {shadowError ? (
          <p className="dloop-hint" role="status">
            {shadowError}
          </p>
        ) : null}
        <ShadowPart shadow={page.learning.shadow} />
      </details>
    </DecisionRow>
  );
}

const SEND_LEVELS = ["titles", "excerpts", "sections"] as const;
type SendLevel = (typeof SEND_LEVELS)[number];

function readSendLevel(values: Readonly<Record<string, unknown>>): SendLevel {
  const raw = values["decision.send_level"];
  return typeof raw === "string" && (SEND_LEVELS as readonly string[]).includes(raw)
    ? (raw as SendLevel)
    : "excerpts";
}

const SEND_LEVEL_OPTIONS: readonly ChoiceOption<SendLevel>[] = [
  { value: "titles", label: "Titles only" },
  { value: "excerpts", label: "Titles and short excerpts (up to 200 characters)" },
  { value: "sections", label: "Titles and sections (up to 2,000 characters)" },
];

const SEND_LEVEL_SHORT: Readonly<Record<SendLevel, string>> = {
  titles: "Titles only",
  excerpts: "Titles and excerpts",
  sections: "Titles and sections",
};

/** The stored `decision.never_send` setting: a list of folders/tags, or the backend's
 * `"!unreadable"` sentinel for a stored value that failed closed. Any other unparseable value
 * is treated as an empty, non-damaged list rather than surfacing the warning — only the exact
 * sentinel does, per the backend contract. */
function parseNeverSend(raw: unknown): { list: string[]; damaged: boolean } {
  if (typeof raw !== "string") return { list: [], damaged: false };
  if (raw === "!unreadable") return { list: [], damaged: true };
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed) && parsed.every((entry) => typeof entry === "string")) {
      return { list: parsed as string[], damaged: false };
    }
  } catch {
    // Falls through: an unparseable, non-sentinel value is treated as empty, not damaged.
  }
  return { list: [], damaged: false };
}

function isValidNeverSendEntry(value: string): boolean {
  return value.length >= 1 && value.length <= 128 && (value.endsWith("/") || value.startsWith("#"));
}

function heldBackWords(n: number): string {
  if (n <= 0) return "Nothing has been held back today.";
  if (n === 1) return "Your never-send list held something back from 1 request today.";
  return `Your never-send list held something back from ${n} requests today.`;
}

/** "What leaves this computer": send level, the never-send editor (including the damaged-list
 * recovery) and a link to the Context tab. */
export function PrivacyRow({
  page,
  values,
  writable,
  patch,
  onOpenContext,
  open: openProp,
  onToggle: onToggleProp,
}: SetupRowsProps) {
  const [open, toggle] = useRowOpen(true, openProp, onToggleProp);
  const [draft, setDraft] = useState("");
  const [listBusy, setListBusy] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [levelBusy, setLevelBusy] = useState(false);
  const [levelError, setLevelError] = useState<string | null>(null);
  const [clearConfirming, setClearConfirming] = useState(false);

  const sendLevel = readSendLevel(values);
  const { list, damaged } = parseNeverSend(values["decision.never_send"]);
  const trimmedDraft = draft.trim();
  const canAdd =
    writable &&
    !listBusy &&
    isValidNeverSendEntry(trimmedDraft) &&
    !list.includes(trimmedDraft) &&
    list.length < 32;

  async function saveList(next: string[]): Promise<void> {
    if (!writable) return;
    setListBusy(true);
    setListError(null);
    try {
      await patch({ "decision.never_send": JSON.stringify(next) });
      setDraft("");
    } catch {
      setListError("Could not save. Try again.");
    } finally {
      setListBusy(false);
    }
  }

  async function handleLevelChange(next: SendLevel): Promise<void> {
    // `Choice` disables its own radios via the `disabled` prop below; this guard is kept
    // as defence in depth for a caller that renders the row some other way.
    if (!writable) return;
    setLevelBusy(true);
    setLevelError(null);
    try {
      await patch({ "decision.send_level": next });
    } catch {
      setLevelError("Could not save. Try again.");
    } finally {
      setLevelBusy(false);
    }
  }

  async function handleClearDamaged(): Promise<void> {
    if (!writable) return;
    setListBusy(true);
    setListError(null);
    try {
      await patch({ "decision.never_send": "[]" });
      setClearConfirming(false);
    } catch {
      setListError("Could not save. Try again.");
    } finally {
      setListBusy(false);
    }
  }

  return (
    <DecisionRow
      id="privacy"
      name="What leaves this computer"
      description="Choose what the Decision model receives and exclude private folders or tags."
      status={SEND_LEVEL_SHORT[sendLevel]}
      open={open}
      onToggle={toggle}
      control={null}
    >
      <p>{t("decision_page.privacy.intro")}</p>
      <Choice
        label="Send level"
        options={SEND_LEVEL_OPTIONS}
        value={sendLevel}
        disabled={!writable || levelBusy}
        onChange={(next) => void handleLevelChange(next)}
      />
      {levelError ? (
        <p className="dloop-hint" role="status">
          {levelError}
        </p>
      ) : null}
      {damaged ? (
        <p className="dloop-hint" role="status">
          Your never-send list could not be read. VIKI sends nothing to the Decision model until you
          save a new list.
        </p>
      ) : null}
      <div className="dloop-field">
        <label htmlFor="never-send-input">Never send</label>
        <p id="never-send-hint" className="dloop-hint">
          {list.length >= 32
            ? "You've listed the most VIKI allows (32)."
            : t("decision_page.privacy.never_hint")}
        </p>
        <input
          id="never-send-input"
          type="text"
          value={draft}
          aria-describedby="never-send-hint"
          disabled={!writable || listBusy}
          onChange={(event) => setDraft(event.target.value)}
        />
        <div className="dloop-button-row">
          <button
            type="button"
            className="btn go"
            disabled={!canAdd}
            onClick={() => void saveList([...list, trimmedDraft])}
          >
            Add
          </button>
          {damaged && !clearConfirming ? (
            <button
              type="button"
              className="btn txt"
              disabled={!writable || listBusy}
              onClick={() => setClearConfirming(true)}
            >
              Clear the damaged list
            </button>
          ) : null}
        </div>
      </div>
      {clearConfirming ? (
        <ConfirmLine
          text="VIKI will send titles and excerpts again, with no never-send list. Clear it?"
          confirmLabel="Clear"
          busy={listBusy}
          onConfirm={() => void handleClearDamaged()}
          onCancel={() => setClearConfirming(false)}
        />
      ) : null}
      {listError ? (
        <p className="dloop-hint" role="status">
          {listError}
        </p>
      ) : null}
      {list.length > 0 ? (
        <ul>
          {list.map((entry) => (
            <li key={entry} className="dloop-button-row">
              <span>{entry}</span>
              <button
                type="button"
                className="btn txt"
                aria-label={tf("decision_page.privacy.remove", { entry })}
                disabled={!writable || listBusy}
                onClick={() => void saveList(list.filter((item) => item !== entry))}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      <p>{heldBackWords(page.privacy.held_back_today)}</p>
      <div className="dloop-button-row">
        <button type="button" className="btn txt" disabled={!writable} onClick={onOpenContext}>
          See the last state sent
        </button>
      </div>
    </DecisionRow>
  );
}
