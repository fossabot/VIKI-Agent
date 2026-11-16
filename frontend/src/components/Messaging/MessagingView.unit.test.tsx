import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, expect, test, vi } from "vitest";
import {
  MESSAGING_PROVIDERS,
  type MessagingApi,
  MessagingApiError,
  type MessagingConfig,
  type MessagingConfigSource,
  type MessagingProvider,
  type MessagingProviderConfig,
  type MessagingStatus,
  type MessagingStopReason,
} from "../../clients/messaging/messagingApi";
import { readCss } from "../../testing/css";
import { MessagingView } from "./MessagingView";

const status = (state: MessagingStatus["mattermost"]["state"]) => ({
  configured: state !== "not_configured",
  state,
  restart_required: false,
  reason: null,
});
const reading = (state: MessagingStatus["mattermost"]["state"]): MessagingStatus => ({
  mattermost: status(state),
  slack: status(state),
  discord: status(state),
});
function providerConfig(
  source: MessagingConfigSource,
  values: MessagingProviderConfig["values"] = {},
): MessagingProviderConfig {
  return { source, values };
}
const allConfig = (source: MessagingConfigSource): MessagingConfig => ({
  mattermost: providerConfig(source),
  slack: providerConfig(source),
  discord: providerConfig(source),
});
const api = (state: MessagingStatus["mattermost"]["state"]): MessagingApi => ({
  get: vi.fn(async () => reading(state)),
  enable: vi.fn(async () => reading("connected")),
  disable: vi.fn(async () => reading("disabled")),
  disableMattermost: vi.fn(async () => reading("disabled")),
  config: vi.fn(async () => allConfig("none")),
  save: vi.fn(async () => reading("disabled")),
  remove: vi.fn(async () => reading("not_configured")),
});
const PROVIDER_NAME: Record<MessagingProvider, string> = {
  mattermost: "Mattermost",
  slack: "Slack",
  discord: "Discord",
};
const card = (provider: MessagingProvider = "mattermost") =>
  within(screen.getByRole("region", { name: `${PROVIDER_NAME[provider]} bot` }));
/** The row's switch (role="switch", named "<Name> bot" via `aria-labelledby`) — distinct from the
 * disclosure button, which shares the same accessible name but keeps the default "button" role. */
function toggle(provider: MessagingProvider = "mattermost") {
  return card(provider).getByRole<HTMLButtonElement>("switch", {
    name: `${PROVIDER_NAME[provider]} bot`,
  });
}
/** Clicks the row's disclosure (the "<Name> bot" heading button) to open or close its body.
 * `getByRole` skips content under the body's `hidden` attribute, so any `getByRole` lookup on
 * body content — a text field, "Save changes", "Open setup guide" — needs this first. */
