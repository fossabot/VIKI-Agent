/**
 * The first-run view: a three-step setup guide (Claude Code, sign-in or a model connection, a
 * vault) whose third step holds three cards — Create, Open, Set up later — that swap for the
 * matching form when chosen (same view, `mode`); Back returns to the guide, Escape on a form
 * returns to the guide (Escape on the guide itself is App's own return-to-chat rule — not this
 * component's concern).
 */
import { type ReactElement, type ReactNode, useContext, useEffect, useRef, useState } from "react";
import { ModelConnectionsContext } from "../../clients/app/modelConnectionsApi";
import type { ProviderLoginApi } from "../../clients/app/providerLoginApi";
import type { CreateVaultResult, PolicyInspection, ShellApi } from "../../clients/app/shellApi";
import { type CommandState, commandFor, type Navigation } from "../../commands";
import { t, tf } from "../../i18n/t";
import type { ShellStore } from "../../state/shell";
import { useSnapshot } from "../../state/viki";
import { SignInStrip } from "../Cards/SignInStrip";
import { useConnectionLabels } from "../Profiles/modelLabel";
import { CreateVaultForm } from "./CreateVaultForm";
import "./Onboarding.css";
import { OpenVaultForm } from "./OpenVaultForm";
import { CLAUDE_SETUP_URL, setupSteps } from "./setupSteps";

export type OnboardingMode = "choose" | "create" | "open";

export interface OnboardingViewProps {
  store: ShellStore;
  api: ShellApi;
  mode: OnboardingMode;
  onMode: (mode: OnboardingMode) => void;
  /** Return to chat: after "Set up later" (even when the patch fails) and after a successful
   * select's restart-required panel. */
  onDone: () => void;
  /** App gives a Start with VIKI vault its team's Bots. */
  onVaultCreated?: (result: CreateVaultResult) => void;
  onNavigate: (nav: Partial<Navigation>) => void;
  /** The Settings `PolicyPanel`, reached only through this render prop — this view never
   * imports a Settings file. */
  renderInspection: (inspection: PolicyInspection, workspaceId: string) => ReactNode;
  /** Opens a link through the app's `open.url` path; absent: the setup-guide link is not shown. */
  onOpenUrl?: (url: string) => void;
  /** Claude Code's sign-in; absent: step 2 shows no Sign in button. */
  signIn?: ProviderLoginApi;
}

const OPTIONAL_KEYS = [
  "onboarding.guide.optional.bots",
  "onboarding.guide.optional.connections",
  "onboarding.guide.optional.code",
  "onboarding.guide.optional.routines",
  "onboarding.guide.optional.voice",
  "onboarding.guide.optional.messaging",
] as const;

