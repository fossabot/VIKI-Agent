import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { BotRow, BotsApi } from "../../clients/messaging/botsApi";
import type {
  PreparedProfilePreview,
  ProfileLaunchApi,
} from "../../clients/profiles/profileLaunchApi";
import { replacementRequest } from "./BotMemoryReplacement";
import { BotThreads } from "./BotThreads";

afterEach(cleanup);
const bot = {
  bot_id: "bot",
  profile_id: "profile",
  workspace_id: "vault",
  chat_session_id: "main",
  profile: { revision: 3, archived: false },
} as BotRow;
const thread = {
  session_id: "thread",
  profile_revision: 1,
  title: "<b>Research</b>",
  created_at: "2026-10-14T00:00:00.000Z",
  started: true,
  archived: false,
};
function preview(sessionId: string): PreparedProfilePreview {
  return {
    launchable: true,
    session_id: sessionId,
    profile_id: "profile",
    profile_revision: 3,
    workspace_id: "vault",
    fields: [],
    blockers: [],
    warnings: [],
    prefs: { mode: "default", effort: "high", model: null },
    generation: null,
    workspace_revision: 1,
    policy: "ordinary",
    policy_revision: 1,
    settings_revision: 0,
    runtime: "claude",
    capability_key: "a".repeat(64),
    draft: { key: sessionId, revision: 0, text: "", saved_at: null, attachments: [] },
    bot_memory: { bot_id: bot.bot_id, ref: `bot-memory/${bot.bot_id}`, routine: false },
  };
}
function setup() {
  const api = {
    threads: vi.fn().mockResolvedValue({ bot_id: bot.bot_id, threads: [thread], has_more: false }),
    newChat: vi.fn(),
  } as unknown as BotsApi;
  const launch = {
    prepare: vi
      .fn()
      .mockImplementation(
        async (profile_id, _command, session_id, profile_revision, workspace_id) => ({
          launchable: true,
          session_id,
          profile_id,
          profile_revision,
          workspace_id,
          blockers: [],
          bot_memory: { bot_id: "bot", ref: "bot-memory/bot", routine: false },
        }),
      ),
  } as unknown as ProfileLaunchApi;
  const props = { api, launch, bot, disabled: false, onPrepared: vi.fn(), onOpen: vi.fn() };
  const view = render(<BotThreads {...props} />);
  return { ...props, view };
}

it("opens saved threads as text and prepares a fresh current-revision chat without replacing main", async () => {
  const props = setup();
  await act(async () => {});
  const link = screen.getByRole("button", { name: "Open thread <b>Research</b>" });
  expect(link.querySelector("b")).toBeNull();
  fireEvent.click(link);
  expect(props.onOpen).toHaveBeenCalledWith("thread");
  await act(async () => fireEvent.click(screen.getByRole("button", { name: /New task thread/ })));
  expect(props.launch.prepare).toHaveBeenCalledWith(
    "profile",
    expect.any(String),
    expect.any(String),
    3,
    "vault",
    expect.any(AbortSignal),
    "bot",
  );
  const answer = props.onPrepared.mock.calls[0]?.[0] as PreparedProfilePreview;
  expect(answer.session_id).not.toBe("main");
  expect(props.api.newChat).not.toHaveBeenCalled();
});

it("prepares scoped memory requests without sending or deleting anything", async () => {
  const props = setup();
  props.view.rerender(
    <BotThreads
      {...props}
      bot={{
        ...bot,
        workspace: {
          name: "Vault",
          policy: "ordinary",
          active: true,
        },
      }}
      memoryApi={
        {
          entries: vi.fn().mockResolvedValue({ entries: [], partial: false, partial_reasons: [] }),
        } as never
      }
    />,
  );
  vi.mocked(props.launch.prepare).mockImplementation(
    async (_profile, _command, session_id, profile_revision, workspace_id) => ({
      ...preview(session_id),
      profile_revision,
      workspace_id,
      bot_memory: { bot_id: bot.bot_id, ref: "server-selected-ref", routine: false },
    }),
  );
  await act(async () => {});
  fireEvent.click(screen.getByText("Bot memory"));
  await act(async () =>
    fireEvent.click(screen.getByRole("button", { name: "Ask to forget all…" })),
  );
  expect(props.onPrepared).toHaveBeenCalledWith(
    expect.objectContaining({ profile_id: "profile" }),
    expect.stringContaining(
      "Ask me to confirm the listed files before changing or deleting anything",
    ),
    false,
  );
  expect(screen.queryByRole("button", { name: /set up memory/i })).toBeNull();
  expect(props.onPrepared.mock.calls[0]?.[1]).toContain("server-selected-ref/");
  expect(props.onPrepared.mock.calls[0]?.[1]).not.toContain("bot-memory/bot/");
  expect(props.onOpen).not.toHaveBeenCalled();
  expect(props.api.newChat).not.toHaveBeenCalled();
});

