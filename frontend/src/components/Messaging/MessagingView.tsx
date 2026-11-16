import { useCallback, useEffect, useId, useRef, useState } from "react";
import { flushSync } from "react-dom";
import {
  MESSAGING_PROVIDERS,
  type MessagingApi,
  MessagingApiError,
  type MessagingConfig,
  type MessagingField,
  type MessagingProvider,
  type MessagingProviderConfig,
  type MessagingProviderStatus,
  type MessagingState,
  type MessagingStopReason,
} from "../../clients/messaging/messagingApi";
import { Loading, Spinner } from "../Loading/Loading";
import { MessagingConfigForm } from "./MessagingConfigForm";
import "./MessagingView.css";

interface ProviderCopy {
  name: string;
  logo: string;
  guideUrl: string;
  retention: string;
  textOnly: boolean;
  steps: readonly string[];
  helpCommand?: string;
  reasons: Record<MessagingStopReason, string>;
  saveHints: Readonly<Record<string, string>>;
}

// Fixed copy per provider. Mattermost's reasons name Mattermost; Slack
// and Discord each get their own wording for their own flows (OAuth page, Socket Mode, Gateway
// heartbeats). Neither Mattermost nor Discord has a separate app-token concept, so each one's
// app_token_rejected just mirrors its own start_failed string.
const PROVIDERS: Record<MessagingProvider, ProviderCopy> = {
  mattermost: {
    name: "Mattermost",
    logo: "/logos/mattermost.svg",
    guideUrl: "https://docs.mattermost.com/developers/integrate/reference/bot-accounts",
    retention: "your Mattermost server's",
    textOnly: false,
    steps: [
      "Create a Mattermost bot with the Member role, copy its access token and invite it to your team. Use an HTTPS server.",
      "Find your human Mattermost user ID, rather than the bot ID. Enable MFA for your account.",
    ],
    helpCommand: "!viki help",
    reasons: {
      token_rejected:
        "Mattermost rejected the bot token. Create a new token under Integrations > Bot Accounts and paste its Access Token (not the Token ID), then select Save changes and turn the bot on.",
      app_token_rejected:
        "Mattermost didn't finish connecting. Check its saved settings, then turn the bot on again.",
      server_unreachable:
        "VIKI Agent couldn't reach the Mattermost server or verify its HTTPS certificate. Check the Server URL, your network or VPN, and the server's certificate, then turn the bot on again.",
      account_check_failed:
        "Mattermost accepted the bot token, but the account check failed. Check that Your user ID is your own user ID, not the bot's, and that the bot is active and not a System Admin.",
      start_failed:
        "Mattermost didn't finish connecting. Check its saved settings, then turn the bot on again.",
      connection_lost:
        "The Mattermost connection stopped after it was connected. Turn the bot on to start it again.",
      limit_reached:
        "The Mattermost bot stopped because it reached one of its safety limits, such as too many waiting requests or messages at once. Turn the bot on to start a fresh conversation.",
      conversation_failed:
        "The Mattermost bot stopped because its Claude Code conversation reported an error or was closed. Open that conversation in VIKI Agent to see what happened, then turn the bot on again.",
      safety_check:
        "The Mattermost bot stopped because a request changed after it was shown, or a request or reply could not be shown completely. Check VIKI Agent, then turn the bot on again.",
      internal_error:
        "The Mattermost bot stopped after an unexpected error. Turn the bot on to start it again.",
    },
    saveHints: {
      invalid_token:
        "That isn't a Mattermost bot token. A bot token is 26 lowercase letters and digits: create one under Integrations > Bot Accounts and paste its Access Token.",
    },
  },
  slack: {
    name: "Slack",
    logo: "/logos/slack.png",
    guideUrl: "https://docs.slack.dev/apis/events-api/using-socket-mode/",
    retention: "Slack's",
    textOnly: true,
    steps: [
      "Create a Slack app with Socket Mode enabled. Give its bot users:read, im:write, im:read, im:history and chat:write scopes, and subscribe only to message.im events.",
      "Create an app token with connections:write, and find your human Slack user ID.",
    ],
    reasons: {
      token_rejected:
        "Slack rejected the bot token. In your Slack app's settings, copy the bot token from the OAuth & Permissions page (it starts with xoxb-), paste it in Bot token, then select Save changes and turn the bot on.",
      app_token_rejected:
        "Slack rejected the app token. In your Slack app's settings, open Basic Information, generate an app-level token with the connections:write scope (it starts with xapp-), paste it in App token, then select Save changes and turn the bot on.",
      server_unreachable:
        "VIKI Agent couldn't reach Slack or verify its HTTPS certificate. Check your network, VPN or proxy, then turn the bot on again.",
      account_check_failed:
        "Slack accepted the bot token, but the account check failed. Check that Your member ID is your own member ID, not the bot's, and that the bot has the users:read, im:write, im:read, im:history and chat:write scopes; reinstall the app after adding scopes.",
      start_failed:
        "Slack didn't finish connecting. Check that Socket Mode is on for your app and that your network allows its connection, then turn the bot on again after any displayed cooldown.",
      connection_lost:
        "The Slack connection stopped after it was connected, for example when Slack refreshed its Socket Mode connection. Turn the bot on to start it again.",
      limit_reached:
        "The Slack bot stopped because it reached one of its safety limits, such as too many waiting requests or messages at once. Turn the bot on to start a fresh conversation.",
      conversation_failed:
        "The Slack bot stopped because its Claude Code conversation reported an error or was closed. Open that conversation in VIKI Agent to see what happened, then turn the bot on again.",
      safety_check:
        "The Slack bot stopped because a request changed after it was shown, or a request or reply could not be shown completely. Check VIKI Agent, then turn the bot on again.",
      internal_error:
        "The Slack bot stopped after an unexpected error. Turn the bot on to start it again.",
    },
    saveHints: {
      invalid_token:
        "That isn't a Slack bot token. Copy the bot token from the OAuth & Permissions page of your Slack app's settings; it starts with xoxb-.",
      invalid_app_token:
        "That isn't a Slack app token. Generate an app-level token with the connections:write scope under Basic Information in your Slack app's settings; it starts with xapp-.",
    },
  },
  discord: {
    name: "Discord",
    logo: "/logos/discord.svg",
    guideUrl: "https://docs.discord.com/developers/quick-start/getting-started",
    retention: "Discord's",
    textOnly: true,
    steps: [
      "Create a Discord bot and find your human Discord user ID. Open a direct message with the bot.",
      "Use a public HTTPS app or support page for the client URL.",
    ],
    reasons: {
      token_rejected:
        "Discord rejected the bot token. In the Discord developer portal, open your application's Bot page, select Reset Token, paste the new token in Bot token, then select Save changes and turn the bot on.",
      app_token_rejected:
        "Discord didn't finish connecting. Check your network, then turn the bot on again after any displayed cooldown.",
      server_unreachable:
        "VIKI Agent couldn't reach Discord or verify its HTTPS certificate. Check your network, VPN or proxy, then turn the bot on again.",
      account_check_failed:
        "Discord accepted the bot token, but the account check failed. Check that Your user ID is your own numeric user ID, not the bot's, and that the bot can send you a direct message.",
      start_failed:
        "Discord didn't finish connecting. Check your network, then turn the bot on again after any displayed cooldown.",
      connection_lost:
        "The Discord connection stopped after it was connected, for example after a missed heartbeat or a reconnect request. Turn the bot on to start it again.",
      limit_reached:
        "The Discord bot stopped because it reached one of its safety limits, such as too many waiting requests or messages at once. Turn the bot on to start a fresh conversation.",
      conversation_failed:
        "The Discord bot stopped because its Claude Code conversation reported an error or was closed. Open that conversation in VIKI Agent to see what happened, then turn the bot on again.",
      safety_check:
        "The Discord bot stopped because a request changed after it was shown, or a request or reply could not be shown completely. Check VIKI Agent, then turn the bot on again.",
      internal_error:
        "The Discord bot stopped after an unexpected error. Turn the bot on to start it again.",
    },
    saveHints: {},
  },
};