export function OnboardingView({
  store,
  api,
  mode,
  onMode,
  onDone,
  onVaultCreated,
  onNavigate,
  renderInspection,
  onOpenUrl,
  signIn,
}: OnboardingViewProps): ReactElement {
  const snapshot = useSnapshot(store);
  const modelConnections = useContext(ModelConnectionsContext);
  const h1Ref = useRef<HTMLHeadingElement>(null);
  const [openPrefill, setOpenPrefill] = useState<string | undefined>(undefined);

  // The effect body never reads `mode`; the dependency is deliberate so the h1 refocuses on
  // every entry to this view, including a swap between the cards and a form.
  // biome-ignore lint/correctness/useExhaustiveDependencies: intentional re-run key, not a data dependency
  useEffect(() => {
    h1Ref.current?.focus();
  }, [mode]);

  useEffect(() => {
    if (mode === "choose") return;
    function onWindowKeyDown(event: globalThis.KeyboardEvent): void {
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key !== "Escape") return;
      // The app menu (TopBar/AppMenu.tsx) can open over a create/open form; Escape then belongs
      // to the menu alone (it closes; this form stays put). Checking the live DOM for an open
      // `[role="menu"]` — the same technique useModalFocus.ts uses to stand down for a nested
      // menu — makes this correct regardless of which of the two window listeners happens to run
      // first, rather than relying on registration order between sibling components.
      if (document.querySelector('[role="menu"]') !== null) return;
      event.stopImmediatePropagation();
      onMode("choose");
    }
    window.addEventListener("keydown", onWindowKeyDown);
    return () => window.removeEventListener("keydown", onWindowKeyDown);
  }, [mode, onMode]);

  function goToWorkspaces(): void {
    onNavigate({ view: "settings", page: "workspaces" });
  }

  const createState = commandFor("workspace.create", snapshot, "onboarding");
  const openState = commandFor("workspace.open", snapshot, "onboarding");
  const laterState = commandFor("workspace.later", snapshot, "onboarding");
  const defaultModel = snapshot.settings?.effective["chat.default_model"];
  const connectionDefault =
    typeof defaultModel === "string" && defaultModel.startsWith("viki-llm/");
  const connections = useConnectionLabels(connectionDefault, defaultModel);
  const steps = setupSteps(snapshot, connections);
  const goToProviders = (): void => onNavigate({ view: "settings", page: "providers" });
  const providerSetup = (
    <button type="button" className="btn alt sm" onClick={goToProviders}>
      {t("cmd.provider.setup")}
    </button>
  );

  return (
    <section className="onboarding-view" aria-labelledby="onboarding-title">
      <h1 id="onboarding-title" tabIndex={-1} ref={h1Ref}>
        {t("onboarding.title")}
      </h1>
      {mode === "choose" ? (
        <>
          <p className="onboarding-lead">{t("onboarding.guide.lead")}</p>
          <ol className="setup-steps" aria-label={t("onboarding.guide.steps")}>
            <SetupStep
              id="setup-step-claude"
              number={1}
              title={t("onboarding.guide.claude.title")}
              state={steps.claude.state}
              body={t("onboarding.guide.claude.body")}
            >
              {steps.claude.state === "needed" ? (
                <>
                  <p>{t(steps.claude.problem)}</p>
                  <div className="setup-actions">
                    {onOpenUrl !== undefined && (
                      <button
                        type="button"
                        className="btn alt sm"
                        onClick={() => onOpenUrl(CLAUDE_SETUP_URL)}
                      >
                        {t("providers.setup.guide")}
                      </button>
                    )}
                    {providerSetup}
                  </div>
                </>
              ) : steps.claude.state === "done" ? (
                <>
                  <p>
                    {steps.claude.version === ""
                      ? t("onboarding.guide.claude.found_unknown")
                      : tf("onboarding.guide.claude.found", { version: steps.claude.version })}
                  </p>
                  {steps.claude.older && (
                    <>
                      <p>
                        {tf("onboarding.guide.claude.older", {
                          min: steps.claude.tested[0],
                          max: steps.claude.tested[1],
                        })}
                      </p>
                      <div className="setup-actions">{providerSetup}</div>
                    </>
                  )}
                </>
              ) : null}
            </SetupStep>
            <SetupStep
              id="setup-step-model"
              number={2}
              title={t("onboarding.guide.model.title")}
              state={steps.model.state}
              body={t("onboarding.guide.model.body")}
            >
              {"connection" in steps.model ? (
                <>
                  <p>
                    {t(
                      steps.model.connection === "missing"
                        ? "onboarding.guide.model.connection_missing"
                        : "onboarding.guide.model.connection_unread",
                    )}
                  </p>
                  <div className="setup-actions">{providerSetup}</div>
                </>
              ) : steps.model.state === "needed" && steps.model.billing ? (
                <>
                  <p>{t("workspaces.setup.billing")}</p>
                  <div className="setup-actions">{providerSetup}</div>
                </>
              ) : steps.model.state === "needed" && steps.model.connectionMin !== undefined ? (
                <>
                  <p>
                    {tf("onboarding.guide.model.connection_version", {
                      min: steps.model.connectionMin,
                    })}
                  </p>
                  <div className="setup-actions">{providerSetup}</div>
                </>
              ) : steps.model.state === "needed" || steps.model.state === "unchecked" ? (
                <>
                  <p>
                    {t(
                      steps.model.state === "needed"
                        ? "onboarding.guide.model.signed_out"
                        : "onboarding.guide.model.unchecked",
                    )}
                  </p>
                  {signIn !== undefined && <SignInStrip api={signIn} context="setup" />}
                  {modelConnections !== null && (
                    <div className="setup-actions">
                      <button type="button" className="btn alt sm" onClick={goToProviders}>
                        {t("llm.setup")}
                      </button>
                    </div>
                  )}
                </>
              ) : steps.model.state === "waiting" ? (
                <p>{t("onboarding.guide.model.waiting")}</p>
              ) : steps.model.state === "done" ? (
                <p>
                  {t(
                    steps.model.route === "connection"
                      ? "onboarding.guide.model.connection"
                      : "onboarding.guide.model.signed_in",
                  )}
                </p>
              ) : null}
            </SetupStep>
            <SetupStep
              id="setup-step-vault"
              number={3}
              title={t("onboarding.guide.vault.title")}
              state={steps.vault.state}
              body={t("onboarding.guide.vault.body")}
            >
              {steps.vault.state === "done" ? (
                <p>{tf("onboarding.guide.vault.done", { name: steps.vault.name })}</p>
              ) : steps.vault.state === "restart" ? (
                <p>{t("onboarding.guide.vault.restart")}</p>
              ) : (
                <>
                  {steps.vault.state === "needed" && steps.vault.problem !== null && (
                    <p>{t(steps.vault.problem)}</p>
                  )}
                  <fieldset className="onboarding-cards" aria-labelledby="setup-step-vault-title">
                    <OnboardingCard
                      id="onboarding-card-create"
                      label={t("cmd.workspace.create")}
                      desc={t("onboarding.choose.create_desc")}
                      state={createState}
                      onClick={() => onMode("create")}
                    />
                    <OnboardingCard
                      id="onboarding-card-open"
                      label={t("cmd.workspace.open")}
                      desc={t("onboarding.choose.open_desc")}
                      state={openState}
                      onClick={() => onMode("open")}
                    />
                    <OnboardingCard
                      id="onboarding-card-later"
                      label={t("cmd.workspace.later")}
                      desc={t("onboarding.choose.later_desc")}
                      state={laterState}
                      onClick={() => {
                        void store
                          .patch({ "onboarding.completed": true })
                          .catch(() => {})
                          .finally(() => onDone());
                      }}
                    />
                  </fieldset>
                </>
              )}
            </SetupStep>
          </ol>
          <section className="setup-optional" aria-labelledby="setup-optional-title">
            <h2 id="setup-optional-title">{t("onboarding.guide.optional.title")}</h2>
            <ul>
              {OPTIONAL_KEYS.map((key) => (
                <li key={key}>{t(key)}</li>
              ))}
            </ul>
          </section>
          <p className="setup-later">{t("onboarding.guide.later")}</p>
        </>
      ) : mode === "create" ? (
        <CreateVaultForm
          api={api}
          store={store}
          onBack={() => onMode("choose")}
          onDone={onDone}
          onGoToWorkspaces={goToWorkspaces}
          onOpenExisting={(path) => {
            setOpenPrefill(path);
            onMode("open");
          }}
          {...(onVaultCreated !== undefined ? { onCreated: onVaultCreated } : {})}
        />
      ) : (
        <OpenVaultForm
          api={api}
          store={store}
          {...(openPrefill !== undefined ? { initialPath: openPrefill } : {})}
          onBack={() => onMode("choose")}
          onDone={onDone}
          onGoToWorkspaces={goToWorkspaces}
          renderInspection={renderInspection}
        />
      )}
    </section>
  );
}

