import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ModelConnectionsContext } from "../../clients/app/modelConnectionsApi";
import {
  type PreparedProfilePreview,
  type ProfileLaunchApi,
  ProfileLaunchApiError,
} from "../../clients/profiles/profileLaunchApi";
import { ProfileSessionReview } from "./ProfileSessionReview";

afterEach(() => {
  cleanup();
});
const preview = (
  session_id: string,
  name: string,
  overrides: Partial<PreparedProfilePreview> = {},
): PreparedProfilePreview => ({
  session_id,
  profile_id: "profile",
  profile_revision: 2,
  generation: null,
  fields: [
    {
      field: "identity.name",
      preview_class: "display_only",
      status: "supported",
      effective_value: name,
      source: "profile",
      reason: null,
    },
  ],
  launchable: false,
  blockers: ["capability_changed"],
  warnings: ["capability_changed"],
  prefs: { mode: "plan", effort: "high", model: null },
  workspace_id: "workspace",
  workspace_revision: 1,
  policy: "ordinary",
  policy_revision: 1,
  settings_revision: 0,
  runtime: "claude",
  capability_key: "a".repeat(64),
  draft: { key: session_id, revision: 1, text: "editable", saved_at: null, attachments: [] },
  ...overrides,
});
const api = (inspect: ProfileLaunchApi["inspect"]): ProfileLaunchApi => ({
  inspect,
  prepare: vi.fn(),
  models: vi.fn(),
  capabilities: vi.fn(),
});

it("reloads the bound revision and maps a current launch blocker to the way on", async () => {
  const source = api(vi.fn().mockResolvedValue(preview("a", "Review helper")));
  render(<ProfileSessionReview api={source} sessionId="a" />);
  expect(await screen.findByText(/Profile instructions · Review helper · revision 2/)).toBeTruthy();
  const alert = screen.getByRole("alert").textContent ?? "";
  expect(alert).toContain("capabilities changed");
  expect(alert).toContain("start a new draft from the profile in Agents");
  expect(alert).not.toContain("capability_changed");
  expect(source.prepare).not.toHaveBeenCalled();
});

it("says where a launchable chat's instructions go: the composer, or a Bot's system prompt", async () => {
  const ready = { launchable: true, blockers: [], warnings: [] };
  const plain = api(vi.fn().mockResolvedValue(preview("a", "Review helper", ready)));
  const { unmount } = render(<ProfileSessionReview api={plain} sessionId="a" />);
  expect(await screen.findByText(/editable in the composer/)).toBeTruthy();
  expect(screen.queryByText(/system prompt/)).toBeNull();
  unmount();
  const bot = preview("b", "Review helper", {
    ...ready,
    draft: { key: "b", revision: 0, text: "", saved_at: null, attachments: [] },
    bot_memory: { bot_id: "bot", ref: "bot-memory/bot", routine: false },
  });
  render(<ProfileSessionReview api={api(vi.fn().mockResolvedValue(bot))} sessionId="b" />);
  expect(await screen.findByText(/as its system prompt each time this chat starts/)).toBeTruthy();
  expect(screen.queryByText(/editable in the composer/)).toBeNull();
});

it("renders the panel open and says so in the summary when the session cannot start", async () => {
  const source = api(vi.fn().mockResolvedValue(preview("a", "Review helper")));
  render(<ProfileSessionReview api={source} sessionId="a" />);
  const details = await screen.findByText(/cannot start again/);
  expect(details.closest("details")?.open).toBe(true);
});

// A session that is still sending keeps going regardless of this result; the copy
// must stay true either way, and never say Send itself is blocked.
it("says a running conversation keeps going, not that Send is blocked", async () => {
  const source = api(vi.fn().mockResolvedValue(preview("a", "Review helper")));
  render(<ProfileSessionReview api={source} sessionId="a" />);
  const alert = (await screen.findByRole("alert")).textContent ?? "";
  expect(alert).toContain("keeps going until it ends");
  expect(alert).not.toMatch(/send.*blocked/i);
  expect(alert).not.toContain("Create a new draft");
});

// `provider_unavailable` is not drift like the other codes — a new draft fails the identical
// way while the provider stays unavailable, so it needs its own way on.
it("gives provider_unavailable its own way on, not the shared new-draft one", async () => {
  const source = api(
    vi
      .fn()
      .mockResolvedValue(preview("a", "Review helper", { blockers: ["provider_unavailable"] })),
  );
  render(<ProfileSessionReview api={source} sessionId="a" />);
  const alert = (await screen.findByRole("alert")).textContent ?? "";
  expect(alert).toContain("Check Settings, then use Refresh review.");
  expect(alert).not.toContain("Create a new draft");
  expect(alert).not.toContain("new draft from the profile");
});

