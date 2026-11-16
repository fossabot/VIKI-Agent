import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { type ReactElement, useEffect } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ModelConnectionsContext } from "../../clients/app/modelConnectionsApi";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import type { BotRow, BotsApi } from "../../clients/messaging/botsApi";
import {
  type ProfileApi,
  ProfileApiError,
  type ProfileDefinition,
  type ProfileDocumentV1,
  type ProfileSummary,
} from "../../clients/profiles/profileApi";
import {
  type BlockedProfilePreview,
  type PreparedProfilePreview,
  type ProfileLaunchApi,
  ProfileLaunchApiError,
} from "../../clients/profiles/profileLaunchApi";
import { t, tf } from "../../i18n/t";
import { hasUnsavedWork } from "../../state/unsavedWork";
import type { AvatarUrls } from "../Avatar/avatarUrls";
import { DEFAULT_IDENTITY, IdentityContext } from "../Avatar/identity";
import { BotHuesContext } from "../Bots/botHues";
import { TEAM, type TeamSeat } from "../Bots/team";
import { ProfilesView } from "./ProfilesView";
import { problemText } from "./profileCopy";
import { SEAT_STARTERS, type SeatStarter } from "./seatStarters";

// Stubbed only where a test simulates the real editor's completion (as AvatarField's own test
// does); every other test renders the real editor, which none of them open.
vi.mock("../Avatar/AvatarEditor", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../Avatar/AvatarEditor")>();
  return { ...actual, AvatarEditor: vi.fn(actual.AvatarEditor) };
});

const A = "00000000-0000-4000-8000-000000000001";
const B = "00000000-0000-4000-8000-000000000002";
const NEW_AVATAR = "00000000-0000-4000-8000-000000000004";
const WORKSPACE = "00000000-0000-4000-8000-000000000003";
const NOW = "2026-09-21T00:00:00.000Z";

const vault = (workspace_id: string, name: string, persistent = true): WorkspaceJson => ({
  workspace_id,
  kind: "vault",
  root: `/vault/${name}`,
  root_available: true,
  aliased: false,
  name,
  policy: "ordinary",
  policy_revision: 1,
  revision: 1,
  source: "explicit",
  layout: "flat",
  portable_id: null,
  identity: { established: true, git_root_oids: 0, inherits_from: null, probed_at: NOW },
  face_dir: null,
  bootstrap: "complete",
  persistent,
  created_at: NOW,
  updated_at: NOW,
});

const document = (name = "Alpha"): ProfileDocumentV1 => ({
  format_version: 1,
  identity: {
    name,
    purpose: "Synthetic purpose",
    avatar_asset_id: null,
    accent: "orange",
    shape: "rounded",
  },
  runtime: { provider: "claude", model: null, effort: "high", permission_mode: null },
  instructions: "Synthetic instructions",
  workspace: { allowed_workspace_ids: [WORKSPACE] },
  context: { items: [{ kind: "selection", id: "context-1" }] },
  native_assets: [{ kind: "agent", id: "native-1" }],
  tools: { required: [], denied: ["Bash"], mcp_configuration: null },
  memory: { mode: "provider_inherited", sources: [] },
  limits: {
    timeout_seconds: null,
    max_root_tasks: null,
    token_budget: 100,
    currency_budget: null,
  },
  provenance: { kind: "user", derived_from: null, native_source: null },
});

const summary = (profile_id = A, name = "Alpha", archived = false): ProfileSummary => ({
  profile_id,
  revision: 1,
  current_revision: 1,
  state_revision: 1,
  created_at: NOW,
  updated_at: NOW,
  archived,
  identity: document(name).identity,
  runtime: document(name).runtime,
  provenance: document(name).provenance,
});

const definition = (profile_id = A, name = "Alpha"): ProfileDefinition => ({
  profile_id,
  revision: 1,
  current_revision: 1,
  state_revision: 1,
  created_at: NOW,
  updated_at: NOW,
  archived: false,
  document: document(name),
});

const definitionWith = (value: ProfileDocumentV1): ProfileDefinition => ({
  ...definition(),
  document: value,
});

function api(overrides: Partial<ProfileApi> = {}): ProfileApi {
  return {
    list: vi.fn(async () => [summary()]),
    get: vi.fn(async (id) => definition(id)),
    create: vi.fn(async (id) => ({ profile_id: id, revision: 1, state_revision: 1 })),
    revise: vi.fn(async (id, _command, revision) => ({
      profile_id: id,
      revision: revision + 1,
      state_revision: 1,
    })),
    archive: vi.fn(async (id, _command, state) => ({
      profile_id: id,
      revision: 1,
      state_revision: state + 1,
    })),
    duplicate: vi.fn(async (_id, _command, _revision, target) => ({
      profile_id: target,
      revision: 1,
      state_revision: 1,
    })),
    compare: vi.fn(async (id, left, right) => ({
      profile_id: id,
      left,
      right,
      changes: [],
    })),
    ...overrides,
  };
}

const prepared = (): PreparedProfilePreview => ({
  launchable: true,
  fields: [
    {
      field: "runtime.model",
      preview_class: "effective",
      status: "supported",
      effective_value: "sonnet",
      source: "inherited",
      reason: null,
    },
  ],
  blockers: [],
  warnings: [],
  prefs: { mode: "default", effort: "high", model: "sonnet" },
  session_id: B,
  profile_id: A,
  profile_revision: 1,
  generation: "00000000-0000-4000-8000-000000000005",
  workspace_id: WORKSPACE,
  workspace_revision: 1,
  policy: "ordinary",
  policy_revision: 1,
  settings_revision: 1,
  runtime: "claude",
  capability_key: "a".repeat(64),
  draft: { key: B, revision: 1, text: "Synthetic instructions", saved_at: null, attachments: [] },
});

function launchApi(overrides: Partial<ProfileLaunchApi> = {}): ProfileLaunchApi {
  return {
    capabilities: vi.fn<ProfileLaunchApi["capabilities"]>(async () => ({
      provider: "claude",
      adapter_version: "profile-v1",
      native_version: "2.1.259",
      observed_at: NOW,
      catalog_key: "a".repeat(64),
      capabilities: [
        { id: "plan_mode", status: "supported", reason: null, evidence: "fixture_exact" },
      ],
    })),
    models: vi.fn<ProfileLaunchApi["models"]>(async () => ({
      provider: "claude",
      adapter_version: "profile-v1",
      native_version: "2.1.259",
      observed_at: NOW,
      catalog_key: "a".repeat(64),
      status: "supported",
      models: [{ value: "sonnet", label: "Sonnet", description: "Observed model" }],
    })),
    inspect: vi.fn(async () => prepared()),
    prepare: vi.fn(async () => prepared()),
    ...overrides,
  };
}

async function settle(): Promise<void> {
  await act(async () => Promise.resolve());
}

afterEach(() => {
  cleanup();
});

