import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { WorkspaceJson } from "../../clients/app/shellApi";
import { type BotRow, type BotsApi, BotsApiError } from "../../clients/messaging/botsApi";
import { t } from "../../i18n/t";
import { BotCodeFolder } from "./BotCodeFolder";

const BOT = "00000000-0000-4000-8000-000000000001";
const OTHER_BOT = "00000000-0000-4000-8000-00000000000b";
const PROFILE = "00000000-0000-4000-8000-000000000002";
const OTHER_PROFILE = "00000000-0000-4000-8000-00000000000c";
const VAULT = "00000000-0000-4000-8000-000000000003";
const APP = "00000000-0000-4000-8000-000000000004";
const LIB = "00000000-0000-4000-8000-000000000005";
const STAMP = "2026-11-12T00:00:00.000Z";

const row = (overrides: Partial<BotRow> = {}): BotRow => ({
  bot_id: BOT,
  profile_id: PROFILE,
  workspace_id: VAULT,
  chat_session_id: "00000000-0000-4000-8000-000000000006",
  state_revision: 1,
  created_at: STAMP,
  updated_at: STAMP,
  profile: null,
  workspace: null,
  chat: { profile_revision: null, started: false, archived: false },
  routine_ids: [],
  delivery: { enabled: false, available: false, reason: "off" },
  latest_delivery: null,
  code_folder: null,
  ...overrides,
});

const folder = (
  workspace_id: string,
  name: string,
  patch: Partial<WorkspaceJson> = {},
): WorkspaceJson => ({
  workspace_id,
  kind: "code",
  root: `/code/${name}`,
  root_available: true,
  aliased: false,
  name,
  policy: "ordinary",
  policy_revision: 1,
  revision: 1,
  source: "explicit",
  layout: "flat",
  portable_id: null,
  identity: { established: true, git_root_oids: 1, inherits_from: null, probed_at: STAMP },
  face_dir: null,
  bootstrap: "none",
  persistent: true,
  created_at: STAMP,
  updated_at: STAMP,
  ...patch,
});