it("hides the ready-to-send note while blocked, and shows it once launchable", async () => {
  const source = api(
    vi.fn().mockResolvedValue(preview("a", "Ready", { launchable: true, blockers: [] })),
  );
  render(<ProfileSessionReview api={source} sessionId="a" />);
  expect(await screen.findByText(/Use Send when you are ready\./)).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("does not show the ready-to-send note while blocked", async () => {
  const source = api(vi.fn().mockResolvedValue(preview("a", "Blocked")));
  render(<ProfileSessionReview api={source} sessionId="a" />);
  await screen.findByRole("alert");
  expect(screen.queryByText(/Use Send when you are ready\./)).toBeNull();
});

it("adds an effective-value column for every field", async () => {
  const source = api(vi.fn().mockResolvedValue(preview("a", "Review helper")));
  render(<ProfileSessionReview api={source} sessionId="a" />);
  await screen.findByText("Effective value");
  expect(screen.getByRole("cell", { name: "Review helper" })).toBeTruthy();
});

it("re-inspects when Refresh review is clicked", async () => {
  const inspect = vi.fn().mockResolvedValue(preview("a", "Review helper"));
  const source = api(inspect);
  render(<ProfileSessionReview api={source} sessionId="a" />);
  await screen.findByText(/Profile instructions/);
  expect(inspect).toHaveBeenCalledTimes(1);
  await act(async () => {
    screen.getByRole("button", { name: "Refresh review" }).click();
  });
  expect(inspect).toHaveBeenCalledTimes(2);
});

it("re-inspects when App passes a new refresh key after a start refusal", async () => {
  const inspect = vi.fn().mockResolvedValue(preview("a", "Review helper"));
  const source = api(inspect);
  const view = render(<ProfileSessionReview api={source} sessionId="a" refreshKey={1} />);
  await screen.findByText(/Profile instructions/);
  expect(inspect).toHaveBeenCalledTimes(1);
  view.rerender(<ProfileSessionReview api={source} sessionId="a" refreshKey={1} />);
  expect(inspect).toHaveBeenCalledTimes(1);
  view.rerender(<ProfileSessionReview api={source} sessionId="a" refreshKey={2} />);
  expect(inspect).toHaveBeenCalledTimes(2);
});

it("reports a successful inspection so the composer can hide its model chip", async () => {
  const onProfileSession = vi.fn();
  const source = api(vi.fn().mockResolvedValue(preview("a", "Review helper")));
  render(<ProfileSessionReview api={source} sessionId="a" onProfileSession={onProfileSession} />);
  await screen.findByText(/Profile instructions/);
  expect(onProfileSession).toHaveBeenCalledWith("a", null);
});

it("hands the bound identity to the callback when the server names one", async () => {
  const onProfileSession = vi.fn();
  const identity = {
    name: "Planner",
    avatar_asset_id: null,
    accent: "blue" as const,
    shape: "circle" as const,
    profile_revision: 2,
  };
  const source = api(vi.fn().mockResolvedValue(preview("a", "Review helper", { identity })));
  render(<ProfileSessionReview api={source} sessionId="a" onProfileSession={onProfileSession} />);
  await screen.findByText(/Profile instructions/);
  expect(onProfileSession).toHaveBeenCalledWith("a", identity);
});

it("does not display a late preview from a different selected session", async () => {
  let resolve!: (value: PreparedProfilePreview) => void;
  const source = api(
    vi.fn().mockImplementation((id: string) =>
      id === "a"
        ? new Promise<PreparedProfilePreview>((done) => {
            resolve = done;
          })
        : Promise.resolve(preview("b", "Second")),
    ),
  );
  const view = render(<ProfileSessionReview api={source} sessionId="a" />);
  view.rerender(<ProfileSessionReview api={source} sessionId="b" />);
  await screen.findByText(/Profile instructions · Second/);
  await act(async () => resolve(preview("a", "First")));
  expect(screen.queryByText(/Profile instructions · First/)).toBeNull();
});

it("ordinary sessions do not acquire a profile label and are reported unbound", async () => {
  const onProfileSession = vi.fn();
  const onUnboundSession = vi.fn();
  const source = api(vi.fn().mockRejectedValue(new ProfileLaunchApiError(404, "not_found")));
  const view = render(
    <ProfileSessionReview
      api={source}
      sessionId="ordinary"
      onProfileSession={onProfileSession}
      onUnboundSession={onUnboundSession}
    />,
  );
  await act(async () => {});
  expect(view.container.textContent).toBe("");
  expect(onProfileSession).not.toHaveBeenCalled();
  expect(onUnboundSession).toHaveBeenCalledWith("ordinary");
});

it("a failed inspection reports neither bound nor unbound", async () => {
  const onProfileSession = vi.fn();
  const onUnboundSession = vi.fn();
  const source = api(vi.fn().mockRejectedValue(new ProfileLaunchApiError(503, "unavailable")));
  render(
    <ProfileSessionReview
      api={source}
      sessionId="a"
      onProfileSession={onProfileSession}
      onUnboundSession={onUnboundSession}
    />,
  );
  expect(await screen.findByText("Profile details are unavailable.")).toBeTruthy();
  expect(onProfileSession).not.toHaveBeenCalled();
  expect(onUnboundSession).not.toHaveBeenCalled();
});

it("shows the generated MCP file and its argument", async () => {
  const json = JSON.stringify({ mcpServers: { viki_echo: { args: [], command: "python" } } });
  const argv_element = `--mcp-config=C:/data/mcp/${"a".repeat(64)}.json`;
  const source = api(
    vi
      .fn()
      .mockResolvedValue(preview("a", "Review helper", { mcp_config: { json, argv_element } })),
  );
  const { container } = render(<ProfileSessionReview api={source} sessionId="a" />);
  await screen.findByText(/Profile instructions · Review helper · revision 2/);
  expect(container.querySelector("section.mcp-preview pre")?.textContent).toBe(json);
  expect(container.querySelector("section.mcp-preview code")?.textContent).toBe(argv_element);
  expect(screen.getByText("App MCP servers")).toBeTruthy();
  expect(screen.getByText("Generated file")).toBeTruthy();
  expect(screen.getByText("Argument")).toBeTruthy();
});

it("shows no MCP section when mcp_config is null or absent", async () => {
  const withNull = render(
    <ProfileSessionReview
      api={api(vi.fn().mockResolvedValue(preview("a", "Review helper", { mcp_config: null })))}
      sessionId="a"
    />,
  );
  await act(async () => {});
  expect(withNull.container.querySelector("section.mcp-preview")).toBeNull();

  const absent = render(
    <ProfileSessionReview
      api={api(vi.fn().mockResolvedValue(preview("a", "Review helper")))}
      sessionId="a"
    />,
  );
  await act(async () => {});
  expect(absent.container.querySelector("section.mcp-preview")).toBeNull();
});

it("says a copy is made on first send, then names the existing copy's branch", async () => {
  const code_copy = { workspace_id: "w", name: "Repo", branch: null };
  const { container, rerender } = render(
    <ProfileSessionReview
      api={api(vi.fn().mockResolvedValue(preview("a", "Helper", { code_copy })))}
      sessionId="a"
    />,
  );
  expect(await screen.findByText("Gets a copy of Repo when you send")).toBeTruthy();
  rerender(
    <ProfileSessionReview
      api={api(
        vi.fn().mockResolvedValue(
          preview("b", "Helper", {
            code_copy: { ...code_copy, branch: "viki/bot-1" },
          }),
        ),
      )}
      sessionId="b"
    />,
  );
  expect(await screen.findByText("Works in a copy of Repo (branch viki/bot-1)")).toBeTruthy();
  expect(container.textContent).not.toContain("Gets a copy");
});

it("shows no copy line for a null or absent code_copy", async () => {
  const withNull = render(
    <ProfileSessionReview
      api={api(vi.fn().mockResolvedValue(preview("a", "Helper", { code_copy: null })))}
      sessionId="a"
    />,
  );
  await screen.findByText(/Profile instructions/);
  expect(withNull.container.textContent).not.toMatch(/copy of/);
  const absent = render(
    <ProfileSessionReview
      api={api(vi.fn().mockResolvedValue(preview("a", "Helper")))}
      sessionId="a"
    />,
  );
  await screen.findAllByText(/Profile instructions/);
  expect(absent.container.textContent).not.toMatch(/copy of/);
});

it("renders copy names as text, not markup, and drops a stale selection's copy", async () => {
  let resolve!: (value: PreparedProfilePreview) => void;
  const hostile = { workspace_id: "w", name: "<b>x</b>", branch: null };
  const source = api(
    vi.fn().mockImplementation((id: string) =>
      id === "a"
        ? new Promise<PreparedProfilePreview>((done) => {
            resolve = done;
          })
        : Promise.resolve(preview("b", "Second")),
    ),
  );
  const view = render(<ProfileSessionReview api={source} sessionId="a" />);
  view.rerender(<ProfileSessionReview api={source} sessionId="b" />);
  await screen.findByText(/Profile instructions · Second/);
  await act(async () => resolve(preview("a", "First", { code_copy: hostile })));
  expect(view.container.textContent).not.toContain("copy of");
  view.rerender(
    <ProfileSessionReview
      api={api(vi.fn().mockResolvedValue(preview("c", "Third", { code_copy: hostile })))}
      sessionId="c"
    />,
  );
  await screen.findByText("Gets a copy of <b>x</b> when you send");
  expect(view.container.querySelector("b")).toBeNull();
});

const modelField = (field: string, effective_value: unknown) => ({
  field,
  preview_class: "display_only" as const,
  status: "supported" as const,
  effective_value,
  source: "profile",
  reason: null,
});

it("labels a connection model in the summary and field cells without its ref id", async () => {
  const source = api(
    vi.fn().mockResolvedValue(
      preview("a", "Helper", {
        prefs: {
          mode: "plan",
          effort: "high",
          model: "viki-llm/11111111-2222-4333-8444-555555555555/gpt-x",
        },
        fields: [
          modelField("prefs.model", "viki-llm/11111111-2222-4333-8444-555555555555/gpt-x"),
          modelField("runtime.model", "viki-llm/11111111-2222-4333-8444-555555555555/gpt-x"),
        ],
      }),
    ),
  );
  const { container } = render(<ProfileSessionReview api={source} sessionId="a" />);
  await screen.findByText(/Profile instructions/);
  expect(container.textContent).toContain("Model: gpt-x.");
  expect(container.textContent).not.toContain("11111111-2222-4333-8444-555555555555");
  expect(container.textContent).not.toContain("viki-llm/");
});

it("names the connection when the list loads, and says Claude Code's default for null", async () => {
  const list = vi.fn().mockResolvedValue({
    revision: 1,
    default_model: null,
    connections: [
      {
        id: "c",
        model_ref: "viki-llm/11111111-2222-4333-8444-555555555555/gpt-x",
        name: "Work gateway",
        model: "gpt-x",
        has_api_key: true,
      },
    ],
  });
  const { rerender, container } = render(
    <ModelConnectionsContext.Provider value={{ list } as never}>
      <ProfileSessionReview
        api={api(
          vi.fn().mockResolvedValue(
            preview("a", "Helper", {
              prefs: {
                mode: "plan",
                effort: "high",
                model: "viki-llm/11111111-2222-4333-8444-555555555555/gpt-x",
              },
              fields: [
                modelField("runtime.model", "viki-llm/11111111-2222-4333-8444-555555555555/gpt-x"),
              ],
            }),
          ),
        )}
        sessionId="a"
      />
    </ModelConnectionsContext.Provider>,
  );
  await vi.waitFor(() => expect(container.textContent).toContain("Work gateway · gpt-x"));
  expect(container.textContent).not.toContain("11111111-2222-4333-8444-555555555555");
  rerender(
    <ProfileSessionReview
      api={api(vi.fn().mockResolvedValue(preview("b", "Helper")))}
      sessionId="b"
    />,
  );
  await vi.waitFor(() => expect(container.textContent).toContain("Model: Claude Code's default."));
});

it.each([
  ["connection_bot_only", "Start a new draft from the Bot."],
  ["connection_missing", "was removed. Choose another model"],
  ["connections_unavailable", "use Refresh review."],
  ["connection_invalid", "no longer valid"],
])("explains %s in plain words with a way on", async (code, words) => {
  const source = api(vi.fn().mockResolvedValue(preview("a", "Helper", { blockers: [code] })));
  render(<ProfileSessionReview api={source} sessionId="a" />);
  const alert = (await screen.findByRole("alert")).textContent ?? "";
  expect(alert).toContain(words);
  expect(alert).not.toContain(code);
  expect(alert).not.toContain("start a new draft from the profile in Agents");
});
