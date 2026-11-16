/**
 * Tests for `ImportPanel`.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createRef, useState } from "react";
import { afterEach, expect, test, vi } from "vitest";
import type { ShellApi } from "../../clients/app/shellApi";
import { ShellApiError } from "../../clients/app/shellApi";
import { t } from "../../i18n/t";
import { createShellStore } from "../../state/shell";
import { AppStore } from "../../state/store";
import {
  deferredShellApi,
  fakeShellApi,
  importPreviewFixture,
  settingsFixture,
} from "../../testShell";
import { ReviewPanel } from "../Settings/ReviewPanel";
import { ImportPanel } from "./ImportPanel";

afterEach(cleanup);

function mount(api: ShellApi | null) {
  const headingRef = createRef<HTMLHeadingElement>();
  const store = createShellStore(api, new AppStore());
  const conflicts: ShellApiError[] = [];
  const renderConflict = (error: ShellApiError) => {
    conflicts.push(error);
    return <p role="dialog">conflict: {error.code}</p>;
  };
  return {
    ...render(
      <ImportPanel
        api={api}
        store={store}
        renderConflict={renderConflict}
        headingRef={headingRef}
      />,
    ),
    conflicts,
    store,
  };
}

function previewButton(): HTMLElement {
  return screen.getByRole("button", { name: "Preview" });
}

test("a file picked through the input lands in the textarea at its full 262,144-byte size", async () => {
  const api = fakeShellApi();
  mount(api);
  const content = "a".repeat(262_144);
  const file = new File([content], "settings.json", { type: "application/json" });
  const input = screen.getByLabelText("Choose file") as HTMLInputElement;
  await act(async () => {
    Object.defineProperty(input, "files", { value: [file], configurable: true });
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await waitFor(() => {
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value.length).toBe(262_144);
  });
});

test("a file bigger than the read cap is refused before it is ever decoded", async () => {
  const api = fakeShellApi();
  mount(api);
  const file = new File(["a".repeat(1_048_577)], "huge.json", { type: "application/json" });
  const input = screen.getByLabelText("Choose file") as HTMLInputElement;
  await act(async () => {
    Object.defineProperty(input, "files", { value: [file], configurable: true });
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
  expect(screen.getByRole("alert").textContent).toContain("The request is too large.");
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
});

test("a file that fails to read reports a failure instead of leaving the panel silent", async () => {
  const api = fakeShellApi();
  mount(api);
  // jsdom's FileReader never fails on its own, so the failure is forced the same way a locked
  // file or an unhydrated cloud placeholder would fail in the real browser: `onerror` fires,
  // `onload` never does.
  const originalReadAsText = FileReader.prototype.readAsText;
  FileReader.prototype.readAsText = function stubReadAsText(this: FileReader) {
    const event = new ProgressEvent("error") as unknown as ProgressEvent<FileReader>;
    queueMicrotask(() => this.onerror?.call(this, event));
  };
  try {
    const file = new File(["{}"], "settings.json", { type: "application/json" });
    const input = screen.getByLabelText("Choose file") as HTMLInputElement;
    await act(async () => {
      Object.defineProperty(input, "files", { value: [file], configurable: true });
      input.dispatchEvent(new Event("change", { bubbles: true }));
      await Promise.resolve();
    });
    expect(screen.getByRole("alert").textContent).toContain(
      "The operation could not be completed.",
    );
  } finally {
    FileReader.prototype.readAsText = originalReadAsText;
  }
});

test("picking a new file clears an earlier ready preview instead of leaving it on screen", async () => {
  const api = fakeShellApi({
    importPreview: importPreviewFixture({
      changes: [
        { key: "appearance.density", current: "comfortable", incoming: "compact", action: "apply" },
      ],
    }),
  });
  mount(api);
  await act(async () => {
    previewButton().click();
    await Promise.resolve();
  });
  expect(screen.getByRole("button", { name: "Apply" })).toBeTruthy();

  const file = new File(["{}"], "settings.json", { type: "application/json" });
  const input = screen.getByLabelText("Choose file") as HTMLInputElement;
  await act(async () => {
    Object.defineProperty(input, "files", { value: [file], configurable: true });
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
  // Cleared synchronously by the pick itself, before the new file has even finished reading.
  expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
});

test("edited import cannot apply its old preview", async () => {
  const api = fakeShellApi();
  mount(api);
  await act(async () => {
    previewButton().click();
  });
  expect(screen.getByRole("button", { name: "Apply" })).toBeTruthy();

  fireEvent.change(screen.getByRole("textbox"), { target: { value: '{"edited":true}' } });
  expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
  expect(api.calls.filter((call) => call.method === "importApply")).toHaveLength(0);

  api.data.importPreview = importPreviewFixture({ preview_id: "fresh-preview" });
  await act(async () => {
    previewButton().click();
  });
  expect(api.calls.filter((call) => call.method === "importPreview").at(-1)?.args).toEqual([
    '{"edited":true}',
  ]);
  await act(async () => {
    screen.getByRole("button", { name: "Apply" }).click();
  });
  expect(api.calls.find((call) => call.method === "importApply")?.args[0]).toBe("fresh-preview");
});

test.each(["resolve", "reject"] as const)(
  "obsolete import preview %s is ignored",
  async (outcome) => {
    const api = deferredShellApi();
    mount(api);
    await act(async () => previewButton().click());
    const obsolete = api.pending[0];
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "new content" } });
    await act(async () => {
      if (outcome === "resolve") obsolete?.resolve(importPreviewFixture());
      else obsolete?.reject(new ShellApiError(503, "unavailable"));
    });
    expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();

    await act(async () => {
      previewButton().click();
      previewButton().click();
    });
    await act(async () => api.pending[2]?.resolve(importPreviewFixture({ preview_id: "latest" })));
    await act(async () => api.pending[1]?.resolve(importPreviewFixture({ preview_id: "older" })));
    await act(async () => screen.getByRole("button", { name: "Apply" }).click());
    expect(api.pending.find((call) => call.method === "importApply")?.args[0]).toBe("latest");
  },
);

test("obsolete file callbacks cannot replace edited import content or its preview", async () => {
  const api = fakeShellApi();
  mount(api);
  const readers: FileReader[] = [];
  const originalReadAsText = FileReader.prototype.readAsText;
  FileReader.prototype.readAsText = function stubReadAsText(this: FileReader) {
    readers.push(this);
  };
  try {
    const input = screen.getByLabelText("Choose file") as HTMLInputElement;
    for (const content of ["old file", "new file"]) {
      fireEvent.change(input, { target: { files: [new File([content], "settings.json")] } });
    }
    const [old, current] = readers;
    await act(async () => {
      Object.defineProperty(current, "result", { value: "new file" });
      current?.onload?.call(current, new ProgressEvent("load") as ProgressEvent<FileReader>);
    });
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("new file");
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "edited content" } });
    await act(async () => previewButton().click());
    await act(async () => {
      Object.defineProperty(old, "result", { value: "old file" });
      old?.onload?.call(old, new ProgressEvent("load") as ProgressEvent<FileReader>);
      old?.onerror?.call(old, new ProgressEvent("error") as ProgressEvent<FileReader>);
    });
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("edited content");
    expect(screen.getByRole("button", { name: "Apply" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  } finally {
    FileReader.prototype.readAsText = originalReadAsText;
  }
});

test("apply rows are checked by default; other actions render read-only", async () => {
  const api = fakeShellApi({
    importPreview: importPreviewFixture({
      changes: [
        { key: "appearance.density", current: "comfortable", incoming: "compact", action: "apply" },
        { key: "appearance.theme", current: "dark", incoming: "dark", action: "unchanged" },
        { key: "old.key", current: null, incoming: "x", action: "unknown_key" },
        { key: "bad.key", current: "x", incoming: "???", action: "invalid_value" },
        { key: "conflict.key", current: "a", incoming: "b", action: "conflict" },
      ],
    }),
  });
  mount(api);
  await act(async () => {
    previewButton().click();
    await Promise.resolve();
  });
  const checkboxes = screen.getAllByRole("checkbox") as HTMLInputElement[];
  expect(checkboxes).toHaveLength(1);
  expect(checkboxes[0]?.checked).toBe(true);
  expect(screen.getByText("Unchanged")).toBeTruthy();
  expect(screen.getByText("Unknown setting")).toBeTruthy();
  expect(screen.getByText("Invalid value")).toBeTruthy();
  expect(screen.getByText("Conflict")).toBeTruthy();
  expect(screen.getByText("a")).toBeTruthy();
  expect(screen.getByText("b")).toBeTruthy();
});

test("a newer-format preview hides Apply and shows the unsupported sentence", async () => {
  const api = fakeShellApi({
    importPreview: importPreviewFixture({ supported: false, unsupported_reason: "newer_format" }),
  });
  mount(api);
  await act(async () => {
    previewButton().click();
    await Promise.resolve();
  });
  expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
  expect(screen.getByRole("alert").textContent).toContain(
    "Written by a newer version; cannot be applied.",
  );
});

test("workspace rows need revalidation, executable definitions are disabled, duplicates are listed", async () => {
  const api = fakeShellApi({
    importPreview: importPreviewFixture({
      workspaces: [
        { name: "My notes", display_root: "D:\\X", policy: "ordinary", action: "revalidate" },
      ],
      executable_definitions: [{ id: "cmd1" }],
      duplicate_ids: ["dup-1"],
    }),
  });
  mount(api);
  await act(async () => {
    previewButton().click();
    await Promise.resolve();
  });
  expect(screen.getByText(/re-validation|revalidat/i, { exact: false })).toBeTruthy();
  expect(screen.getByText("Disabled: not imported.")).toBeTruthy();
  expect(screen.getByText("dup-1")).toBeTruthy();
});

test("apply sends only the checked apply keys with the preview revision, then adopts the result", async () => {
  const api = fakeShellApi({
    settings: { ...fakeShellApi().data.settings, revision: 7 },
    importPreview: importPreviewFixture({
      settings_revision: 7,
      changes: [
        { key: "appearance.density", current: "comfortable", incoming: "compact", action: "apply" },
        { key: "appearance.theme", current: "system", incoming: "dark", action: "apply" },
      ],
    }),
  });
  const { store } = mount(api);
  // The preview owns its reviewed revision; the shell's launch read agrees with it here.
  await act(async () => {
    await store.refreshSettings();
  });
  await act(async () => {
    previewButton().click();
    await Promise.resolve();
  });
  const checkboxes = screen.getAllByRole("checkbox") as HTMLInputElement[];
  await act(async () => {
    checkboxes[1]?.click();
  });
  await act(async () => {
    screen.getByRole("button", { name: "Apply" }).click();
    await Promise.resolve();
  });
  const call = api.calls.find((c) => c.method === "importApply");
  expect(call?.args[0]).toBe(api.data.importPreview.preview_id);
  expect(call?.args[2]).toBe(7);
  expect(call?.args[3]).toEqual(["appearance.density"]);
});

test("apply succeeds: a role=status success line shows and Apply is no longer offered", async () => {
  const api = fakeShellApi({
    importPreview: importPreviewFixture({
      changes: [
        { key: "appearance.density", current: "comfortable", incoming: "compact", action: "apply" },
      ],
    }),
  });
  mount(api);
  await act(async () => {
    previewButton().click();
    await Promise.resolve();
  });
  await act(async () => {
    screen.getByRole("button", { name: "Apply" }).click();
    await Promise.resolve();
  });
  expect(screen.getByRole("status").textContent).toBe("Import applied.");
  expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
});

test("two clicks on Apply while the first is still in flight send exactly one importApply call", async () => {
  const api = deferredShellApi();
  mount(api);
  await act(async () => {
    previewButton().click();
  });
  const previewCall = api.pending.find((p) => p.method === "importPreview");
  await act(async () => {
    previewCall?.resolve(
      importPreviewFixture({
        changes: [
          {
            key: "appearance.density",
            current: "comfortable",
            incoming: "compact",
            action: "apply",
          },
        ],
      }),
    );
  });
  const applyButton = screen.getByRole("button", { name: "Apply" });
  await act(async () => {
    applyButton.click();
    applyButton.click();
  });
  expect(api.pending.filter((p) => p.method === "importApply")).toHaveLength(1);
});

test.each(["success", "failure", "conflict"] as const)(
  "submitted import keeps its reviewed content and settled %s visible",
  async (outcome) => {
    const read = vi.spyOn(FileReader.prototype, "readAsText").mockImplementation(() => {});
    try {
      const api = deferredShellApi();
      const { store, conflicts } = mount(api);
      const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
      const file = screen.getByLabelText("Choose file") as HTMLInputElement;
      fireEvent.change(textarea, { target: { value: "reviewed content" } });
      await act(async () => previewButton().click());
      await act(async () =>
        api.pending[0]?.resolve(importPreviewFixture({ preview_id: "reviewed" })),
      );
      const apply = screen.getByRole("button", { name: "Apply" }) as HTMLButtonElement;
      const checkbox = screen.getByRole("checkbox") as HTMLInputElement;
      await act(async () => {
        apply.click();
        fireEvent.change(textarea, { target: { value: "unreviewed edit" } });
        fireEvent.change(file, { target: { files: [new File(["new file"], "settings.json")] } });
        previewButton().click();
        checkbox.click();
      });
      expect(textarea.value).toBe("reviewed content");
      expect(checkbox.checked).toBe(true);
      for (const control of [textarea, file, previewButton(), checkbox, apply]) {
        expect((control as HTMLInputElement).disabled).toBe(true);
      }
      expect(apply.getAttribute("aria-busy")).toBe("true");
      expect(screen.getByRole("status").textContent).toMatch(/Loading/);
      expect(read).not.toHaveBeenCalled();
      expect(api.pending.filter((call) => call.method === "importPreview")).toHaveLength(1);
      const submitted = api.pending.find((call) => call.method === "importApply");
      expect(submitted?.args[0]).toBe("reviewed");
      expect(submitted?.args[3]).toEqual(["appearance.density"]);

      await act(async () => {
        if (outcome === "success") {
          submitted?.resolve({ ...fakeShellApi().data.settings, revision: 17 });
        } else {
          submitted?.reject(
            new ShellApiError(409, outcome === "conflict" ? "stale_revision" : "unavailable"),
          );
        }
      });
      expect(textarea.value).toBe("reviewed content");
      for (const control of [
        textarea,
        file,
        previewButton(),
        ...(outcome === "conflict" ? [] : [checkbox]),
      ]) {
        expect((control as HTMLInputElement).disabled).toBe(false);
      }
      if (outcome === "success") {
        expect(store.getSnapshot().settings?.revision).toBe(17);
        expect(screen.getByRole("status").textContent).toBe("Import applied.");
        expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
      } else if (outcome === "conflict") {
        expect(conflicts).toHaveLength(1);
        expect(screen.getByRole("dialog").textContent).toContain("stale_revision");
        expect(screen.queryByRole("checkbox")).toBeNull();
        expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
      } else {
        expect(screen.getByRole("alert").textContent).toContain(
          "The operation could not be completed.",
        );
      }
      expect(api.pending.filter((call) => call.method === "importApply")).toHaveLength(1);

      fireEvent.change(textarea, { target: { value: "next content" } });
      expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
      expect(screen.queryByRole("status")).toBeNull();
      expect(screen.queryByRole("alert")).toBeNull();
      expect(screen.queryByRole("dialog")).toBeNull();
      await act(async () => previewButton().click());
      const fresh = api.pending.filter((call) => call.method === "importPreview").at(-1);
      expect(fresh?.args).toEqual(["next content"]);
      await act(async () =>
        fresh?.resolve(
          importPreviewFixture({
            preview_id: "fresh",
            settings_revision: outcome === "success" ? 17 : 3,
          }),
        ),
      );
      const freshApply = screen.getByRole("button", { name: "Apply" }) as HTMLButtonElement;
      expect(freshApply.disabled).toBe(false);
      expect(freshApply.getAttribute("aria-busy")).toBe("false");
      expect(screen.queryByRole("status")).toBeNull();
    } finally {
      read.mockRestore();
    }
  },
);

test("a stale-revision refusal on apply is handed to renderConflict", async () => {
  const api = fakeShellApi({ importPreview: importPreviewFixture() });
  const { conflicts } = mount(api);
  await act(async () => {
    previewButton().click();
    await Promise.resolve();
  });
  api.fail("importApply", new ShellApiError(409, "stale_revision"));
  await act(async () => {
    screen.getByRole("button", { name: "Apply" }).click();
    await Promise.resolve();
  });
  expect(conflicts).toHaveLength(1);
  expect(screen.getByRole("dialog")).toBeTruthy();
});

test("apply uses the server preview revision before the shell has read settings", async () => {
  const api = fakeShellApi({
    settings: settingsFixture({}, { revision: 7 }),
    importPreview: importPreviewFixture({ settings_revision: 7 }),
  });
  const { store } = mount(api);
  expect(store.getSnapshot().settings).toBeNull();
  await act(async () => previewButton().click());
  await act(async () => screen.getByRole("button", { name: "Apply" }).click());
  expect(api.calls.find((call) => call.method === "importApply")?.args[2]).toBe(7);
  expect(store.getSnapshot().settings?.revision).toBe(8);
  expect(screen.getByRole("status").textContent).toBe("Import applied.");
});

test("an observed later revision discards the old selection before a same-tick Apply", async () => {
  const api = fakeShellApi();
  const { store } = mount(api);
  const content = '{"settings":{"values":{"appearance.density":"compact"}}}';
  fireEvent.change(screen.getByRole("textbox"), { target: { value: content } });
  await act(async () => store.refreshSettings());
  await act(async () => previewButton().click());
  const oldApply = screen.getByRole("button", { name: "Apply" });
  api.data.settings = settingsFixture({ "appearance.theme": "light" }, { revision: 4 });
  await act(async () => {
    store.adoptSettings(api.data.settings);
    oldApply.click();
  });
  expect(api.calls.filter((call) => call.method === "importApply")).toHaveLength(0);
  expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
  expect(screen.queryByRole("checkbox")).toBeNull();
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(content);

  api.data.importPreview = importPreviewFixture({
    preview_id: "fresh-preview",
    settings_revision: 4,
  });
  await act(async () => previewButton().click());
  expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(true);
  await act(async () => screen.getByRole("button", { name: "Apply" }).click());
  expect(api.calls.find((call) => call.method === "importApply")?.args.slice(0, 3)).toEqual([
    "fresh-preview",
    expect.any(String),
    4,
  ]);
  expect(store.getSnapshot().settings?.revision).toBe(5);
  expect(store.getSnapshot().settings?.effective["appearance.theme"]).toBe("light");
  expect(screen.getByRole("status").textContent).toBe("Import applied.");
});

test("a fresh server preview can apply while the shell still holds an older settings read", async () => {
  const api = fakeShellApi();
  const { store } = mount(api);
  await act(async () => store.refreshSettings());
  expect(store.getSnapshot().settings?.revision).toBe(3);
  api.data.settings = settingsFixture({}, { revision: 4 });
  api.data.importPreview = importPreviewFixture({ settings_revision: 4 });
  await act(async () => previewButton().click());
  expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(true);
  await act(async () => screen.getByRole("button", { name: "Apply" }).click());
  expect(api.calls.find((call) => call.method === "importApply")?.args[2]).toBe(4);
  expect(store.getSnapshot().settings?.revision).toBe(5);
  expect(screen.getByRole("status").textContent).toBe("Import applied.");
});

test("conflict reload requires a new preview and keeps a newly conflicting setting read-only", async () => {
  const initial = settingsFixture({}, { revision: 0 });
  const current = settingsFixture({ "appearance.theme": "light" }, { revision: 1 });
  const api = fakeShellApi({
    settings: initial,
    importPreview: importPreviewFixture({
      settings_revision: 0,
      changes: [
        { key: "appearance.theme", current: "system", incoming: "dark", action: "apply" },
        { key: "privacy.hide_previews", current: false, incoming: true, action: "apply" },
      ],
    }),
  });
  const store = createShellStore(api, new AppStore());
  function Host() {
    const [dismissed, setDismissed] = useState<ShellApiError | null>(null);
    return (
      <ImportPanel
        api={api}
        store={store}
        headingRef={createRef()}
        renderConflict={(error) =>
          error === dismissed ? null : (
            <ReviewPanel
              error={error}
              changes={{}}
              onReload={() => {
                if (error.current === null) void store.refreshSettings();
                else store.adoptSettings(error.current);
                setDismissed(error);
              }}
              onClose={() => setDismissed(error)}
            />
          )
        }
      />
    );
  }
  render(<Host />);
  const content = JSON.stringify({
    viki_export: { kind: "settings", format_version: 1 },
    settings: { values: { "appearance.theme": "dark", "privacy.hide_previews": true } },
  });
  fireEvent.change(screen.getByRole("textbox"), { target: { value: content } });
  await act(async () => store.refreshSettings());
  await act(async () => previewButton().click());
  api.data.settings = current;
  api.fail("importApply", new ShellApiError(409, "stale_revision", { current }));
  await act(async () => screen.getByRole("button", { name: "Apply" }).click());
  expect(screen.getByRole("dialog")).toBeTruthy();
  expect(screen.queryByRole("checkbox")).toBeNull();
  expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
  await act(async () =>
    screen.getByRole("button", { name: t("settings.review.reload_and_reenter") }).click(),
  );
  expect(store.getSnapshot().settings?.revision).toBe(1);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
  expect(api.calls.filter((call) => call.method === "importApply")).toHaveLength(1);
  expect(api.calls.filter((call) => call.method === "importPreview")).toHaveLength(1);
  expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(content);

  api.data.importPreview = importPreviewFixture({
    preview_id: "fresh-preview",
    settings_revision: 1,
    changes: [
      { key: "appearance.theme", current: "light", incoming: "dark", action: "conflict" },
      { key: "privacy.hide_previews", current: false, incoming: true, action: "apply" },
    ],
  });
  await act(async () => previewButton().click());
  expect(screen.getByText("Conflict")).toBeTruthy();
  expect(screen.getAllByRole("checkbox")).toHaveLength(1);
  await act(async () => screen.getByRole("button", { name: "Apply" }).click());
  expect(
    api.calls.filter((call) => call.method === "importApply").map((call) => call.args[2]),
  ).toEqual([0, 1]);
  expect(api.calls.filter((call) => call.method === "importApply").at(-1)?.args[3]).toEqual([
    "privacy.hide_previews",
  ]);
  expect(store.getSnapshot().settings?.effective["appearance.theme"]).toBe("light");
  expect(store.getSnapshot().settings?.effective["privacy.hide_previews"]).toBe(true);
});
