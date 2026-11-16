/**
 * The Providers page: the readiness facts `capabilities.provider.claude`
 * and `capabilities.git` already carry, the two non-spawning checks, and the setup instructions —
 * plain text, plus copyable per-OS install/update commands for a missing or outdated CLI and a
 * button to Anthropic's own setup guide. Model connections also provide the bundled Magpie
 * dashboard, which starts its own supervised process when opened or used for inference.
 * A Copy button touches the clipboard, and the guide button calls `onOpenUrl`.
 * On every OS the chat's
 * not-signed-in card offers Claude Code's sign-in console (ADR-052).
 */
import { useState } from "react";
import type { CheckName, CheckResult, ProviderReadiness } from "../../clients/app/shellApi";
import { ShellApiError } from "../../clients/app/shellApi";
import type { CommandState } from "../../commands";
import { commandFor } from "../../commands";
import type { Key } from "../../i18n/index";
import { errorText, t, tf } from "../../i18n/t";
import { type PlatformKind, platformKind } from "../../platform";
import type { ShellSnapshot, ShellStore } from "../../state/shell";
import { CopyButton } from "../Conversation/Copy";
import { McpSetup, type McpStart } from "../Mcp/McpSetup";
import { CLAUDE_SETUP_URL } from "../Onboarding/setupSteps";
import { ModelConnectionsPanel } from "./ModelConnectionsPanel";
import "./Settings.css";

/** No `onCommand`/`onNavigate`: this page issues no command a dispatcher needs to see (its two
 * checks call `store.runCheck` directly) and opens no other view. `onOpenUrl` is the one
 * exception: the setup section's guide button opens Anthropic's
 * install docs through it, same signature as `Conversation`'s and `Cards`' own prop. */
export interface ProvidersPageProps {
  store: ShellStore;
  snapshot: ShellSnapshot;
  onOpenUrl: (url: string) => void;
}

/** The reason a disabled control is off (the same pattern as `WorkspacesPage`). */
function ReasonHint({ id, state }: { id: string; state: CommandState }) {
  if (state.enabled || state.reason === null) return null;
  return (
    <span id={id} className="sr-only">
      {t(state.reason)}
    </span>
  );
}

function describedBy(id: string, state: CommandState): string | undefined {
  return state.enabled || state.reason === null ? undefined : id;
}

function reasonTitle(state: CommandState): string | undefined {
  return state.enabled || state.reason === null ? undefined : t(state.reason);
}

/** `en-US`, medium date / short time (e.g. "Oct 9, 2026, 12:00 PM"); the raw string stands in for
 * a value `Date` cannot parse. Re-declared per page rather than shared, same as `ReasonHint`. */
function localTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? iso
    : date.toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short" });
}

/** The card-head pill's tone: a readiness string of "ready" reads ready, "missing"/"unusable"
 * reads error (both the CLI's and git's own vocabulary use these two words for the broken
 * cases), anything else (a login/config quirk, not a failure) reads attention. */
function readinessTone(state: string): "ready" | "attention" | "error" {
  if (state === "ready") return "ready";
  if (state === "missing" || state === "unusable") return "error";
  return "attention";
}

const CHECKS: readonly CheckName[] = ["provider_cli", "git"];

interface SetupCommand {
  method: "powershell" | "winget" | "terminal" | "homebrew" | "claude";
  command: string;
}

/** Per-OS install commands: PowerShell/WinGet on Windows, a curl one-liner plus Homebrew
 * on macOS, the same curl line alone on Linux (no Homebrew cask there). */
const INSTALL: Readonly<Record<PlatformKind, readonly SetupCommand[]>> = {
  windows: [
    { method: "powershell", command: "irm https://claude.ai/install.ps1 | iex" },
    { method: "winget", command: "winget install Anthropic.ClaudeCode" },
  ],
  mac: [
    { method: "terminal", command: "curl -fsSL https://claude.ai/install.sh | bash" },
    { method: "homebrew", command: "brew install --cask claude-code" },
  ],
  linux: [{ method: "terminal", command: "curl -fsSL https://claude.ai/install.sh | bash" }],
};

/** Per-OS update commands: the CLI's own self-update first, then the package manager the
 * user may have installed it with. */
const UPDATE: Readonly<Record<PlatformKind, readonly SetupCommand[]>> = {
  windows: [
    { method: "claude", command: "claude update" },
    { method: "winget", command: "winget upgrade Anthropic.ClaudeCode" },
  ],
  mac: [
    { method: "claude", command: "claude update" },
    { method: "homebrew", command: "brew upgrade claude-code" },
  ],
  linux: [{ method: "claude", command: "claude update" }],
};

/** One copyable command: a method label, the command itself, and its own Copy button — `method`
 * is a `SetupCommand["method"]` literal, so the two template-literal keys below stay in `Key`
 * with no cast. */