it("does not draft a memory request when the prepared Bot binding is missing or wrong", async () => {
  const props = setup();
  props.view.rerender(
    <BotThreads
      {...props}
      bot={{ ...bot, workspace: { name: "Vault", policy: "ordinary", active: true } }}
      memoryApi={{ entries: vi.fn() } as never}
    />,
  );
  vi.mocked(props.launch.prepare).mockImplementation(async (_profile, _command, sessionId) => ({
    ...preview(sessionId),
    bot_memory: { bot_id: "other", ref: "bot-memory/other", routine: false },
  }));
  await act(async () => {});
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Ask to review" })));
  expect(
    screen
      .getAllByRole("alert")
      .some((alert) => alert.textContent?.includes("could not be added to the draft")),
  ).toBe(true);
  expect(screen.queryByText(/inherited memory settings/)).toBeNull();
  expect(props.onPrepared).not.toHaveBeenCalled();
});

it("reads app-kept memory and drafts its absolute path for a workspace that is not ordinary", async () => {
  const props = setup();
  const entries = vi.fn().mockResolvedValue({ entries: [], partial: false, partial_reasons: [] });
  props.view.rerender(
    <BotThreads
      {...props}
      bot={{
        ...bot,
        bot_id: BOT,
        workspace_id: WORKSPACE,
        workspace: { name: "Vault", policy: "protected", active: true },
      }}
      memoryApi={{ entries } as never}
    />,
  );
  vi.mocked(props.launch.prepare).mockImplementation(async (_profile, _command, sessionId) => ({
    ...preview(sessionId),
    workspace_id: WORKSPACE,
    bot_memory: { bot_id: BOT, path: `/data/bot-memory/${WORKSPACE}/${BOT}`, routine: false },
  }));
  await act(async () => {});
  expect(
    screen.getByText("Kept on this computer in VIKI Agent's data folder, outside the vault."),
  ).toBeTruthy();
  expect(screen.queryByText(/inherited memory settings/)).toBeNull();
  expect(entries).toHaveBeenCalled();
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Ask to review" })));
  expect(props.onPrepared.mock.calls[0]?.[1]).toContain(`/data/bot-memory/${WORKSPACE}/${BOT}`);

  props.view.rerender(
    <BotThreads
      {...props}
      bot={{ ...bot, workspace: { name: "Vault", policy: "ordinary", active: false } }}
    />,
  );
  await act(async () => {});
  expect(screen.getByText("Open this Bot's vault to view its memory.")).toBeTruthy();
});

const WORKSPACE = "11111111-1111-4111-8111-111111111111";
const BOT = "22222222-2222-4222-8222-222222222222";
const MEMORY_REF = `bot-memory/${BOT}/MEMORY.md`;

