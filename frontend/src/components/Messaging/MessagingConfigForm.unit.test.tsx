import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MessagingProviderConfig } from "../../clients/messaging/messagingApi";
import { MessagingConfigForm } from "./MessagingConfigForm";

afterEach(cleanup);

type Props = ComponentProps<typeof MessagingConfigForm>;

function renderForm(overrides: Partial<Props> = {}) {
  const onSave = vi.fn();
  const onRemove = vi.fn();
  const onOpenGuide = vi.fn();
  const props: Props = {
    provider: "mattermost",
    config: null,
    busy: false,
    active: false,
    resetSignal: 0,
    onSave,
    onRemove,
    ...overrides,
  };
  const view = render(<MessagingConfigForm {...props} />);
  return { onSave, onRemove, onOpenGuide, ...view };
}

const noneConfig = (): MessagingProviderConfig => ({ source: "none", values: {} });
const unreadableConfig = (): MessagingProviderConfig => ({ source: "unreadable", values: {} });
const savedMattermost = (): MessagingProviderConfig => ({
  source: "saved",
  values: { url: "https://mm.example.com", owner_id: "abcdefghijklmnopqrstuvwxyz" },
});

function fill(label: string, value: string): void {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

const MATTERMOST_FIELDS = [
  { label: "Server URL", hint: "Your Mattermost server's HTTPS address." },
  { label: "Your user ID", hint: "Your own 26-character Mattermost user ID, not the bot's." },
  { label: "Bot token", hint: "The bot account's access token." },
] as const;
const SLACK_FIELDS = [
  { label: "Bot token", hint: "The installed bot's token, starting with xoxb-." },
  {
    label: "App token",
    hint: "The app-level token with connections:write, starting with xapp-.",
  },
  { label: "Your member ID", hint: "Your own Slack member ID, not the bot's." },
] as const;
const DISCORD_FIELDS = [
  { label: "Bot token", hint: "The token from the application's Bot page." },
  { label: "Your user ID", hint: "Your own numeric Discord user ID, not the bot's." },
  {
    label: "Client URL",
    hint: "A public HTTPS app or support page. Discord sees it in the bot's user agent; VIKI Agent never opens it.",
  },
] as const;

describe("config === null", () => {
  it("renders only the credentials section and the footer", () => {
    renderForm({ config: null });
    expect(screen.getByRole("heading", { name: "Get your credentials" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Required" })).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(document.querySelector('input[type="password"]')).toBeNull();
    expect(screen.queryByRole("button", { name: "Remove saved settings" })).toBeNull();
    const save = screen.getByRole<HTMLButtonElement>("button", { name: "Save changes" });
    expect(save.disabled).toBe(true);
  });

  it("stays footer-only even when active is true", () => {
    renderForm({ config: null, active: true });
    expect(
      screen.queryByText("Saving or removing turns this bot off. Turn it on again afterwards."),
    ).toBeNull();
  });
});

describe("Get your credentials", () => {
  it.each([
    [
      "mattermost",
      "Create a bot account with the Member role on your Mattermost server, invite it to your team, then paste the server URL, its token and your user ID here.",
    ],
    [
      "slack",
      "Create an internal Slack app with Socket Mode, install it, then paste its bot token, app token and your member ID here.",
    ],
    [
      "discord",
      "Create a Discord application with a bot, open a direct message with it, then paste its token, your user ID and a client URL here.",
    ],
  ] as const)("%s shows its own credential line", (provider, line) => {
    renderForm({ provider });
    expect(screen.getByText(line)).toBeTruthy();
  });

  it("shows Open setup guide only when onOpenGuide is given, and calls it on click", () => {
    const { unmount } = renderForm();
    expect(screen.queryByRole("button", { name: "Open setup guide" })).toBeNull();
    unmount();
    const onOpenGuide = vi.fn();
    renderForm({ onOpenGuide });
    const button = screen.getByRole("button", { name: "Open setup guide" });
    fireEvent.click(button);
    expect(onOpenGuide).toHaveBeenCalledTimes(1);
  });
});

describe("Required fields", () => {
  it.each([
    ["mattermost", MATTERMOST_FIELDS],
    ["slack", SLACK_FIELDS],
    ["discord", DISCORD_FIELDS],
  ] as const)(
    "%s lists its fields in wire order with label, hint and aria-describedby",
    (provider, expected) => {
      const { container } = renderForm({ provider, config: noneConfig() });
      const labels = Array.from(
        container.querySelectorAll<HTMLLabelElement>(".messaging-config-field label"),
      );
      expect(labels.map((label) => label.textContent)).toEqual(
        expected.map((field) => field.label),
      );
      for (const field of expected) {
        const input = screen.getByLabelText(field.label);
        const hint = screen.getByText(field.hint);
        expect(input.getAttribute("aria-describedby")).toBe(hint.id);
      }
    },
  );
});

describe("input wiring", () => {
  it("prefills non-secret fields from saved values and leaves secrets empty", () => {
    renderForm({ config: savedMattermost() });
    expect(screen.getByLabelText<HTMLInputElement>("Server URL").value).toBe(
      "https://mm.example.com",
    );
    expect(screen.getByLabelText<HTMLInputElement>("Your user ID").value).toBe(
      "abcdefghijklmnopqrstuvwxyz",
    );
    const token = screen.getByLabelText<HTMLInputElement>("Bot token");
    expect(token.value).toBe("");
    expect(token.type).toBe("password");
  });

  it("the url field keeps its placeholder; secrets get a guard, autofill-off and the saved placeholder", () => {
    renderForm({ config: noneConfig() });
    expect(screen.getByLabelText<HTMLInputElement>("Server URL").placeholder).toBe(
      "https://mattermost.example.com",
    );
    const freshToken = screen.getByLabelText("Bot token");
    expect(freshToken.getAttribute("type")).toBe("password");
    expect(freshToken.getAttribute("autocomplete")).toBe("new-password");
    expect(freshToken.getAttribute("spellcheck")).toBe("false");
    expect(freshToken.getAttribute("placeholder")).toBeNull();
  });

  it("a saved secret shows the replace-token placeholder", () => {
    renderForm({ config: savedMattermost() });
    expect(screen.getByLabelText("Bot token").getAttribute("placeholder")).toBe(
      "Saved. Enter a new token to replace it.",
    );
  });

  it("prefills once config first arrives, from null to an object", () => {
    const { rerender } = renderForm({ config: null, resetSignal: 0 });
    rerender(
      <MessagingConfigForm
        provider="mattermost"
        config={savedMattermost()}
        busy={false}
        active={false}
        resetSignal={0}
        onSave={() => {}}
        onRemove={() => {}}
      />,
    );
    expect(screen.getByLabelText<HTMLInputElement>("Server URL").value).toBe(
      "https://mm.example.com",
    );
  });

  // The view's `mergeConfigs` keeps a provider's exact `config` object across a re-read that
  // changed nothing for it (a plain Refresh, another provider's save) — this is what that looks
  // like from the form's side: the same reference comes back, resetSignal does not change, and
  // typed input must survive. A re-read that actually changes this provider's
  // content, by contrast, is expected to reset the form once it lands — see "the first save from
  // none..." and "correcting a saved owner ID..." in MessagingView.unit.test.tsx.
  it("the same config object passed again keeps typed input", () => {
    const saved = savedMattermost();
    const { rerender } = renderForm({ config: saved, resetSignal: 0 });
    fill("Your user ID", "typed-owner-id");
    rerender(
      <MessagingConfigForm
        provider="mattermost"
        config={saved}
        busy={false}
        active={false}
        resetSignal={0}
        onSave={() => {}}
        onRemove={() => {}}
      />,
    );
    expect(screen.getByLabelText<HTMLInputElement>("Your user ID").value).toBe("typed-owner-id");
  });

  // The saved non-secret values a reload echoes back can be byte-identical to what was already
  // shown even when only a secret changed (the server never returns one), so `config`'s own
  // reference never changes either; `resetSignal` is the only thing that still clears the token
  // field in that case (the same `config` reference as above proves this
  // isn't actually riding on a content change).
  it("resets — clearing the token — when resetSignal changes, even with the same config object", () => {
    const saved = savedMattermost();
    const { rerender } = renderForm({ config: saved, resetSignal: 0 });
    fill("Bot token", "a-typed-token");
    expect(screen.getByLabelText<HTMLInputElement>("Bot token").value).toBe("a-typed-token");
    rerender(
      <MessagingConfigForm
        provider="mattermost"
        config={saved}
        busy={false}
        active={false}
        resetSignal={1}
        onSave={() => {}}
        onRemove={() => {}}
      />,
    );
    expect(screen.getByLabelText<HTMLInputElement>("Bot token").value).toBe("");
  });

  // A genuinely different `config` object (not merely a different resetSignal)
  // must still refill the form once it arrives, even without a resetSignal bump of its own —
  // this is MessagingConfigForm's half of "the first save from none..." in MessagingView's
  // suite, isolated from the view's timing.
  it("refills when a different config object arrives, resetSignal unchanged", () => {
    const { rerender } = renderForm({ config: noneConfig(), resetSignal: 0 });
    fill("Server URL", "https://typed.example.com");
    rerender(
      <MessagingConfigForm
        provider="mattermost"
        config={savedMattermost()}
        busy={false}
        active={false}
        resetSignal={0}
        onSave={() => {}}
        onRemove={() => {}}
      />,
    );
    expect(screen.getByLabelText<HTMLInputElement>("Server URL").value).toBe(
      "https://mm.example.com",
    );
  });
});

describe("notes", () => {
  it("shows the unreadable note only when settings can't be read", () => {
    const message =
      "Saved settings can't be read with this account. Enter them again or remove them.";
    renderForm({ config: unreadableConfig() });
    expect(screen.getByText(message)).toBeTruthy();
    cleanup();
    renderForm({ config: savedMattermost() });
    expect(screen.queryByText(message)).toBeNull();
  });

  it("shows the active note only while the bot is active and configured", () => {
    const message = "Saving or removing turns this bot off. Turn it on again afterwards.";
    renderForm({ config: savedMattermost(), active: true });
    expect(screen.getByText(message)).toBeTruthy();
    cleanup();
    renderForm({ config: savedMattermost(), active: false });
    expect(screen.queryByText(message)).toBeNull();
  });
});

describe("Save changes is enabled only when the input is complete, fresh, and the form is idle", () => {
  function save(): HTMLButtonElement {
    return screen.getByRole<HTMLButtonElement>("button", { name: "Save changes" });
  }

  it("enabled once every field of a fresh config is filled", () => {
    renderForm({ config: noneConfig() });
    fill("Server URL", "https://x.example.com");
    fill("Your user ID", "owner-id");
    fill("Bot token", "token-value");
    expect(save().disabled).toBe(false);
  });

  it("disabled while any required field is blank or whitespace-only", () => {
    renderForm({ config: noneConfig() });
    fill("Server URL", "https://x.example.com");
    fill("Your user ID", "   ");
    fill("Bot token", "token-value");
    expect(save().disabled).toBe(true);
  });

  it("disabled while a fresh config's secret is empty", () => {
    renderForm({ config: noneConfig() });
    fill("Server URL", "https://x.example.com");
    fill("Your user ID", "owner-id");
    expect(save().disabled).toBe(true);
  });

  it("disabled while busy, even with a complete fresh form", () => {
    renderForm({ config: noneConfig(), busy: true });
    fill("Server URL", "https://x.example.com");
    fill("Your user ID", "owner-id");
    fill("Bot token", "token-value");
    expect(save().disabled).toBe(true);
  });

  it("disabled for a saved config with no edits and a blank secret", () => {
    renderForm({ config: savedMattermost() });
    expect(save().disabled).toBe(true);
  });

  it("enabled for a saved config once a non-secret field changes, secret left blank to keep it", () => {
    renderForm({ config: savedMattermost() });
    fill("Your user ID", "a-new-owner-id");
    expect(save().disabled).toBe(false);
  });

  it("disabled for an unreadable config until every field, including the secret, is re-entered", () => {
    renderForm({ config: unreadableConfig() });
    fill("Server URL", "https://x.example.com");
    fill("Your user ID", "owner-id");
    expect(save().disabled).toBe(true);
    fill("Bot token", "token-value");
    expect(save().disabled).toBe(false);
  });
});

// The server refuses a Mattermost save that changes Server URL while leaving
// Bot token empty (the old token would otherwise travel to a new server silently), so the form
// has to require the token again as soon as the URL differs from what is saved.
describe("Mattermost: changing the saved Server URL requires the token again", () => {
  function save(): HTMLButtonElement {
    return screen.getByRole<HTMLButtonElement>("button", { name: "Save changes" });
  }
  function tokenPlaceholder(): string | null {
    return screen.getByLabelText("Bot token").getAttribute("placeholder");
  }

  it("disables Save and swaps the token placeholder while the token is left empty", () => {
    renderForm({ config: savedMattermost() });
    fill("Server URL", "https://different.example.com");
    expect(save().disabled).toBe(true);
    expect(tokenPlaceholder()).toBe("Enter the bot token again for a new server URL.");
  });

  it("enables Save once the token is filled, and onSave carries the url and the new token", () => {
    const { onSave } = renderForm({ config: savedMattermost() });
    fill("Server URL", "https://different.example.com");
    fill("Bot token", "fresh-token");
    expect(save().disabled).toBe(false);
    fireEvent.click(save());
    expect(onSave).toHaveBeenCalledWith({
      url: "https://different.example.com",
      owner_id: "abcdefghijklmnopqrstuvwxyz",
      bot_token: "fresh-token",
    });
  });

  it("the token is optional again once the URL is changed back to the saved value", () => {
    renderForm({ config: savedMattermost() });
    fill("Server URL", "https://different.example.com");
    fill("Server URL", "https://mm.example.com");
    expect(tokenPlaceholder()).toBe("Saved. Enter a new token to replace it.");
    // A different, unrelated edit proves Save does not wait on the token: if it did, this
    // would stay disabled like the "no edits" case above.
    fill("Your user ID", "a-new-owner-id");
    expect(save().disabled).toBe(false);
  });
});

describe("Remove saved settings", () => {
  it("is absent when there is nothing saved", () => {
    renderForm({ config: noneConfig() });
    expect(screen.queryByRole("button", { name: "Remove saved settings" })).toBeNull();
  });

  it.each([["saved", savedMattermost()] as const, ["unreadable", unreadableConfig()] as const])(
    "is present and calls onRemove for a %s config",
    (_label, config) => {
      const { onRemove } = renderForm({ config });
      const button = screen.getByRole<HTMLButtonElement>("button", {
        name: "Remove saved settings",
      });
      expect(button.disabled).toBe(false);
      fireEvent.click(button);
      expect(onRemove).toHaveBeenCalledTimes(1);
    },
  );

  it("is disabled while busy", () => {
    renderForm({ config: savedMattermost(), busy: true });
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: "Remove saved settings" }).disabled,
    ).toBe(true);
  });
});

describe("onSave payload", () => {
  it("trims every field for a fresh config", () => {
    const { onSave } = renderForm({ config: noneConfig() });
    fill("Server URL", "  https://x.example.com  ");
    fill("Your user ID", "  owner-id  ");
    fill("Bot token", "  token-value  ");
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(onSave).toHaveBeenCalledWith({
      url: "https://x.example.com",
      owner_id: "owner-id",
      bot_token: "token-value",
    });
  });

  it("omits a blank secret for a saved config, trims the changed non-secret field", () => {
    const { onSave } = renderForm({ config: savedMattermost() });
    fill("Your user ID", "  a-new-owner-id  ");
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(onSave).toHaveBeenCalledWith({
      url: "https://mm.example.com",
      owner_id: "a-new-owner-id",
    });
  });
});