function CommandRow({ command }: { command: SetupCommand }) {
  const method = t(`providers.method.${command.method}`);
  return (
    <div className="providers-command">
      <span>{method}</span>
      <code className="mono">{command.command}</code>
      <CopyButton
        className="btn txt sm"
        label={tf("providers.copy", { method })}
        getText={() => command.command}
      />
    </div>
  );
}

function GuideButton({ onOpenUrl }: { onOpenUrl: (url: string) => void }) {
  return (
    <button type="button" className="btn txt sm" onClick={() => onOpenUrl(CLAUDE_SETUP_URL)}>
      {t("providers.setup.guide")}
    </button>
  );
}

/** The CLI setup block: the "missing" text, one row per `INSTALL` command for this
 * OS, and a guide button. */
function MissingSetup({ onOpenUrl }: { onOpenUrl: (url: string) => void }) {
  return (
    <>
      <p>{t("workspaces.setup.missing")}</p>
      {INSTALL[platformKind()].map((command) => (
        <CommandRow key={command.method} command={command} />
      ))}
      <GuideButton onOpenUrl={onOpenUrl} />
    </>
  );
}

/** The version-mismatch setup block: only for `version_status === "older"` ("newer" adds
 * nothing here, just the version row's own note); one row per `UPDATE` command for this OS. */
function OlderSetup({
  provider,
  onOpenUrl,
}: {
  provider: ProviderReadiness;
  onOpenUrl: (url: string) => void;
}) {
  return (
    <>
      <p>{tf("providers.setup.update", { min: provider.tested[0] })}</p>
      {UPDATE[platformKind()].map((command) => (
        <CommandRow key={command.method} command={command} />
      ))}
      <p>{t("providers.setup.update_package")}</p>
      <GuideButton onOpenUrl={onOpenUrl} />
    </>
  );
}

/** Priority order mirrors `ProviderReadiness.cli`: a CLI that cannot even run makes the login
 * state moot, so only the first applicable cause is shown for that half. `null` means the CLI
 * side has nothing to say (ready, and either signed in or billing-only trouble). */
function cliSetupCase(
  provider: ProviderReadiness,
): "missing" | "unusable" | "config_dir" | "login" | null {
  if (provider.cli === "missing") return "missing";
  if (provider.cli === "unusable") return "unusable";
  if (provider.cli === "config_dir_override") return "config_dir";
  if (provider.login === "signed_out" || provider.login === "unknown") return "login";
  return null;
}

const NO_RESULTS: Record<CheckName, CheckResult | null> = { provider_cli: null, git: null };
const NO_ERRORS: Record<CheckName, ShellApiError | null> = { provider_cli: null, git: null };