const STATE_LABELS: Record<MessagingState, string> = {
  not_configured: "Not configured",
  disabled: "Disabled",
  connecting: "Connecting",
  connected: "Connected",
  stopped: "Stopped",
};
const STATUS_POLL_MS = 2000;
type Action = "enable" | "disable" | "save" | "remove";
interface Entry {
  status: MessagingProviderStatus | null;
  busy: Action | null;
  reading: boolean;
  error: string | null;
  cooldownUntil: number;
}
type Entries = Record<MessagingProvider, Entry>;
type ConfigState = "reading" | "ready" | "failed";
const EMPTY: Entry = { status: null, busy: null, reading: true, error: null, cooldownUntil: 0 };

/** One value per provider, keyed off `MESSAGING_PROVIDERS` rather than three literal properties
 * repeated at every call site. */
function perProvider<T>(value: T): Record<MessagingProvider, T> {
  return Object.fromEntries(MESSAGING_PROVIDERS.map((provider) => [provider, value])) as Record<
    MessagingProvider,
    T
  >;
}

const INITIAL: Entries = perProvider(EMPTY);

function focusHeading(element: HTMLHeadingElement | null) {
  element?.focus();
}
/** True when a saved-settings read would show the same thing the user already sees: same
 * source, same non-secret values (a saved secret is never echoed back, so a token-only change
 * looks identical here by design — the form's reset still has to fire for that case, which is
 * why the view resets a form off its own save/remove, never off this). */
