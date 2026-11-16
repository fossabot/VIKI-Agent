/** Starts Claude Code sign-in only after the user clicks. The caller controls whether the strip
 * is available; this component starts no chat message. The explanation line names the user's
 * terminal by `platformKind()`: a PowerShell window on Windows, a Terminal window on macOS, and
 * the desktop's terminal on Linux. */
import { type ReactElement, useRef, useState } from "react";
import { type ProviderLoginApi, ProviderLoginApiError } from "../../clients/app/providerLoginApi";
import type { Key } from "../../i18n/index";
import { t } from "../../i18n/t";
import { type PlatformKind, platformKind } from "../../platform";
import "./Errors.css";

export interface SignInStripProps {
  api: ProviderLoginApi;
  /** "setup" words the line and the opened status for the setup guide; chat is the default. */
  context?: "chat" | "setup";
}

const LINE_KEY: Readonly<Record<"chat" | "setup", Readonly<Record<PlatformKind, Key>>>> = {
  chat: {
    windows: "shell.signin.line",
    mac: "shell.signin.line_mac",
    linux: "shell.signin.line_linux",
  },
  setup: {
    windows: "onboarding.signin.line",
    mac: "onboarding.signin.line_mac",
    linux: "onboarding.signin.line_linux",
  },
};

const OPENED_KEY: Readonly<Record<"chat" | "setup", Key>> = {
  chat: "shell.signin.opened",
  setup: "onboarding.signin.opened",
};

export function SignInStrip({ api, context = "chat" }: SignInStripProps): ReactElement {
  const [busy, setBusy] = useState(false);
  const [opened, setOpened] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  const call = useRef<AbortController | null>(null);

  async function open(): Promise<void> {
    if (busy) return;
    call.current?.abort();
    const controller = new AbortController();
    call.current = controller;
    setBusy(true);
    setOpened(false);
    setRefusal(null);
    try {
      await api.open(controller.signal);
      setOpened(true);
    } catch (error) {
      setRefusal(error instanceof ProviderLoginApiError ? error.code : "unavailable");
    }
    setBusy(false);
  }

  return (
    <div className="signin-strip">
      <p className="error-line jp">{t(LINE_KEY[context][platformKind()])}</p>
      <div className="card-actions jp">
        <button type="button" className="btn alt sm" disabled={busy} onClick={() => void open()}>
          {busy ? t("shell.signin.opening") : t("shell.signin.button")}
        </button>
      </div>
      <p role="status" aria-live="polite">
        {opened ? t(OPENED_KEY[context]) : ""}
      </p>
      {refusal !== null ? (
        <p role="alert">
          {refusal === "provider_unavailable"
            ? t("shell.signin.provider")
            : t("shell.signin.failed")}
        </p>
      ) : null}
    </div>
  );
}