export function ProvidersPage({ store, snapshot, onOpenUrl }: ProvidersPageProps) {
  const [results, setResults] = useState<Record<CheckName, CheckResult | null>>(NO_RESULTS);
  const [errors, setErrors] = useState<Record<CheckName, ShellApiError | null>>(NO_ERRORS);

  const provider = snapshot.capabilities?.provider.claude ?? null;
  const git = snapshot.capabilities?.git ?? null;
  const activeId = snapshot.capabilities?.workspace.active?.id;
  const mcpStart: McpStart = {
    path: snapshot.workspaces?.workspaces.find((w) => w.workspace_id === activeId)?.root ?? null,
    chatOnly: snapshot.capabilities?.workspace.chat_only === true,
  };

  async function run(check: CheckName): Promise<void> {
    const state = commandFor("diagnostics.check", snapshot, undefined, { check });
    if (!state.enabled) return;
    setErrors((previous) => ({ ...previous, [check]: null }));
    try {
      const result = await store.runCheck(check);
      setResults((previous) => ({ ...previous, [check]: result }));
    } catch (error) {
      setErrors((previous) => ({
        ...previous,
        [check]: error instanceof ShellApiError ? error : new ShellApiError(0, "unavailable"),
      }));
    }
  }

  return (
    <section
      className="settings-page"
      aria-labelledby="providers-page-title"
      aria-busy={snapshot.checkRunning !== null}
    >
      <h2 className="settings-page-title" id="providers-page-title">
        {t("settings.tab.providers")}
      </h2>

      <p className="settings-page-intro">{t("settings.page.providers.desc")}</p>

      {CHECKS.map((check) => {
        const isProvider = check === "provider_cli";
        const state = commandFor("diagnostics.check", snapshot, undefined, { check });
        const result = results[check];
        const error = errors[check];
        const checkReasonId = `check-reason-${check}`;
        const providerCase = isProvider && provider !== null ? cliSetupCase(provider) : null;

        return (
          <div key={check} className="settings-card">
            <div className="settings-card-head">
              <h3 className="settings-card-title">
                {isProvider ? t("workspaces.provider.cli") : "Git"}
              </h3>
              {isProvider && provider !== null ? (
                <span className="settings-pill" data-tone={readinessTone(provider.cli)}>
                  {t(`workspaces.provider.cli.${provider.cli}`)}
                </span>
              ) : null}
              {!isProvider && git !== null ? (
                <span className="settings-pill" data-tone={readinessTone(git.state)}>
                  {t(`workspaces.git.${git.state}`)}
                </span>
              ) : null}
              <div className="settings-actions">
                <button
                  type="button"
                  className="btn alt sm"
                  aria-disabled={!state.enabled}
                  aria-describedby={describedBy(checkReasonId, state)}
                  title={reasonTitle(state)}
                  onClick={() => {
                    if (state.enabled) void run(check);
                  }}
                >
                  {t(`workspaces.check.${check}`)}
                </button>
                <ReasonHint id={checkReasonId} state={state} />
              </div>
            </div>

            {isProvider && provider !== null ? (
              <dl className="settings-facts">
                <div>
                  <dt>{t("workspaces.provider.version")}</dt>
                  <dd className="mono">
                    {provider.version}
                    {provider.version_status === "older" || provider.version_status === "newer" ? (
                      <span>
                        {" · " +
                          tf(`providers.version.${provider.version_status}`, {
                            min: provider.tested[0],
                            max: provider.tested[1],
                          })}
                      </span>
                    ) : null}
                  </dd>
                </div>
                <div>
                  <dt>{t("providers.source")}</dt>
                  <dd>{provider.source !== "" ? t(`providers.source.${provider.source}`) : "—"}</dd>
                </div>
                <div>
                  <dt>{t("workspaces.provider.login")}</dt>
                  <dd>{t(`workspaces.provider.login.${provider.login}`)}</dd>
                </div>
                <div>
                  <dt>{t("workspaces.provider.billing")}</dt>
                  <dd>{t(`workspaces.provider.billing.${provider.billing}`)}</dd>
                </div>
                <div>
                  <dt>{t("workspaces.provider.last_checked_at")}</dt>
                  <dd>
                    {provider.last_checked_at !== null ? (
                      <time dateTime={provider.last_checked_at}>
                        {localTime(provider.last_checked_at)}
                      </time>
                    ) : (
                      "—"
                    )}
                  </dd>
                </div>
                {provider.problems.length > 0 ? (
                  <div>
                    <dt>{t("workspaces.provider.problems")}</dt>
                    <dd>
                      <ul>
                        {provider.problems.map((problem) => (
                          <li key={problem}>{t(`providers.problem.${problem}` as Key)}</li>
                        ))}
                      </ul>
                    </dd>
                  </div>
                ) : null}
              </dl>
            ) : null}

            {!isProvider && git !== null ? (
              <dl className="settings-facts">
                <div>
                  <dt>{t("workspaces.provider.version")}</dt>
                  <dd className="mono">{git.version}</dd>
                </div>
              </dl>
            ) : null}

            {result !== null ? (
              <dl className="settings-facts">
                <div>
                  <dt>{t("workspaces.check.status")}</dt>
                  <dd>{t(`workspaces.check.status.${result.status}`)}</dd>
                </div>
                <div>
                  <dt>{t("workspaces.check.version")}</dt>
                  <dd className="mono">{result.result.version}</dd>
                </div>
                <div>
                  <dt>{t("workspaces.check.duration")}</dt>
                  <dd>{result.duration_ms} ms</dd>
                </div>
                <div>
                  <dt>{t("workspaces.check.checked_at")}</dt>
                  <dd>
                    <time dateTime={result.checked_at}>{localTime(result.checked_at)}</time>
                  </dd>
                </div>
              </dl>
            ) : null}

            {error !== null ? (
              <p className="settings-error" role="alert">
                {errorText(error)}
              </p>
            ) : null}

            {isProvider &&
            provider !== null &&
            (providerCase !== null ||
              provider.billing === "blocked" ||
              provider.version_status === "older") ? (
              <section aria-labelledby="provider-setup-title">
                <h3 id="provider-setup-title">{t("cmd.provider.setup")}</h3>
                {providerCase === "missing" ? <MissingSetup onOpenUrl={onOpenUrl} /> : null}
                {providerCase === "unusable" ? <p>{t("workspaces.setup.unusable")}</p> : null}
                {providerCase === "config_dir" ? <p>{t("workspaces.setup.config_dir")}</p> : null}
                {providerCase === "login" ? <p>{t("workspaces.setup.login")}</p> : null}
                {provider.version_status === "older" ? (
                  <OlderSetup provider={provider} onOpenUrl={onOpenUrl} />
                ) : null}
                {provider.billing === "blocked" ? <p>{t("workspaces.setup.billing")}</p> : null}
                <p>{t("workspaces.setup.terminal_note")}</p>
              </section>
            ) : null}
          </div>
        );
      })}
      <ModelConnectionsPanel store={store} snapshot={snapshot} />
      <div className="settings-card">
        <McpSetup start={mcpStart} />
      </div>
    </section>
  );
}