describe("ProfilesView", () => {
  it("labels profile sections and field hints", async () => {
    render(<ProfilesView api={api()} initialSelectedId={A} />);
    await settle();
    expect(screen.getByRole("group", { name: "Identity" })).toBeTruthy();
    expect(screen.getByRole("group", { name: "Model and permissions" })).toBeTruthy();
    expect(screen.getByRole("group", { name: "What it can use" })).toBeTruthy();
    expect(screen.getByText("Give this profile its role and working guidance.")).toBeTruthy();
    expect(screen.getByText("These servers are saved with the profile.")).toBeTruthy();
  });

  it("shows a selection hint without reading an unselected profile", async () => {
    const profiles = api();
    render(<ProfilesView api={profiles} />);
    await settle();
    expect(screen.getByText("Select a profile to review its configuration.")).toBeTruthy();
    expect(profiles.get).not.toHaveBeenCalled();
    expect(screen.queryByRole("region", { name: "Profile overview" })).toBeNull();
  });

  it("Bot settings notes the memory rule and Bot rules under Instructions", async () => {
    const note =
      "followed by VIKI Agent's memory rule and Bot rules, as part of Claude Code's system prompt";
    const { unmount } = render(<ProfilesView api={api()} botSettings initialSelectedId={A} />);
    await settle();
    expect(screen.getByText(new RegExp(note))).toBeTruthy();
    unmount();
    render(<ProfilesView api={api()} initialSelectedId={A} />);
    await settle();
    expect(screen.queryByText(new RegExp(note))).toBeNull();
  });

  it("preselects the profile named by initialSelectedId", async () => {
    const profiles = api({ list: vi.fn(async () => [summary(A, "Alpha"), summary(B, "Beta")]) });
    render(<ProfilesView api={profiles} initialSelectedId={B} />);
    await settle();
    expect(profiles.get).toHaveBeenCalledWith(B, undefined, expect.anything());
    const row = screen.getByRole("button", { name: "Beta — claude — revision 1" });
    expect(row.getAttribute("aria-current")).toBeTruthy();
  });

  it("shows saved configuration, labels edits as Draft and keeps the editor visible", async () => {
    const loaded = definition();
    loaded.document.runtime.model = "configured-model";
    loaded.document.runtime.permission_mode = "plan";
    const profiles = api({ get: vi.fn(async () => loaded) });
    render(<ProfilesView api={profiles} />);
    await settle();
    const row = screen.getByRole("button", { name: "Alpha — claude — revision 1" });
    fireEvent.click(row);
    await settle();
    const overview = screen.getByRole("region", { name: "Profile overview" });
    expect(within(overview).getByText("Saved configuration")).toBeTruthy();
    expect(within(overview).getByText("Alpha")).toBeTruthy();
    expect(within(overview).getByText("Synthetic purpose")).toBeTruthy();
    expect(within(overview).getByText("configured-model")).toBeTruthy();
    expect(within(overview).getByText(t("profiles.overview.permissionSetting"))).toBeTruthy();
    expect(within(overview).getByText("Plan first")).toBeTruthy();
    expect(Array.from(overview.querySelectorAll("dt"), (dt) => dt.textContent)).toEqual([
      "Runs on",
      "Model",
      t("profiles.overview.permissionSetting"),
    ]);
    expect(screen.getByRole("group", { name: "Identity" })).toBeTruthy();
    expect(screen.getByRole("group", { name: "Model and permissions" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "Try it in a chat" })).toBeTruthy();
    expect((screen.getByLabelText("Model") as HTMLSelectElement).value).toBe("configured-model");
    expect(
      Array.from(
        (screen.getByRole("combobox", { name: "Effort" }) as HTMLSelectElement).options,
        (option) => [option.value, option.textContent],
      ),
    ).toEqual([
      ["low", "Low"],
      ["medium", "Medium"],
      ["high", "High"],
      ["xhigh", "Extra high"],
      ["max", "Maximum"],
    ]);
    expect(
      (screen.getByRole("button", { name: "Save revision" }) as HTMLButtonElement).disabled,
    ).toBe(true);

    fireEvent.change(screen.getByLabelText("Purpose"), { target: { value: "Unsaved purpose" } });
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "" } });
    expect(within(overview).getByText("Draft")).toBeTruthy();
    expect(within(overview).queryByText("Saved configuration")).toBeNull();
    expect(within(overview).getByText("Unsaved purpose")).toBeTruthy();
    expect(within(overview).getByText("App default")).toBeTruthy();
    expect(within(row).getByText("Synthetic purpose")).toBeTruthy();
    expect(row.getAttribute("aria-current")).toBe("true");
    expect(
      (screen.getByRole("button", { name: "Save revision" }) as HTMLButtonElement).disabled,
    ).toBe(false);
    expect(profiles.get).toHaveBeenCalledTimes(1);
    expect(profiles.revise).not.toHaveBeenCalled();
  });

  it("shows neutral accent labels with legacy values and saves the selected key", async () => {
    const profiles = api();
    render(<ProfilesView api={profiles} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Alpha — claude — revision 1" }));
    await settle();
    const accent = screen.getByRole("combobox", { name: "Accent" }) as HTMLSelectElement;
    expect(Array.from(accent.options, (option) => [option.textContent, option.value])).toEqual([
      ["Gray 2", "blue"],
      ["Gray 3", "green"],
      ["Primary", "orange"],
      ["Gray 4", "purple"],
      ["Foreground", "red"],
      ["Gray 1", "slate"],
    ]);
    expect(accent.value).toBe("orange");
    const gray4 = within(accent).getByRole("option", { name: "Gray 4" }) as HTMLOptionElement;
    fireEvent.change(accent, { target: { value: gray4.value } });
    expect(accent.value).toBe("purple");
    expect(hasUnsavedWork()).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await settle();
    expect(profiles.revise).toHaveBeenCalledExactlyOnceWith(A, expect.any(String), 1, {
      ...document(),
      identity: { ...document().identity, accent: "purple" },
    });
  });

  it("shows Bot look controls only in Bot settings and stages avatar removal", async () => {
    const withAvatar = {
      ...definition(),
      document: { ...document(), identity: { ...document().identity, avatar_asset_id: B } },
    };
    let reads = 0;
    const profiles = api({
      get: vi.fn(async (id) => ({
        ...withAvatar,
        profile_id: id,
        document:
          reads++ === 0
            ? withAvatar.document
            : {
                ...withAvatar.document,
                identity: { ...withAvatar.document.identity, avatar_asset_id: null },
              },
      })),
    });
    const set = vi.fn(async () => "saved" as const);
    render(
      <BotHuesContext.Provider value={{ hues: {}, icons: {}, set }}>
        <ProfilesView api={profiles} bots={{} as BotsApi} botSettings />
      </BotHuesContext.Provider>,
    );
    await settle();
    const row = screen.getByRole("button", { name: "Alpha — claude — revision 1" });
    expect(row.querySelector(".avatar")?.getAttribute("style")).toContain("width: 40px");
    expect(row.querySelector("svg.bot-icon")).not.toBeNull();
    fireEvent.click(row);
    await settle();
    const overview = screen.getByRole("region", { name: "Profile overview" });
    expect(overview.querySelector(".avatar")?.getAttribute("style")).toContain("width: 64px");
    expect(screen.getByRole("region", { name: "Look" })).toBeTruthy();
    expect(screen.getByRole("group", { name: "Bot color" })).toBeTruthy();
    expect(screen.queryByRole("combobox", { name: "Accent" })).toBeNull();
    expect(screen.queryByLabelText("Avatar")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    await settle();
    expect(screen.getByText("Save the profile to finish removing its image.")).toBeTruthy();
    expect(set).toHaveBeenCalledWith(A, { icon: null });
    expect(
      (screen.getByRole("button", { name: "Save revision" }) as HTMLButtonElement).disabled,
    ).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await settle();
    expect(profiles.revise).toHaveBeenCalledWith(A, expect.any(String), 1, {
      ...document(),
      identity: { ...document().identity, avatar_asset_id: null },
    });
    await waitFor(() =>
      expect(screen.queryByText("Save the profile to finish removing its image.")).toBeNull(),
    );
  });

  it("keeps the regular avatar editor when Bots are present outside Bot settings", async () => {
    render(<ProfilesView api={api()} bots={{} as BotsApi} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Alpha — claude — revision 1" }));
    await settle();
    expect(screen.getByRole("combobox", { name: "Accent" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Set" })).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Look" })).toBeNull();
    expect(screen.queryByRole("group", { name: "Bot color" })).toBeNull();
    expect(
      screen
        .getByRole("navigation", { name: "Active profiles" })
        .compareDocumentPosition(screen.getByRole("region", { name: "Profile overview" })) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("puts selected Bot overview and Look before the reachable profile catalog", async () => {
    const profiles = api({
      list: vi.fn(async () => [summary(A), summary(B, "Beta")]),
    });
    const set = vi.fn(async () => "saved" as const);
    const { container } = render(
      <BotHuesContext.Provider value={{ hues: {}, icons: {}, set }}>
        <ProfilesView api={profiles} botSettings initialSelectedId={A} title="Bot settings" />
      </BotHuesContext.Provider>,
    );
    await settle();

    const title = screen.getByRole("heading", { name: "Bot settings", level: 1 });
    const overview = screen.getByRole("region", { name: "Profile overview" });
    const look = screen.getByRole("region", { name: "Look" });
    const intro = container.querySelector(".bot-settings-catalog-head .page-intro");
    if (!intro) throw new Error("Bot settings catalog intro is missing");
    const catalog = screen.getByRole("heading", { name: "Profiles", level: 2 });
    const list = screen.getByRole("navigation", { name: "Active profiles" });
    const color = screen.getByRole("group", { name: "Bot color" });
    const follows = (before: Element, after: Element) =>
      Boolean(before.compareDocumentPosition(after) & Node.DOCUMENT_POSITION_FOLLOWING);

    expect(follows(title, overview)).toBe(true);
    expect(follows(overview, look)).toBe(true);
    expect(follows(look, catalog)).toBe(true);
    expect(follows(catalog, intro)).toBe(true);
    expect(follows(intro, list)).toBe(true);
    expect(follows(list, color)).toBe(true);
    expect(screen.getByRole("button", { name: "New profile" })).toBeTruthy();
    expect(screen.getByLabelText("Show archived")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Beta — claude — revision 1" })).toBeTruthy();
  });

  it("alerts when Reset cannot save the icon choice", async () => {
    const withAvatar = {
      ...definition(),
      document: { ...document(), identity: { ...document().identity, avatar_asset_id: B } },
    };
    const set = vi.fn(async () => {
      throw new Error("Synthetic settings failure");
    });
    render(
      <BotHuesContext.Provider value={{ hues: {}, icons: {}, set }}>
        <ProfilesView
          api={api({ get: vi.fn(async (id) => ({ ...withAvatar, profile_id: id })) })}
          botSettings
        />
      </BotHuesContext.Provider>,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Alpha — claude — revision 1" }));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toBe("The icon could not be saved. Try again."),
    );
    expect(
      (screen.getByRole("button", { name: "Save revision" }) as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  it("keeps Look and its confirmed icon status mounted through a held profile reload", async () => {
    const saved = {
      ...definition(),
      document: { ...document(), identity: { ...document().identity, avatar_asset_id: B } },
    };
    const committed = {
      ...saved,
      revision: 2,
      current_revision: 2,
      document: {
        ...saved.document,
        identity: { ...saved.document.identity, name: "Changed", avatar_asset_id: null },
      },
    };
    let finishSave!: (receipt: {
      profile_id: string;
      revision: number;
      state_revision: number;
    }) => void;
    let finishGet!: (profile: ProfileDefinition) => void;
    const heldSave = new Promise<{ profile_id: string; revision: number; state_revision: number }>(
      (resolve) => {
        finishSave = resolve;
      },
    );
    const heldGet = new Promise<ProfileDefinition>((resolve) => {
      finishGet = resolve;
    });
    let reads = 0;
    const get = vi.fn<ProfileApi["get"]>(async (id) => {
      if (reads++ === 0) return { ...saved, profile_id: id };
      return heldGet;
    });
    const revise = vi.fn<ProfileApi["revise"]>(() => heldSave);
    const set = vi.fn(async () => "saved" as const);
    render(
      <BotHuesContext.Provider value={{ hues: {}, icons: {}, set }}>
        <ProfilesView api={api({ get, revise })} botSettings initialSelectedId={A} />
      </BotHuesContext.Provider>,
    );
    await settle();
    const look = screen.getByRole("region", { name: "Look" });
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    await settle();
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), {
      target: { value: "Changed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await waitFor(() => expect(revise).toHaveBeenCalledOnce());
    expect(
      (screen.getByRole("button", { name: "Upload image…" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect((screen.getByRole("button", { name: "Reset" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect(
      (screen.getByRole("button", { name: "Choose icon…" }) as HTMLButtonElement).disabled,
    ).toBe(false);
    expect((screen.getByRole("radio", { name: "Pink" }) as HTMLInputElement).disabled).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Choose icon…" }));
    fireEvent.click(screen.getByRole("radio", { name: "Gear" }));
    fireEvent.click(screen.getByRole("button", { name: "Use icon" }));
    const status = look.querySelector(".bot-look-status");
    await waitFor(() => expect(status?.textContent).toBe("Icon changed to Gear."));
    finishSave({ profile_id: A, revision: 2, state_revision: 1 });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("region", { name: "Look" })).toBe(look);
    expect(look.querySelector(".bot-look-status")).toBe(status);
    expect(status?.textContent).toBe("Icon changed to Gear.");
    expect(screen.getByText("Save the profile to finish removing its image.")).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: "Upload image…" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect((screen.getByRole("button", { name: "Reset" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect(
      (screen.getByRole("button", { name: "Choose icon…" }) as HTMLButtonElement).disabled,
    ).toBe(false);
    expect((screen.getByRole("radio", { name: "Pink" }) as HTMLInputElement).disabled).toBe(false);
    finishGet(committed);
    await waitFor(() =>
      expect(screen.queryByText("Save the profile to finish removing its image.")).toBeNull(),
    );
    expect(screen.getByRole("region", { name: "Look" })).toBe(look);
    expect(look.querySelector(".bot-look-status")).toBe(status);
    expect(status?.textContent).toBe("Icon changed to Gear.");
    expect(
      (screen.getByRole("button", { name: "Upload image…" }) as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  it("disables only image draft actions for archived Bot profiles", async () => {
    render(
      <BotHuesContext.Provider
        value={{ hues: {}, icons: {}, set: vi.fn(async () => "saved" as const) }}
      >
        <ProfilesView
          api={api({ get: vi.fn(async (id) => ({ ...definition(id), archived: true })) })}
          botSettings
          initialSelectedId={A}
        />
      </BotHuesContext.Provider>,
    );
    await settle();
    expect(
      (screen.getByRole("button", { name: "Upload image…" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect((screen.getByRole("button", { name: "Reset" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect(
      (screen.getByRole("button", { name: "Choose icon…" }) as HTMLButtonElement).disabled,
    ).toBe(false);
    expect((screen.getByRole("radio", { name: "Pink" }) as HTMLInputElement).disabled).toBe(false);
  });

  it("does not show a late Reset error under a newly selected profile", async () => {
    let failReset!: (error: Error) => void;
    const reset = new Promise<"saved">((_resolve, reject) => {
      failReset = reject;
    });
    let finishBeta!: (profile: ProfileDefinition) => void;
    const heldBetaGet = new Promise<ProfileDefinition>((resolve) => {
      finishBeta = resolve;
    });
    const get = vi.fn<ProfileApi["get"]>(async (id) =>
      id === B ? heldBetaGet : definition(id, "Alpha"),
    );
    const set = vi.fn(() => reset);
    render(
      <BotHuesContext.Provider value={{ hues: {}, icons: {}, set }}>
        <ProfilesView
          api={api({
            list: vi.fn(async () => [summary(A, "Alpha"), summary(B, "Beta")]),
            get,
          })}
          botSettings
          initialSelectedId={A}
        />
      </BotHuesContext.Provider>,
    );
    await settle();
    const alphaLook = screen.getByRole("region", { name: "Look" });
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    fireEvent.click(screen.getByRole("button", { name: "Beta — claude — revision 1" }));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(alphaLook.isConnected).toBe(false);
    expect(screen.queryByRole("region", { name: "Look" })).toBeNull();
    finishBeta(definition(B, "Beta"));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Beta" })).toBeTruthy());
    failReset(new Error("Synthetic settings failure"));
    await settle();
    expect(screen.queryByText("The icon could not be saved. Try again.")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("ignores an older Reset failure when a newer same-profile Reset is pending", async () => {
    let failFirst!: (error: Error) => void;
    let failSecond!: (error: Error) => void;
    const first = new Promise<"saved">((_resolve, reject) => {
      failFirst = reject;
    });
    const second = new Promise<"saved">((_resolve, reject) => {
      failSecond = reject;
    });
    const set = vi.fn().mockReturnValueOnce(first).mockReturnValueOnce(second);
    render(
      <BotHuesContext.Provider value={{ hues: {}, icons: {}, set }}>
        <ProfilesView api={api()} botSettings initialSelectedId={A} />
      </BotHuesContext.Provider>,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    failFirst(new Error("Older settings failure"));
    await settle();
    expect(screen.queryByRole("alert")).toBeNull();
    failSecond(new Error("Current settings failure"));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toBe("The icon could not be saved. Try again."),
    );
  });

  it("stages an uploaded Bot image on the profile draft", async () => {
    const { AvatarEditor } = await import("../Avatar/AvatarEditor");
    const urls: AvatarUrls = {
      acquire: vi.fn(() => () => {}),
      get: vi.fn((id) => (id === NEW_AVATAR ? "blob:synthetic-draft-avatar" : null)),
      dispose: vi.fn(),
    };
    vi.mocked(AvatarEditor).mockImplementationOnce(({ onSaved }) => {
      useEffect(() => onSaved(NEW_AVATAR), [onSaved]);
      return <div data-testid="stub-editor" />;
    });
    const revise = vi.fn<ProfileApi["revise"]>(async (id, _command, revision) => ({
      profile_id: id,
      revision: revision + 1,
      state_revision: 1,
    }));
    render(
      <IdentityContext.Provider value={{ ...DEFAULT_IDENTITY, urls }}>
        <BotHuesContext.Provider
          value={{ hues: {}, icons: {}, set: vi.fn(async () => "saved" as const) }}
        >
          <ProfilesView api={api({ revise })} botSettings />
        </BotHuesContext.Provider>
      </IdentityContext.Provider>,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Alpha — claude — revision 1" }));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Upload image…" }));
    await waitFor(() => expect(hasUnsavedWork()).toBe(true));
    const overview = screen.getByRole("region", { name: "Profile overview" });
    await waitFor(() =>
      expect(overview.querySelector(".avatar img")?.getAttribute("src")).toBe(
        "blob:synthetic-draft-avatar",
      ),
    );
    expect(screen.getByRole("region", { name: "Look" }).querySelector(".avatar")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await settle();
    expect(revise.mock.calls[0]?.[3].identity.avatar_asset_id).toBe(NEW_AVATAR);
  });

  it("shows the saved avatar and purpose as text without changing the selection name", async () => {
    const identity: ProfileSummary["identity"] = {
      ...summary().identity,
      purpose: "Review <b>synthetic</b> notes.",
      avatar_asset_id: NEW_AVATAR,
      accent: "purple",
      shape: "circle",
    };
    const acquire = vi.fn<AvatarUrls["acquire"]>(() => () => {});
    const urls: AvatarUrls = {
      acquire,
      get: () => "blob:synthetic-profile",
      dispose: () => {},
    };
    const profiles = api({ list: vi.fn(async () => [{ ...summary(), identity }]) });
    render(
      <IdentityContext.Provider value={{ ...DEFAULT_IDENTITY, urls }}>
        <ProfilesView api={profiles} />
      </IdentityContext.Provider>,
    );
    await settle();
    const row = screen.getByRole("button", {
      name: "Alpha — claude — revision 1",
      description: identity.purpose,
    });
    expect(within(row).getByText(identity.purpose).querySelector("b")).toBeNull();
    const avatar = within(row).getByRole("img", { name: "Alpha" });
    expect(avatar.getAttribute("data-accent")).toBe("purple");
    expect(avatar.getAttribute("data-shape")).toBe("circle");
    expect(avatar.querySelector("img")?.getAttribute("src")).toBe("blob:synthetic-profile");
    expect(acquire).toHaveBeenCalledWith(NEW_AVATAR, 32, expect.any(Function));
    expect(profiles.get).not.toHaveBeenCalled();
  });

  it("shows distinct identity/runtime/provenance/revision sections and a chat action", async () => {
    render(<ProfilesView api={api()} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha — claude — revision 1/ }));
    await settle();
    expect(screen.getByRole("group", { name: "Identity" })).toBeTruthy();
    expect(screen.getByRole("group", { name: "Model and permissions" })).toBeTruthy();
    fireEvent.click(screen.getByText("Advanced"));
    expect(screen.getByRole("region", { name: "Provenance" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "Revision" })).toBeTruthy();
    expect(
      screen.getByText(/A profile supplies a Bot's name, instructions and configuration/),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Start a chat" })).toBeTruthy();
  });

  it("renders editor groups in the specified order", async () => {
    const { container } = render(<ProfilesView api={api()} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    const editor = container.querySelector(".profile-editor") as HTMLElement;
    const groups = Array.from(
      editor.querySelectorAll(":scope > fieldset > legend"),
      (legend) => legend.textContent,
    );
    expect(groups).toEqual([
      "Identity",
      "Instructions",
      "Model and permissions",
      "What it can use",
    ]);
    expect(container.querySelector(":scope .profile-advanced > summary")?.textContent).toBe(
      "Advanced",
    );
  });

  it("omits the cannot-apply box for default execution fields", async () => {
    const value = document();
    value.workspace.allowed_workspace_ids = [];
    value.context.items = [];
    value.native_assets = [];
    value.tools.required = [];
    value.tools.denied = [];
    value.memory = { mode: "provider_inherited", sources: [] };
    value.limits = {
      timeout_seconds: null,
      max_root_tasks: null,
      token_budget: null,
      currency_budget: null,
    };
    render(<ProfilesView api={api({ get: vi.fn(async () => definitionWith(value)) })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    expect(screen.queryByRole("region", { name: "Cannot apply in a chat" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Clear all" })).toBeNull();
  });

  it("shows only set unsupported families, including zero limits", async () => {
    const value = document();
    value.context.items = [];
    value.native_assets = [];
    value.tools.required = [];
    value.tools.denied = [];
    value.memory = { mode: "provider_inherited", sources: [] };
    value.limits = {
      timeout_seconds: 0,
      max_root_tasks: null,
      token_budget: null,
      currency_budget: null,
    };
    render(<ProfilesView api={api({ get: vi.fn(async () => definitionWith(value)) })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    const box = screen.getByRole("region", { name: "Cannot apply in a chat" });
    expect(within(box).queryByText("MCP configuration")).toBeNull();
    expect(within(box).getByText("Limits")).toBeTruthy();
    for (const label of [
      "Context items",
      "Native assets",
      "Required tools",
      "Denied tools",
      "Memory",
    ]) {
      expect(within(box).queryByText(label)).toBeNull();
    }
    expect(within(box).getByText(/timeout_seconds.*0/)).toBeTruthy();
  });

  it.each([
    [
      "Context items",
      (value: ProfileDocumentV1) => (value.context.items = [{ kind: "selection", id: "one" }]),
    ],
    [
      "Native assets",
      (value: ProfileDocumentV1) => (value.native_assets = [{ kind: "agent", id: "one" }]),
    ],
    ["Required tools", (value: ProfileDocumentV1) => (value.tools.required = ["Read"])],
    ["Denied tools", (value: ProfileDocumentV1) => (value.tools.denied = ["Read"])],
    ["Memory", (value: ProfileDocumentV1) => (value.memory.sources = [{ kind: "source" }])],
    ["Memory", (value: ProfileDocumentV1) => (value.memory.mode = "disabled")],
    ["Limits", (value: ProfileDocumentV1) => (value.limits.currency_budget = 0)],
  ])("shows %s when its unsupported family is set", async (label, setValue) => {
    const value = document();
    value.workspace.allowed_workspace_ids = [];
    value.context.items = [];
    value.native_assets = [];
    value.tools.required = [];
    value.tools.denied = [];
    value.memory = { mode: "provider_inherited", sources: [] };
    value.limits = {
      timeout_seconds: null,
      max_root_tasks: null,
      token_budget: null,
      currency_budget: null,
    };
    (setValue as (value: ProfileDocumentV1) => void)(value);
    render(<ProfilesView api={api({ get: vi.fn(async () => definitionWith(value)) })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    const box = screen.getByRole("region", { name: "Cannot apply in a chat" });
    expect(within(box).getByRole("group", { name: label as string })).toBeTruthy();
    expect(within(box).getAllByRole("group")).toHaveLength(1);
  });

  it("explains cannot-apply evaluation without exposing raw reason codes", async () => {
    const blocked: BlockedProfilePreview = {
      launchable: false,
      fields: [
        {
          field: "limits.timeout_seconds",
          preview_class: "unsupported",
          status: "unsupported",
          effective_value: 0,
          source: "profile",
          reason: "timeout_unproven",
        },
      ],
      blockers: ["limits.timeout_seconds"],
      profile_id: A,
      profile_revision: 1,
    };
    const value = document();
    value.limits.timeout_seconds = 0;
    render(
      <ProfilesView
        api={api({ get: vi.fn(async () => definitionWith(value)) })}
        launchApi={launchApi({ prepare: vi.fn(async () => blocked) })}
        workspaceId={WORKSPACE}
      />,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Start a chat" }));
    await settle();
    const reason = within(screen.getByRole("region", { name: "Cannot apply in a chat" })).getByText(
      "This field cannot be used: The timeout limit has not been verified as enforceable.",
    );
    expect(reason.closest("details")).toBeNull();
    const technical = screen.getByText(/timeout_unproven/);
    expect(technical.closest("details")).toBeTruthy();
    expect((technical.closest("details") as HTMLDetailsElement).open).toBe(false);
  });

  it("clears only one unsupported family, while Clear all preserves MCP and provenance", async () => {
    const value = document();
    value.context.items = [{ kind: "selection", id: "context-2" }];
    value.memory = { mode: "disabled", sources: [] };
    value.tools.mcp_configuration = { servers: [] };
    value.provenance = { kind: "derived", derived_from: { id: A }, native_source: null };
    const revise = vi.fn<ProfileApi["revise"]>(async (id, _command, revision) => ({
      profile_id: id,
      revision: revision + 1,
      state_revision: 1,
    }));
    render(<ProfilesView api={api({ get: vi.fn(async () => definitionWith(value)), revise })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    const box = screen.getByRole("region", { name: "Cannot apply in a chat" });
    fireEvent.click(within(box).getAllByRole("button", { name: "Clear" })[0] as HTMLElement);
    expect(within(box).queryByRole("group", { name: "Context items" })).toBeNull();
    expect(within(box).getByText(/disabled/)).toBeTruthy();
    fireEvent.click(within(box).getByRole("button", { name: "Clear all" }));
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await settle();
    expect(revise).toHaveBeenCalledWith(A, expect.any(String), 1, {
      ...value,
      context: { items: [] },
      native_assets: [],
      tools: { required: [], denied: [], mcp_configuration: { servers: [] } },
      memory: { mode: "provider_inherited", sources: [] },
      limits: {
        timeout_seconds: null,
        max_root_tasks: null,
        token_budget: null,
        currency_budget: null,
      },
      provenance: { kind: "derived", derived_from: { id: A }, native_source: null },
    });
  });

  it("keeps Try it in a chat visible and hides technical evidence until opened", async () => {
    const savedModel = "viki-llm/00000000-0000-4000-8000-000000000099/custom-model";
    const value = document();
    value.runtime.model = savedModel;
    const profiles = api({ get: vi.fn(async () => definitionWith(value)) });
    render(<ProfilesView api={profiles} launchApi={launchApi()} workspaceId={WORKSPACE} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    const review = screen.getByRole("region", { name: "Try it in a chat" });
    expect(review.textContent).toContain(
      "Saved runtime: Claude Code; model custom-model; effort High; permission Use my default",
    );
    expect(review.textContent).not.toContain("00000000-0000-4000-8000-000000000099");
    expect(within(review).getByRole("button", { name: "Start a chat" })).toBeTruthy();
    const technicalEvidence = review.querySelector("details") as HTMLDetailsElement;
    expect(technicalEvidence.open).toBe(false);
    fireEvent.click(within(review).getByText(t("profiles.technicalEvidence")));
    expect(technicalEvidence.open).toBe(true);
    expect(within(review).getByText(/Provider evidence:/)).toBeTruthy();
  });

  it("labels saved runtime values without showing unsaved draft values", async () => {
    const saved = document();
    saved.runtime.model = "viki-llm/00000000-0000-4000-8000-000000000099/custom-model";
    saved.runtime.effort = "high";
    saved.runtime.permission_mode = "plan";
    const profiles = api({ get: vi.fn(async () => definitionWith(saved)) });
    render(<ProfilesView api={profiles} launchApi={launchApi()} workspaceId={WORKSPACE} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();

    const overview = screen.getByRole("region", { name: "Profile overview" });
    expect(within(overview).getByText("Permission setting")).toBeTruthy();
    expect(Array.from(overview.querySelectorAll("dt"), (item) => item.textContent)).toEqual([
      "Runs on",
      "Model",
      "Permission setting",
    ]);

    fireEvent.click(screen.getByText("Advanced"));
    expect(screen.getByRole("button", { name: "Duplicate as Claude Code" })).toBeTruthy();

    const effort = screen.getByRole("combobox", { name: "Effort" }) as HTMLSelectElement;
    const permission = screen.getByRole("combobox", {
      name: "Permission mode",
    }) as HTMLSelectElement;
    fireEvent.change(effort, { target: { value: "low" } });
    fireEvent.change(permission, { target: { value: "" } });
    expect(effort.value).toBe("low");
    expect(permission.value).toBe("");

    const review = screen.getByRole("region", { name: "Try it in a chat" });
    expect(review.textContent).toContain(
      "Saved runtime: Claude Code; model custom-model; effort High; permission Plan first",
    );
  });

  it("labels model values in effective fields and revision comparisons", async () => {
    const connectedModel = "viki-llm/00000000-0000-4000-8000-000000000099/custom-model";
    const compare = vi.fn<ProfileApi["compare"]>(async (id, left, right) => ({
      profile_id: id,
      left,
      right,
      changes: [{ field: "runtime.model", before: connectedModel, after: connectedModel }],
    }));
    const prepare = vi.fn(async () => ({
      ...prepared(),
      fields: [
        {
          field: "runtime.model",
          preview_class: "effective" as const,
          status: "supported" as const,
          effective_value: connectedModel,
          source: "inherited" as const,
          reason: null,
        },
      ],
    }));
    const value = document();
    value.runtime.model = connectedModel;
    render(
      <ProfilesView
        api={api({ get: vi.fn(async () => definitionWith(value)), compare })}
        launchApi={launchApi({ prepare })}
        workspaceId={WORKSPACE}
      />,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Start a chat" }));
    await settle();
    const review = screen.getByRole("region", { name: "Try it in a chat" });
    expect(review.textContent).toContain("custom-model");
    expect(review.textContent).not.toContain("00000000-0000-4000-8000-000000000099");
    fireEvent.click(within(review).getByText(t("profiles.technicalEvidence")));
    const effective = screen.getByRole("region", { name: "Effective profile fields" });
    expect(effective.textContent).toContain("custom-model");
    expect(effective.textContent).not.toContain("00000000-0000-4000-8000-000000000099");
    fireEvent.click(screen.getByText("Advanced"));
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    await settle();
    expect(screen.getAllByText(/custom-model/).length).toBeGreaterThan(0);
    expect(screen.getByRole("region", { name: "Compare revisions" }).textContent).not.toContain(
      "00000000-0000-4000-8000-000000000099",
    );
  });

  it("preserves advanced restrictions and reuses a command only for the exact retry", async () => {
    const revise = vi
      .fn<ProfileApi["revise"]>()
      .mockRejectedValue(new ProfileApiError(409, "conflict"));
    render(<ProfilesView api={api({ revise })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha — claude/ }));
    await settle();
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), {
      target: { value: "Changed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await settle();
    const first = revise.mock.calls[0];
    const retry = revise.mock.calls[1];
    expect(first?.[1]).toBe(retry?.[1]);
    expect(first?.[3].workspace).toEqual(document().workspace);
    expect(first?.[3].context).toEqual(document().context);
    expect(first?.[3].native_assets).toEqual(document().native_assets);
    expect(first?.[3].tools).toEqual(document().tools);
    expect(first?.[3].limits).toEqual(document().limits);

    fireEvent.change(screen.getByRole("textbox", { name: "Purpose" }), {
      target: { value: "Different payload" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await settle();
    expect(revise.mock.calls[2]?.[1]).not.toBe(first?.[1]);
  });

  it("keeps archived browsing separate and creates with inherited advanced defaults", async () => {
    const list = vi.fn<ProfileApi["list"]>((archived) =>
      Promise.resolve(archived ? [summary(B, "Archived", true)] : [summary()]),
    );
    const create = vi.fn<ProfileApi["create"]>(async (id) => ({
      profile_id: id,
      revision: 1,
      state_revision: 1,
    }));
    render(<ProfilesView api={api({ list, create })} />);
    await settle();
    fireEvent.click(screen.getByRole("checkbox", { name: "Show archived" }));
    await settle();
    expect(screen.getByRole("navigation", { name: "Archived profiles" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Archived — claude/ })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "New profile" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), {
      target: { value: "Created" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create profile" }));
    await settle();
    const sent = create.mock.calls[0]?.[2];
    expect(sent?.identity.name).toBe("Created");
    expect(sent?.workspace.allowed_workspace_ids).toEqual([]);
    expect(sent?.tools).toEqual({ required: [], denied: [], mcp_configuration: null });
    expect(sent?.limits).toEqual({
      timeout_seconds: null,
      max_root_tasks: null,
      token_budget: null,
      currency_budget: null,
    });
  });

  it("ignores a late selected definition when an API ignores abort", async () => {
    let resolveA!: (value: ProfileDefinition) => void;
    let resolveB!: (value: ProfileDefinition) => void;
    const a = new Promise<ProfileDefinition>((resolve) => {
      resolveA = resolve;
    });
    const b = new Promise<ProfileDefinition>((resolve) => {
      resolveB = resolve;
    });
    const get = vi.fn<ProfileApi["get"]>((id) => (id === A ? a : b));
    render(
      <ProfilesView api={api({ list: vi.fn(async () => [summary(), summary(B, "Beta")]), get })} />,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha — claude/ }));
    fireEvent.click(screen.getByRole("button", { name: /Beta — claude/ }));
    await act(async () => resolveB(definition(B, "Beta")));
    expect((screen.getByRole("textbox", { name: "Name" }) as HTMLInputElement).value).toBe("Beta");
    await act(async () => resolveA(definition(A, "Alpha stale")));
    expect((screen.getByRole("textbox", { name: "Name" }) as HTMLInputElement).value).toBe("Beta");
  });

  it("does not navigate back when a revision finishes after selecting another profile", async () => {
    let resolveRevision!: (value: {
      profile_id: string;
      revision: number;
      state_revision: number;
    }) => void;
    const revise = vi.fn<ProfileApi["revise"]>(
      () =>
        new Promise((resolve) => {
          resolveRevision = resolve;
        }),
    );
    render(
      <ProfilesView
        api={api({
          list: vi.fn(async () => [summary(), summary(B, "Beta")]),
          get: vi.fn(async (id) => definition(id, id === B ? "Beta" : "Alpha")),
          revise,
        })}
      />,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), {
      target: { value: "Changed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    fireEvent.click(screen.getByRole("button", { name: /Beta .* claude/ }));
    fireEvent.click(
      within(screen.getByRole("alertdialog")).getByRole("button", { name: "Discard" }),
    );
    await settle();
    await act(async () => resolveRevision({ profile_id: A, revision: 2, state_revision: 1 }));
    expect((screen.getByRole("textbox", { name: "Name" }) as HTMLInputElement).value).toBe("Beta");
  });

  it("ignores an old API mutation error after the API prop changes", async () => {
    let rejectRevision!: (error: unknown) => void;
    const revise = vi.fn<ProfileApi["revise"]>(
      () =>
        new Promise((_resolve, reject) => {
          rejectRevision = reject;
        }),
    );
    const view = render(<ProfilesView api={api({ revise })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), {
      target: { value: "Changed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    view.rerender(<ProfilesView api={api()} />);
    await settle();
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), {
      target: { value: "Changed again" },
    });
    await act(async () => rejectRevision(new ProfileApiError(409, "conflict")));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("button", { name: "Save revision" }).hasAttribute("disabled")).toBe(
      false,
    );
  });

  it("settles an ignored mutation after unmount without updating the view", async () => {
    let resolveRevision!: (value: {
      profile_id: string;
      revision: number;
      state_revision: number;
    }) => void;
    const revise = vi.fn<ProfileApi["revise"]>(
      () =>
        new Promise((resolve) => {
          resolveRevision = resolve;
        }),
    );
    const view = render(<ProfilesView api={api({ revise })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), {
      target: { value: "Changed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    view.unmount();
    await act(async () => resolveRevision({ profile_id: A, revision: 2, state_revision: 1 }));
    expect(screen.queryByRole("region", { name: "Profiles" })).toBeNull();
  });

  it("shows observed capability truth before creating a local draft", async () => {
    const identity = {
      name: "Alpha",
      avatar_asset_id: null,
      accent: "orange" as const,
      shape: "rounded" as const,
      profile_revision: 1,
    };
    const prepare = vi.fn<ProfileLaunchApi["prepare"]>(async () => ({ ...prepared(), identity }));
    const onPrepared = vi.fn();
    render(
      <ProfilesView
        api={api()}
        launchApi={launchApi({ prepare })}
        workspaceId={WORKSPACE}
        onPrepared={onPrepared}
      />,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    await settle();
    expect(screen.getByText(/Provider evidence: 2.1.259/)).toBeTruthy();
    expect(screen.getByText("plan_mode: supported")).toBeTruthy();
    expect(screen.getByText("Launch readiness has not been evaluated.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Start a chat" }));
    await settle();
    expect(prepare).toHaveBeenCalledWith(
      A,
      expect.any(String),
      expect.any(String),
      1,
      WORKSPACE,
      expect.any(AbortSignal),
    );
    // The answer's identity rides along, so the chat opens bound from its first render.
    expect(onPrepared).toHaveBeenCalledWith(B, identity);
    expect(screen.getByText("No capability blockers.")).toBeTruthy();
  });

  it("refuses to combine provider evidence from different observations", async () => {
    const source = launchApi({
      models: vi.fn(async () => ({
        provider: "claude",
        adapter_version: "profile-v1",
        native_version: "2.1.259",
        observed_at: NOW,
        catalog_key: "b".repeat(64),
        status: "supported" as const,
        models: [{ value: "sonnet", label: "Sonnet", description: "Observed model" }],
      })),
    });
    render(<ProfilesView api={api()} launchApi={source} workspaceId={WORKSPACE} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    await settle();
    expect(screen.getByRole("alert").textContent).toContain("Provider evidence changed");
    expect(screen.getByRole("button", { name: "Start a chat" }).hasAttribute("disabled")).toBe(
      true,
    );
    expect(source.prepare).not.toHaveBeenCalled();
  });

  it("reuses preview identities for an exact retry and requires saved fields", async () => {
    const prepare = vi
      .fn<ProfileLaunchApi["prepare"]>()
      .mockRejectedValueOnce(new ProfileLaunchApiError(409, "conflict"))
      .mockResolvedValue(prepared());
    render(<ProfilesView api={api()} launchApi={launchApi({ prepare })} workspaceId={WORKSPACE} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Start a chat" }));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Start a chat" }));
    await settle();
    expect(prepare.mock.calls[0]?.slice(1, 3)).toEqual(prepare.mock.calls[1]?.slice(1, 3));
    fireEvent.click(screen.getByRole("button", { name: "Start a chat" }));
    await settle();
    expect(prepare.mock.calls[2]?.slice(1, 3)).not.toEqual(prepare.mock.calls[1]?.slice(1, 3));

    fireEvent.change(screen.getByRole("textbox", { name: "Purpose" }), {
      target: { value: "Unsaved" },
    });
    fireEvent.click(screen.getByText(t("profiles.technicalEvidence")));
    expect(screen.getByText("Save this revision before starting a chat.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Start a chat" }).hasAttribute("disabled")).toBe(
      true,
    );
  });

  it("ignores a prepared draft after the selected profile changes", async () => {
    let resolvePrepare!: (value: PreparedProfilePreview) => void;
    const prepare = vi.fn<ProfileLaunchApi["prepare"]>(
      () =>
        new Promise((resolve) => {
          resolvePrepare = resolve;
        }),
    );
    const onPrepared = vi.fn();
    render(
      <ProfilesView
        api={api({
          list: vi.fn(async () => [summary(), summary(B, "Beta")]),
          get: vi.fn(async (id) => definition(id, id === B ? "Beta" : "Alpha")),
        })}
        launchApi={launchApi({ prepare })}
        workspaceId={WORKSPACE}
        onPrepared={onPrepared}
      />,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Start a chat" }));
    fireEvent.click(screen.getByRole("button", { name: /Beta .* claude/ }));
    await settle();
    await act(async () => resolvePrepare(prepared()));
    expect(onPrepared).not.toHaveBeenCalled();
    expect((screen.getByRole("textbox", { name: "Name" }) as HTMLInputElement).value).toBe("Beta");
  });

  it("ignores a prepared draft after the active workspace changes", async () => {
    let resolvePrepare!: (value: PreparedProfilePreview) => void;
    const prepare = vi.fn<ProfileLaunchApi["prepare"]>(
      () =>
        new Promise((resolve) => {
          resolvePrepare = resolve;
        }),
    );
    const profiles = api();
    const launches = launchApi({ prepare });
    const onPrepared = vi.fn();
    const view = render(
      <ProfilesView
        api={profiles}
        launchApi={launches}
        workspaceId={WORKSPACE}
        onPrepared={onPrepared}
      />,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Start a chat" }));
    view.rerender(
      <ProfilesView api={profiles} launchApi={launches} workspaceId={B} onPrepared={onPrepared} />,
    );
    await act(async () => resolvePrepare(prepared()));
    expect(onPrepared).not.toHaveBeenCalled();
  });

  it("ignores a late comparison after the selected profile changes", async () => {
    let resolveCompare!: (value: Awaited<ReturnType<ProfileApi["compare"]>>) => void;
    const compare = vi.fn<ProfileApi["compare"]>(
      () =>
        new Promise((resolve) => {
          resolveCompare = resolve;
        }),
    );
    render(
      <ProfilesView
        api={api({
          list: vi.fn(async () => [summary(), summary(B, "Beta")]),
          get: vi.fn(async (id) => definition(id, id === B ? "Beta" : "Alpha")),
          compare,
        })}
      />,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    fireEvent.click(screen.getByText("Advanced"));
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    fireEvent.click(screen.getByRole("button", { name: /Beta .* claude/ }));
    await settle();
    await act(async () =>
      resolveCompare({
        profile_id: A,
        left: 1,
        right: 1,
        changes: [{ field: "identity.name", before: "Alpha", after: "Stale" }],
      }),
    );
    expect(screen.queryByRole("heading", { name: "identity.name" })).toBeNull();
  });

  it("asks before discarding a dirty edit when another profile is clicked, and keeps the edit on cancel", async () => {
    const get = vi.fn<ProfileApi["get"]>(async (id) => definition(id));
    render(
      <ProfilesView
        api={api({
          list: vi.fn(async () => [
            summary(),
            {
              ...summary(B),
              identity: { ...summary().identity, purpose: "Second synthetic purpose" },
            },
          ]),
          get,
        })}
      />,
    );
    await settle();
    const selected = screen.getByRole("button", {
      name: "Alpha — claude — revision 1",
      description: "Synthetic purpose",
    });
    const other = screen.getByRole("button", {
      name: "Alpha — claude — revision 1",
      description: "Second synthetic purpose",
    });
    fireEvent.click(selected);
    await settle();
    expect(get).toHaveBeenCalledWith(A, undefined, expect.any(AbortSignal));
    expect(selected.getAttribute("aria-current")).toBe("true");
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), {
      target: { value: "Changed" },
    });
    fireEvent.click(other);
    expect(screen.getByRole("alertdialog", { name: "Discard changes" })).toBeTruthy();
    // `document` in this file is the profile fixture, so the focus is read from the node's document.
    const discard = screen.getByRole("alertdialog");
    expect(discard.ownerDocument.activeElement).toBe(
      within(discard).getByRole("button", { name: "Cancel" }),
    );
    fireEvent.click(
      within(screen.getByRole("alertdialog")).getByRole("button", { name: "Cancel" }),
    );
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(get).toHaveBeenCalledTimes(1);
    expect(selected.getAttribute("aria-current")).toBe("true");
    expect(other.hasAttribute("aria-current")).toBe(false);
    expect((screen.getByRole("textbox", { name: "Name" }) as HTMLInputElement).value).toBe(
      "Changed",
    );
    fireEvent.click(other);
    fireEvent.click(
      within(screen.getByRole("alertdialog")).getByRole("button", { name: "Discard" }),
    );
    await settle();
    expect(get).toHaveBeenCalledTimes(2);
    expect(other.getAttribute("aria-current")).toBe("true");
  });

  it("disables Archive and Duplicate while the document is dirty", async () => {
    render(<ProfilesView api={api()} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    fireEvent.click(screen.getByText("Advanced"));
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), {
      target: { value: "Changed" },
    });
    expect((screen.getByRole("button", { name: "Archive" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect(
      (
        screen.getByRole("button", {
          name: t("profiles.duplicateAsClaudeCode"),
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    expect(screen.getByText("Save or discard this edit first.")).toBeTruthy();
  });

  it("treats a new profile with typed text as dirty", async () => {
    render(<ProfilesView api={api()} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "New profile" }));
    const draftId = screen.getByText(/Profile ID:/).textContent;
    fireEvent.click(screen.getByText("Advanced"));
    expect(screen.getByRole("region", { name: "Provenance" })).toBeTruthy();
    expect(screen.getByText(draftId as string)).toBeTruthy();
    fireEvent.change(screen.getByRole("textbox", { name: "Instructions" }), {
      target: { value: "Draft instructions" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    expect(screen.getByRole("alertdialog")).toBeTruthy();
    fireEvent.click(
      within(screen.getByRole("alertdialog")).getByRole("button", { name: "Cancel" }),
    );
    expect(
      (screen.getByRole("textbox", { name: "Instructions" }) as HTMLTextAreaElement).value,
    ).toBe("Draft instructions");
  });

  it("renders the avatar row, and Reset clears the id and marks the form dirty", async () => {
    const withAvatar = {
      ...definition(),
      document: { ...document(), identity: { ...document().identity, avatar_asset_id: B } },
    };
    render(<ProfilesView api={api({ get: vi.fn(async () => withAvatar) })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    expect(screen.getByText("Avatar")).toBeTruthy();
    expect(hasUnsavedWork()).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    expect(hasUnsavedWork()).toBe(true);
  });

  it("Set then Save revision sends the newly chosen avatar id", async () => {
    const { AvatarEditor } = await import("../Avatar/AvatarEditor");
    vi.mocked(AvatarEditor).mockImplementationOnce(({ onSaved }) => {
      useEffect(() => onSaved(NEW_AVATAR), [onSaved]);
      return <div data-testid="stub-editor" />;
    });
    const revise = vi.fn<ProfileApi["revise"]>(async (id, _command, revision) => ({
      profile_id: id,
      revision: revision + 1,
      state_revision: 1,
    }));
    render(<ProfilesView api={api({ revise })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Set" }));
    await waitFor(() => expect(hasUnsavedWork()).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await settle();
    expect(revise.mock.calls[0]?.[3].identity.avatar_asset_id).toBe(NEW_AVATAR);
  });

  it("Reset then Save revision sends a null avatar id", async () => {
    const withAvatar = {
      ...definition(),
      document: { ...document(), identity: { ...document().identity, avatar_asset_id: B } },
    };
    const revise = vi.fn<ProfileApi["revise"]>(async (id, _command, revision) => ({
      profile_id: id,
      revision: revision + 1,
      state_revision: 1,
    }));
    render(<ProfilesView api={api({ get: vi.fn(async () => withAvatar), revise })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await settle();
    expect(revise.mock.calls[0]?.[3].identity.avatar_asset_id).toBeNull();
  });

  it("names the avatar when a save is refused because it is no longer available", async () => {
    const revise = vi
      .fn<ProfileApi["revise"]>()
      .mockRejectedValue(new ProfileApiError(400, "avatar_missing"));
    render(<ProfilesView api={api({ revise })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), {
      target: { value: "Changed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(
      "The avatar is no longer available. Replace or reset it.",
    );
  });

  it("keeps the compare and duplicate controls outside the form, and gates Save on an edit", async () => {
    const profiles = api();
    render(<ProfilesView api={profiles} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    fireEvent.click(screen.getByText("Advanced"));
    expect(
      (screen.getByRole("button", { name: "Save revision" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    const rightRevision = screen.getByRole("spinbutton", {
      name: "Right revision",
    }) as HTMLInputElement;
    expect(screen.queryByRole("textbox", { name: "Duplicate provider" })).toBeNull();
    expect(screen.getByRole("button", { name: t("profiles.duplicateAsClaudeCode") })).toBeTruthy();
    expect(rightRevision.form).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: t("profiles.duplicateAsClaudeCode") }));
    await settle();
    expect(profiles.duplicate).toHaveBeenCalledWith(
      A,
      expect.any(String),
      1,
      expect.any(String),
      "claude",
    );
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), {
      target: { value: "Changed" },
    });
    expect(
      (screen.getByRole("button", { name: "Save revision" }) as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  it("shows a legacy provider until the explicit fix action is clicked", async () => {
    const saved = definition();
    saved.document.runtime.provider = "legacy-provider";
    const revise = vi.fn<ProfileApi["revise"]>(async (id, _command, revision) => ({
      profile_id: id,
      revision: revision + 1,
      state_revision: 1,
    }));
    const launch = launchApi();
    const profiles = api({ get: vi.fn(async () => saved), revise });
    render(<ProfilesView api={profiles} launchApi={launch} workspaceId={WORKSPACE} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    const modelGroup = screen.getByRole("group", { name: "Model and permissions" });
    expect(within(modelGroup).getByText(/Runs on: legacy-provider/)).toBeTruthy();
    expect(within(modelGroup).getByRole("button", { name: "Use Claude Code" })).toBeTruthy();
    expect(saved.document.runtime.provider).toBe("legacy-provider");
    fireEvent.click(within(modelGroup).getByRole("button", { name: "Use Claude Code" }));
    expect(within(modelGroup).queryByText("legacy-provider")).toBeNull();
    await settle();
    expect(launch.models).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await settle();
    expect(revise.mock.calls[0]?.[3].runtime.provider).toBe("claude");
  });

  it("uses one Claude model observation and keeps unsupported saved permissions visible", async () => {
    const saved = definition();
    saved.document.runtime.model = "sonnet";
    saved.document.runtime.permission_mode = "acceptEdits";
    const models = vi.fn<ProfileLaunchApi["models"]>(async () => ({
      provider: "claude",
      adapter_version: "profile-v1",
      native_version: "2.1.259",
      observed_at: NOW,
      catalog_key: "a".repeat(64),
      status: "supported",
      models: [{ value: "sonnet", label: "Sonnet", description: "Observed model" }],
    }));
    render(
      <ProfilesView
        api={api({ get: vi.fn(async () => saved) })}
        launchApi={launchApi({ models })}
        workspaceId={WORKSPACE}
        defaultModel="haiku"
      />,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    expect(models).toHaveBeenCalledTimes(1);
    expect(
      screen.getByText("App default is haiku. Change it in Settings › Chat › Default model."),
    ).toBeTruthy();
    const permission = screen.getByRole("combobox", {
      name: "Permission mode",
    }) as HTMLSelectElement;
    expect(permission.value).toBe("acceptEdits");
    expect(
      (
        within(permission).getByRole("option", {
          name: /acceptEdits.*unavailable/i,
        }) as HTMLOptionElement
      ).disabled,
    ).toBe(true);
    expect(
      within(screen.getByRole("region", { name: "Profile overview" })).getByText(
        /The saved permission mode \(acceptEdits\) is unavailable\./,
      ),
    ).toBeTruthy();
  });

  it("retries failed model evidence for the same Claude profile", async () => {
    const models = vi
      .fn<ProfileLaunchApi["models"]>()
      .mockRejectedValueOnce(new ProfileLaunchApiError(503, "unavailable"))
      .mockResolvedValue({
        provider: "claude",
        adapter_version: "profile-v1",
        native_version: "2.1.259",
        observed_at: NOW,
        catalog_key: "a".repeat(64),
        status: "supported",
        models: [{ value: "sonnet", label: "Sonnet", description: "Observed model" }],
      });
    render(<ProfilesView api={api()} launchApi={launchApi({ models })} workspaceId={WORKSPACE} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    expect(models).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await settle();
    await settle();
    expect(models).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("disables editing and Save for an archived profile", async () => {
    render(
      <ProfilesView
        api={api({ get: vi.fn(async (id) => ({ ...definition(id), archived: true })) })}
      />,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    expect(screen.getByText("Unarchive this profile to edit it.")).toBeTruthy();
    expect(screen.getByRole("group", { name: "Identity" }).hasAttribute("disabled")).toBe(true);
    expect(
      screen.getByRole("group", { name: "Model and permissions" }).hasAttribute("disabled"),
    ).toBe(true);
    expect(
      (screen.getByRole("textbox", { name: "Instructions" }) as HTMLTextAreaElement).disabled,
    ).toBe(true);
    expect(
      (screen.getByRole("button", { name: "Save revision" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("ends ready after a click on the selected profile reloads a pending evidence load", async () => {
    let resolveCapabilities!: (
      value: Awaited<ReturnType<ProfileLaunchApi["capabilities"]>>,
    ) => void;
    const capabilities = vi.fn<ProfileLaunchApi["capabilities"]>(
      () =>
        new Promise((resolve) => {
          resolveCapabilities = resolve;
        }),
    );
    render(
      <ProfilesView api={api()} launchApi={launchApi({ capabilities })} workspaceId={WORKSPACE} />,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    expect(screen.getByText("Loading provider evidence…")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    await settle();
    await act(async () =>
      resolveCapabilities({
        provider: "claude",
        adapter_version: "profile-v1",
        native_version: "2.1.259",
        observed_at: NOW,
        catalog_key: "a".repeat(64),
        capabilities: [
          { id: "plan_mode", status: "supported", reason: null, evidence: "fixture_exact" },
        ],
      }),
    );
    await settle();
    expect(
      (screen.getByRole("button", { name: "Start a chat" }) as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  it("resends the same draft ids after a reload interrupts Create draft", async () => {
    const prepare = vi.fn<ProfileLaunchApi["prepare"]>(() => new Promise(() => {}));
    render(<ProfilesView api={api()} launchApi={launchApi({ prepare })} workspaceId={WORKSPACE} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Start a chat" }));
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Start a chat" }));
    await settle();
    expect(prepare.mock.calls.length).toBe(2);
    expect(prepare.mock.calls[0]?.slice(1, 3)).toEqual(prepare.mock.calls[1]?.slice(1, 3));
  });

  it("shows the fixed ellipsis while creating a draft", async () => {
    const prepare = vi.fn<ProfileLaunchApi["prepare"]>(() => new Promise(() => {}));
    render(<ProfilesView api={api()} launchApi={launchApi({ prepare })} workspaceId={WORKSPACE} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Start a chat" }));
    expect(screen.getByText("Starting chat…")).toBeTruthy();
  });

  it("renders blockers for a blocked preview and does not call onPrepared", async () => {
    const blocked: BlockedProfilePreview = {
      launchable: false,
      fields: [],
      blockers: ["runtime.effort"],
      profile_id: A,
      profile_revision: 1,
    };
    const prepare = vi.fn<ProfileLaunchApi["prepare"]>(async () => blocked);
    const onPrepared = vi.fn();
    render(
      <ProfilesView
        api={api()}
        launchApi={launchApi({ prepare })}
        workspaceId={WORKSPACE}
        onPrepared={onPrepared}
      />,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Start a chat" }));
    await settle();
    expect(screen.getByText(problemText("runtime.effort"))).toBeTruthy();
    expect(onPrepared).not.toHaveBeenCalled();
  });

  it("names the field and limit when a save exceeds the UTF-8 byte bound", async () => {
    const revise = vi.fn<ProfileApi["revise"]>();
    render(<ProfilesView api={api({ revise })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), {
      target: { value: "あ".repeat(43) },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await settle();
    expect(screen.getByRole("alert").textContent).toBe("Name is over 128 bytes.");
    expect(revise).not.toHaveBeenCalled();
  });

  it("reports an unsaved edit to the app, and clears it when undone and on unmount", async () => {
    const view = render(<ProfilesView api={api()} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    expect(hasUnsavedWork()).toBe(false);
    const name = screen.getByRole("textbox", { name: "Name" });
    fireEvent.change(name, { target: { value: "Changed" } });
    expect(hasUnsavedWork()).toBe(true);
    fireEvent.change(name, { target: { value: "Alpha" } });
    expect(hasUnsavedWork()).toBe(false);
    fireEvent.change(name, { target: { value: "Changed" } });
    view.unmount();
    expect(hasUnsavedWork()).toBe(false);
  });

  it("starts a new profile from a seat and creates it with that seat's job", async () => {
    const create = vi.fn<ProfileApi["create"]>(async (id) => ({
      profile_id: id,
      revision: 1,
      state_revision: 1,
    }));
    render(<ProfilesView api={api({ create })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "New profile" }));
    const starters = screen.getByRole("group", { name: "Start from a seat" });
    expect(
      within(starters)
        .getAllByRole("button")
        .map((button) => button.textContent),
    ).toEqual(["Chief", "Ops", "Research", "Writer", "Engineer", "Desk", "Checker"]);
    fireEvent.click(within(starters).getByRole("button", { name: "Research" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect((screen.getByRole("textbox", { name: "Name" }) as HTMLInputElement).value).toBe(
      "Research",
    );
    expect(
      (screen.getByRole("textbox", { name: "Instructions" }) as HTMLTextAreaElement).value,
    ).toContain("owner/research");
    fireEvent.click(within(starters).getByRole("button", { name: "Writer" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Create profile" }));
    await settle();
    const sent = create.mock.calls[0]?.[2];
    expect(sent?.identity).toMatchObject({ name: "Writer", accent: "green" });
    expect(sent?.instructions).toContain("owner/writer");
    expect(sent?.runtime).toEqual({
      provider: "claude",
      model: null,
      effort: "high",
      permission_mode: null,
    });
  });

  it("clears a failed create's error when a seat replaces the draft", async () => {
    const create = vi
      .fn<ProfileApi["create"]>()
      .mockRejectedValue(new ProfileApiError(422, "invalid_document"));
    render(<ProfilesView api={api({ create })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "New profile" }));
    fireEvent.click(screen.getByRole("button", { name: "Create profile" }));
    await settle();
    expect(screen.getByRole("alert").textContent).toBe("The profile fields are not valid.");
    fireEvent.click(screen.getByRole("button", { name: "Ops" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("asks before a seat replaces typed text, keeps it on cancel, and offers no seat to edit", async () => {
    render(<ProfilesView api={api()} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "New profile" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), {
      target: { value: "Mine" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Chief" }));
    fireEvent.click(
      within(screen.getByRole("alertdialog")).getByRole("button", { name: "Cancel" }),
    );
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect((screen.getByRole("textbox", { name: "Name" }) as HTMLInputElement).value).toBe("Mine");
    fireEvent.click(screen.getByRole("button", { name: "Chief" }));
    fireEvent.click(
      within(screen.getByRole("alertdialog")).getByRole("button", { name: "Discard" }),
    );
    expect((screen.getByRole("textbox", { name: "Name" }) as HTMLInputElement).value).toBe("Chief");
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), {
      target: { value: "Mine" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    fireEvent.click(
      within(screen.getByRole("alertdialog")).getByRole("button", { name: "Discard" }),
    );
    await settle();
    expect(screen.queryByRole("group", { name: "Start from a seat" })).toBeNull();
  });

  it("asks before replacing a dirty document with New profile, and cancel keeps the edit", async () => {
    render(<ProfilesView api={api()} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), {
      target: { value: "Changed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "New profile" }));
    fireEvent.click(
      within(screen.getByRole("alertdialog")).getByRole("button", { name: "Cancel" }),
    );
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect((screen.getByRole("textbox", { name: "Name" }) as HTMLInputElement).value).toBe(
      "Changed",
    );
    expect(screen.queryByRole("heading", { name: "New profile" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "New profile" }));
    fireEvent.click(
      within(screen.getByRole("alertdialog")).getByRole("button", { name: "Discard" }),
    );
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(screen.getByRole("group", { name: "Start from a seat" })).toBeTruthy();
  });

  it("skips the evidence gate for a non-claude runtime, and shows its own blocker", async () => {
    const capabilities = vi.fn<ProfileLaunchApi["capabilities"]>();
    const models = vi.fn<ProfileLaunchApi["models"]>();
    const blocked: BlockedProfilePreview = {
      launchable: false,
      fields: [],
      blockers: ["runtime.provider"],
      profile_id: A,
      profile_revision: 1,
    };
    const prepare = vi.fn<ProfileLaunchApi["prepare"]>(async () => blocked);
    const get = vi.fn<ProfileApi["get"]>(async (id) => ({
      ...definition(id),
      document: { ...document(), runtime: { ...document().runtime, provider: "codex" } },
    }));
    render(
      <ProfilesView
        api={api({ get })}
        launchApi={launchApi({ capabilities, models, prepare })}
        workspaceId={WORKSPACE}
      />,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    await settle();
    expect(capabilities).not.toHaveBeenCalled();
    expect(models).not.toHaveBeenCalled();
    expect(
      (screen.getByRole("button", { name: "Start a chat" }) as HTMLButtonElement).disabled,
    ).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Start a chat" }));
    await settle();
    expect(screen.getByText(problemText("runtime.provider"))).toBeTruthy();
  });

  it("holds the title, intro and toolbar in one page head", async () => {
    const { container } = render(<ProfilesView api={api()} />);
    await settle();
    const head = container.querySelector("header.page-head");
    expect(head?.querySelector("h1.page-title")?.textContent).toBe("Profiles");
    expect(head?.querySelector("p.page-intro")?.getAttribute("role")).toBe("note");
    const actions = head?.querySelector("div.page-actions.profiles-toolbar");
    expect(
      within(actions as HTMLElement).getByRole("checkbox", { name: "Show archived" }),
    ).toBeTruthy();
    expect(
      within(actions as HTMLElement).getByRole("button", { name: "New profile" }),
    ).toBeTruthy();
  });

  it("focuses its heading on entry", async () => {
    render(<ProfilesView api={api()} />);
    await settle();
    const heading = screen.getByRole("heading", { level: 1 });
    // `document` in this file is the profile fixture, so the page is reached through the node.
    expect(heading.ownerDocument.activeElement).toBe(heading);
    expect(heading.getAttribute("tabindex")).toBe("-1");
  });

  it("renders set unsupported groups with their value, reason and Clear", async () => {
    render(<ProfilesView api={api()} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();

    const unsupported = screen.getByRole("region", { name: "Cannot apply in a chat" });
    const groups = ["Context items", "Native assets", "Denied tools", "Limits"];
    for (const label of groups) {
      const group = within(unsupported).getByRole("group", { name: label });
      expect(group.textContent).toContain("Not evaluated yet");
    }
    for (const label of ["Required tools", "Memory"]) {
      expect(within(unsupported).queryByRole("group", { name: label })).toBeNull();
    }
    expect(within(unsupported).getByText(/Bash/)).toBeTruthy();
    expect(
      (
        within(unsupported)
          .getByRole("group", { name: "Denied tools" })
          .querySelector("button") as HTMLButtonElement
      ).disabled,
    ).toBe(false);
    expect(within(unsupported).queryByRole("group", { name: "Required tools" })).toBeNull();
  });

  it("shows the unenforceable-restriction note on required and denied tool rows", async () => {
    const value = document();
    value.tools.required = ["Read"];
    render(<ProfilesView api={api({ get: vi.fn(async () => definitionWith(value)) })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    const unsupported = screen.getByRole("region", { name: "Cannot apply in a chat" });
    for (const label of ["Required tools", "Denied tools"]) {
      expect(within(unsupported).getByRole("group", { name: label }).textContent).toContain(
        "VIKI cannot enforce this, so it blocks launch.",
      );
    }
    expect(screen.getByRole("group", { name: "MCP servers" }).textContent).not.toContain(
      "VIKI cannot enforce this, so it blocks launch.",
    );
  });

  it("keeps the vault allowlist in What it can use", async () => {
    render(<ProfilesView api={api()} workspaces={[vault(WORKSPACE, "Main")]} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    const whatItCanUse = screen.getByRole("group", { name: "What it can use" });
    expect(within(whatItCanUse).getByRole("group", { name: "Vaults" })).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Execution fields" })).toBeNull();
  });

  it("associates the Vaults and MCP group titles and hints without orphan labels", async () => {
    const { container } = render(
      <ProfilesView api={api()} workspaces={[vault(WORKSPACE, "Main")]} />,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    for (const [name, hint] of [
      ["Vaults", "Choose which registered vaults this profile may use."],
      ["MCP servers", "These servers are saved with the profile."],
    ] as const) {
      const group = screen.getByRole("group", { name });
      const title = group.parentElement?.querySelector(".field-label");
      const description = screen.getByText(hint);
      expect(group.tagName).toBe("FIELDSET");
      expect(title?.textContent).toBe(name);
      expect(group.getAttribute("aria-labelledby")).toBe(title?.id);
      expect(group.getAttribute("aria-describedby")).toBe(description.id);
      expect(title?.tagName).not.toBe("LABEL");
      expect(group.querySelectorAll("fieldset")).toHaveLength(0);
    }
    for (const label of container.querySelectorAll('label[for]:not([for=""])')) {
      expect((label as HTMLLabelElement).control).not.toBeNull();
    }
    const ids = [...container.querySelectorAll("[id]")].map((element) => element.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("opens Advanced before comparing revisions", async () => {
    const compare = vi.fn<ProfileApi["compare"]>(async (id, left, right) => ({
      profile_id: id,
      left,
      right,
      changes: [],
    }));
    render(<ProfilesView api={api({ compare })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    fireEvent.click(screen.getByText("Advanced"));
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    await settle();
    expect(compare).toHaveBeenCalledOnce();
  });

  it("keeps revision and state revision values in Advanced", async () => {
    render(<ProfilesView api={api()} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    fireEvent.click(screen.getByText("Advanced"));
    expect(screen.getByRole("region", { name: "Revision" }).textContent).toContain(
      "Selected 1; current 1; state 1",
    );
  });

  it("shows loading and provider failure before Technical evidence opens", async () => {
    const pendingCapabilities = vi.fn<ProfileLaunchApi["capabilities"]>(
      () => new Promise(() => {}),
    );
    const pending = render(
      <ProfilesView
        api={api()}
        launchApi={launchApi({ capabilities: pendingCapabilities })}
        workspaceId={WORKSPACE}
      />,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    const review = screen.getByRole("region", { name: "Try it in a chat" });
    const loading = screen.getByText("Loading provider evidence…");
    expect(loading.getAttribute("role")).toBe("status");
    expect(loading.closest("details")).toBeNull();
    expect((review.querySelector("details") as HTMLDetailsElement).open).toBe(false);
    pending.unmount();

    const failed = launchApi({
      capabilities: vi
        .fn<ProfileLaunchApi["capabilities"]>()
        .mockRejectedValue(new ProfileLaunchApiError(503, "provider_unavailable")),
    });
    render(<ProfilesView api={api()} launchApi={failed} workspaceId={WORKSPACE} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    await settle();
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toBe("The provider is unavailable.");
    expect(alert.closest("details")).toBeNull();
    expect(
      (
        screen
          .getByRole("region", { name: "Try it in a chat" })
          .querySelector("details") as HTMLDetailsElement
      ).open,
    ).toBe(false);
  });

  it("shows unavailable, workspace, dirty and archived start reasons outside the disclosure", async () => {
    const unavailable = render(<ProfilesView api={api()} workspaceId={WORKSPACE} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    expect(screen.getByText("Try it in a chat is unavailable.").closest("details")).toBeNull();
    unavailable.unmount();

    render(<ProfilesView api={api()} launchApi={launchApi()} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    expect(
      screen.getByText("Select an active workspace before starting a chat.").closest("details"),
    ).toBeNull();
    cleanup();

    render(<ProfilesView api={api()} launchApi={launchApi()} workspaceId={WORKSPACE} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), {
      target: { value: "Changed" },
    });
    expect(
      screen.getByText("Save this revision before starting a chat.").closest("details"),
    ).toBeNull();
    cleanup();

    render(
      <ProfilesView
        api={api({ get: vi.fn(async (id) => ({ ...definition(id), archived: true })) })}
        launchApi={launchApi()}
        workspaceId={WORKSPACE}
      />,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    const archived = screen.getByText("Archived profiles cannot start a chat.");
    expect(archived.closest("details")).toBeNull();
    expect(
      (
        screen
          .getByRole("region", { name: "Try it in a chat" })
          .querySelector("details") as HTMLDetailsElement
      ).open,
    ).toBe(false);
  });

  it.each([
    [
      "runtime.effort",
      "effort_unsupported",
      null,
      "high",
      "This field cannot be used: Claude Code does not support this effort setting.",
    ],
    [
      "runtime.permission_mode",
      null,
      "plan",
      "plan",
      "The saved permission mode (plan) is unavailable.",
    ],
  ])(
    "explains blocked %s before Technical evidence opens",
    async (field, reason, permissionMode, effectiveValue, message) => {
      const blocked: BlockedProfilePreview = {
        launchable: false,
        fields: [
          {
            field: field as string,
            preview_class: "unsupported",
            status: "unsupported",
            effective_value: effectiveValue,
            source: "profile",
            reason: reason as string | null,
          },
        ],
        blockers: [field as string],
        profile_id: A,
        profile_revision: 1,
      };
      const value = document();
      value.runtime.permission_mode = permissionMode as "plan" | null;
      render(
        <ProfilesView
          api={api({ get: vi.fn(async () => definitionWith(value)) })}
          launchApi={launchApi({ prepare: vi.fn(async () => blocked) })}
          workspaceId={WORKSPACE}
        />,
      );
      await settle();
      fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
      await settle();
      await settle();
      fireEvent.click(screen.getByRole("button", { name: "Start a chat" }));
      await settle();
      const blocker = screen.getByText(message as string);
      expect(blocker.closest("details")).toBeNull();
      expect(
        (
          screen
            .getByRole("region", { name: "Try it in a chat" })
            .querySelector("details") as HTMLDetailsElement
        ).open,
      ).toBe(false);
    },
  );

  it("toggles a vault id into and out of the allowlist", async () => {
    const other = "00000000-0000-4000-8000-000000000004";
    render(
      <ProfilesView api={api()} workspaces={[vault(WORKSPACE, "Main"), vault(other, "Side")]} />,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    const group = screen.getByRole("group", { name: "Vaults" });
    const mainBox = within(group).getByRole("checkbox", { name: "Main" }) as HTMLInputElement;
    const sideBox = within(group).getByRole("checkbox", { name: "Side" }) as HTMLInputElement;
    expect(mainBox.checked).toBe(true);
    expect(sideBox.checked).toBe(false);
    fireEvent.click(sideBox);
    expect(sideBox.checked).toBe(true);
    fireEvent.click(mainBox);
    expect(mainBox.checked).toBe(false);
  });

  it("lets an unknown vault id be unchecked", async () => {
    render(<ProfilesView api={api()} workspaces={[]} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    const group = screen.getByRole("group", { name: "Vaults" });
    const unknownBox = within(group).getByRole("checkbox", {
      name: `Unknown workspace (${WORKSPACE.slice(0, 8)})`,
    }) as HTMLInputElement;
    expect(unknownBox.checked).toBe(true);
    fireEvent.click(unknownBox);
    expect(screen.queryByRole("checkbox", { name: /Unknown workspace/ })).toBeNull();
  });

  it("lets an id naming a listed but non-persistent vault be unchecked", async () => {
    render(<ProfilesView api={api()} workspaces={[vault(WORKSPACE, "Scratch", false)]} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    const group = screen.getByRole("group", { name: "Vaults" });
    const unknownBox = within(group).getByRole("checkbox", {
      name: `Unknown workspace (${WORKSPACE.slice(0, 8)})`,
    }) as HTMLInputElement;
    expect(unknownBox.checked).toBe(true);
    fireEvent.click(unknownBox);
    expect(screen.queryByRole("checkbox", { name: /Unknown workspace/ })).toBeNull();
  });

  it("sends the MCP servers entered in the editor on save", async () => {
    const revise = vi.fn<ProfileApi["revise"]>(async (id, _command, revision) => ({
      profile_id: id,
      revision: revision + 1,
      state_revision: 1,
    }));
    render(<ProfilesView api={api({ revise })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    const mcpGroup = screen.getByRole("group", { name: "MCP servers" });
    fireEvent.click(within(mcpGroup).getByRole("button", { name: "Add server" }));
    fireEvent.change(within(mcpGroup).getByLabelText("Name"), { target: { value: "echo" } });
    fireEvent.change(within(mcpGroup).getByLabelText("Command"), { target: { value: "npx" } });
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await settle();
    expect(revise.mock.calls[0]?.[3].tools.mcp_configuration).toEqual({
      servers: [
        { name: "echo", transport: "stdio", command: "npx", args: [], url: null, note: null },
      ],
    });
  });

  it("reviews imported MCP entries before saving them in a profile revision", async () => {
    const configuration = {
      servers: [
        {
          name: "echo",
          transport: "stdio",
          command: "example-mcp",
          args: ["a b", ""],
          url: null,
          note: null,
        },
      ],
    };
    const importMcpConfiguration = vi.fn().mockResolvedValue(configuration);
    const revise = vi.fn<ProfileApi["revise"]>(async (id, _command, revision) => ({
      profile_id: id,
      revision: revision + 1,
      state_revision: 1,
    }));
    render(<ProfilesView api={api({ revise, importMcpConfiguration })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    fireEvent.click(screen.getByText("Import MCP JSON"));
    fireEvent.change(screen.getByLabelText("MCP JSON"), {
      target: { value: '{"mcpServers":{"echo":{"command":"example-mcp"}}}' },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add from JSON" }));
    await settle();
    expect(revise).not.toHaveBeenCalled();
    expect((screen.getByLabelText("Command") as HTMLInputElement).value).toBe("example-mcp");
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await settle();
    expect(revise.mock.calls[0]?.[3].tools.mcp_configuration).toEqual(configuration);
  });

  it("shows a 400's field error under the MCP server input that caused it", async () => {
    const revise = vi.fn<ProfileApi["revise"]>().mockRejectedValue(
      new ProfileApiError(400, "invalid_document", {
        field: "tools.mcp_configuration.servers[0].name",
        reason: "invalid_name",
      }),
    );
    render(<ProfilesView api={api({ revise })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    const mcpGroup = screen.getByRole("group", { name: "MCP servers" });
    fireEvent.click(within(mcpGroup).getByRole("button", { name: "Add server" }));
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await settle();
    const name = within(mcpGroup).getByLabelText("Name");
    expect(name.getAttribute("aria-invalid")).toBe("true");
    expect(within(mcpGroup).getByRole("alert").textContent).toBe(
      "Use 1 to 26 lower-case letters, digits, - or _, starting with a letter.",
    );

    fireEvent.change(name, { target: { value: "echo" } });
    expect(within(mcpGroup).queryByRole("alert")).toBeNull();
  });

  it("shows nothing inside the MCP editor for a 400 outside tools.mcp_configuration", async () => {
    const revise = vi.fn<ProfileApi["revise"]>().mockRejectedValue(
      new ProfileApiError(400, "invalid_document", {
        field: "identity.name",
        reason: "invalid_length",
      }),
    );
    render(<ProfilesView api={api({ revise })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    fireEvent.change(screen.getByLabelText("Instructions"), { target: { value: "changed" } });
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await settle();
    expect(screen.getByRole("alert").textContent).toBe("The profile fields are not valid.");
    const mcpGroup = screen.getByRole("group", { name: "MCP servers" });
    expect(within(mcpGroup).queryByRole("alert")).toBeNull();
  });

  it("clears a stale MCP field error on the next failed save", async () => {
    const revise = vi
      .fn<ProfileApi["revise"]>()
      .mockRejectedValueOnce(
        new ProfileApiError(400, "invalid_document", {
          field: "tools.mcp_configuration.servers[0].name",
          reason: "invalid_name",
        }),
      )
      .mockRejectedValueOnce(new ProfileApiError(409, "conflict"));
    render(<ProfilesView api={api({ revise })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    const mcpGroup = screen.getByRole("group", { name: "MCP servers" });
    fireEvent.click(within(mcpGroup).getByRole("button", { name: "Add server" }));
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await settle();
    expect(within(mcpGroup).getByRole("alert")).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await settle();
    expect(within(mcpGroup).queryByRole("alert")).toBeNull();
    const name = within(mcpGroup).getByLabelText("Name");
    expect(name.hasAttribute("aria-invalid")).toBe(false);
  });

  it("clears a stored MCP configuration and saves it as null", async () => {
    const withMcp = (): ProfileDefinition => ({
      ...definition(),
      document: {
        ...document(),
        tools: {
          ...document().tools,
          mcp_configuration: {
            servers: [
              { name: "echo", transport: "stdio", command: "npx", args: [], url: null, note: null },
            ],
          },
        },
      },
    });
    const revise = vi.fn<ProfileApi["revise"]>(async (id, _command, revision) => ({
      profile_id: id,
      revision: revision + 1,
      state_revision: 1,
    }));
    render(<ProfilesView api={api({ get: vi.fn(async () => withMcp()), revise })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    const mcpGroup = screen.getByRole("group", { name: "MCP servers" });
    fireEvent.click(within(mcpGroup).getByRole("button", { name: "Clear" }));
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await settle();
    expect(revise.mock.calls[0]?.[3].tools.mcp_configuration).toBeNull();
  });

  it("shows a blocked start-preview's reasons on their rows", async () => {
    const blocked: BlockedProfilePreview = {
      launchable: false,
      fields: [
        {
          field: "context.items",
          preview_class: "task_input",
          status: "unverified",
          effective_value: [{ kind: "selection", id: "context-1" }],
          source: "profile",
          reason: "context_unresolved",
        },
        {
          field: "memory.mode",
          preview_class: "native_effective",
          status: "unsupported",
          effective_value: "isolated",
          source: "profile",
          reason: "memory_isolation_unproven",
        },
        {
          field: "limits.token_budget",
          preview_class: "unsupported",
          status: "unsupported",
          effective_value: 100,
          source: "profile",
          reason: "token_budget_unproven",
        },
        {
          field: "tools.mcp_configuration",
          preview_class: "unsupported",
          status: "unsupported",
          effective_value: { servers: [] },
          source: "profile",
          reason: "mcp_config_unproven",
        },
      ],
      blockers: ["context.items", "memory.mode", "limits.token_budget"],
      profile_id: A,
      profile_revision: 1,
    };
    const prepare = vi.fn<ProfileLaunchApi["prepare"]>(async () => blocked);
    const profile = document();
    profile.memory.mode = "disabled";
    profile.tools.mcp_configuration = { servers: [] };
    render(
      <ProfilesView
        api={api({ get: vi.fn(async () => definitionWith(profile)) })}
        launchApi={launchApi({ prepare })}
        workspaceId={WORKSPACE}
      />,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Start a chat" }));
    await settle();

    expect(
      within(screen.getByRole("group", { name: "Context items" })).getByText(
        problemText("context.items", { reason: "context_unresolved" }),
      ),
    ).toBeTruthy();
    expect(
      within(screen.getByRole("group", { name: "Memory" })).getByText(
        problemText("memory.mode", { reason: "memory_isolation_unproven" }),
      ),
    ).toBeTruthy();
    expect(
      within(screen.getByRole("group", { name: "Limits" })).getByText(
        problemText("limits.token_budget", { reason: "token_budget_unproven" }),
      ),
    ).toBeTruthy();
    const mcpSummary = within(screen.getByRole("group", { name: "MCP servers" }));
    expect(
      mcpSummary.getByText(
        problemText("tools.mcp_configuration", { reason: "mcp_config_unproven" }),
      ),
    ).toBeTruthy();
    expect(mcpSummary.queryByText(/mcp_config_unproven/)).toBeNull();
    expect(
      within(screen.getByRole("group", { name: "Native assets" })).getByText("Not evaluated yet"),
    ).toBeTruthy();
  });

  it("shows supported MCP preview evidence as available", async () => {
    const preview: BlockedProfilePreview = {
      launchable: false,
      fields: [
        {
          field: "tools.mcp_configuration",
          preview_class: "native_effective",
          status: "supported",
          effective_value: null,
          source: "default",
          reason: null,
        },
      ],
      blockers: ["runtime.effort"],
      profile_id: A,
      profile_revision: 1,
    };
    render(
      <ProfilesView
        api={api()}
        launchApi={launchApi({ prepare: vi.fn(async () => preview) })}
        workspaceId={WORKSPACE}
      />,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "Start a chat" }));
    await settle();
    const mcp = screen.getByRole("group", { name: "MCP servers" });
    expect(mcp.textContent).toContain("Available.");
    expect(mcp.textContent).not.toContain("cannot be used");
    expect(mcp.textContent).not.toContain("not been verified as usable");
  });

  it("sends a revise whose document holds the empty value after Clear then Save revision", async () => {
    const revise = vi.fn<ProfileApi["revise"]>(async (id, _command, revision) => ({
      profile_id: id,
      revision: revision + 1,
      state_revision: revision + 1,
    }));
    render(<ProfilesView api={api({ revise })} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    fireEvent.click(
      within(screen.getByRole("group", { name: "Denied tools" })).getByRole("button", {
        name: "Clear",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    await settle();
    expect(revise).toHaveBeenCalledTimes(1);
    const sent = revise.mock.calls[0]?.[3];
    expect(sent?.tools).toEqual({ required: [], denied: [], mcp_configuration: null });
  });

  it("disables the workspace allowlist while the profile is archived", async () => {
    render(
      <ProfilesView
        api={api({ get: vi.fn(async (id) => ({ ...definition(id), archived: true })) })}
        workspaces={[vault(WORKSPACE, "Main")]}
      />,
    );
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    const group = screen.getByRole("group", { name: "Vaults" });
    const mainBox = within(group).getByRole("checkbox", { name: "Main" }) as HTMLInputElement;
    expect(mainBox.disabled).toBe(true);
  });

  describe("seat starter", () => {
    const chief = TEAM.find((s) => s.seat === "chief") as TeamSeat;
    const chiefStarter = chief.starter as SeatStarter;
    const OLD = "Older Chief instructions";

    function chiefApi(patch: (doc: ProfileDocumentV1) => void = () => {}) {
      const saved = definition(chief.profileId ?? "", "Chief");
      saved.document.identity.purpose = chiefStarter.purpose;
      saved.document.identity.accent = "green";
      saved.document.instructions = OLD;
      patch(saved.document);
      const revise = vi.fn<ProfileApi["revise"]>(async (id, _command, revision) => ({
        profile_id: id,
        revision: revision + 1,
        state_revision: 1,
      }));
      return {
        saved,
        revise,
        profiles: api({
          list: vi.fn(async () => [summary(saved.profile_id, "Chief")]),
          get: vi.fn(async () => saved),
          revise,
        }),
      };
    }

    async function open(profiles: ProfileApi, name = /Chief .* claude/) {
      render(<ProfilesView api={profiles} />);
      await settle();
      fireEvent.click(screen.getByRole("button", { name }));
      await settle();
    }

    it("offers the latest starter for a seat profile with older instructions", async () => {
      const { profiles } = chiefApi();
      await open(profiles);
      const compare = globalThis.document.querySelector(".profile-starter-compare") as HTMLElement;
      expect(compare.textContent).toContain(tf("bots.starter.differs", { name: "Chief" }));
      const details = compare.querySelector("details") as HTMLDetailsElement;
      expect(details.open).toBe(false);
      expect(within(details).getByText(t("bots.starter.compare"))).toBeTruthy();
      expect(
        within(details).queryByRole("heading", { name: t("bots.starter.purpose") }),
      ).toBeNull();
      expect(
        within(details).getByRole("heading", { name: t("bots.starter.instructions") }),
      ).toBeTruthy();
      expect(details.querySelector(".line-diff-del")?.textContent).toContain(OLD);
      expect(details.querySelector(".line-diff-add")).toBeTruthy();
    });

    it("fills only purpose and instructions, and revises after Save revision", async () => {
      const { profiles, revise, saved } = chiefApi((doc) => {
        doc.identity.purpose = "Older purpose";
        doc.identity.name = "Chief of staff";
      });
      await open(profiles);
      fireEvent.click(screen.getByRole("button", { name: t("bots.starter.use") }));
      expect((screen.getByRole("textbox", { name: "Purpose" }) as HTMLTextAreaElement).value).toBe(
        chiefStarter.purpose,
      );
      expect(
        (screen.getByRole("textbox", { name: "Instructions" }) as HTMLTextAreaElement).value,
      ).toBe(chiefStarter.instructions);
      expect(revise).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
      await settle();
      expect(revise).toHaveBeenCalledTimes(1);
      expect(revise.mock.calls[0]?.[3]).toEqual({
        ...saved.document,
        identity: { ...saved.document.identity, purpose: chiefStarter.purpose },
        instructions: chiefStarter.instructions,
      });
    });

    it("disables Use the starter text for an archived profile", async () => {
      const { profiles, saved } = chiefApi();
      profiles.get = vi.fn(async () => ({ ...saved, archived: true }));
      await open(profiles);
      expect(
        (screen.getByRole("button", { name: t("bots.starter.use") }) as HTMLButtonElement).disabled,
      ).toBe(true);
    });

    it("shows nothing for a matching text, a non-seat profile or a new draft", async () => {
      const matching = chiefApi((doc) => {
        doc.instructions = chiefStarter.instructions;
      });
      await open(matching.profiles);
      expect(globalThis.document.querySelector(".profile-starter-compare")).toBeNull();
      cleanup();
      await open(api(), /Alpha .* claude/);
      expect(globalThis.document.querySelector(".profile-starter-compare")).toBeNull();
      cleanup();
      render(<ProfilesView api={api()} />);
      await settle();
      fireEvent.click(screen.getByRole("button", { name: "New profile" }));
      expect(globalThis.document.querySelector(".profile-starter-compare")).toBeNull();
    });

    it("lists a button for every starter", async () => {
      render(<ProfilesView api={api()} />);
      await settle();
      fireEvent.click(screen.getByRole("button", { name: "New profile" }));
      const starters = screen.getByRole("group", { name: "Start from a seat" });
      expect(
        within(starters)
          .getAllByRole("button")
          .map((button) => button.textContent),
      ).toEqual(SEAT_STARTERS.map((starter) => starter.name));
    });

    describe("missing-seats pointer", () => {
      const pointer = () => screen.queryByText(t("bots.config.missingSeats"));
      const seatRows = () => TEAM.map((s) => summary(s.profileId ?? "", s.name));

      async function pointerView(rows: ProfileSummary[], workspaces = [vault(WORKSPACE, "Main")]) {
        render(
          <ProfilesView
            api={api({ list: vi.fn(async () => rows) })}
            bots={{} as BotsApi}
            workspaceId={WORKSPACE}
            workspaces={workspaces}
          />,
        );
        await settle();
      }

      it("shows when a seat has no profile", async () => {
        const dropped = TEAM.find((s) => s.seat === "chief") as TeamSeat;
        await pointerView(seatRows().filter((row) => row.profile_id !== dropped.profileId));
        expect(pointer()?.getAttribute("role")).toBe("note");
      });

      it("hides under Show archived", async () => {
        const dropped = TEAM.find((s) => s.seat === "chief") as TeamSeat;
        await pointerView(seatRows().filter((row) => row.profile_id !== dropped.profileId));
        expect(pointer()).not.toBeNull();
        fireEvent.click(screen.getByRole("checkbox", { name: "Show archived" }));
        await settle();
        expect(pointer()).toBeNull();
      });

      it("hides while the list has not resolved", async () => {
        render(
          <ProfilesView
            api={api({ list: vi.fn(() => new Promise<ProfileSummary[]>(() => {})) })}
            bots={{} as BotsApi}
            workspaceId={WORKSPACE}
            workspaces={[vault(WORKSPACE, "Main")]}
          />,
        );
        await settle();
        expect(pointer()).toBeNull();
      });

      it("hides when every seat has a profile, outside a persistent vault or without bots", async () => {
        await pointerView(seatRows());
        expect(pointer()).toBeNull();
        cleanup();
        await pointerView([summary()], [vault(WORKSPACE, "Main", false)]);
        expect(pointer()).toBeNull();
        cleanup();
        render(
          <ProfilesView
            api={api()}
            workspaceId={WORKSPACE}
            workspaces={[vault(WORKSPACE, "Main")]}
          />,
        );
        await settle();
        expect(pointer()).toBeNull();
      });
    });
  });
});

describe("ProfilesView model connections", () => {
  const CONNECTION_ID = "11111111-1111-4111-8111-111111111111";
  const REF = `viki-llm/${CONNECTION_ID}/vendor/model-x`;
  const NAMED = "Work gateway · vendor/model-x";
  const LIST_ROW = {
    id: CONNECTION_ID,
    model_ref: REF,
    name: "Work gateway",
    model: "vendor/model-x",
    base_url: "https://example.invalid/v1",
    api_mode: "chat_completions",
    has_api_key: true,
    is_default: false,
  };

  function connectionsList(rows: readonly object[] = [LIST_ROW]) {
    return vi.fn(async () => ({ revision: 1, default_model: null, connections: [...rows] }));
  }

  function withConnections(list: ReturnType<typeof connectionsList>, ui: ReactElement) {
    return (
      <ModelConnectionsContext.Provider value={{ list } as never}>
        {ui}
      </ModelConnectionsContext.Provider>
    );
  }

  const savedWith = (model: string | null): ProfileDefinition => {
    const value = document();
    value.runtime.model = model;
    return definitionWith(value);
  };

  const select = (): HTMLSelectElement => screen.getByLabelText("Model") as HTMLSelectElement;

  async function open(): Promise<void> {
    await settle();
    fireEvent.click(screen.getByRole("button", { name: /Alpha .* claude/ }));
    await settle();
    await settle();
  }

  it("offers connections by the Bot settings flag, not by the bots prop", async () => {
    const list = connectionsList();
    const botView = render(
      withConnections(
        list,
        <ProfilesView
          api={api({ get: vi.fn(async () => savedWith("sonnet")) })}
          bots={{} as BotsApi}
          botSettings
          initialSelectedId={A}
        />,
      ),
    );
    await settle();
    await settle();
    expect(Array.from(select().querySelectorAll("optgroup"), (group) => group.label)).toContain(
      "Your connections",
    );
    expect(within(select()).getByRole("option", { name: NAMED })).toBeTruthy();
    expect(list).toHaveBeenCalledTimes(1);
    botView.unmount();

    const agentsList = connectionsList();
    render(
      withConnections(
        agentsList,
        <ProfilesView
          api={api({ get: vi.fn(async () => savedWith("sonnet")) })}
          bots={{} as BotsApi}
          initialSelectedId={A}
        />,
      ),
    );
    await settle();
    await settle();
    expect(select().querySelector("optgroup[label='Your connections']")).toBeNull();
    expect(within(select()).queryByRole("option", { name: NAMED })).toBeNull();
    expect(agentsList).not.toHaveBeenCalled();
  });

  it("names a saved connection in Agents › Profiles overview, field and saved runtime line", async () => {
    const list = connectionsList();
    render(
      withConnections(
        list,
        <ProfilesView
          api={api({ get: vi.fn(async () => savedWith(REF)) })}
          launchApi={launchApi()}
          workspaceId={WORKSPACE}
        />,
      ),
    );
    await open();
    const overview = screen.getByRole("region", { name: "Profile overview" });
    expect(within(overview).getByText(NAMED)).toBeTruthy();
    expect(select().value).toBe(REF);
    expect(select().selectedOptions[0]?.textContent).toBe(NAMED);
    expect(select().querySelector("optgroup[label='Your connections']")).toBeNull();
    expect(screen.getByRole("region", { name: "Try it in a chat" }).textContent).toContain(NAMED);
    expect(window.document.body.textContent).not.toContain(CONNECTION_ID);
    expect(list).toHaveBeenCalledTimes(1);
  });

  it("names a connection from a historical revision while the current draft is native", async () => {
    const list = connectionsList();
    const compare = vi.fn<ProfileApi["compare"]>(async (id, left, right) => ({
      profile_id: id,
      left,
      right,
      changes: [{ field: "runtime.model", before: REF, after: "sonnet" }],
    }));
    render(
      withConnections(
        list,
        <ProfilesView
          api={api({ get: vi.fn(async () => savedWith("sonnet")), compare })}
          launchApi={launchApi()}
          workspaceId={WORKSPACE}
        />,
      ),
    );
    await open();
    expect(list).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("Advanced"));
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    await settle();
    await settle();
    const region = screen.getByRole("region", { name: "Compare revisions" });
    expect(Array.from(region.querySelectorAll("pre"), (node) => node.textContent)).toEqual([
      NAMED,
      "Sonnet",
    ]);
    expect(window.document.body.textContent).not.toContain(CONNECTION_ID);
    expect(list).toHaveBeenCalledTimes(1);
  });

  it("calls a removed saved connection removed only after a successful list", async () => {
    const removed = "Saved connection (removed or unreadable)";
    const view = (list: ReturnType<typeof connectionsList>, botSettings: boolean) =>
      withConnections(
        list,
        <ProfilesView
          api={api({ get: vi.fn(async () => savedWith(REF)) })}
          botSettings={botSettings}
          initialSelectedId={A}
        />,
      );
    const failing = vi.fn(async () => {
      throw new Error("list failed");
    });
    for (const botSettings of [true, false]) {
      const empty = render(view(connectionsList([]), botSettings));
      await settle();
      await settle();
      expect(screen.getByText(removed)).toBeTruthy();
      expect(select().value).toBe(REF);
      expect(select().selectedOptions[0]?.textContent).toBe("vendor/model-x");
      expect(window.document.body.textContent).not.toContain(CONNECTION_ID);
      empty.unmount();

      const failed = render(view(failing as never, botSettings));
      await settle();
      await settle();
      expect(screen.queryByText(removed)).toBeNull();
      expect(select().value).toBe(REF);
      expect(window.document.body.textContent).not.toContain(CONNECTION_ID);
      failed.unmount();
    }
  });

  it("shows the Bot-only refusal when Try it prepares a saved connection in Agents", async () => {
    const blocked: BlockedProfilePreview = {
      launchable: false,
      fields: [
        {
          field: "runtime.model",
          preview_class: "unsupported",
          status: "unsupported",
          effective_value: REF,
          source: "profile",
          reason: "connection_bot_only",
        },
      ],
      blockers: ["runtime.model"],
      profile_id: A,
      profile_revision: 1,
    };
    const onPrepared = vi.fn();
    render(
      withConnections(
        connectionsList(),
        <ProfilesView
          api={api({ get: vi.fn(async () => savedWith(REF)) })}
          launchApi={launchApi({ prepare: vi.fn(async () => blocked) })}
          workspaceId={WORKSPACE}
          onPrepared={onPrepared}
        />,
      ),
    );
    await open();
    fireEvent.click(screen.getByRole("button", { name: "Start a chat" }));
    await settle();
    const refusal = problemText("runtime.model", { reason: "connection_bot_only" });
    expect(refusal).toContain("Only this Bot's own chats, task threads and Bot-thread turns");
    expect(screen.getByText(refusal)).toBeTruthy();
    fireEvent.click(screen.getByText(t("profiles.technicalEvidence")));
    expect(screen.getByRole("region", { name: "Effective profile fields" }).textContent).toContain(
      NAMED,
    );
    expect(onPrepared).not.toHaveBeenCalled();
    expect(window.document.body.textContent).not.toContain(CONNECTION_ID);
  });
});

describe("ProfilesView code folder", () => {
  const BOT = "00000000-0000-4000-8000-0000000000b0";
  const FOLDER = "00000000-0000-4000-8000-0000000000f0";
  const codeFolder = (): WorkspaceJson => ({
    ...vault(FOLDER, "App"),
    kind: "code",
    root: "/code/App",
    bootstrap: "none",
  });
  const botRow = (profile = A): BotRow => ({
    bot_id: BOT,
    profile_id: profile,
    workspace_id: WORKSPACE,
    chat_session_id: B,
    state_revision: 4,
    created_at: NOW,
    updated_at: NOW,
    profile: null,
    workspace: null,
    chat: { profile_revision: null, started: false, archived: false },
    routine_ids: [],
    delivery: { enabled: false, available: false, reason: "off" },
    latest_delivery: null,
    code_folder: null,
  });
  const botsApi = (rows: BotRow[] = [botRow()]) =>
    ({
      list: vi.fn(async () => ({
        bots: rows,
        scheduler: { state: "running" as const, attended: true, paused: false },
      })),
      setCodeFolder: vi.fn(async (_id: string, revision: number, workspace: string | null) => ({
        bot: {
          ...rows[0],
          state_revision: revision + 1,
          code_folder: workspace === null ? null : { workspace_id: workspace, name: "App" },
        },
      })),
    }) as unknown as BotsApi & {
      list: ReturnType<typeof vi.fn>;
      setCodeFolder: ReturnType<typeof vi.fn>;
    };
  const view = (bots: BotsApi, botSettings: boolean, profiles = api()) => (
    <ProfilesView
      api={profiles}
      bots={bots}
      botSettings={botSettings}
      workspaceId={WORKSPACE}
      workspaces={[vault(WORKSPACE, "Main"), codeFolder()]}
      initialSelectedId={A}
    />
  );
  const field = () => screen.queryByLabelText(t("bots.code_folder.title"));

  it("offers the grant in Bot settings for the selected profile's Bot", async () => {
    const bots = botsApi();
    render(view(bots, true));
    await settle();
    await settle();
    const select = field() as HTMLSelectElement;
    expect(select.closest(".profile-detail")).not.toBeNull();
    expect(select.closest("details")?.open).toBe(false);
    expect(Array.from(select.options, (option) => option.textContent)).toEqual([
      t("bots.code_folder.none"),
      "App",
    ]);
    fireEvent.change(select, { target: { value: FOLDER } });
    await settle();
    expect(bots.setCodeFolder.mock.calls[0]?.slice(0, 3)).toEqual([BOT, 4, FOLDER]);
    expect((field() as HTMLSelectElement).value).toBe(FOLDER);
  });

  it("is absent from Agents › Profiles even when the Bots API is supplied", async () => {
    const bots = botsApi();
    render(view(bots, false));
    await settle();
    await settle();
    expect(field()).toBeNull();
    expect(screen.queryByText(t("bots.code_folder.title"))).toBeNull();
    expect(bots.list).not.toHaveBeenCalled();
  });

  it("is absent without a Bots API, a vault or a Bot for this profile", async () => {
    render(
      <ProfilesView
        api={api()}
        botSettings
        workspaceId={WORKSPACE}
        workspaces={[codeFolder()]}
        initialSelectedId={A}
      />,
    );
    await settle();
    await settle();
    expect(field()).toBeNull();
    cleanup();

    const noVault = botsApi();
    render(
      <ProfilesView
        api={api()}
        bots={noVault}
        botSettings
        workspaces={[codeFolder()]}
        initialSelectedId={A}
      />,
    );
    await settle();
    await settle();
    expect(field()).toBeNull();
    expect(noVault.list).not.toHaveBeenCalled();
    cleanup();

    render(view(botsApi([botRow(B)]), true));
    await settle();
    await settle();
    expect(field()).toBeNull();
  });

  it("is absent for a new profile draft", async () => {
    render(view(botsApi(), true));
    await settle();
    await settle();
    expect(field()).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "New profile" }));
    await settle();
    expect(field()).toBeNull();
  });

  it("follows the selected profile's Bot", async () => {
    const bots = botsApi([botRow(A)]);
    render(view(bots, true, api({ list: vi.fn(async () => [summary(A), summary(B, "Beta")]) })));
    await settle();
    await settle();
    expect(field()).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Beta — claude — revision 1" }));
    await settle();
    await settle();
    expect(field()).toBeNull();
  });
});
