import { useEffect, useId, useState } from "react";
import {
  MESSAGING_FIELDS,
  MESSAGING_SECRET_FIELDS,
  type MessagingField,
  type MessagingProvider,
  type MessagingProviderConfig,
} from "../../clients/messaging/messagingApi";
import "./MessagingConfigForm.css";

const CREDENTIAL_LINES: Record<MessagingProvider, string> = {
  mattermost:
    "Create a bot account with the Member role on your Mattermost server, invite it to your team, then paste the server URL, its token and your user ID here.",
  slack:
    "Create an internal Slack app with Socket Mode, install it, then paste its bot token, app token and your member ID here.",
  discord:
    "Create a Discord application with a bot, open a direct message with it, then paste its token, your user ID and a client URL here.",
};

interface FieldCopy {
  label: string;
  hint: string;
  placeholder?: string;
}

const URL_COPY: FieldCopy = {
  label: "Server URL",
  hint: "Your Mattermost server's HTTPS address.",
  placeholder: "https://mattermost.example.com",
};
const OWNER_ID_COPY: Record<MessagingProvider, FieldCopy> = {
  mattermost: {
    label: "Your user ID",
    hint: "Your own 26-character Mattermost user ID, not the bot's.",
  },
  slack: { label: "Your member ID", hint: "Your own Slack member ID, not the bot's." },
  discord: { label: "Your user ID", hint: "Your own numeric Discord user ID, not the bot's." },
};
const BOT_TOKEN_COPY: Record<MessagingProvider, FieldCopy> = {
  mattermost: { label: "Bot token", hint: "The bot account's access token." },
  slack: { label: "Bot token", hint: "The installed bot's token, starting with xoxb-." },
  discord: { label: "Bot token", hint: "The token from the application's Bot page." },
};
const APP_TOKEN_COPY: FieldCopy = {
  label: "App token",
  hint: "The app-level token with connections:write, starting with xapp-.",
};
const CLIENT_URL_COPY: FieldCopy = {
  label: "Client URL",
  hint: "A public HTTPS app or support page. Discord sees it in the bot's user agent; VIKI Agent never opens it.",
};

function fieldCopy(provider: MessagingProvider, field: MessagingField): FieldCopy {
  if (field === "url") return URL_COPY;
  if (field === "owner_id") return OWNER_ID_COPY[provider];
  if (field === "bot_token") return BOT_TOKEN_COPY[provider];
  if (field === "app_token") return APP_TOKEN_COPY;
  return CLIENT_URL_COPY;
}

function isSecret(field: MessagingField): boolean {
  return MESSAGING_SECRET_FIELDS.includes(field);
}

type Values = Partial<Record<MessagingField, string>>;

function initialValues(
  provider: MessagingProvider,
  config: MessagingProviderConfig | null,
): Values {
  const values: Values = {};
  for (const field of MESSAGING_FIELDS[provider])
    values[field] = isSecret(field) ? "" : (config?.values[field] ?? "");
  return values;
}

export interface MessagingConfigFormProps {
  provider: MessagingProvider;
  config: MessagingProviderConfig | null;
  busy: boolean;
  active: boolean;
  /** Bumped by the view once this provider's own save or remove lands — the reload that follows
   * resolves later, so at the moment of the bump `config` can still be the pre-save object;
   * `resetSignal` alone is what still clears a token when the reload's saved non-secret values
   * come back unchanged. `config` itself is the other trigger: the view keeps a
   * provider's object stable across a re-read that changed nothing for it (`mergeConfigs`), so a
   * plain Refresh or another provider's save does not reset this form, but the reload that
   * follows THIS provider's own save still lands here once it arrives — see the
   * reset effect below. */
  resetSignal: number;
  onSave: (values: Values) => void;
  onRemove: () => void;
  onOpenGuide?: () => void;
}