interface SetupStepProps {
  id: string;
  number: number;
  title: string;
  state: "checking" | "done" | "needed" | "unchecked" | "waiting" | "restart";
  body: string;
  children?: ReactNode;
}

/** One list item named by its heading; the state is text, so it never rests on colour alone. The
 * visible number is hidden from the name because the list already announces the position. */
function SetupStep({ id, number, title, state, body, children }: SetupStepProps): ReactElement {
  return (
    <li className="setup-step" data-state={state} aria-labelledby={`${id}-title`}>
      <div className="setup-step-head">
        <h2 id={`${id}-title`}>
          <span aria-hidden="true">{`${number}. `}</span>
          {title}
        </h2>
        <span className="setup-state">{t(`onboarding.guide.state.${state}`)}</span>
      </div>
      <p>{body}</p>
      {children}
    </li>
  );
}

interface OnboardingCardProps {
  id: string;
  label: string;
  desc: string;
  state: CommandState;
  onClick: () => void;
}

/** A card is a disabled-focusable button: never hidden, never `disabled`,
 * its reason in a visually hidden span named by `aria-describedby` and repeated in `title`. */
function OnboardingCard({ id, label, desc, state, onClick }: OnboardingCardProps): ReactElement {
  const reasonId = `${id}-reason`;
  return (
    <button
      type="button"
      id={id}
      className="onboarding-card"
      aria-disabled={!state.enabled}
      {...(state.reason !== null ? { "aria-describedby": reasonId, title: t(state.reason) } : {})}
      onClick={() => {
        if (!state.enabled) return;
        onClick();
      }}
    >
      <span className="onboarding-card-label">{label}</span>
      <span className="onboarding-card-desc">{desc}</span>
      {state.reason !== null ? (
        <span id={reasonId} className="sr-only">
          {t(state.reason)}
        </span>
      ) : null}
    </button>
  );
}