function openRow(provider: MessagingProvider = "mattermost"): void {
  fireEvent.click(card(provider).getByRole("button", { name: `${PROVIDER_NAME[provider]} bot` }));
}
function expanded(provider: MessagingProvider): string | null {
  return card(provider)
    .getByRole("button", { name: `${PROVIDER_NAME[provider]} bot` })
    .getAttribute("aria-expanded");
}
async function settled() {
  await act(async () => {
    for (let index = 0; index < 3; index++) await Promise.resolve();
  });
}
function deferred<T>() {
  let resolve!: (result: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

test("the head is a page-head: title, intro, actions, then the about details", async () => {
  render(<MessagingView api={api("disabled")} />);
  await settled();
  const head = document.querySelector("header.page-head") as HTMLElement;
  expect(
    Array.from(head.children).map((el) => el.tagName + (el.className && `.${el.className}`)),
  ).toEqual(["H1.page-title", "P.page-intro", "DIV.page-actions", "DETAILS.messaging-about"]);
  const title = head.querySelector("h1") as HTMLElement;
  expect(title.tabIndex).toBe(-1);
  expect(document.querySelector("section")?.getAttribute("aria-labelledby")).toBe(title.id);
  expect(within(head).getByRole("button", { name: "Refresh status" }).className).toBe("btn alt");
});

test("the stylesheet drops the old head rules and stills motion under data-motion=reduce", () => {
  const css = readCss(import.meta.url, "MessagingView.css");
  expect(css).not.toMatch(/messaging-title-row|messaging-heading/);
  expect(css).toContain(".messaging-about p {");
  const reduce = css.slice(css.lastIndexOf("@media (prefers-reduced-motion: no-preference)"));
  expect(reduce).toMatch(
    /html\[data-motion="reduce"\] \.messaging-disclosure \.ico,\s*html\[data-motion="reduce"\] \.messaging-switch-track,\s*html\[data-motion="reduce"\] \.messaging-switch-track::after \{\s*transition: none;/,
  );
});

test("official local logos are decorative beside the same provider headings and controls", async () => {
  const service = api("disabled");
  const { container } = render(<MessagingView api={service} />);
  await settled();
  expect(
    screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent),
  ).toEqual(["Mattermost bot", "Slack bot", "Discord bot"]);
  expect(container.querySelectorAll("img")).toHaveLength(3);
  expect(screen.queryByRole("img")).toBeNull();
  for (const [provider, name, extension] of [
    ["mattermost", "Mattermost", "svg"],
    ["slack", "Slack", "png"],
    ["discord", "Discord", "svg"],
  ] as const) {
    const heading = card(provider).getByRole("heading", { name: `${name} bot` });
    const logo = heading.querySelector("img");
    expect(logo).not.toBeNull();
    expect(logo?.getAttribute("alt")).toBe("");
    expect(logo?.getAttribute("aria-hidden")).toBe("true");
    expect(logo?.getAttribute("src")).toBe(`/logos/${provider}.${extension}`);
    expect(logo?.getAttribute("src")).not.toMatch(/^(?:https?:)?\/\//);
    expect(card(provider).getByText("Disabled")).toBeTruthy();
    expect(toggle(provider).getAttribute("aria-checked")).toBe("false");
  }
  expect(service.enable).not.toHaveBeenCalled();
  await act(async () => void fireEvent.click(toggle("slack")));
  expect(service.enable).toHaveBeenCalledWith("slack", expect.any(AbortSignal));
  expect(card("slack").getByText("Connected")).toBeTruthy();
  expect(toggle("slack").getAttribute("aria-checked")).toBe("true");
  expect(card("mattermost").getByText("Disabled")).toBeTruthy();
  expect(card("discord").getByText("Disabled")).toBeTruthy();
});

test("an unconfigured bot explains setup with credential fields and no connect controls", async () => {
  render(<MessagingView api={api("not_configured")} />);
  await settled();
  expect(screen.getByRole("heading", { name: "Messaging" })).toBe(document.activeElement);
  openRow();
  expect(card().getByText("Not configured")).toBeTruthy();
  expect(card().getByText("Needs setup")).toBeTruthy();
  expect(screen.getByText(/Save each provider's settings below and turn it on/)).toBeTruthy();
  expect(
    screen.getByText(/Member role, copy its access token and invite it to your team/),
  ).toBeTruthy();
  expect(screen.getByText(/human Mattermost user ID.*MFA/)).toBeTruthy();
  expect(screen.getByText("!viki help")).toBeTruthy();
  expect(screen.getByText(/owner.*private direct messages/)).toBeTruthy();
  expect(screen.getByText(/host awake.*window open/)).toBeTruthy();
  expect(card().getByText(/retention policy/)).toBeTruthy();
  expect(card().getByRole("textbox", { name: "Server URL" })).toBeTruthy();
  expect(card().getByText("Bot token")).toBeTruthy();
  expect(screen.queryByRole("button", { name: /Connect|Disable/ })).toBeNull();
});

test.each(["connecting", "connected"] as const)(
  "the %s bot can be disabled once",
  async (state) => {
    const service = api(state);
    const pending = deferred<MessagingStatus>();
    vi.mocked(service.disable).mockReturnValue(pending.promise);
    render(<MessagingView api={service} />);
    await settled();
    expect(card().getByText(state === "connecting" ? "Connecting" : "Connected")).toBeTruthy();
    const disable = toggle();
    expect(disable.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(disable);
    fireEvent.click(disable);
    expect(service.disable).toHaveBeenCalledTimes(1);
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Refresh status" }).disabled).toBe(
      false,
    );
    await act(async () => pending.resolve(reading("disabled")));
    expect(card().getByText("Disabled")).toBeTruthy();
    expect(toggle().getAttribute("aria-checked")).toBe("false");
  },
);

test("a failed disable keeps the last status and lets the user refresh or retry", async () => {
  const service = api("connected");
  vi.mocked(service.disable).mockRejectedValue(new Error("private backend details"));
  render(<MessagingView api={service} />);
  await settled();
  await act(async () => void fireEvent.click(toggle()));
  expect(card().getByRole("alert").textContent).toMatch(/Couldn't confirm.*stopped/);
  expect(card().getByRole("alert").textContent).not.toContain("private backend details");
  expect(card().queryByText("Stopped")).toBeNull();
  expect(card().getByText("Connected")).toBeTruthy();
  vi.mocked(service.get).mockResolvedValue(reading("stopped"));
  await act(
    async () => void fireEvent.click(screen.getByRole("button", { name: "Refresh status" })),
  );
  expect(card().queryByRole("alert")).toBeNull();
  expect(card().getByText("Stopped")).toBeTruthy();
});

test("status failure is retryable and refresh does not retain a current-looking old reading", async () => {
  const service = api("connected");
  vi.mocked(service.get).mockRejectedValueOnce(new Error("unavailable"));
  render(<MessagingView api={service} />);
  await settled();
  expect(card().getByRole("alert").textContent).toMatch(/status is unavailable/);
  expect(card().queryByText("Connected")).toBeNull();
  await act(
    async () => void fireEvent.click(screen.getByRole("button", { name: "Refresh status" })),
  );
  expect(card().getByText("Connected")).toBeTruthy();
  const pending = deferred<MessagingStatus>();
  vi.mocked(service.get).mockReturnValueOnce(pending.promise);
  fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));
  expect(card().getByText("Reading status…")).toBeTruthy();
  expect(card().queryByText("Connected")).toBeNull();
  await act(async () => pending.resolve(reading("stopped")));
  expect(card().getByText("Stopped")).toBeTruthy();
});

test("late old-client results and errors cannot overwrite the current client's status", async () => {
  const old = api("connected");
  const pending = deferred<MessagingStatus>();
  vi.mocked(old.get).mockReturnValue(pending.promise);
  const { rerender } = render(<MessagingView api={old} />);
  const signal = vi.mocked(old.get).mock.calls[0]?.[0];
  rerender(<MessagingView api={api("not_configured")} />);
  await settled();
  expect(signal?.aborted).toBe(true);
  await act(async () => pending.resolve(reading("connected")));
  expect(card().getByText("Not configured")).toBeTruthy();
  expect(card().queryByText("Connected")).toBeNull();
});

test.each(["get", "disable", "enable"] as const)(
  "unmount cancels a pending %s request",
  async (method) => {
    const service = api("connected");
    const pending = deferred<MessagingStatus>();
    if (method === "get") vi.mocked(service.get).mockReturnValue(pending.promise);
    else vi.mocked(service[method]).mockReturnValue(pending.promise);
    const { unmount } = render(<MessagingView api={service} />);
    if (method !== "get") {
      await settled();
      if (method === "enable") {
        vi.mocked(service.get).mockResolvedValue(reading("disabled"));
        await act(
          async () => void fireEvent.click(screen.getByRole("button", { name: "Refresh status" })),
        );
      }
      fireEvent.click(toggle());
    }
    const signal =
      method === "get"
        ? vi.mocked(service.get).mock.calls[0]?.[0]
        : vi.mocked(service[method]).mock.calls[0]?.[1];
    unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => pending.reject(new Error("late rejection")));
  },
);

test("the existing Capabilities destination is shown only with a real navigation callback", async () => {
  const onCapabilities = vi.fn();
  const service = api("not_configured");
  const { rerender } = render(<MessagingView api={service} onCapabilities={onCapabilities} />);
  await settled();
  expect(screen.getByText(/observed plugins in Capabilities/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Open Capabilities" }));
  expect(onCapabilities).toHaveBeenCalledOnce();
  rerender(<MessagingView api={service} />);
  expect(screen.queryByRole("button", { name: "Open Capabilities" })).toBeNull();
});

test("test_disable_enable_and_other_provider_state_remain_independent", async () => {
  const service = api("connected");
  render(<MessagingView api={service} />);
  await settled();
  expect(service.enable).not.toHaveBeenCalled();
  await act(async () => void fireEvent.click(toggle()));
  expect(card().getByText("Disabled")).toBeTruthy();
  expect(card("slack").getByText("Connected")).toBeTruthy();
  expect(card("discord").getByText("Connected")).toBeTruthy();
  await act(async () => void fireEvent.click(toggle()));
  expect(service.enable).toHaveBeenCalledWith("mattermost", expect.any(AbortSignal));
  expect(card().getByText("Connected")).toBeTruthy();
  await act(async () => void fireEvent.click(toggle("slack")));
  expect(card("slack").getByText("Disabled")).toBeTruthy();
  expect(card().getByText("Connected")).toBeTruthy();
  expect(card("discord").getByText("Connected")).toBeTruthy();
});

test("Refresh before Disable cannot revive the disabled provider", async () => {
  const service = api("connected");
  render(<MessagingView api={service} />);
  await settled();
  const refresh = deferred<MessagingStatus>();
  vi.mocked(service.get).mockReturnValueOnce(refresh.promise);
  fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));
  await act(async () => void fireEvent.click(toggle()));
  await act(async () =>
    refresh.resolve({
      mattermost: status("connected"),
      slack: status("disabled"),
      discord: status("stopped"),
    }),
  );
  expect(card().getByText("Disabled")).toBeTruthy();
  expect(card("slack").getByText("Disabled")).toBeTruthy();
  expect(toggle("discord").getAttribute("aria-checked")).toBe("false");
});

test("crossed Slack and Discord replies apply only their own provider", async () => {
  const service = api("disabled"),
    slack = deferred<MessagingStatus>(),
    discord = deferred<MessagingStatus>();
  vi.mocked(service.enable).mockImplementation((provider) =>
    provider === "slack" ? slack.promise : discord.promise,
  );
  render(<MessagingView api={service} />);
  await settled();
  fireEvent.click(toggle("slack"));
  fireEvent.click(toggle("discord"));
  await act(async () =>
    discord.resolve({
      ...reading("disabled"),
      discord: status("connected"),
      slack: status("stopped"),
    }),
  );
  expect(card("discord").getByText("Connected")).toBeTruthy();
  expect(card("slack").getByText("Enabling…")).toBeTruthy();
  await act(async () => slack.resolve({ ...reading("disabled"), slack: status("connected") }));
  expect(card("slack").getByText("Connected")).toBeTruthy();
  expect(card("discord").getByText("Connected")).toBeTruthy();
  expect(card().getByText("Disabled")).toBeTruthy();
});

test("Disable supersedes a pending Enable and ignores its late result", async () => {
  const service = api("disabled"),
    enable = deferred<MessagingStatus>();
  vi.mocked(service.enable).mockReturnValue(enable.promise);
  render(<MessagingView api={service} />);
  await settled();
  fireEvent.click(toggle("slack"));
  const signal = vi.mocked(service.enable).mock.calls[0]?.[1];
  await act(async () => void fireEvent.click(toggle("slack")));
  expect(signal?.aborted).toBe(true);
  await act(async () => enable.resolve(reading("connected")));
  expect(card("slack").getByText("Disabled")).toBeTruthy();
});

test("pending Disable stays disabled while both Enable and cleanup are unresolved", async () => {
  const service = api("disabled"),
    enable = deferred<MessagingStatus>(),
    disable = deferred<MessagingStatus>();
  vi.mocked(service.enable).mockReturnValue(enable.promise);
  vi.mocked(service.disable).mockReturnValue(disable.promise);
  render(<MessagingView api={service} />);
  await settled();
  fireEvent.click(toggle("slack"));
  fireEvent.click(toggle("slack"));
  const stopping = toggle("slack");
  expect(card("slack").getByText("Disabling…")).toBeTruthy();
  expect(stopping.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(stopping);
  fireEvent.click(stopping);
  expect(service.enable).toHaveBeenCalledTimes(1);
  expect(service.disable).toHaveBeenCalledTimes(1);
  await act(async () => enable.resolve(reading("connected")));
  expect(toggle("slack").getAttribute("aria-disabled")).toBe("true");
  await act(async () => disable.resolve(reading("disabled")));
  expect(card("slack").getByText("Disabled")).toBeTruthy();
  expect(toggle("slack").getAttribute("aria-checked")).toBe("false");
});

test("stopped connections can Connect but root invalidation requires restart", async () => {
  const service = api("stopped");
  vi.mocked(service.get).mockResolvedValue({
    ...reading("stopped"),
    discord: { ...status("stopped"), restart_required: true },
  });
  render(<MessagingView api={service} />);
  await settled();
  expect(toggle("slack").getAttribute("aria-checked")).toBe("false");
  expect(toggle("slack").getAttribute("aria-disabled")).toBeNull();
  expect(card("discord").getByText(/Restart VIKI Agent/)).toBeTruthy();
  const discordSwitch = toggle("discord");
  expect(discordSwitch.getAttribute("aria-disabled")).toBe("true");
  const restartNote = card("discord").getByText(/Restart VIKI Agent/);
  expect(discordSwitch.getAttribute("aria-describedby")?.split(" ")).toContain(restartNote.id);
  fireEvent.click(discordSwitch);
  expect(service.enable).not.toHaveBeenCalled();
});

test("restart_required refusal removes Connect until the app restarts", async () => {
  const service = api("disabled");
  vi.mocked(service.enable).mockRejectedValue(new MessagingApiError(409, "restart_required"));
  render(<MessagingView api={service} />);
  await settled();
  await act(async () => void fireEvent.click(toggle("slack")));
  expect(card("slack").getByText("Stopped")).toBeTruthy();
  expect(toggle("slack").getAttribute("aria-disabled")).toBe("true");
  await act(async () => void fireEvent.click(toggle("slack")));
  expect(service.enable).toHaveBeenCalledTimes(1);
  expect(toggle("discord").getAttribute("aria-disabled")).toBeNull();
});

test("cooldown remains local through Refresh and expires without a request", async () => {
  vi.useFakeTimers();
  const service = api("disabled");
  vi.mocked(service.enable).mockRejectedValue(new MessagingApiError(429, "cooldown", 7));
  render(<MessagingView api={service} />);
  await settled();
  await act(async () => void fireEvent.click(toggle("slack")));
  expect(toggle("slack").getAttribute("aria-disabled")).toBe("true");
  expect(toggle("discord").getAttribute("aria-disabled")).toBeNull();
  await act(
    async () => void fireEvent.click(screen.getByRole("button", { name: "Refresh status" })),
  );
  expect(toggle("slack").getAttribute("aria-disabled")).toBe("true");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(7000);
  });
  expect(toggle("slack").getAttribute("aria-disabled")).toBeNull();
  expect(service.enable).toHaveBeenCalledTimes(1);
});

test("Slack and Discord setup explains credentials with Socket Mode and client URL notes", async () => {
  render(<MessagingView api={api("not_configured")} />);
  await settled();
  openRow("slack");
  openRow("discord");
  expect(card("slack").getByText(/install it, then paste its bot token/)).toBeTruthy();
  expect(
    card("discord").getByText(/Use a public HTTPS app or support page for the client URL\./),
  ).toBeTruthy();
  expect(card("slack").getByRole("textbox", { name: "Your member ID" })).toBeTruthy();
  expect(card("discord").getByRole("textbox", { name: "Your user ID" })).toBeTruthy();
  expect(screen.getByText(/Save each provider's settings below and turn it on/)).toBeTruthy();
});

test("source pill reflects saved, unreadable and missing settings only once ready", async () => {
  const service = api("disabled");
  vi.mocked(service.config).mockResolvedValue({
    mattermost: providerConfig("saved", { url: "https://mm.example.com", owner_id: "abc123" }),
    slack: providerConfig("unreadable"),
    discord: providerConfig("none"),
  });
  render(<MessagingView api={service} />);
  expect(screen.queryByText("Saved settings")).toBeNull();
  await settled();
  expect(card("mattermost").getByText("Saved settings")).toBeTruthy();
  expect(card("slack").getByText("Settings unreadable")).toBeTruthy();
  expect(card("discord").getByText("Needs setup")).toBeTruthy();
});

test("a failed settings read shows a retry note and never touches provider status", async () => {
  const service = api("connected");
  vi.mocked(service.config).mockRejectedValue(new Error("unavailable"));
  render(<MessagingView api={service} />);
  await settled();
  expect(card().getByText("Settings are unavailable. Refresh status to try again.")).toBeTruthy();
  expect(card().getByText("Connected")).toBeTruthy();
  expect(card().queryByText(/Saved settings|Needs setup|Settings unreadable/)).toBeNull();
});

test("the configured toggle carries a switch track and reflects aria-checked", async () => {
  const service = api("disabled");
  render(<MessagingView api={service} />);
  await settled();
  const control = toggle();
  expect(control.className).toBe("messaging-toggle");
  expect(control.getAttribute("aria-checked")).toBe("false");
  expect(control.querySelector(".messaging-switch-track")).not.toBeNull();
  await act(async () => void fireEvent.click(control));
  expect(toggle().getAttribute("aria-checked")).toBe("true");
});

test("Open setup guide calls onOpenUrl with each provider's guide URL", async () => {
  const onOpenUrl = vi.fn();
  render(<MessagingView api={api("disabled")} onOpenUrl={onOpenUrl} />);
  await settled();
  openRow("mattermost");
  openRow("slack");
  openRow("discord");
  fireEvent.click(card("mattermost").getByRole("button", { name: "Open setup guide" }));
  expect(onOpenUrl).toHaveBeenCalledWith(
    "https://docs.mattermost.com/developers/integrate/reference/bot-accounts",
  );
  fireEvent.click(card("slack").getByRole("button", { name: "Open setup guide" }));
  expect(onOpenUrl).toHaveBeenCalledWith(
    "https://docs.slack.dev/apis/events-api/using-socket-mode/",
  );
  fireEvent.click(card("discord").getByRole("button", { name: "Open setup guide" }));
  expect(onOpenUrl).toHaveBeenCalledWith(
    "https://docs.discord.com/developers/quick-start/getting-started",
  );
});

test("Open setup guide is absent without onOpenUrl", async () => {
  render(<MessagingView api={api("disabled")} />);
  await settled();
  openRow("mattermost");
  openRow("slack");
  openRow("discord");
  expect(screen.queryByRole("button", { name: "Open setup guide" })).toBeNull();
});

test("filling required fields and saving updates status, chip and reloads settings", async () => {
  const service = api("disabled");
  vi.mocked(service.config)
    .mockResolvedValueOnce(allConfig("none"))
    .mockResolvedValue({
      mattermost: providerConfig("saved", { url: "https://mm.example.com", owner_id: "abc123" }),
      slack: providerConfig("none"),
      discord: providerConfig("none"),
    });
  const pending = deferred<MessagingStatus>();
  vi.mocked(service.save).mockReturnValue(pending.promise);
  render(<MessagingView api={service} />);
  await settled();
  openRow();
  fireEvent.change(card().getByRole("textbox", { name: "Server URL" }), {
    target: { value: "https://mm.example.com" },
  });
  fireEvent.change(card().getByRole("textbox", { name: "Your user ID" }), {
    target: { value: "abc123" },
  });
  fireEvent.change(card().getByLabelText("Bot token"), { target: { value: "token-value" } });
  fireEvent.click(card().getByRole("button", { name: "Save changes" }));
  expect(card().getByText("Saving…")).toBeTruthy();
  await act(async () =>
    pending.resolve({ ...reading("disabled"), mattermost: status("disabled") }),
  );
  expect(service.save).toHaveBeenCalledWith(
    "mattermost",
    { url: "https://mm.example.com", owner_id: "abc123", bot_token: "token-value" },
    expect.any(AbortSignal),
  );
  // The token input is cleared once the save lands.
  expect((card().getByLabelText("Bot token") as HTMLInputElement).value).toBe("");
  await settled();
  expect(card().getByText("Saved settings")).toBeTruthy();
});

test("an invalid save shows the field-specific message, a plain failure shows the generic one", async () => {
  const service = api("disabled");
  render(<MessagingView api={service} />);
  await settled();
  openRow();
  fireEvent.change(card().getByRole("textbox", { name: "Server URL" }), {
    target: { value: "https://mm.example.com" },
  });
  fireEvent.change(card().getByRole("textbox", { name: "Your user ID" }), {
    target: { value: "abc123" },
  });
  fireEvent.change(card().getByLabelText("Bot token"), { target: { value: "token-value" } });
  vi.mocked(service.save).mockRejectedValueOnce(new MessagingApiError(400, "invalid_config"));
  await act(async () => void fireEvent.click(card().getByRole("button", { name: "Save changes" })));
  expect(card().getByRole("alert").textContent).toBe(
    "Check the Mattermost settings. A value is missing or in the wrong format.",
  );
  vi.mocked(service.save).mockRejectedValueOnce(new Error("private backend detail"));
  await act(async () => void fireEvent.click(card().getByRole("button", { name: "Save changes" })));
  expect(card().getByRole("alert").textContent).toBe(
    "Couldn't save the Mattermost settings. Try again.",
  );
  expect(card().getByRole("alert").textContent).not.toContain("private backend detail");
});

test("Remove saved settings shows Removing…, surfaces a failure, then reloads settings on success", async () => {
  const service = api("disabled");
  vi.mocked(service.config)
    .mockResolvedValueOnce(
      (() => ({
        mattermost: providerConfig("saved", {
          url: "https://mm.example.com",
          owner_id: "abc123",
        }),
        slack: providerConfig("none"),
        discord: providerConfig("none"),
      }))(),
    )
    .mockResolvedValue(allConfig("none"));
  render(<MessagingView api={service} />);
  await settled();
  openRow();
  vi.mocked(service.remove).mockRejectedValueOnce(new Error("private backend detail"));
  await act(
    async () => void fireEvent.click(card().getByRole("button", { name: "Remove saved settings" })),
  );
  expect(card().getByRole("alert").textContent).toBe(
    "Couldn't remove the Mattermost settings. Try again.",
  );
  const pending = deferred<MessagingStatus>();
  vi.mocked(service.remove).mockReturnValue(pending.promise);
  fireEvent.click(card().getByRole("button", { name: "Remove saved settings" }));
  expect(card().getByText("Removing…")).toBeTruthy();
  await act(async () =>
    pending.resolve({ ...reading("disabled"), mattermost: status("not_configured") }),
  );
  expect(service.remove).toHaveBeenCalledWith("mattermost", expect.any(AbortSignal));
  await settled();
  expect(card().getByText("Needs setup")).toBeTruthy();
});

// A settings re-read (this card's own save, or an unrelated Refresh) must not wipe
// what the user is still typing in a different card.
test("typed input in another card survives this card's save and a status refresh", async () => {
  const service = api("disabled");
  render(<MessagingView api={service} />);
  await settled();
  openRow("mattermost");
  openRow("slack");
  fireEvent.change(card("slack").getByLabelText("Bot token"), {
    target: { value: "typed-bot-token" },
  });
  fireEvent.change(card("slack").getByLabelText("App token"), {
    target: { value: "typed-app-token" },
  });
  fireEvent.change(card("slack").getByLabelText("Your member ID"), {
    target: { value: "typed-member-id" },
  });
  fireEvent.change(card().getByRole("textbox", { name: "Server URL" }), {
    target: { value: "https://mm.example.com" },
  });
  fireEvent.change(card().getByRole("textbox", { name: "Your user ID" }), {
    target: { value: "abc123" },
  });
  fireEvent.change(card().getByLabelText("Bot token"), { target: { value: "mm-token" } });
  await act(async () => void fireEvent.click(card().getByRole("button", { name: "Save changes" })));
  expect((card("slack").getByLabelText("Bot token") as HTMLInputElement).value).toBe(
    "typed-bot-token",
  );
  expect((card("slack").getByLabelText("App token") as HTMLInputElement).value).toBe(
    "typed-app-token",
  );
  expect((card("slack").getByLabelText("Your member ID") as HTMLInputElement).value).toBe(
    "typed-member-id",
  );
  await act(
    async () => void fireEvent.click(screen.getByRole("button", { name: "Refresh status" })),
  );
  expect((card("slack").getByLabelText("Bot token") as HTMLInputElement).value).toBe(
    "typed-bot-token",
  );
  expect((card("slack").getByLabelText("App token") as HTMLInputElement).value).toBe(
    "typed-app-token",
  );
  expect((card("slack").getByLabelText("Your member ID") as HTMLInputElement).value).toBe(
    "typed-member-id",
  );
});

// An `aria-disabled` switch without its own click guard would let a stray
// click abort a running Save (`run()` aborts the pending request for the same provider). The
// switch must stay refused and a click on it must call neither enable nor disable, while the
// save itself is left to land undisturbed.
test("a click on the refused switch during Save calls neither enable nor disable, and the save still lands", async () => {
  const service = api("disabled");
  const pending = deferred<MessagingStatus>();
  vi.mocked(service.save).mockReturnValue(pending.promise);
  render(<MessagingView api={service} />);
  await settled();
  openRow();
  fireEvent.change(card().getByRole("textbox", { name: "Server URL" }), {
    target: { value: "https://mm.example.com" },
  });
  fireEvent.change(card().getByRole("textbox", { name: "Your user ID" }), {
    target: { value: "abc123" },
  });
  fireEvent.change(card().getByLabelText("Bot token"), { target: { value: "mm-token" } });
  fireEvent.click(card().getByRole("button", { name: "Save changes" }));
  expect(card().getByText("Saving…")).toBeTruthy();
  const control = toggle();
  expect(control.getAttribute("aria-checked")).toBe("false");
  expect(control.getAttribute("aria-disabled")).toBe("true");
  expect(
    card().queryByText("Saving or removing turns this bot off. Turn it on again afterwards."),
  ).toBeNull();
  const signal = vi.mocked(service.save).mock.calls[0]?.[2];
  fireEvent.click(control);
  expect(service.enable).not.toHaveBeenCalled();
  expect(service.disable).not.toHaveBeenCalled();
  expect(signal?.aborted).toBe(false);
  await act(async () =>
    pending.resolve({ ...reading("disabled"), mattermost: status("disabled") }),
  );
  expect(service.save).toHaveBeenCalledTimes(1);
  expect(card().getByText("Disabled")).toBeTruthy();
});

// Refresh status can race a Save's own reload — the settings read it starts may
// answer (with pre-save data) only after the save's own reload already found one in flight and
// skipped itself. One more read must still happen once that in-flight one settles.
test("a settings refresh racing a save still reaches the saved settings afterward", async () => {
  const service = api("disabled");
  const initial = allConfig("none");
  const afterSave: MessagingConfig = {
    mattermost: providerConfig("saved", { url: "https://mm.example.com", owner_id: "abc123" }),
    slack: providerConfig("none"),
    discord: providerConfig("none"),
  };
  const refreshRead = deferred<MessagingConfig>();
  vi.mocked(service.config)
    .mockResolvedValueOnce(initial)
    .mockReturnValueOnce(refreshRead.promise)
    .mockResolvedValue(afterSave);
  const savePending = deferred<MessagingStatus>();
  vi.mocked(service.save).mockReturnValue(savePending.promise);
  render(<MessagingView api={service} />);
  await settled();
  openRow();
  fireEvent.change(card().getByRole("textbox", { name: "Server URL" }), {
    target: { value: "https://mm.example.com" },
  });
  fireEvent.change(card().getByRole("textbox", { name: "Your user ID" }), {
    target: { value: "abc123" },
  });
  fireEvent.change(card().getByLabelText("Bot token"), { target: { value: "mm-token" } });
  fireEvent.click(card().getByRole("button", { name: "Save changes" }));
  fireEvent.click(screen.getByRole("button", { name: "Refresh status" }));
  await settled();
  // The save commits while the refresh's own settings read (now the in-flight one) is still
  // pending: the save's own reload finds one running and skips itself.
  await act(async () =>
    savePending.resolve({ ...reading("disabled"), mattermost: status("disabled") }),
  );
  // The in-flight read settles with the pre-save snapshot it actually fetched.
  await act(async () => refreshRead.resolve(initial));
  await settled();
  expect(card().getByText("Saved settings")).toBeTruthy();
});

// The reload that follows a save resolves in a later task in real life, after the
// resetSignal bump already fired the reset against the pre-save `config`. A mock that answers
// `config()` in a plain microtask (as above) lets React batch the bump and the reload into one
// render and hides this; a deferred promise resolved in its own `act()` call, after the save's
// own act() has already settled, reproduces the real timing gap.
test("the first save from none refills the fields with the saved values once the reload lands", async () => {
  const service = api("disabled");
  const configReload = deferred<MessagingConfig>();
  vi.mocked(service.config)
    .mockResolvedValueOnce(allConfig("none"))
    .mockReturnValueOnce(configReload.promise);
  vi.mocked(service.save).mockResolvedValue({
    ...reading("disabled"),
    mattermost: status("disabled"),
  });
  render(<MessagingView api={service} />);
  await settled();
  openRow();
  fireEvent.change(card().getByRole("textbox", { name: "Server URL" }), {
    target: { value: "https://mm.example.com" },
  });
  fireEvent.change(card().getByRole("textbox", { name: "Your user ID" }), {
    target: { value: "abc123" },
  });
  fireEvent.change(card().getByLabelText("Bot token"), { target: { value: "mm-token" } });
  // The save lands and bumps resetSignal while the reload it triggered is still pending.
  await act(async () => void fireEvent.click(card().getByRole("button", { name: "Save changes" })));
  // The reload answers later, with the real saved values.
  await act(async () =>
    configReload.resolve({
      mattermost: providerConfig("saved", { url: "https://mm.example.com", owner_id: "abc123" }),
      slack: providerConfig("none"),
      discord: providerConfig("none"),
    }),
  );
  await settled();
  expect((card().getByRole("textbox", { name: "Server URL" }) as HTMLInputElement).value).toBe(
    "https://mm.example.com",
  );
  expect((card().getByRole("textbox", { name: "Your user ID" }) as HTMLInputElement).value).toBe(
    "abc123",
  );
});

test("correcting a saved owner ID shows the new value once the reload lands", async () => {
  const service = api("disabled");
  const configReload = deferred<MessagingConfig>();
  vi.mocked(service.config)
    .mockResolvedValueOnce({
      mattermost: providerConfig("saved", { url: "https://mm.example.com", owner_id: "owner-a" }),
      slack: providerConfig("none"),
      discord: providerConfig("none"),
    })
    .mockReturnValueOnce(configReload.promise);
  vi.mocked(service.save).mockResolvedValue({
    ...reading("disabled"),
    mattermost: status("disabled"),
  });
  render(<MessagingView api={service} />);
  await settled();
  openRow();
  expect((card().getByRole("textbox", { name: "Your user ID" }) as HTMLInputElement).value).toBe(
    "owner-a",
  );
  fireEvent.change(card().getByRole("textbox", { name: "Your user ID" }), {
    target: { value: "owner-b" },
  });
  await act(async () => void fireEvent.click(card().getByRole("button", { name: "Save changes" })));
  await act(async () =>
    configReload.resolve({
      mattermost: providerConfig("saved", { url: "https://mm.example.com", owner_id: "owner-b" }),
      slack: providerConfig("none"),
      discord: providerConfig("none"),
    }),
  );
  await settled();
  expect((card().getByRole("textbox", { name: "Your user ID" }) as HTMLInputElement).value).toBe(
    "owner-b",
  );
});

// The client refuses the 400 invalid_token Save refusal with a fixed hint of its own,
// never the generic "couldn't save" message, and never echoes the typed token back.
test("a malformed Mattermost token is refused at Save with a hint", async () => {
  const service = api("disabled");
  render(<MessagingView api={service} />);
  await settled();
  openRow();
  fireEvent.change(card().getByRole("textbox", { name: "Server URL" }), {
    target: { value: "https://mm.example.com" },
  });
  fireEvent.change(card().getByRole("textbox", { name: "Your user ID" }), {
    target: { value: "abc123" },
  });
  fireEvent.change(card().getByLabelText("Bot token"), { target: { value: "not-a-real-token" } });
  vi.mocked(service.save).mockRejectedValueOnce(new MessagingApiError(400, "invalid_token"));
  await act(async () => void fireEvent.click(card().getByRole("button", { name: "Save changes" })));
  expect(card().getByRole("alert").textContent).toBe(
    "That isn't a Mattermost bot token. A bot token is 26 lowercase letters and digits: create one under Integrations > Bot Accounts and paste its Access Token.",
  );
  expect(card().getByRole("alert").textContent).not.toContain("not-a-real-token");
});

// An open card reaches Stopped with its reason and a Connect button on its own, within one
// 2 s poll, with no Refresh or navigation; polling then stops since nothing is active anymore.
test("a failed start turns into Stopped with its reason while the page is open", async () => {
  vi.useFakeTimers();
  const service = api("disabled");
  vi.mocked(service.enable).mockResolvedValue(reading("connecting"));
  render(<MessagingView api={service} />);
  await settled();
  await act(async () => void fireEvent.click(toggle()));
  expect(card().getByText("Connecting")).toBeTruthy();
  vi.mocked(service.get).mockResolvedValue({
    ...reading("stopped"),
    mattermost: { ...status("stopped"), reason: "token_rejected" },
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(card().getByText("Stopped")).toBeTruthy();
  expect(
    card().getByText(
      "Mattermost rejected the bot token. Create a new token under Integrations > Bot Accounts and paste its Access Token (not the Token ID), then select Save changes and turn the bot on.",
    ),
  ).toBeTruthy();
  expect(toggle().getAttribute("aria-checked")).toBe("false");
  const callsOnceStopped = vi.mocked(service.get).mock.calls.length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000);
  });
  expect(service.get).toHaveBeenCalledTimes(callsOnceStopped);
});

// `status` keeps its stale Stopped reason until a new
// action resolves, so the reason paragraph must gate on `busy` too, not just on `status.state` and
// `status.reason` — otherwise a reconnect in flight still shows the old reason underneath it.
test("the stop reason hides while a new action is in flight", async () => {
  vi.useFakeTimers();
  const service = api("disabled");
  vi.mocked(service.enable).mockResolvedValue(reading("connecting"));
  render(<MessagingView api={service} />);
  await settled();
  await act(async () => void fireEvent.click(toggle()));
  expect(card().getByText("Connecting")).toBeTruthy();
  vi.mocked(service.get).mockResolvedValue({
    ...reading("stopped"),
    mattermost: { ...status("stopped"), reason: "token_rejected" },
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(card().getByText("Stopped")).toBeTruthy();
  expect(
    card().getByText(
      "Mattermost rejected the bot token. Create a new token under Integrations > Bot Accounts and paste its Access Token (not the Token ID), then select Save changes and turn the bot on.",
    ),
  ).toBeTruthy();
  const region = screen.getByRole("region", { name: "Mattermost bot" });
  const pending = deferred<MessagingStatus>();
  vi.mocked(service.enable).mockReturnValue(pending.promise);
  fireEvent.click(toggle());
  expect(region.querySelector(".messaging-reason")).toBeNull();
  await act(async () => pending.resolve(reading("connecting")));
  expect(region.querySelector(".messaging-reason")).toBeNull();
});

// The Stopped-with-reason flow above must behave the same way under
// StrictMode's mount→cleanup→mount effect double-invoke — one poll per 2 s tick, no overlap, and
// polling stops once the card is Stopped — the same way src/app-tests/vaults/sourcesHelp.unit.test.tsx's StrictMode
// render proves App's own effects clean up correctly.
test("the status poll runs once and stops under StrictMode", async () => {
  vi.useFakeTimers();
  const service = api("disabled");
  vi.mocked(service.enable).mockResolvedValue(reading("connecting"));
  render(
    <StrictMode>
      <MessagingView api={service} />
    </StrictMode>,
  );
  await settled();
  await act(async () => void fireEvent.click(toggle()));
  expect(card().getByText("Connecting")).toBeTruthy();
  vi.mocked(service.get).mockResolvedValue({
    ...reading("stopped"),
    mattermost: { ...status("stopped"), reason: "token_rejected" },
  });
  const callsBeforeTick = vi.mocked(service.get).mock.calls.length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(vi.mocked(service.get).mock.calls.length - callsBeforeTick).toBe(1);
  expect(card().getByText("Stopped")).toBeTruthy();
  expect(
    card().getByText(
      "Mattermost rejected the bot token. Create a new token under Integrations > Bot Accounts and paste its Access Token (not the Token ID), then select Save changes and turn the bot on.",
    ),
  ).toBeTruthy();
  expect(toggle().getAttribute("aria-checked")).toBe("false");
  const callsOnceStopped = vi.mocked(service.get).mock.calls.length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000);
  });
  expect(service.get).toHaveBeenCalledTimes(callsOnceStopped);
});

// A poll that was already in flight when a newer action (Disable) landed must not revive
// the stale reading it fetched before that action started.
test("a status poll never overwrites a newer action", async () => {
  vi.useFakeTimers();
  const service = api("connected");
  render(<MessagingView api={service} />);
  await settled();
  const pollGet = deferred<MessagingStatus>();
  vi.mocked(service.get).mockReturnValueOnce(pollGet.promise);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  await act(async () => void fireEvent.click(toggle()));
  expect(card().getByText("Disabled")).toBeTruthy();
  await act(async () => pollGet.resolve(reading("connected")));
  expect(card().getByText("Disabled")).toBeTruthy();
  expect(card().queryByText("Connected")).toBeNull();
});

// Every provider x code has its own fixed copy — Mattermost's names Mattermost; Slack and
// Discord each get their own wording, with each provider's app_token_rejected mirroring its own
// start_failed (neither has a separate app-token concept).
test("each provider's stop reasons have fixed copy", async () => {
  const cases: ReadonlyArray<[MessagingProvider, MessagingStopReason, string]> = [
    [
      "mattermost",
      "token_rejected",
      "Mattermost rejected the bot token. Create a new token under Integrations > Bot Accounts and paste its Access Token (not the Token ID), then select Save changes and turn the bot on.",
    ],
    [
      "mattermost",
      "app_token_rejected",
      "Mattermost didn't finish connecting. Check its saved settings, then turn the bot on again.",
    ],
    [
      "mattermost",
      "server_unreachable",
      "VIKI Agent couldn't reach the Mattermost server or verify its HTTPS certificate. Check the Server URL, your network or VPN, and the server's certificate, then turn the bot on again.",
    ],
    [
      "mattermost",
      "account_check_failed",
      "Mattermost accepted the bot token, but the account check failed. Check that Your user ID is your own user ID, not the bot's, and that the bot is active and not a System Admin.",
    ],
    [
      "mattermost",
      "start_failed",
      "Mattermost didn't finish connecting. Check its saved settings, then turn the bot on again.",
    ],
    [
      "mattermost",
      "connection_lost",
      "The Mattermost connection stopped after it was connected. Turn the bot on to start it again.",
    ],
    [
      "slack",
      "token_rejected",
      "Slack rejected the bot token. In your Slack app's settings, copy the bot token from the OAuth & Permissions page (it starts with xoxb-), paste it in Bot token, then select Save changes and turn the bot on.",
    ],
    [
      "slack",
      "app_token_rejected",
      "Slack rejected the app token. In your Slack app's settings, open Basic Information, generate an app-level token with the connections:write scope (it starts with xapp-), paste it in App token, then select Save changes and turn the bot on.",
    ],
    [
      "slack",
      "server_unreachable",
      "VIKI Agent couldn't reach Slack or verify its HTTPS certificate. Check your network, VPN or proxy, then turn the bot on again.",
    ],
    [
      "slack",
      "account_check_failed",
      "Slack accepted the bot token, but the account check failed. Check that Your member ID is your own member ID, not the bot's, and that the bot has the users:read, im:write, im:read, im:history and chat:write scopes; reinstall the app after adding scopes.",
    ],
    [
      "slack",
      "start_failed",
      "Slack didn't finish connecting. Check that Socket Mode is on for your app and that your network allows its connection, then turn the bot on again after any displayed cooldown.",
    ],
    [
      "slack",
      "connection_lost",
      "The Slack connection stopped after it was connected, for example when Slack refreshed its Socket Mode connection. Turn the bot on to start it again.",
    ],
    [
      "discord",
      "token_rejected",
      "Discord rejected the bot token. In the Discord developer portal, open your application's Bot page, select Reset Token, paste the new token in Bot token, then select Save changes and turn the bot on.",
    ],
    [
      "discord",
      "app_token_rejected",
      "Discord didn't finish connecting. Check your network, then turn the bot on again after any displayed cooldown.",
    ],
    [
      "discord",
      "server_unreachable",
      "VIKI Agent couldn't reach Discord or verify its HTTPS certificate. Check your network, VPN or proxy, then turn the bot on again.",
    ],
    [
      "discord",
      "account_check_failed",
      "Discord accepted the bot token, but the account check failed. Check that Your user ID is your own numeric user ID, not the bot's, and that the bot can send you a direct message.",
    ],
    [
      "discord",
      "start_failed",
      "Discord didn't finish connecting. Check your network, then turn the bot on again after any displayed cooldown.",
    ],
    [
      "discord",
      "connection_lost",
      "The Discord connection stopped after it was connected, for example after a missed heartbeat or a reconnect request. Turn the bot on to start it again.",
    ],
    [
      "mattermost",
      "limit_reached",
      "The Mattermost bot stopped because it reached one of its safety limits, such as too many waiting requests or messages at once. Turn the bot on to start a fresh conversation.",
    ],
    [
      "mattermost",
      "conversation_failed",
      "The Mattermost bot stopped because its Claude Code conversation reported an error or was closed. Open that conversation in VIKI Agent to see what happened, then turn the bot on again.",
    ],
    [
      "mattermost",
      "safety_check",
      "The Mattermost bot stopped because a request changed after it was shown, or a request or reply could not be shown completely. Check VIKI Agent, then turn the bot on again.",
    ],
    [
      "mattermost",
      "internal_error",
      "The Mattermost bot stopped after an unexpected error. Turn the bot on to start it again.",
    ],
    [
      "slack",
      "limit_reached",
      "The Slack bot stopped because it reached one of its safety limits, such as too many waiting requests or messages at once. Turn the bot on to start a fresh conversation.",
    ],
    [
      "slack",
      "conversation_failed",
      "The Slack bot stopped because its Claude Code conversation reported an error or was closed. Open that conversation in VIKI Agent to see what happened, then turn the bot on again.",
    ],
    [
      "slack",
      "safety_check",
      "The Slack bot stopped because a request changed after it was shown, or a request or reply could not be shown completely. Check VIKI Agent, then turn the bot on again.",
    ],
    [
      "slack",
      "internal_error",
      "The Slack bot stopped after an unexpected error. Turn the bot on to start it again.",
    ],
    [
      "discord",
      "limit_reached",
      "The Discord bot stopped because it reached one of its safety limits, such as too many waiting requests or messages at once. Turn the bot on to start a fresh conversation.",
    ],
    [
      "discord",
      "conversation_failed",
      "The Discord bot stopped because its Claude Code conversation reported an error or was closed. Open that conversation in VIKI Agent to see what happened, then turn the bot on again.",
    ],
    [
      "discord",
      "safety_check",
      "The Discord bot stopped because a request changed after it was shown, or a request or reply could not be shown completely. Check VIKI Agent, then turn the bot on again.",
    ],
    [
      "discord",
      "internal_error",
      "The Discord bot stopped after an unexpected error. Turn the bot on to start it again.",
    ],
  ];
  for (const [provider, reason, copy] of cases) {
    const service = api("stopped");
    vi.mocked(service.get).mockResolvedValue({
      ...reading("stopped"),
      [provider]: { ...status("stopped"), reason },
    });
    const { unmount } = render(<MessagingView api={service} />);
    await settled();
    expect(card(provider).getByText(copy)).toBeTruthy();
    unmount();
  }
});

// Slack and Discord cards reach Stopped with their own reason copy and a Connect button
// within one 2 s poll, exactly like Mattermost (mirrors the Mattermost test above), and
// polling then stops since nothing is active anymore.
test.each([
  [
    "slack",
    "app_token_rejected",
    "Slack rejected the app token. In your Slack app's settings, open Basic Information, generate an app-level token with the connections:write scope (it starts with xapp-), paste it in App token, then select Save changes and turn the bot on.",
  ],
  [
    "discord",
    "token_rejected",
    "Discord rejected the bot token. In the Discord developer portal, open your application's Bot page, select Reset Token, paste the new token in Bot token, then select Save changes and turn the bot on.",
  ],
] as const)(
  "a failed Slack or Discord start turns into Stopped with its reason while the page is open",
  async (provider, reason, copy) => {
    vi.useFakeTimers();
    const service = api("disabled");
    vi.mocked(service.enable).mockResolvedValue(reading("connecting"));
    render(<MessagingView api={service} />);
    await settled();
    await act(async () => void fireEvent.click(toggle(provider)));
    expect(card(provider).getByText("Connecting")).toBeTruthy();
    const stopped: MessagingStatus = {
      ...reading("stopped"),
      [provider]: { ...status("stopped"), reason },
    };
    vi.mocked(service.get).mockResolvedValue(stopped);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(card(provider).getByText("Stopped")).toBeTruthy();
    expect(card(provider).getByText(copy)).toBeTruthy();
    expect(toggle(provider).getAttribute("aria-checked")).toBe("false");
    const callsOnceStopped = vi.mocked(service.get).mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(service.get).toHaveBeenCalledTimes(callsOnceStopped);
  },
);

// Slack's own two save refusals (a bad bot token, a bad app token) each get their own
// field-specific hint, exactly like Mattermost's (mirrors that test above), and neither
// echoes the typed value back.
test.each([
  [
    "invalid_token",
    "That isn't a Slack bot token. Copy the bot token from the OAuth & Permissions page of your Slack app's settings; it starts with xoxb-.",
  ],
  [
    "invalid_app_token",
    "That isn't a Slack app token. Generate an app-level token with the connections:write scope under Basic Information in your Slack app's settings; it starts with xapp-.",
  ],
] as const)(
  "a wrong Slack token is refused at Save with a hint for that field",
  async (code, hint) => {
    const service = api("disabled");
    render(<MessagingView api={service} />);
    await settled();
    openRow("slack");
    fireEvent.change(card("slack").getByRole("textbox", { name: "Your member ID" }), {
      target: { value: "member-1" },
    });
    fireEvent.change(card("slack").getByLabelText("Bot token"), {
      target: { value: "not-a-real-token" },
    });
    fireEvent.change(card("slack").getByLabelText("App token"), {
      target: { value: "not-a-real-token" },
    });
    vi.mocked(service.save).mockRejectedValueOnce(new MessagingApiError(400, code));
    await act(
      async () => void fireEvent.click(card("slack").getByRole("button", { name: "Save changes" })),
    );
    expect(card("slack").getByRole("alert").textContent).toBe(hint);
    expect(card("slack").getByRole("alert").textContent).not.toContain("not-a-real-token");
  },
);

// (a) The compact redesign's whole point: every row starts collapsed to a heading and a status
// pill. `getByRole` would skip the body regardless of whether it is truly collapsed or merely
// missing (it always ignores `hidden` content), so this checks the `hidden` flag itself rather
// than inferring "closed" from an absent query.
test("rows start closed", async () => {
  render(<MessagingView api={api("disabled")} />);
  await settled();
  for (const provider of MESSAGING_PROVIDERS) {
    const disclosure = card(provider).getByRole("button", {
      name: `${PROVIDER_NAME[provider]} bot`,
    });
    expect(disclosure.getAttribute("aria-expanded")).toBe("false");
    const bodyId = disclosure.getAttribute("aria-controls");
    const body = bodyId ? document.getElementById(bodyId) : null;
    expect(body).not.toBeNull();
    expect((body as HTMLElement).hidden).toBe(true);
  }
});

// (b) Rows open and close independently of each other, and the body is never unmounted while
// closed — a draft typed in one open row survives another row's own open/close.
test("rows open and close independently, and a draft in one survives another opening", async () => {
  render(<MessagingView api={api("disabled")} />);
  await settled();
  openRow("mattermost");
  expect(expanded("mattermost")).toBe("true");
  openRow("slack");
  expect(expanded("mattermost")).toBe("true");
  expect(expanded("slack")).toBe("true");
  fireEvent.change(card("slack").getByLabelText("Bot token"), { target: { value: "draft-token" } });
  openRow("mattermost");
  expect(expanded("mattermost")).toBe("false");
  expect(expanded("slack")).toBe("true");
  expect((card("slack").getByLabelText("Bot token") as HTMLInputElement).value).toBe("draft-token");
});

// (c) The Set up control only ever stands in for an unconfigured provider; a configured one
// (any state) shows the switch instead, never both. Clicking Set up opens the row and focuses
// its first field, synchronously enough that the field already exists to receive focus.
test("Set up appears only when unconfigured, never alongside the switch, and focuses the first field on click", async () => {
  render(<MessagingView api={api("not_configured")} />);
  await settled();
  for (const provider of MESSAGING_PROVIDERS) {
    expect(
      card(provider).getByRole("button", { name: `Set up ${PROVIDER_NAME[provider]}` }),
    ).toBeTruthy();
    expect(card(provider).queryByRole("switch")).toBeNull();
  }
  fireEvent.click(card("mattermost").getByRole("button", { name: "Set up Mattermost" }));
  expect(document.activeElement).toBe(
    card("mattermost").getByRole("textbox", { name: "Server URL" }),
  );
  fireEvent.click(card("slack").getByRole("button", { name: "Set up Slack" }));
  expect(document.activeElement).toBe(card("slack").getByLabelText("Bot token"));
  cleanup();
  render(<MessagingView api={api("disabled")} />);
  await settled();
  for (const provider of MESSAGING_PROVIDERS) {
    expect(
      card(provider).queryByRole("button", { name: `Set up ${PROVIDER_NAME[provider]}` }),
    ).toBeNull();
    expect(
      card(provider).getByRole("switch", { name: `${PROVIDER_NAME[provider]} bot` }),
    ).toBeTruthy();
  }
});

// (d) The alert, reason, restart and cooldown notes sit between the row's heading and its
// (possibly hidden) body, never inside it, so a collapsed row still surfaces them.
test("closed row: alert, reason, restart and cooldown notes have no [hidden] ancestor", async () => {
  function assertVisibleWhileClosed(provider: MessagingProvider, selector: string) {
    expect(expanded(provider)).toBe("false");
    const region = screen.getByRole("region", { name: `${PROVIDER_NAME[provider]} bot` });
    const note = region.querySelector(selector);
    expect(note, selector).not.toBeNull();
    expect((note as Element).closest("[hidden]")).toBeNull();
  }

  {
    const service = api("connected");
    vi.mocked(service.disable).mockRejectedValue(new Error("boom"));
    render(<MessagingView api={service} />);
    await settled();
    await act(async () => void fireEvent.click(toggle()));
    assertVisibleWhileClosed("mattermost", ".messaging-error");
    cleanup();
  }
  {
    const service = api("disabled");
    vi.mocked(service.enable).mockRejectedValue(new MessagingApiError(429, "cooldown", 7));
    render(<MessagingView api={service} />);
    await settled();
    await act(async () => void fireEvent.click(toggle()));
    assertVisibleWhileClosed("mattermost", ".messaging-cooldown");
    cleanup();
  }
  {
    const service = api("disabled");
    vi.mocked(service.enable).mockRejectedValue(new MessagingApiError(409, "restart_required"));
    render(<MessagingView api={service} />);
    await settled();
    await act(async () => void fireEvent.click(toggle()));
    assertVisibleWhileClosed("mattermost", ".messaging-restart");
    cleanup();
  }
  {
    const service = api("stopped");
    vi.mocked(service.get).mockResolvedValue({
      ...reading("stopped"),
      mattermost: { ...status("stopped"), reason: "token_rejected" },
    });
    render(<MessagingView api={service} />);
    await settled();
    assertVisibleWhileClosed("mattermost", ".messaging-reason");
  }
});

// (e) The page header has a one-line lead, and the guidance sits behind "How messaging works".
test("the page header has the exact lead text and the how-it-works guidance", async () => {
  render(<MessagingView api={api("disabled")} />);
  await settled();
  expect(screen.getByText("Reach VIKI Agent from a private direct message.")).toBeTruthy();
  expect(screen.getByText("How messaging works")).toBeTruthy();
  expect(
    screen.getByText(
      "Only the configured owner can use private direct messages to send text and answer approvals.",
    ),
  ).toBeTruthy();
  expect(
    screen.getByText(
      "Keep the desktop host awake and the VIKI Agent window open while using a bot.",
    ),
  ).toBeTruthy();
  expect(
    screen.getByText(
      "Save each provider's settings below and turn it on. A bot you leave on reconnects when VIKI Agent starts in the same workspace.",
    ),
  ).toBeTruthy();
});