function sameProviderConfig(a: MessagingProviderConfig, b: MessagingProviderConfig): boolean {
  if (a.source !== b.source) return false;
  const aKeys = Object.keys(a.values) as MessagingField[];
  const bKeys = Object.keys(b.values) as MessagingField[];
  return aKeys.length === bKeys.length && aKeys.every((key) => a.values[key] === b.values[key]);
}
/** Keeps each provider's previous object when a re-read changed nothing for it, so passing
 * `configs[provider]` down does not look like a new config to a form the user is mid-typing in.
 * The provider that did change (including one whose values an in-flight
 * read raced past) gets the fresh object. */
function mergeConfigs(previous: MessagingConfig | null, next: MessagingConfig): MessagingConfig {
  if (!previous) return next;
  const merged = { ...next };
  for (const provider of MESSAGING_PROVIDERS)
    if (sameProviderConfig(previous[provider], next[provider]))
      merged[provider] = previous[provider];
  return merged;
}
/** Enable/Disable keep their own messages (restart_required, generic connect/stop); Save and
 * Remove get their own copy. */
function actionErrorMessage(provider: MessagingProvider, action: Action, error: unknown): string {
  const name = PROVIDERS[provider].name;
  if (action === "save") {
    const code = error instanceof MessagingApiError ? error.code : null;
    const hint = code ? PROVIDERS[provider].saveHints[code] : undefined;
    if (hint) return hint;
    return code === "invalid_config"
      ? `Check the ${name} settings. A value is missing or in the wrong format.`
      : `Couldn't save the ${name} settings. Try again.`;
  }
  if (action === "remove") return `Couldn't remove the ${name} settings. Try again.`;
  if (error instanceof MessagingApiError && error.code === "restart_required")
    return `Restart VIKI Agent before connecting ${name} again.`;
  return action === "disable"
    ? `Couldn't confirm that the ${name} bot stopped. Refresh status or try again.`
    : `Couldn't connect the ${name} bot. Refresh status or try again.`;
}

