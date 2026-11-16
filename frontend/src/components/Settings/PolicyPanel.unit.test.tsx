import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { ShellApiError } from "../../clients/app/shellApi";
import { t } from "../../i18n/t";
import {
  FAKE_WORKSPACE_ID,
  fakeShellApi,
  inspectionFixture,
  workspaceFixture,
} from "../../testShell";
import { PolicyPanel } from "./PolicyPanel";

afterEach(() => {
  cleanup();
});

const CODE_ROW = workspaceFixture({ kind: "code", policy: "existing_readonly" });

test("a code folder's targets, notes and Claude config acknowledgement read as trust", () => {
  const api = fakeShellApi();
  const inspection = inspectionFixture({
    policy: "existing_readonly",
    markers: { mcp_json: true },
    acknowledgements_required: ["claude_config_present"],
  });
  render(
    <PolicyPanel
      workspaceId={FAKE_WORKSPACE_ID}
      api={api}
      inspection={inspection}
      workspaces={[CODE_ROW]}
    />,
  );
  const markers = screen.getAllByRole("listitem").map((li) => li.textContent ?? "");
  const claudeDir = markers.findIndex((m) => m.includes(".claude directory"));
  expect(markers[claudeDir + 1]).toContain(".mcp.json file");
  expect(screen.queryByRole("button", { name: "Protected" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Ordinary" })).toBeNull();
  expect(
    screen.getByText(
      "Until you trust it, VIKI starts no session here and runs no Git command on its working tree. You can browse its files.",
    ),
  ).toBeTruthy();
  expect(screen.queryByText(/stays read-only in the Vault view/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Trust this folder" }));
  expect(
    screen.getByLabelText(
      "This folder supplies Claude Code configuration (.claude/ or .mcp.json). VIKI sessions run its hooks and helper commands and connect its MCP servers without asking.",
    ),
  ).toBeTruthy();
});

test("a vault row keeps its six markers and no mcp_json", () => {
  render(
    <PolicyPanel
      workspaceId={FAKE_WORKSPACE_ID}
      api={fakeShellApi()}
      inspection={inspectionFixture({ policy: "ordinary" })}
      workspaces={[workspaceFixture()]}
    />,
  );
  expect(screen.queryByText(".mcp.json file")).toBeNull();
  expect(screen.getByText(".claude directory")).toBeTruthy();
});

test("a trusted code folder offers Stop trusting and its own note", () => {
  const api = fakeShellApi();
  const inspection = inspectionFixture({
    policy: "ordinary",
    excluded_from_writes: ["CLAUDE.md", "AGENTS.md"],
  });
  render(
    <PolicyPanel
      workspaceId={FAKE_WORKSPACE_ID}
      api={api}
      inspection={inspection}
      workspaces={[workspaceFixture({ kind: "code", policy: "ordinary" })]}
    />,
  );
  expect(screen.getByRole("button", { name: "Stop trusting" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Files the Code editor won't save" })).toBeTruthy();
  expect(screen.getByText(t("workspaces.code.trusted_note"))).toBeTruthy();
  const first = screen.getByText(t("workspaces.code.trusted_note"));
  const list = first.closest("ul");
  expect(Array.from(list?.querySelectorAll("li") ?? []).map((item) => item.textContent)).toEqual([
    t("workspaces.code.trusted_note"),
    t("workspaces.code.trust_writes"),
    t("workspaces.code.trust_loads"),
    t("workspaces.code.trust_sandbox"),
  ]);
});

test("a vault keeps its own excluded-path heading", () => {
  render(
    <PolicyPanel
      workspaceId={FAKE_WORKSPACE_ID}
      api={fakeShellApi()}
      inspection={inspectionFixture({
        policy: "ordinary",
        excluded_from_writes: ["notes/AGENTS.md"],
      })}
      workspaces={[workspaceFixture()]}
    />,
  );
  expect(screen.getByRole("heading", { name: "Paths note saves never write" })).toBeTruthy();
});

test("a workspace_active refusal on a code folder reads in words", async () => {
  const api = fakeShellApi();
  api.fail("setPolicy", new ShellApiError(409, "workspace_active"));
  render(
    <PolicyPanel
      workspaceId={FAKE_WORKSPACE_ID}
      api={api}
      inspection={inspectionFixture({ policy: "ordinary" })}
      workspaces={[workspaceFixture({ kind: "code", policy: "ordinary" })]}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Stop trusting" }));
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() =>
    expect(
      screen.getByText(
        "The active folder's trust can't change. Select another workspace and restart first.",
      ),
    ).toBeTruthy(),
  );
});

test.each([
  ["existing_readonly", "Trust this folder"],
  ["ordinary", "Stop trusting"],
] as const)("a code row on %s offers exactly the one trust button", (policy, label) => {
  render(
    <PolicyPanel
      workspaceId={FAKE_WORKSPACE_ID}
      api={fakeShellApi()}
      inspection={inspectionFixture({ policy })}
      workspaces={[workspaceFixture({ kind: "code", policy })]}
    />,
  );
  const names = Array.from(
    document.querySelectorAll(".policy-targets button"),
    (b) => b.textContent,
  );
  expect(names).toEqual([label]);
});

test("ordinary offers exactly existing_readonly and protected", () => {
  const api = fakeShellApi();
  const inspection = inspectionFixture({ policy: "ordinary" });
  render(<PolicyPanel workspaceId={FAKE_WORKSPACE_ID} api={api} inspection={inspection} />);
  expect(screen.getByRole("button", { name: "Read-only (existing folder)" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Protected" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Ordinary" })).toBeNull();
});

test("existing_readonly offers exactly ordinary and protected", () => {
  const api = fakeShellApi();
  const inspection = inspectionFixture({ policy: "existing_readonly" });
  render(<PolicyPanel workspaceId={FAKE_WORKSPACE_ID} api={api} inspection={inspection} />);
  expect(screen.getByRole("button", { name: "Ordinary" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Protected" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Read-only (existing folder)" })).toBeNull();
});

test("protected offers no transition at all", () => {
  const api = fakeShellApi();
  const inspection = inspectionFixture({ policy: "protected" });
  render(<PolicyPanel workspaceId={FAKE_WORKSPACE_ID} api={api} inspection={inspection} />);
  expect(screen.queryByRole("button", { name: "Ordinary" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Read-only (existing folder)" })).toBeNull();
  expect(screen.getByText("A protected vault's policy cannot be changed.")).toBeTruthy();
});

test("ordinary, persistent, bootstrap complete: the edit-in-vault sentence is shown", () => {
  const api = fakeShellApi();
  const inspection = inspectionFixture({ policy: "ordinary" });
  const row = workspaceFixture({ bootstrap: "complete", persistent: true });
  render(
    <PolicyPanel
      workspaceId={FAKE_WORKSPACE_ID}
      api={api}
      inspection={inspection}
      workspaces={[row]}
    />,
  );
  expect(
    screen.getByText("Notes in this vault can be created and edited in the Vault view."),
  ).toBeTruthy();
});

test("ordinary with bootstrap none: the edit-in-vault sentence is absent, the read-only sentence is shown", () => {
  const api = fakeShellApi();
  const inspection = inspectionFixture({ policy: "ordinary" });
  const row = workspaceFixture({ bootstrap: "none", persistent: true });
  render(
    <PolicyPanel
      workspaceId={FAKE_WORKSPACE_ID}
      api={api}
      inspection={inspection}
      workspaces={[row]}
    />,
  );
  expect(
    screen.queryByText("Notes in this vault can be created and edited in the Vault view."),
  ).toBeNull();
  expect(
    screen.getByText(
      "VIKI edits notes only in a vault it created, so this folder stays read-only in the Vault view.",
    ),
  ).toBeTruthy();
});

test("ordinary with bootstrap pending: its own sentence is shown, not the general read-only one", () => {
  const api = fakeShellApi();
  const inspection = inspectionFixture({ policy: "ordinary" });
  const row = workspaceFixture({ bootstrap: "pending", persistent: true });
  render(
    <PolicyPanel
      workspaceId={FAKE_WORKSPACE_ID}
      api={api}
      inspection={inspection}
      workspaces={[row]}
    />,
  );
  expect(
    screen.getByText(
      "This vault's setup has not finished, so its notes cannot be edited yet. Finish or cancel the setup first.",
    ),
  ).toBeTruthy();
  expect(
    screen.queryByText(
      "VIKI edits notes only in a vault it created, so this folder stays read-only in the Vault view.",
    ),
  ).toBeNull();
});

/** jsdom exposes `navigator.platform` through a prototype getter, so this file's own property is
 * removed afterwards rather than restored — there is no own descriptor to put back (the pattern in
 * src/app-tests/vaults/vaultWritePlatform.unit.test.tsx). */
function setPlatform(value: string): () => void {
  Object.defineProperty(window.navigator, "platform", { value, configurable: true });
  return () => {
    delete (window.navigator as { platform?: string }).platform;
  };
}

test("ordinary, persistent, bootstrap complete, on macOS: the edit-in-vault sentence is shown", () => {
  const restore = setPlatform("MacIntel");
  try {
    const api = fakeShellApi();
    const inspection = inspectionFixture({ policy: "ordinary" });
    const row = workspaceFixture({ bootstrap: "complete", persistent: true });
    render(
      <PolicyPanel
        workspaceId={FAKE_WORKSPACE_ID}
        api={api}
        inspection={inspection}
        workspaces={[row]}
      />,
    );
    expect(
      screen.getByText("Notes in this vault can be created and edited in the Vault view."),
    ).toBeTruthy();
  } finally {
    restore();
  }
});

test("ordinary, persistent, bootstrap complete, on Linux: the edit-in-vault sentence is shown", () => {
  const restore = setPlatform("Linux x86_64");
  try {
    const api = fakeShellApi();
    const inspection = inspectionFixture({ policy: "ordinary" });
    const row = workspaceFixture({ bootstrap: "complete", persistent: true });
    render(
      <PolicyPanel
        workspaceId={FAKE_WORKSPACE_ID}
        api={api}
        inspection={inspection}
        workspaces={[row]}
      />,
    );
    expect(
      screen.getByText("Notes in this vault can be created and edited in the Vault view."),
    ).toBeTruthy();
  } finally {
    restore();
  }
});

test("ordinary, persistent, bootstrap complete, on Win32: the edit-in-vault sentence is still shown", () => {
  const restore = setPlatform("Win32");
  try {
    const api = fakeShellApi();
    const inspection = inspectionFixture({ policy: "ordinary" });
    const row = workspaceFixture({ bootstrap: "complete", persistent: true });
    render(
      <PolicyPanel
        workspaceId={FAKE_WORKSPACE_ID}
        api={api}
        inspection={inspection}
        workspaces={[row]}
      />,
    );
    expect(
      screen.getByText("Notes in this vault can be created and edited in the Vault view."),
    ).toBeTruthy();
  } finally {
    restore();
  }
});

test.each(["MacIntel", "Linux x86_64"])(
  "on %s, ordinary with bootstrap pending: its own sentence is shown, not no_write_platform",
  (platform) => {
    const restore = setPlatform(platform);
    try {
      const api = fakeShellApi();
      const inspection = inspectionFixture({ policy: "ordinary" });
      const row = workspaceFixture({ bootstrap: "pending", persistent: true });
      render(
        <PolicyPanel
          workspaceId={FAKE_WORKSPACE_ID}
          api={api}
          inspection={inspection}
          workspaces={[row]}
        />,
      );
      expect(
        screen.getByText(
          "This vault's setup has not finished, so its notes cannot be edited yet. Finish or cancel the setup first.",
        ),
      ).toBeTruthy();
      expect(
        screen.queryByText(
          "This platform cannot save notes from the Vault view yet. Edit them in a chat or another editor instead.",
        ),
      ).toBeNull();
    } finally {
      restore();
    }
  },
);

test("ordinary with no matching listing row: the read-only sentence is shown, never a claim the row can't back up", () => {
  const api = fakeShellApi();
  const inspection = inspectionFixture({ policy: "ordinary" });
  render(<PolicyPanel workspaceId={FAKE_WORKSPACE_ID} api={api} inspection={inspection} />);
  expect(
    screen.queryByText("Notes in this vault can be created and edited in the Vault view."),
  ).toBeNull();
  expect(
    screen.getByText(
      "VIKI edits notes only in a vault it created, so this folder stays read-only in the Vault view.",
    ),
  ).toBeTruthy();
});

test("after an apply, the sentence follows setPolicy's own note_write, before any listing refresh", async () => {
  const api = fakeShellApi({
    workspace: workspaceFixture({ bootstrap: "none", persistent: true }),
  });
  const inspection = inspectionFixture({
    policy: "existing_readonly",
    acknowledgements_required: [],
  });
  render(<PolicyPanel workspaceId={FAKE_WORKSPACE_ID} api={api} inspection={inspection} />);
  fireEvent.click(screen.getByRole("button", { name: "Ordinary" }));
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(api.calls.some((c) => c.method === "setPolicy")).toBe(true));
  // The fake's own `data.workspace.bootstrap` is "none", so setPolicy's `note_write` is false:
  // the panel shows the read-only sentence for the newly-ordinary row, with no `workspaces` prop
  // and before the caller has re-fetched one.
  await waitFor(() =>
    expect(
      screen.getByText(
        "VIKI edits notes only in a vault it created, so this folder stays read-only in the Vault view.",
      ),
    ).toBeTruthy(),
  );
});

test("after an apply, the sentence follows setPolicy's own note_write when it is true, before any listing refresh", async () => {
  const api = fakeShellApi({
    workspace: workspaceFixture({ bootstrap: "complete", persistent: true }),
  });
  const inspection = inspectionFixture({
    policy: "existing_readonly",
    acknowledgements_required: [],
  });
  render(<PolicyPanel workspaceId={FAKE_WORKSPACE_ID} api={api} inspection={inspection} />);
  fireEvent.click(screen.getByRole("button", { name: "Ordinary" }));
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(api.calls.some((c) => c.method === "setPolicy")).toBe(true));
  // The fake's own `data.workspace` is ordinary/persistent/bootstrap-complete, so setPolicy's
  // `note_write` is true: the panel shows the editing sentence with no `workspaces` prop at all,
  // so the listing fallback (which would answer false with no row to check) cannot be what
  // produced it.
  await waitFor(() =>
    expect(
      screen.getByText("Notes in this vault can be created and edited in the Vault view."),
    ).toBeTruthy(),
  );
});

test("apply is gated on every acknowledgement, one checkbox at a time", async () => {
  const api = fakeShellApi();
  const inspection = inspectionFixture({
    policy: "existing_readonly",
    acknowledgements_required: ["hooks_present", "git_present"],
  });
  render(<PolicyPanel workspaceId={FAKE_WORKSPACE_ID} api={api} inspection={inspection} />);
  fireEvent.click(screen.getByRole("button", { name: "Protected" }));

  const apply = screen.getByRole("button", { name: "Apply" });
  expect(apply.getAttribute("aria-disabled")).toBe("true");

  const hooksBox = screen.getByLabelText(/Claude Code hooks configured/);
  fireEvent.click(hooksBox);
  expect(apply.getAttribute("aria-disabled")).toBe("true");

  const gitBox = screen.getByLabelText(/Git repository/);
  fireEvent.click(gitBox);
  expect(apply.getAttribute("aria-disabled")).toBe("false");

  fireEvent.click(apply);
  await waitFor(() => expect(api.calls.some((c) => c.method === "setPolicy")).toBe(true));
  const call = api.calls.find((c) => c.method === "setPolicy");
  expect(call?.args[3]).toBe("protected");
  expect(call?.args[4]).toEqual(expect.arrayContaining(["hooks_present", "git_present"]));
});

test("a target the backend refuses is aria-disabled with its reason, and never chosen", () => {
  const api = fakeShellApi();
  const inspection = inspectionFixture({
    policy: "ordinary",
    admissible: { ordinary: true, protected: false, reason: "protected_root" },
  });
  render(<PolicyPanel workspaceId={FAKE_WORKSPACE_ID} api={api} inspection={inspection} />);
  const protectedButton = screen.getByRole("button", { name: "Protected" });
  expect(protectedButton.getAttribute("aria-disabled")).toBe("true");
  fireEvent.click(protectedButton);
  // A refused target never becomes the chosen one: no acknowledgement section appears.
  expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
});

test("a 409 acknowledgement_required names the missing items", async () => {
  const api = fakeShellApi();
  const inspection = inspectionFixture({ policy: "ordinary", acknowledgements_required: [] });
  api.fail(
    "setPolicy",
    new ShellApiError(409, "acknowledgement_required", { extra: { required: ["hooks_present"] } }),
  );
  render(<PolicyPanel workspaceId={FAKE_WORKSPACE_ID} api={api} inspection={inspection} />);
  fireEvent.click(screen.getByRole("button", { name: "Protected" }));
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("Existing hooks"));
  expect(screen.getByRole("alert").textContent).not.toContain("hooks_present");
});

test("a stale revision offers a retry that re-fetches the inspection", async () => {
  const api = fakeShellApi();
  const inspection = inspectionFixture({ policy: "ordinary", acknowledgements_required: [] });
  api.fail("setPolicy", new ShellApiError(409, "stale_revision"));
  render(<PolicyPanel workspaceId={FAKE_WORKSPACE_ID} api={api} inspection={inspection} />);
  fireEvent.click(screen.getByRole("button", { name: "Protected" }));
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
  const before = api.calls.filter((c) => c.method === "policy").length;
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() =>
    expect(api.calls.filter((c) => c.method === "policy").length).toBeGreaterThan(before),
  );
});

test("an instruction with no head renders no <pre>; a hostile head renders as text", () => {
  const api = fakeShellApi();
  const hostile = "<script>evil()</script>";
  const inspection = inspectionFixture({
    instructions: [
      {
        path: "D:\\Vault\\CLAUDE.md",
        bytes: 10,
        sha256: "a".repeat(64),
        head: null,
        too_large: false,
      },
      { path: "D:\\Vault\\AGENTS.md", bytes: 20, sha256: null, head: hostile, too_large: false },
    ],
  });
  const { container } = render(
    <PolicyPanel workspaceId={FAKE_WORKSPACE_ID} api={api} inspection={inspection} />,
  );
  const pres = container.querySelectorAll("pre");
  expect(pres.length).toBe(1);
  expect(pres[0]?.textContent).toBe(hostile);
  expect(container.querySelector("script")).toBeNull();
});

test("without a seeded inspection the panel fetches policy(id) itself", async () => {
  const api = fakeShellApi({ inspection: inspectionFixture({ policy: "existing_readonly" }) });
  render(<PolicyPanel workspaceId={FAKE_WORKSPACE_ID} api={api} />);
  expect(screen.getByRole("status").textContent).toBe("Loading…");
  await waitFor(() => expect(screen.getByRole("button", { name: "Ordinary" })).toBeTruthy());
  expect(api.calls.some((c) => c.method === "policy")).toBe(true);
});

test("a failed fetch with no seeded inspection renders the error text", async () => {
  const api = fakeShellApi();
  api.fail("policy", new ShellApiError(0, "network_error"));
  render(<PolicyPanel workspaceId={FAKE_WORKSPACE_ID} api={api} />);
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toBe("The server could not be reached."),
  );
});

test("markers announce presence and absence for screen readers", () => {
  const api = fakeShellApi();
  const inspection = inspectionFixture({
    markers: {
      claude_md: true,
      agents_md: false,
      claude_dir: false,
      hooks: false,
      git: false,
      viki_manifest: false,
    },
  });
  render(<PolicyPanel workspaceId={FAKE_WORKSPACE_ID} api={api} inspection={inspection} />);
  const items = screen.getAllByRole("listitem");
  const claudeMd = items.find((li) => li.textContent?.includes("CLAUDE.md"));
  expect(claudeMd?.textContent).toContain("Present");
});

test("with api null the panel never throws and shows nothing to fetch with", () => {
  const inspection = inspectionFixture({ policy: "protected" });
  render(<PolicyPanel workspaceId={FAKE_WORKSPACE_ID} api={null} inspection={inspection} />);
  expect(screen.getByText("A protected vault's policy cannot be changed.")).toBeTruthy();
});

test("a 403 protected_root submit error renders the catalog sentence", async () => {
  const api = fakeShellApi();
  const inspection = inspectionFixture({ policy: "ordinary", acknowledgements_required: [] });
  api.fail("setPolicy", new ShellApiError(403, "protected_root"));
  render(<PolicyPanel workspaceId={FAKE_WORKSPACE_ID} api={api} inspection={inspection} />);
  fireEvent.click(screen.getByRole("button", { name: "Protected" }));
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toBe(
      "A protected vault's policy cannot be changed.",
    ),
  );
});

test("identity renders present/absent, never the raw boolean", () => {
  const api = fakeShellApi();
  const inspection = inspectionFixture({ identity: { established: true } });
  render(<PolicyPanel workspaceId={FAKE_WORKSPACE_ID} api={api} inspection={inspection} />);
  const dt = screen.getByText("Git identity");
  expect(dt.nextElementSibling?.textContent).toBe("Present");
  expect(screen.queryByText("true")).toBeNull();
  expect(screen.queryByText("false")).toBeNull();
});

test("an overlap resolves to the other registration's name and root, given the listing", () => {
  const api = fakeShellApi();
  const other = workspaceFixture({
    workspace_id: "11111111-1111-4111-8111-111111111111",
    name: "Old vault",
    root: "D:\\Vaults\\Old",
  });
  const inspection = inspectionFixture({ overlaps: [other.workspace_id] });
  render(
    <PolicyPanel
      workspaceId={FAKE_WORKSPACE_ID}
      api={api}
      inspection={inspection}
      workspaces={[other]}
    />,
  );
  expect(screen.getByText("Old vault")).toBeTruthy();
  expect(screen.getByText("D:\\Vaults\\Old")).toBeTruthy();
  expect(screen.queryByText(other.workspace_id)).toBeNull();
});

test("an overlap id absent from the listing renders the not-found chip, never the raw id", () => {
  const api = fakeShellApi();
  const unknownId = "99999999-9999-4999-8999-999999999999";
  const inspection = inspectionFixture({ overlaps: [unknownId] });
  render(
    <PolicyPanel
      workspaceId={FAKE_WORKSPACE_ID}
      api={api}
      inspection={inspection}
      workspaces={[]}
    />,
  );
  expect(screen.getByText("Not found")).toBeTruthy();
  expect(screen.queryByText(unknownId)).toBeNull();
});

test("with no workspaces prop at all (the onboarding host) an overlap falls back the same way", () => {
  const api = fakeShellApi();
  const unknownId = "99999999-9999-4999-8999-999999999999";
  const inspection = inspectionFixture({ overlaps: [unknownId] });
  render(<PolicyPanel workspaceId={FAKE_WORKSPACE_ID} api={api} inspection={inspection} />);
  expect(screen.getByText("Not found")).toBeTruthy();
});

test("identity.inherits_from resolves through the same lookup as overlaps", () => {
  const api = fakeShellApi();
  const parent = workspaceFixture({
    workspace_id: "11111111-1111-4111-8111-111111111111",
    name: "Parent vault",
    root: "D:\\Vaults\\Parent",
  });
  const inspection = inspectionFixture({
    identity: { established: true, inherits_from: parent.workspace_id },
  });
  render(
    <PolicyPanel
      workspaceId={FAKE_WORKSPACE_ID}
      api={api}
      inspection={inspection}
      workspaces={[parent]}
    />,
  );
  expect(screen.getByText("Parent vault")).toBeTruthy();
  expect(screen.queryByText(parent.workspace_id)).toBeNull();
});

test("every button this panel renders carries a styled btn variant", () => {
  const api = fakeShellApi();
  const inspection = inspectionFixture({
    policy: "ordinary",
    admissible: { ordinary: true, protected: false, reason: "protected_root" },
  });
  const { container } = render(
    <PolicyPanel workspaceId={FAKE_WORKSPACE_ID} api={api} inspection={inspection} />,
  );
  // Reveals the Apply button too, so the acknowledgement-gated action is in scope.
  fireEvent.click(screen.getByRole("button", { name: "Read-only (existing folder)" }));
  const buttons = Array.from(container.querySelectorAll("button"));
  expect(buttons.length).toBeGreaterThan(0);
  for (const button of buttons) {
    const classes = button.className.split(/\s+/).filter(Boolean);
    expect(classes.includes("btn")).toBe(true);
    expect(classes.some((c) => c === "go" || c === "alt" || c === "txt")).toBe(true);
  }
});

test("markers render as a data-present checklist, the identity list as settings-facts", () => {
  const api = fakeShellApi();
  const inspection = inspectionFixture({
    markers: {
      claude_md: true,
      agents_md: false,
      claude_dir: false,
      hooks: false,
      git: false,
      viki_manifest: false,
    },
  });
  const { container } = render(
    <PolicyPanel workspaceId={FAKE_WORKSPACE_ID} api={api} inspection={inspection} />,
  );
  const items = screen.getAllByRole("listitem").filter((li) => li.hasAttribute("data-present"));
  expect(items.length).toBe(6);
  const present = items.find((li) => li.getAttribute("data-present") === "true");
  expect(present?.textContent).toContain("CLAUDE.md");
  expect(items.filter((li) => li.getAttribute("data-present") === "false").length).toBe(5);
  expect(container.querySelector("dl.settings-facts")).toBeTruthy();
});

test("a refetch that fails after a successful apply still shows the error, panel stays up", async () => {
  const api = fakeShellApi();
  const inspection = inspectionFixture({ policy: "ordinary", acknowledgements_required: [] });
  // The self-fetch mode's only `policy()` call for a seeded panel is the reload after apply
  // succeeds: script it to fail so the post-apply refetch, not the initial render, is under test.
  api.fail("policy", new ShellApiError(0, "network_error"));
  render(<PolicyPanel workspaceId={FAKE_WORKSPACE_ID} api={api} inspection={inspection} />);
  fireEvent.click(screen.getByRole("button", { name: "Protected" }));
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(api.calls.some((c) => c.method === "setPolicy")).toBe(true));
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toBe("The server could not be reached."),
  );
  // The panel is not blanked by the failed refetch: the policy form (still "ordinary" locally,
  // since the refetch that would have moved it never landed) is still on screen beside the error.
  // The applied target was "protected", so setPolicy's own `note_write` is false: the panel shows
  // the read-only sentence, not a stale claim left over from before the apply.
  expect(
    screen.getByText(
      "VIKI edits notes only in a vault it created, so this folder stays read-only in the Vault view.",
    ),
  ).toBeTruthy();
});