export function MessagingConfigForm({
  provider,
  config,
  busy,
  active,
  resetSignal,
  onSave,
  onRemove,
  onOpenGuide,
}: MessagingConfigFormProps) {
  const id = useId();
  const [values, setValues] = useState<Values>(() => initialValues(provider, config));

  // Resets on a provider swap, on a genuinely new `config` object (the view's `mergeConfigs`
  // keeps the same object across a re-read that changed nothing for this provider, so a plain
  // Refresh or another provider's save does not reset this form), and on `resetSignal` (bumped
  // by the view the moment THIS provider's own save or remove lands — one render before the
  // reload it triggers can possibly resolve). Both are needed: `resetSignal` alone would apply
  // the reset too early, against the pre-save `config` — but it is still the only
  // trigger left for a token-only change, where the reload's saved non-secret values come back
  // identical and `config`'s reference never changes. `resetSignal` is listed only
  // as that extra trigger — it is never read inside the effect body.
  // biome-ignore lint/correctness/useExhaustiveDependencies: see the comment above.
  useEffect(() => {
    setValues(initialValues(provider, config));
  }, [provider, resetSignal, config]);

  const fields = MESSAGING_FIELDS[provider];
  const saved = config?.source === "saved";
  const unreadable = config?.source === "unreadable";
  const removable = config !== null && (saved || unreadable);

  // The server refuses a Mattermost save that changes Server URL while leaving Bot token empty:
  // an old token would otherwise travel to a new server silently. Compared as trimmed
  // strings only — no URL parsing here, the server canonicalises; a cosmetic difference merely
  // asks for the token again, which is safe either way.
  const mattermostUrlChanged =
    provider === "mattermost" &&
    saved &&
    (values.url ?? "").trim() !== (config?.values.url ?? "").trim();

  const allNonSecretFilled = fields
    .filter((field) => !isSecret(field))
    .every((field) => (values[field] ?? "").trim() !== "");
  const allSecretsFilled = fields
    .filter(isSecret)
    .every((field) =>
      field === "bot_token" && mattermostUrlChanged
        ? (values[field] ?? "").trim() !== ""
        : saved || (values[field] ?? "").trim() !== "",
    );
  const hasChanged =
    !saved ||
    fields.some((field) =>
      isSecret(field)
        ? (values[field] ?? "").trim() !== ""
        : (values[field] ?? "").trim() !== (config?.values[field] ?? ""),
    );
  const canSave = config !== null && !busy && allNonSecretFilled && allSecretsFilled && hasChanged;

  function handleSave() {
    const out: Values = {};
    for (const field of fields) {
      const value = (values[field] ?? "").trim();
      if (isSecret(field)) {
        if (value !== "") out[field] = value;
      } else {
        out[field] = value;
      }
    }
    onSave(out);
  }

  return (
    <div className="messaging-config-form">
      <section aria-labelledby={`${id}-credentials`}>
        <h3 id={`${id}-credentials`} className="lbl">
          Get your credentials
        </h3>
        <p>{CREDENTIAL_LINES[provider]}</p>
        {onOpenGuide ? (
          <button type="button" className="btn txt messaging-config-guide" onClick={onOpenGuide}>
            Open setup guide
          </button>
        ) : null}
      </section>
      {config !== null ? (
        <section aria-labelledby={`${id}-required`}>
          <h3 id={`${id}-required`} className="lbl">
            Required
          </h3>
          {unreadable ? (
            <p className="messaging-config-note" role="status">
              Saved settings can't be read with this account. Enter them again or remove them.
            </p>
          ) : null}
          {fields.map((field) => {
            const copy = fieldCopy(provider, field);
            const fieldId = `${id}-${field}`;
            const hintId = `${fieldId}-hint`;
            const secret = isSecret(field);
            const placeholder = secret
              ? field === "bot_token" && mattermostUrlChanged
                ? "Enter the bot token again for a new server URL."
                : saved
                  ? "Saved. Enter a new token to replace it."
                  : undefined
              : copy.placeholder;
            return (
              <div key={field} className="messaging-config-field">
                <label htmlFor={fieldId}>{copy.label}</label>
                <p id={hintId} className="messaging-config-hint">
                  {copy.hint}
                </p>
                <input
                  id={fieldId}
                  type={secret ? "password" : "text"}
                  value={values[field] ?? ""}
                  aria-describedby={hintId}
                  {...(secret ? { autoComplete: "new-password", spellCheck: false } : {})}
                  {...(placeholder !== undefined ? { placeholder } : {})}
                  onChange={(event) =>
                    setValues((current) => ({ ...current, [field]: event.target.value }))
                  }
                />
              </div>
            );
          })}
          {active ? (
            <p className="messaging-config-note" role="status">
              Saving or removing turns this bot off. Turn it on again afterwards.
            </p>
          ) : null}
        </section>
      ) : null}
      <div className="messaging-config-footer">
        <div className="messaging-config-actions">
          {removable ? (
            <button type="button" className="btn alt" disabled={busy} onClick={onRemove}>
              Remove saved settings
            </button>
          ) : null}
          <button type="button" className="btn go" disabled={!canSave} onClick={handleSave}>
            Save changes
          </button>
        </div>
      </div>
    </div>
  );
}