export function MessagingView({
  api,
  onCapabilities,
  onOpenUrl,
}: {
  api: MessagingApi;
  onCapabilities?: () => void;
  onOpenUrl?: (url: string) => void;
}) {
  const id = useId();
  const [entries, setEntries] = useState<Entries>(INITIAL);
  const [refreshing, setRefreshing] = useState(false);
  const [configs, setConfigs] = useState<MessagingConfig | null>(null);
  const [configState, setConfigState] = useState<ConfigState>("reading");
  const [resetSignals, setResetSignals] = useState<Record<MessagingProvider, number>>(
    perProvider(0),
  );
  const [open, setOpen] = useState<Record<MessagingProvider, boolean>>(perProvider(false));
  const generations = useRef<Record<MessagingProvider, number>>(perProvider(0));
  const pending = useRef<
    Record<MessagingProvider, { action: Action; controller: AbortController } | null>
  >(perProvider(null));
  const refreshRequest = useRef<AbortController | null>(null);
  const pollRequest = useRef<AbortController | null>(null);
  const configRequest = useRef<AbortController | null>(null);
  // Set when a read is requested while one is already in flight (Refresh status racing a
  // save/remove's own reload): the in-flight read may have started before
  // that action committed and so can land stale, with nothing else scheduled to correct it.
  const configDirty = useRef(false);
  const requests = useRef(new Set<AbortController>());

  const loadConfig = useCallback(async () => {
    if (configRequest.current) {
      configDirty.current = true;
      return;
    }
    const controller = new AbortController();
    configRequest.current = controller;
    requests.current.add(controller);
    setConfigState("reading");
    try {
      const answer = await api.config(controller.signal);
      if (controller.signal.aborted) return;
      setConfigs((current) => mergeConfigs(current, answer));
      setConfigState("ready");
    } catch {
      if (!controller.signal.aborted) setConfigState("failed");
    } finally {
      requests.current.delete(controller);
      if (configRequest.current === controller) configRequest.current = null;
      if (configDirty.current) {
        configDirty.current = false;
        void loadConfig();
      }
    }
  }, [api]);

  const refresh = useCallback(async () => {
    if (refreshRequest.current) return;
    const controller = new AbortController();
    const versions = { ...generations.current };
    const actions = { ...pending.current };
    refreshRequest.current = controller;
    requests.current.add(controller);
    setRefreshing(true);
    // Settings are read beside status, on their own tracked request: a config failure never
    // touches the per-provider status entries below, and a status failure never touches configs.
    void loadConfig();
    setEntries((current) => {
      const next = { ...current };
      for (const provider of MESSAGING_PROVIDERS)
        if (!actions[provider])
          next[provider] = { ...current[provider], reading: true, error: null };
      return next;
    });
    try {
      const answer = await api.get(controller.signal);
      if (controller.signal.aborted) return;
      setEntries((current) => {
        const next = { ...current };
        for (const provider of MESSAGING_PROVIDERS) {
          if (
            !actions[provider] &&
            versions[provider] === generations.current[provider] &&
            !controller.signal.aborted
          )
            next[provider] = {
              ...current[provider],
              status: answer[provider],
              reading: false,
              error: null,
            };
        }
        return next;
      });
    } catch {
      if (!controller.signal.aborted)
        setEntries((current) => {
          const next = { ...current };
          for (const provider of MESSAGING_PROVIDERS) {
            if (
              !actions[provider] &&
              versions[provider] === generations.current[provider] &&
              !controller.signal.aborted
            )
              next[provider] = {
                ...current[provider],
                status: null,
                reading: false,
                error: "Messaging status is unavailable. Refresh status to try again.",
              };
          }
          return next;
        });
    } finally {
      requests.current.delete(controller);
      if (refreshRequest.current === controller) {
        refreshRequest.current = null;
        setRefreshing(false);
      }
    }
  }, [api, loadConfig]);

  // Background status poll while a bot is active: unlike refresh, it never touches configs,
  // reading or error, and it swallows its own failures, so it cannot flash loading UI or steal
  // the page's one error slot from a real refresh or action.
  const poll = useCallback(async () => {
    if (refreshRequest.current || pollRequest.current) return;
    const controller = new AbortController();
    const versions = { ...generations.current };
    const actions = { ...pending.current };
    pollRequest.current = controller;
    requests.current.add(controller);
    try {
      const answer = await api.get(controller.signal);
      if (controller.signal.aborted) return;
      setEntries((current) => {
        const next = { ...current };
        for (const provider of MESSAGING_PROVIDERS)
          if (
            !actions[provider] &&
            versions[provider] === generations.current[provider] &&
            !controller.signal.aborted
          )
            next[provider] = { ...current[provider], status: answer[provider] };
        return next;
      });
    } catch {
      // Swallowed: a background poll never surfaces its own error state.
    } finally {
      requests.current.delete(controller);
      if (pollRequest.current === controller) pollRequest.current = null;
    }
  }, [api]);

  useEffect(() => {
    setEntries(INITIAL);
    setConfigs(null);
    setConfigState("reading");
    setResetSignals(perProvider(0));
    setOpen(perProvider(false));
    void refresh();
    return () => {
      for (const controller of requests.current) controller.abort();
      requests.current.clear();
      refreshRequest.current = null;
      pollRequest.current = null;
      configRequest.current = null;
      configDirty.current = false;
      for (const provider of MESSAGING_PROVIDERS) {
        generations.current[provider] += 1;
        pending.current[provider] = null;
      }
    };
  }, [refresh]);

  useEffect(() => {
    const deadlines = MESSAGING_PROVIDERS.map((provider) => entries[provider].cooldownUntil).filter(
      (value) => value > 0,
    );
    if (!deadlines.length) return;
    const timer = setTimeout(
      () =>
        setEntries((current) => {
          const next = { ...current };
          for (const provider of MESSAGING_PROVIDERS)
            if (current[provider].cooldownUntil <= Date.now())
              next[provider] = { ...current[provider], cooldownUntil: 0 };
          return next;
        }),
      Math.max(0, Math.min(...deadlines) - Date.now()),
    );
    return () => clearTimeout(timer);
  }, [entries]);

  const watching = MESSAGING_PROVIDERS.some(
    (provider) =>
      entries[provider].status?.state === "connecting" ||
      entries[provider].status?.state === "connected",
  );
  useEffect(() => {
    if (!watching) return;
    const timer = setInterval(() => void poll(), STATUS_POLL_MS);
    return () => clearInterval(timer);
  }, [watching, poll]);

  const run = async (
    provider: MessagingProvider,
    action: Action,
    values?: Partial<Record<MessagingField, string>>,
  ) => {
    const previous = pending.current[provider];
    if (previous?.action === action) return;
    if (
      action === "enable" &&
      (!entries[provider].status?.configured ||
        entries[provider].status?.restart_required ||
        entries[provider].cooldownUntil > Date.now())
    )
      return;
    previous?.controller.abort();
    const controller = new AbortController();
    const version = ++generations.current[provider];
    pending.current[provider] = { action, controller };
    requests.current.add(controller);
    setEntries((current) => ({
      ...current,
      [provider]: { ...current[provider], busy: action, reading: false, error: null },
    }));
    try {
      const answer =
        action === "save"
          ? await api.save(provider, values ?? {}, controller.signal)
          : action === "remove"
            ? await api.remove(provider, controller.signal)
            : await api[action](provider, controller.signal);
      if (!controller.signal.aborted && generations.current[provider] === version) {
        setEntries((current) => {
          if (controller.signal.aborted || generations.current[provider] !== version)
            return current;
          return {
            ...current,
            [provider]: {
              ...current[provider],
              status: answer[provider],
              busy: null,
              error: null,
              cooldownUntil: action === "enable" ? 0 : current[provider].cooldownUntil,
            },
          };
        });
        // Save and Remove change what settings are saved, so the saved-settings read is repeated
        // once the action lands; a failure here only flips configState, never the status above.
        // This provider's own form also resets now — bumping its signal, not relying on the
        // re-read's object identity, is what still clears a token when only the secret changed
        // (the saved non-secret values the server echoes back can be identical either way).
        if (action === "save" || action === "remove") {
          void loadConfig();
          setResetSignals((current) => ({ ...current, [provider]: current[provider] + 1 }));
        }
      }
    } catch (error) {
      if (!controller.signal.aborted && generations.current[provider] === version)
        setEntries((current) => {
          if (controller.signal.aborted || generations.current[provider] !== version)
            return current;
          const retry =
            error instanceof MessagingApiError && error.code === "cooldown"
              ? error.retryAfterSeconds
              : undefined;
          const cooldownUntil =
            typeof retry === "number" && Number.isInteger(retry) && retry >= 1 && retry <= 86400
              ? Date.now() + retry * 1000
              : current[provider].cooldownUntil;
          const message = actionErrorMessage(provider, action, error);
          const status =
            error instanceof MessagingApiError &&
            error.status === 409 &&
            error.code === "restart_required"
              ? {
                  configured: true,
                  state: "stopped" as const,
                  restart_required: true,
                  reason: null,
                }
              : current[provider].status;
          return {
            ...current,
            [provider]: { ...current[provider], status, busy: null, error: message, cooldownUntil },
          };
        });
    } finally {
      requests.current.delete(controller);
      if (pending.current[provider]?.controller === controller) pending.current[provider] = null;
    }
  };

  return (
    <section className="messaging-view" aria-labelledby={`${id}-title`} aria-busy={refreshing}>
      <header className="page-head">
        <h1 className="page-title" id={`${id}-title`} ref={focusHeading} tabIndex={-1}>
          Messaging
        </h1>
        <p className="page-intro">Reach VIKI Agent from a private direct message.</p>
        <div className="page-actions">
          <button
            type="button"
            className="btn alt"
            disabled={refreshing}
            onClick={() => void refresh()}
          >
            Refresh status
          </button>
        </div>
        <details className="messaging-about">
          <summary>How messaging works</summary>
          <p>
            Only the configured owner can use private direct messages to send text and answer
            approvals.
          </p>
          <p>Keep the desktop host awake and the VIKI Agent window open while using a bot.</p>
          <p>
            Save each provider's settings below and turn it on. A bot you leave on reconnects when
            VIKI Agent starts in the same workspace.
          </p>
        </details>
      </header>
      {MESSAGING_PROVIDERS.map((provider) => {
        const entry = entries[provider];
        const copy = PROVIDERS[provider];
        const name = copy.name;
        const headingId = `${id}-${provider}`;
        const bodyId = `${headingId}-body`;
        const statusId = `${headingId}-status`;
        const restartId = `${headingId}-restart`;
        const cooldownId = `${headingId}-cooldown`;
        // Save/Remove never turn the bot on or off by themselves: counting
        // them here flipped the switch and its label while a save merely ran.
        const active =
          entry.busy === "enable" ||
          entry.busy === "disable" ||
          entry.status?.state === "connecting" ||
          entry.status?.state === "connected";
        const savingOrRemoving = entry.busy === "save" || entry.busy === "remove";
        const cooled = entry.cooldownUntil > Date.now();
        const action: Action = active ? "disable" : "enable";
        const idle = entry.busy === null && !entry.reading;
        const dim =
          idle && (entry.status?.state === "disabled" || entry.status?.state === "not_configured");
        const connected = idle && entry.status?.state === "connected";
        // Re-reads (Refresh status, another provider's save) keep showing the last config this
        // card had rather than dropping to null, so they never wipe what the user is mid-typing
        // here; `configState` below only governs the status note.
        const formConfig = configs ? configs[provider] : null;

        const refused =
          entry.busy === action ||
          savingOrRemoving ||
          (action === "enable" && cooled) ||
          (entry.status?.restart_required ?? false);
        const describedBy = [
          statusId,
          entry.status?.restart_required ? restartId : null,
          cooled ? cooldownId : null,
        ]
          .filter((value): value is string => value !== null)
          .join(" ");
        const control =
          entry.status === null ? null : !entry.status.configured ? (
            <button
              type="button"
              className="btn alt sm"
              aria-label={`Set up ${name}`}
              onClick={() => {
                flushSync(() => setOpen((current) => ({ ...current, [provider]: true })));
                document.getElementById(bodyId)?.querySelector("input")?.focus();
              }}
            >
              Set up
            </button>
          ) : (
            <button
              type="button"
              role="switch"
              className="messaging-toggle"
              aria-checked={active}
              aria-labelledby={headingId}
              aria-describedby={describedBy}
              aria-disabled={refused || undefined}
              onClick={() => {
                if (!refused) void run(provider, action);
              }}
            >
              <span className="messaging-switch-track" aria-hidden="true" />
              <span className="messaging-switch-state" aria-hidden="true">
                {active ? "On" : "Off"}
              </span>
            </button>
          );

        return (
          <section
            key={provider}
            className="messaging-bot"
            aria-labelledby={headingId}
            aria-busy={entry.busy !== null || entry.reading}
          >
            <div className="messaging-bot-heading">
              <h2 id={headingId} className="messaging-provider-heading">
                <button
                  type="button"
                  className="messaging-disclosure"
                  aria-expanded={open[provider]}
                  aria-controls={bodyId}
                  onClick={() =>
                    setOpen((current) => ({ ...current, [provider]: !current[provider] }))
                  }
                >
                  <svg
                    className="ico"
                    width="14"
                    height="14"
                    viewBox="0 0 16 16"
                    aria-hidden="true"
                  >
                    <path d="M4 6l4 4 4-4" />
                  </svg>
                  <img
                    src={copy.logo}
                    alt=""
                    aria-hidden="true"
                    width={32}
                    height={32}
                    className={`messaging-provider-logo messaging-provider-logo--${provider}`}
                  />
                  {name} bot
                </button>
              </h2>
              <span
                id={statusId}
                role="status"
                className={`chip messaging-status${dim ? " dim" : ""}`}
                data-state={connected ? "connected" : undefined}
              >
                {!idle ? <Spinner className="spinner-late" /> : null}
                {entry.busy === "enable"
                  ? "Enabling…"
                  : entry.busy === "disable"
                    ? "Disabling…"
                    : entry.busy === "save"
                      ? "Saving…"
                      : entry.busy === "remove"
                        ? "Removing…"
                        : entry.reading
                          ? "Reading status…"
                          : entry.status
                            ? STATE_LABELS[entry.status.state]
                            : "Status unavailable"}
              </span>
              {control}
            </div>
            {entry.status?.restart_required ? (
              <p id={restartId} className="messaging-restart">
                Restart VIKI Agent to use this bot again with the current workspace.
              </p>
            ) : null}
            {entry.busy === null && entry.status?.state === "stopped" && entry.status.reason ? (
              <p className="messaging-reason" role="status">
                {copy.reasons[entry.status.reason]}
              </p>
            ) : null}
            {entry.error ? (
              <p className="messaging-error" role="alert">
                {entry.error}
              </p>
            ) : null}
            {cooled ? (
              <p id={cooldownId} className="messaging-cooldown" role="status">
                Wait up to {Math.ceil((entry.cooldownUntil - Date.now()) / 1000)} seconds before
                connecting again.
              </p>
            ) : null}
            <div id={bodyId} className="messaging-bot-body" hidden={!open[provider]}>
              {configs ? (
                <span className="chip messaging-source">
                  {configs[provider].source === "saved"
                    ? "Saved settings"
                    : configs[provider].source === "unreadable"
                      ? "Settings unreadable"
                      : "Needs setup"}
                </span>
              ) : null}
              <p>Chat with VIKI Agent from a private {name} direct message.</p>
              <p>
                Messages and replies leave this computer and follow {copy.retention} retention
                policy.
              </p>
              {copy.textOnly ? (
                <p>This connection accepts new text messages without files or thread replies.</p>
              ) : null}
              {configs === null && configState === "reading" ? (
                <Loading className="messaging-config-status">Reading settings…</Loading>
              ) : configState === "failed" ? (
                <p className="messaging-config-status" role="status">
                  Settings are unavailable. Refresh status to try again.
                </p>
              ) : null}
              <MessagingConfigForm
                provider={provider}
                config={formConfig}
                busy={savingOrRemoving}
                active={active}
                resetSignal={resetSignals[provider]}
                onSave={(values) => void run(provider, "save", values)}
                onRemove={() => void run(provider, "remove")}
                {...(onOpenUrl ? { onOpenGuide: () => onOpenUrl(copy.guideUrl) } : {})}
              />
              <details className="messaging-setup">
                <summary>Setup steps</summary>
                <ol>
                  {copy.steps.map((step) => (
                    <li key={step}>{step}</li>
                  ))}
                  <li>Paste the values above, select Save changes, then turn the bot on.</li>
                </ol>
                <p>
                  Keep bot and app tokens private. Remove saved settings to delete them from this
                  computer.
                </p>
                {copy.helpCommand ? (
                  <p>
                    After enabling {name}, open a direct message with the bot and send{" "}
                    <code>{copy.helpCommand}</code>.
                  </p>
                ) : null}
              </details>
            </div>
          </section>
        );
      })}
      {onCapabilities ? (
        <section className="messaging-capabilities" aria-label="Other capabilities">
          <p>Find skills, tools, connectors and observed plugins in Capabilities.</p>
          <button type="button" className="btn alt" onClick={onCapabilities}>
            Open Capabilities
          </button>
        </section>
      ) : null}
    </section>
  );
}