function editable(onOpenInEditor?: (ref: string) => void) {
  const props = setup();
  const uuidBot = {
    ...bot,
    bot_id: BOT,
    workspace_id: WORKSPACE,
    workspace: { name: "Vault", policy: "ordinary" as const, active: true },
  } satisfies BotRow;
  const memoryApi = {
    entries: vi.fn(async () => ({
      entries: [{ ref: MEMORY_REF, title: "MEMORY.md", modified: 1 }],
      partial: false,
      partial_reasons: [],
      omitted: 0,
    })),
    note: vi.fn(async () => ({
      note: {
        ref: MEMORY_REF,
        title: "MEMORY.md",
        text: "- tea\n",
        source: "- tea\n",
        byte_size: 6,
        sha256: "c".repeat(64),
        frontmatter: {},
        frontmatter_error: null,
      },
      resolved_links: [],
    })),
  };
  vi.mocked(props.launch.prepare).mockImplementation(async (_profile, _command, sessionId) => ({
    ...preview(sessionId),
    workspace_id: WORKSPACE,
    bot_memory: { bot_id: BOT, ref: `bot-memory/${BOT}`, routine: false },
  }));
  props.view.rerender(
    <BotThreads
      {...props}
      bot={uuidBot}
      memoryApi={memoryApi as never}
      {...(onOpenInEditor ? { onOpenInEditor } : {})}
    />,
  );
  return { props, memoryApi };
}

it("saves an edit of a memory file as an unsent exact-replacement request in a new task thread", async () => {
  const { props } = editable();
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "View memory" }));
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Edit" }));
  fireEvent.change(screen.getByRole("textbox", { name: "New text of this file" }), {
    target: { value: "- coffee\n" },
  });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save" })));

  expect(props.launch.prepare).toHaveBeenCalledTimes(1);
  expect(props.onPrepared).toHaveBeenCalledExactlyOnceWith(
    expect.objectContaining({ profile_id: "profile" }),
    replacementRequest(MEMORY_REF, "c".repeat(64), "- coffee\n"),
    // The user's own text is placed before the page is left, so a refusal keeps it here.
    true,
  );
  expect(props.onOpen).not.toHaveBeenCalled();
});

async function saveEdit(props: ReturnType<typeof editable>["props"], text: string) {
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "View memory" }));
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Edit" }));
  fireEvent.change(screen.getByRole("textbox", { name: "New text of this file" }), {
    target: { value: text },
  });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save" })));
  return props;
}

it("claims a prepared draft only when the page placed it", async () => {
  const { props } = editable();
  props.onPrepared.mockResolvedValue(true);
  await saveEdit(props, "- coffee");
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByText(/unsent draft for/)).toBeTruthy();
  expect(within(dialog).queryByRole("textbox")).toBeNull();
});

it("keeps the edit and says nothing changed when the page did not place the draft", async () => {
  const { props } = editable();
  props.onPrepared.mockResolvedValue(false);
  await saveEdit(props, "- coffee");
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByText(/Nothing was changed/)).toBeTruthy();
  expect(within(dialog).queryByText(/unsent draft for/)).toBeNull();
  expect(
    (within(dialog).getByRole("textbox", { name: "New text of this file" }) as HTMLTextAreaElement)
      .value,
  ).toBe("- coffee");

  // Saving again reuses the same command and session instead of leaving another empty thread.
  props.onPrepared.mockResolvedValue(true);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save" })));
  const [first, second] = vi.mocked(props.launch.prepare).mock.calls;
  expect(second?.[1]).toBe(first?.[1]);
  expect(second?.[2]).toBe(first?.[2]);
  expect(within(dialog).getByText(/unsent draft for/)).toBeTruthy();
});

it("reports a failed edit preparation in the viewer and keeps the edit", async () => {
  const { props } = editable();
  vi.mocked(props.launch.prepare).mockRejectedValue(new Error("offline"));
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "View memory" }));
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Edit" }));
  fireEvent.change(screen.getByRole("textbox", { name: "New text of this file" }), {
    target: { value: "- coffee\n" },
  });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save" })));
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByText(/Nothing was changed/)).toBeTruthy();
  expect(within(dialog).getByRole("textbox")).toBeTruthy();
  expect(props.onPrepared).not.toHaveBeenCalled();
});

it("passes Open in VS Code to the memory dialog", async () => {
  const onOpenInEditor = vi.fn();
  editable(onOpenInEditor);
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "View memory" }));
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Open in VS Code" }));
  expect(onOpenInEditor).toHaveBeenCalledExactlyOnceWith(MEMORY_REF);
});