const FOLDERS = [folder(APP, "App"), folder(LIB, "Library")];

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((ok, fail) => {
    resolve = ok;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function fake(overrides: Partial<BotsApi> = {}, rows: BotRow[] = [row()]) {
  return {
    list: vi.fn(async () => ({
      bots: rows,
      scheduler: { state: "running" as const, attended: true, paused: false },
    })),
    setCodeFolder: vi.fn(async (botId: string, revision: number, workspaceId: string | null) => ({
      bot: row({
        bot_id: botId,
        state_revision: revision + 1,
        code_folder:
          workspaceId === null
            ? null
            : { workspace_id: workspaceId, name: workspaceId === APP ? "App" : "Library" },
      }),
    })),
    ...overrides,
  } as unknown as BotsApi & {
    list: ReturnType<typeof vi.fn>;
    setCodeFolder: ReturnType<typeof vi.fn>;
  };
}

type Props = Parameters<typeof BotCodeFolder>[0];
const view = (api: BotsApi, props: Partial<Props> = {}) => (
  <BotCodeFolder
    api={api}
    profileId={PROFILE}
    workspaceId={VAULT}
    folders={FOLDERS}
    disabled={false}
    {...props}
  />
);

async function settle(): Promise<void> {
  await act(async () => Promise.resolve());
  await act(async () => Promise.resolve());
}

const select = (): HTMLSelectElement => screen.getByLabelText("Code folder") as HTMLSelectElement;
const choose = (value: string): void => {
  fireEvent.change(select(), { target: { value } });
};

afterEach(() => {
  cleanup();
});

describe("BotCodeFolder", () => {
  it("shows nothing without a loaded Bot row or a grant route", async () => {
    const api = fake({}, [row({ profile_id: OTHER_PROFILE, bot_id: OTHER_BOT })]);
    const first = render(view(api));
    await settle();
    expect(first.container.textContent).toBe("");
    first.unmount();

    const failing = fake({ list: vi.fn(async () => Promise.reject(new Error("down"))) });
    const second = render(view(failing));
    await settle();
    expect(second.container.textContent).toBe("");
    second.unmount();

    const old = { list: vi.fn() } as unknown as BotsApi;
    const third = render(view(old));
    await settle();
    expect(third.container.textContent).toBe("");
    expect(old.list).not.toHaveBeenCalled();
  });

  it("matches the Bot by profile and workspace, not by position", async () => {
    const api = fake({}, [
      row({ bot_id: OTHER_BOT, profile_id: OTHER_PROFILE }),
      row({ code_folder: { workspace_id: APP, name: "App" } }),
    ]);
    render(view(api));
    await settle();
    expect(select().value).toBe(APP);
  });

  it("starts closed with the label, hint and permission sentence on the control", async () => {
    render(view(fake()));
    await settle();
    const details = screen.getByText(t("bots.code_folder.title"), { selector: "summary" })
      .parentElement as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(
      within(details).getByText(t("bots.code_folder.none"), { selector: "span" }),
    ).toBeTruthy();
    const described = select().getAttribute("aria-describedby")?.split(" ") ?? [];
    expect(described.map((id) => window.document.getElementById(id)?.textContent)).toEqual([
      t("bots.code_folder.hint"),
      t("bots.code_folder.permissions"),
    ]);
    expect(select().value).toBe("");
  });

  it("offers None and persistent ordinary code folders only, as text", async () => {
    const hostile = "<img src=x onerror=alert(1)>";
    render(
      view(fake(), {
        folders: [
          folder(APP, hostile),
          folder(LIB, "Library"),
          folder("00000000-0000-4000-8000-000000000007", "Untrusted", {
            policy: "existing_readonly",
          }),
          folder("00000000-0000-4000-8000-000000000008", "Protected", { policy: "protected" }),
          folder("00000000-0000-4000-8000-000000000009", "Unsaved", { persistent: false }),
          { ...folder("00000000-0000-4000-8000-00000000000a", "Vault"), kind: "vault" },
        ],
      }),
    );
    await settle();
    expect(Array.from(select().options, (option) => option.textContent)).toEqual([
      t("bots.code_folder.none"),
      hostile,
      "Library",
    ]);
    expect(select().querySelector("img")).toBeNull();
  });

  it("keeps a granted folder that is no longer offered as the selected choice", async () => {
    render(
      view(fake({}, [row({ code_folder: { workspace_id: APP, name: "Gone" } })]), {
        folders: [folder(LIB, "Library")],
      }),
    );
    await settle();
    expect(select().value).toBe(APP);
    expect(select().selectedOptions[0]?.textContent).toBe("Gone");
  });

  it("grants a folder with the loaded revision, disables while pending and adopts the answer", async () => {
    const reply = deferred<{ bot: BotRow }>();
    const api = fake({ setCodeFolder: vi.fn(() => reply.promise) });
    render(view(api));
    await settle();
    choose(APP);
    // No confirmation: nothing is granted yet.
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(api.setCodeFolder).toHaveBeenCalledTimes(1);
    expect(api.setCodeFolder.mock.calls[0]?.slice(0, 3)).toEqual([BOT, 1, APP]);
    expect(api.setCodeFolder.mock.calls[0]?.[3]).toBeInstanceOf(AbortSignal);
    expect(select().disabled).toBe(true);
    choose(LIB);
    expect(api.setCodeFolder).toHaveBeenCalledTimes(1);
    await act(async () => {
      reply.resolve({
        bot: row({ state_revision: 2, code_folder: { workspace_id: APP, name: "App" } }),
      });
    });
    expect(select().disabled).toBe(false);
    expect(select().value).toBe(APP);
    expect(screen.queryByRole("alert")).toBeNull();

    // The next change is a switch, so it asks first and then sends the returned revision.
    choose(LIB);
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await settle();
    expect(api.setCodeFolder.mock.calls[1]?.slice(0, 3)).toEqual([BOT, 2, LIB]);
  });

  it("asks before switching, with Cancel focused, and sends nothing when cancelled", async () => {
    const api = fake({}, [row({ code_folder: { workspace_id: APP, name: "App" } })]);
    render(view(api));
    await settle();
    choose(LIB);
    const dialog = screen.getByRole("alertdialog", { name: t("bots.code_folder.change_title") });
    expect(within(dialog).getByText(t("bots.code_folder.change_body"))).toBeTruthy();
    expect(window.document.activeElement).toBe(
      within(dialog).getByRole("button", { name: t("common.button.cancel") }),
    );
    expect(api.setCodeFolder).not.toHaveBeenCalled();
    expect(select().value).toBe(APP);
    fireEvent.click(within(dialog).getByRole("button", { name: t("common.button.cancel") }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(api.setCodeFolder).not.toHaveBeenCalled();
    expect(select().value).toBe(APP);

    choose(LIB);
    fireEvent.keyDown(screen.getByRole("alertdialog"), { key: "Escape" });
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(api.setCodeFolder).not.toHaveBeenCalled();
  });

  it("asks before clearing and then sends null", async () => {
    const api = fake({}, [row({ code_folder: { workspace_id: APP, name: "App" } })]);
    render(view(api));
    await settle();
    choose("");
    expect(api.setCodeFolder).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await settle();
    expect(api.setCodeFolder.mock.calls[0]?.slice(0, 3)).toEqual([BOT, 1, null]);
    expect(select().value).toBe("");
    expect(screen.getByText(t("bots.code_folder.none"), { selector: "span" })).toBeTruthy();
  });

  it("ignores a change to the grant it already has", async () => {
    const api = fake({}, [row({ code_folder: { workspace_id: APP, name: "App" } })]);
    render(view(api));
    await settle();
    choose(APP);
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(api.setCodeFolder).not.toHaveBeenCalled();
  });

  it("states a conflict, re-reads the Bot and keeps the old choice", async () => {
    const api = fake({
      setCodeFolder: vi.fn(async () => {
        throw new BotsApiError(409, "conflict");
      }),
    });
    render(view(api));
    await settle();
    choose(APP);
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(t("bots.code_folder.conflict"));
    expect(api.list).toHaveBeenCalledTimes(2);
    expect(select().value).toBe("");
    expect(select().disabled).toBe(false);
  });

  it.each([
    ["code_folder_needs_ordinary", "bots.code_folder.needs_ordinary"],
    ["code_folder_untrusted", "bots.code_folder.untrusted"],
    ["code_folder_missing", "bots.code_folder.missing"],
    ["code_folder_not_repository", "bots.code_folder.not_repository"],
    ["code_folder_no_commits", "bots.code_folder.no_commits"],
    ["code_folder_git_unavailable", "bots.code_folder.git_unavailable"],
    ["git_settings_changed", "bots.code_folder.git_settings_changed"],
    ["unavailable", "bots.code_folder.failed"],
    ["capacity", "bots.code_folder.failed"],
  ] as const)("maps %s to its fixed sentence", async (code, key) => {
    const api = fake({
      setCodeFolder: vi.fn(async () => {
        throw new BotsApiError(422, code);
      }),
    });
    render(view(api));
    await settle();
    choose(APP);
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(t(key));
    expect(api.list).toHaveBeenCalledTimes(1);
    // Retry clears the previous sentence while the new request runs.
    choose(LIB);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("says it could not save when the answer is not an API error", async () => {
    const api = fake({ setCodeFolder: vi.fn(async () => Promise.reject(new Error("x"))) });
    render(view(api));
    await settle();
    choose(APP);
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(t("bots.code_folder.failed"));
  });

  it("ignores a stale grant answer after the profile changes", async () => {
    const reply = deferred<{ bot: BotRow }>();
    const api = fake({ setCodeFolder: vi.fn(() => reply.promise) });
    const { rerender } = render(view(api));
    await settle();
    choose(APP);
    expect(select().disabled).toBe(true);
    const next = row({ bot_id: OTHER_BOT, profile_id: OTHER_PROFILE });
    api.list.mockResolvedValue({
      bots: [next],
      scheduler: { state: "running", attended: true, paused: false },
    });
    rerender(view(api, { profileId: OTHER_PROFILE }));
    await settle();
    expect(select().disabled).toBe(false);
    expect(select().value).toBe("");
    await act(async () => {
      reply.resolve({
        bot: row({ state_revision: 2, code_folder: { workspace_id: APP, name: "App" } }),
      });
    });
    expect(select().value).toBe("");
    expect(screen.queryByRole("alert")).toBeNull();
    // The next write belongs to the new Bot and its own revision.
    choose(LIB);
    expect(api.setCodeFolder.mock.calls[1]?.slice(0, 3)).toEqual([OTHER_BOT, 1, LIB]);
  });

  it("ignores a stale grant failure after the client changes", async () => {
    const reply = deferred<{ bot: BotRow }>();
    const api = fake({ setCodeFolder: vi.fn(() => reply.promise) });
    const { rerender } = render(view(api));
    await settle();
    choose(APP);
    const replacement = fake();
    rerender(view(replacement));
    await settle();
    await act(async () => {
      reply.reject(new BotsApiError(409, "conflict"));
    });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(api.list).toHaveBeenCalledTimes(1);
    expect(replacement.list).toHaveBeenCalledTimes(1);
  });

  it("drops a stale roster answer that arrives after the profile changed", async () => {
    const scheduler = { state: "running" as const, attended: true, paused: false };
    const first = deferred<{ bots: BotRow[]; scheduler: typeof scheduler }>();
    const api = fake();
    api.list.mockReturnValueOnce(first.promise).mockResolvedValueOnce({
      bots: [row({ bot_id: OTHER_BOT, profile_id: OTHER_PROFILE })],
      scheduler,
    });
    const { rerender } = render(view(api));
    rerender(view(api, { profileId: OTHER_PROFILE }));
    await settle();
    await act(async () => {
      first.resolve({
        bots: [row({ code_folder: { workspace_id: LIB, name: "Library" } })],
        scheduler,
      });
    });
    expect(select().value).toBe("");
    choose(APP);
    expect(api.setCodeFolder.mock.calls[0]?.slice(0, 3)).toEqual([OTHER_BOT, 1, APP]);
  });

  it("closes the confirmation when the Bot scope changes", async () => {
    const api = fake({}, [row({ code_folder: { workspace_id: APP, name: "App" } })]);
    const { rerender } = render(view(api));
    await settle();
    choose(LIB);
    expect(screen.getByRole("alertdialog")).toBeTruthy();
    rerender(view(api, { workspaceId: LIB }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    await settle();
    expect(api.setCodeFolder).not.toHaveBeenCalled();
  });

  it("disables the control when the parent does", async () => {
    render(view(fake(), { disabled: true }));
    await settle();
    expect(select().disabled).toBe(true);
  });
});