it("holds one memory request while preparation is still in flight", async () => {
  const props = setup();
  props.view.rerender(
    <BotThreads
      {...props}
      bot={{ ...bot, workspace: { name: "Vault", policy: "ordinary", active: true } }}
      memoryApi={{ entries: vi.fn() } as never}
    />,
  );
  let finish!: (value: PreparedProfilePreview) => void;
  vi.mocked(props.launch.prepare).mockImplementation(
    () => new Promise((resolve) => (finish = resolve)),
  );
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Ask to review" }));
  fireEvent.click(screen.getByRole("button", { name: "Ask to forget all…" }));
  expect(props.launch.prepare).toHaveBeenCalledTimes(1);
  const call = vi.mocked(props.launch.prepare).mock.calls[0];
  await act(async () => finish(preview(call?.[2] ?? "late")));
  expect(props.onPrepared).toHaveBeenCalledTimes(1);
});

it("ignores a late memory preparation after the selected Bot changes", async () => {
  const props = setup();
  props.view.rerender(
    <BotThreads
      {...props}
      bot={{ ...bot, workspace: { name: "Vault", policy: "ordinary", active: true } }}
      memoryApi={{ entries: vi.fn() } as never}
    />,
  );
  let finish!: (value: PreparedProfilePreview) => void;
  vi.mocked(props.launch.prepare).mockImplementation(
    () => new Promise((resolve) => (finish = resolve)),
  );
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Ask to review" }));
  const call = vi.mocked(props.launch.prepare).mock.calls[0];
  props.view.rerender(
    <BotThreads
      {...props}
      bot={{ ...bot, bot_id: "other", profile_id: "other-profile" }}
      memoryApi={{ entries: vi.fn() } as never}
    />,
  );
  await act(async () => finish(preview(call?.[2] ?? "late")));
  expect(call?.[5]?.aborted).toBe(true);
  expect(props.onPrepared).not.toHaveBeenCalled();
});

it("aborts pending memory preparation when memory eligibility disappears", async () => {
  const props = setup();
  const memoryApi = { entries: vi.fn() } as never;
  const ordinaryBot = {
    ...bot,
    workspace: { name: "Vault", policy: "ordinary" as const, active: true },
  } satisfies BotRow;
  props.view.rerender(<BotThreads {...props} bot={ordinaryBot} memoryApi={memoryApi} />);
  let finish!: (value: PreparedProfilePreview) => void;
  vi.mocked(props.launch.prepare).mockImplementation(
    () => new Promise((resolve) => (finish = resolve)),
  );
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Ask to review" }));
  const call = vi.mocked(props.launch.prepare).mock.calls[0];
  props.view.rerender(<BotThreads {...props} bot={ordinaryBot} disabled />);
  await act(async () => {});
  await act(async () => finish(preview(call?.[2] ?? "late")));
  expect(call?.[5]?.aborted).toBe(true);
  expect(props.onPrepared).not.toHaveBeenCalled();
});

it("preserves command and session ids across an explicit retry of uncertain preparation", async () => {
  const props = setup();
  vi.mocked(props.launch.prepare).mockRejectedValue(new Error("disconnected"));
  await act(async () => {});
  await act(async () => fireEvent.click(screen.getByRole("button", { name: /New task thread/ })));
  expect(screen.getByRole("alert").textContent).toContain("could not be prepared");
  await act(async () => fireEvent.click(screen.getByRole("button", { name: /New task thread/ })));
  const calls = vi.mocked(props.launch.prepare).mock.calls;
  expect(calls[1]?.slice(0, 5)).toEqual(calls[0]?.slice(0, 5));
  expect(props.onPrepared).not.toHaveBeenCalled();
});

it("ignores a late preparation after the selected Bot changes", async () => {
  const props = setup();
  let finish!: (value: PreparedProfilePreview) => void;
  vi.mocked(props.launch.prepare).mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: /New task thread/ }));
  const call = vi.mocked(props.launch.prepare).mock.calls[0];
  props.view.rerender(
    <BotThreads {...props} bot={{ ...bot, bot_id: "other", profile_id: "other-profile" }} />,
  );
  await act(async () => finish(preview(call?.[2] ?? "late-thread")));
  expect(call?.[5]?.aborted).toBe(true);
  expect(props.onPrepared).not.toHaveBeenCalled();
});

it("explains blocked launch codes in plain words", async () => {
  const props = setup();
  const codes = [
    "transcript_missing",
    "profile_archived",
    "workspace_restart_required",
    "workspace_unavailable",
    "profile_blocked",
    "launch_blocked",
    "launch_changed",
    "capability_changed",
    "app_closing",
    "provider_unavailable",
    "profile_missing",
    "workspace_missing",
    "session_exists",
    "corrupt",
    "mcp_revoked",
    "not_found",
    "newer_version",
    "protection",
    "workspace_not_allowed",
    "connection_bot_only",
    "connection_missing",
    "connections_unavailable",
    "connection_invalid",
  ];
  for (const code of codes) {
    vi.mocked(props.launch.prepare).mockResolvedValue({
      launchable: false,
      profile_id: "profile",
      profile_revision: 3,
      fields: [],
      blockers: [code],
    });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: /New task thread/ })));
    const alert = screen.getByRole("alert").textContent ?? "";
    expect(alert).not.toBe(code);
    expect(alert).not.toContain(`(${code})`);
  }
});

it("explains a removed model connection in plain words, not its code", async () => {
  const props = setup();
  vi.mocked(props.launch.prepare).mockResolvedValue({
    launchable: false,
    profile_id: "profile",
    profile_revision: 3,
    fields: [],
    blockers: ["connection_missing", "connections_unavailable"],
  });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: /New task thread/ })));
  const alert = screen.getByRole("alert").textContent ?? "";
  expect(alert).toContain("This Bot's saved model connection was removed.");
  expect(alert).toContain("Your model connections could not be read, so this Bot cannot start.");
  expect(alert).not.toContain("connection_missing");
  expect(alert).not.toContain("viki-llm/");
});

it("uses blocked preview field reasons for workspace and MCP errors", async () => {
  const props = setup();
  vi.mocked(props.launch.prepare).mockResolvedValue({
    launchable: false,
    profile_id: "profile",
    profile_revision: 3,
    fields: [
      {
        field: "workspace.allowed_workspace_ids",
        preview_class: "unsupported",
        status: "unsupported",
        effective_value: null,
        source: "profile",
        reason: "workspace_not_allowed",
      },
      {
        field: "tools.mcp_configuration",
        preview_class: "unsupported",
        status: "unsupported",
        effective_value: null,
        source: "profile",
        reason: "mcp_policy_blocked",
      },
    ],
    blockers: ["workspace.allowed_workspace_ids", "tools.mcp_configuration"],
  });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: /New task thread/ })));
  const alert = screen.getByRole("alert").textContent ?? "";
  expect(alert).toContain("This workspace is not allowed by the profile.");
  expect(alert).toContain("This profile's MCP servers cannot run in this workspace.");
  expect(alert).not.toContain("(workspace_not_allowed)");
  expect(alert).not.toContain("(mcp_policy_blocked)");
});

it("keeps blocked and archived threads from being opened", async () => {
  const props = setup();
  vi.mocked(props.launch.prepare).mockResolvedValue({
    launchable: false,
    profile_id: "profile",
    profile_revision: 3,
    fields: [],
    blockers: ["workspace_not_allowed"],
  });
  if (!props.api.threads) throw new Error("Threads API missing");
  vi.mocked(props.api.threads).mockResolvedValue({
    bot_id: "bot",
    threads: [{ ...thread, archived: true }],
    has_more: true,
  });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Refresh threads" })));
  expect(screen.queryByRole("button", { name: /Open thread/ })).toBeNull();
  fireEvent.click(screen.getByRole("checkbox", { name: "Show archived threads" }));
  expect(screen.getByRole<HTMLButtonElement>("button", { name: /Open thread/ }).disabled).toBe(
    true,
  );
  await act(async () => fireEvent.click(screen.getByRole("button", { name: /New task thread/ })));
  expect(screen.getByRole("alert").textContent).toContain(
    "This workspace is not allowed by the profile.",
  );
  expect(props.onPrepared).not.toHaveBeenCalled();
  expect(screen.getByText(/Showing the newest 50/)).toBeTruthy();
});
