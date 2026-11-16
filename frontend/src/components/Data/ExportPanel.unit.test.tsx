/**
 * `ExportPanel`. Button queries use `getByRole` because
 * the panel's own `<h2>` heading text is "Export", the same word as the write button's label.
 */
import { act, cleanup, render, screen, within } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, expect, test, vi } from "vitest";
import type { ShellApi } from "../../clients/app/shellApi";
import { ShellApiError } from "../../clients/app/shellApi";
import { deferredShellApi, exportPreviewFixture, fakeShellApi } from "../../testShell";
import { ExportPanel, type PathFieldProps } from "./ExportPanel";

afterEach(cleanup);

function stubPathField(props: PathFieldProps) {
  return (
    <div>
      <label htmlFor={props.id}>{props.label}</label>
      <input id={props.id} value={props.value} onChange={(e) => props.onChange(e.target.value)} />
      {props.error && <span role="alert">{props.error}</span>}
    </div>
  );
}

function mount(api: ShellApi | null) {
  const headingRef = createRef<HTMLHeadingElement>();
  return render(<ExportPanel api={api} renderPathField={stubPathField} headingRef={headingRef} />);
}

function preview(): HTMLElement {
  return screen.getByRole("button", { name: "Preview" });
}

function writeButton(): HTMLElement | null {
  return screen.queryByRole("button", { name: "Export" });
}

/** `navigator.clipboard` is not a real property in jsdom; each test that stubs it must restore
 * the original afterward, or a later test in this file silently inherits a stale mock. */
function stubClipboard(value: Pick<Clipboard, "writeText"> | undefined): () => void {
  const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", { value, configurable: true });
  return () => {
    if (original) Object.defineProperty(navigator, "clipboard", original);
    else Reflect.deleteProperty(navigator, "clipboard");
  };
}

test("include flags start off; the destination and write button appear only after a preview", async () => {
  const api = fakeShellApi({ exportPreview: exportPreviewFixture() });
  mount(api);
  const checkboxes = screen.getAllByRole("checkbox") as HTMLInputElement[];
  for (const box of checkboxes) expect(box.checked).toBe(false);
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(writeButton()).toBeNull();

  await act(async () => {
    preview().click();
    await Promise.resolve();
  });
  expect(api.calls.find((c) => c.method === "exportPreview")?.args).toEqual([
    "diagnostics",
    { paths: false, identity: false, log_tail: false, settings: false, registrations: false },
  ]);
  expect(screen.getByRole("textbox")).toBeTruthy();
  expect(writeButton()).toBeTruthy();
});

test("the preview's content renders as text, and a settings-kind export drops the diagnostics-only flags", async () => {
  const api = fakeShellApi({
    exportPreview: exportPreviewFixture({ content: { "settings.json": '{"a":1}' } }),
  });
  mount(api);
  await act(async () => {
    screen.getByLabelText("Settings").click();
  });
  await act(async () => {
    preview().click();
    await Promise.resolve();
  });
  expect(api.calls.find((c) => c.method === "exportPreview")?.args).toEqual([
    "settings",
    { paths: false, identity: false },
  ]);
  expect(screen.getByText('{"a":1}').tagName).toBe("PRE");
});

test.each(["flags", "kind", "repeat"] as const)(
  "obsolete export preview is ignored after %s changes",
  async (change) => {
    const api = deferredShellApi();
    mount(api);
    await act(async () => preview().click());
    const obsolete = api.pending[0];
    await act(async () => {
      if (change === "flags") screen.getByLabelText("Folder paths").click();
      if (change === "kind") screen.getByLabelText("Settings").click();
    });
    await act(async () => preview().click());
    expect(api.pending[1]?.args[0]).toBe(change === "kind" ? "settings" : "diagnostics");
    expect(api.pending[1]?.args[1]).toMatchObject({ paths: change === "flags" });
    await act(async () =>
      api.pending[1]?.resolve(
        exportPreviewFixture({
          preview_id: "latest",
          content: { "fresh.json": "current preview" },
        }),
      ),
    );
    await act(async () => {
      if (change === "repeat") obsolete?.reject(new ShellApiError(503, "unavailable"));
      else obsolete?.resolve(exportPreviewFixture({ content: { "old.json": "obsolete preview" } }));
    });
    expect(screen.getByText("current preview")).toBeTruthy();
    expect(screen.queryByText("obsolete preview")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    await act(async () => writeButton()?.click());
    expect(api.pending.find((call) => call.method === "exportWrite")?.args[1]).toBe("latest");
  },
);

test("copy writes the preview's own text to the clipboard", async () => {
  const write = vi.fn().mockResolvedValue(undefined);
  const restore = stubClipboard({ writeText: write });
  const api = fakeShellApi({
    exportPreview: exportPreviewFixture({ content: { "x.json": "hello" } }),
  });
  mount(api);
  await act(async () => {
    preview().click();
    await Promise.resolve();
  });
  await act(async () => {
    screen.getByRole("button", { name: "Copy" }).click();
  });
  expect(write).toHaveBeenCalledWith("hello");
  restore();
});

test("a rejected clipboard write reports a failure instead of an unhandled rejection", async () => {
  const write = vi.fn().mockRejectedValue(new Error("denied"));
  const restore = stubClipboard({ writeText: write });
  const api = fakeShellApi({
    exportPreview: exportPreviewFixture({ content: { "x.json": "hello" } }),
  });
  mount(api);
  await act(async () => {
    preview().click();
    await Promise.resolve();
  });
  await act(async () => {
    screen.getByRole("button", { name: "Copy" }).click();
    await Promise.resolve();
  });
  expect(screen.getByRole("alert").textContent).toContain("The operation could not be completed.");
  restore();
});

test("copying with no clipboard API available reports a failure instead of throwing", async () => {
  const restore = stubClipboard(undefined);
  const api = fakeShellApi({
    exportPreview: exportPreviewFixture({ content: { "x.json": "hello" } }),
  });
  mount(api);
  await act(async () => {
    preview().click();
    await Promise.resolve();
  });
  await act(async () => {
    // If this throws, it throws here — synchronously out of the click — which is the defect
    // guarded against: `navigator.clipboard` is undefined and a handler that read `.writeText`
    // off it would throw.
    screen.getByRole("button", { name: "Copy" }).click();
  });
  expect(screen.getByRole("alert").textContent).toContain("The operation could not be completed.");
  restore();
});

test("exists names the destination; invalid_path is a field error on the path field", async () => {
  const api = fakeShellApi({ exportPreview: exportPreviewFixture() });
  mount(api);
  await act(async () => {
    preview().click();
    await Promise.resolve();
  });

  api.fail("exportWrite", new ShellApiError(409, "exists"));
  await act(async () => {
    (writeButton() as HTMLElement).click();
    await Promise.resolve();
  });
  expect(screen.getByRole("alert").textContent).toContain("A file already exists at that path.");

  api.fail("exportWrite", new ShellApiError(400, "invalid_path"));
  await act(async () => {
    (writeButton() as HTMLElement).click();
    await Promise.resolve();
  });
  expect(screen.getByText("Not a usable path.")).toBeTruthy();
});

test("preview_expired clears the stale preview so the user runs Preview again", async () => {
  const api = fakeShellApi({ exportPreview: exportPreviewFixture() });
  mount(api);
  await act(async () => {
    preview().click();
    await Promise.resolve();
  });
  expect(writeButton()).toBeTruthy();

  api.fail("exportWrite", new ShellApiError(404, "preview_expired"));
  await act(async () => {
    (writeButton() as HTMLElement).click();
    await Promise.resolve();
  });
  expect(writeButton()).toBeNull();
});

test("denied_destination explains the deny set", async () => {
  const api = fakeShellApi({ exportPreview: exportPreviewFixture() });
  mount(api);
  await act(async () => {
    preview().click();
    await Promise.resolve();
  });
  api.fail("exportWrite", new ShellApiError(403, "denied_destination"));
  await act(async () => {
    (writeButton() as HTMLElement).click();
    await Promise.resolve();
  });
  expect(screen.getByRole("alert").textContent).toContain(
    "Exports cannot be written inside a vault",
  );
});

test("without a shell api, preview shows the generic refusal instead of crashing", async () => {
  mount(null);
  await act(async () => {
    preview().click();
  });
  expect(screen.getByRole("alert").textContent).toContain("The operation could not be completed.");
});

test("the write result renders path, bytes and sha256 as text", async () => {
  const api = fakeShellApi({ exportPreview: exportPreviewFixture() });
  mount(api);
  await act(async () => {
    preview().click();
    await Promise.resolve();
  });
  await act(async () => {
    (writeButton() as HTMLElement).click();
    await Promise.resolve();
  });
  const result = within(document.querySelector(".data-export-result") as HTMLElement);
  expect(result.getByText("D:\\Exports\\viki-diagnostics.json")).toBeTruthy();
  expect(result.getByText("512 B")).toBeTruthy();
  expect(result.getByText("1".repeat(64))).toBeTruthy();
});

test("clicking write twice while the first is still in flight sends exactly one exportWrite call", async () => {
  const api = deferredShellApi();
  mount(api);
  await act(async () => {
    preview().click();
  });
  const previewCall = api.pending.find((p) => p.method === "exportPreview");
  await act(async () => {
    previewCall?.resolve(exportPreviewFixture());
  });
  const write = writeButton() as HTMLElement;
  await act(async () => {
    write.click();
    write.click();
  });
  expect(api.pending.filter((p) => p.method === "exportWrite")).toHaveLength(1);
});

test("a retry after a failed write reuses the same command id", async () => {
  const api = fakeShellApi({ exportPreview: exportPreviewFixture() });
  mount(api);
  await act(async () => {
    preview().click();
    await Promise.resolve();
  });
  api.fail("exportWrite", new ShellApiError(503, "unavailable"));
  await act(async () => {
    (writeButton() as HTMLElement).click();
    await Promise.resolve();
  });
  await act(async () => {
    (writeButton() as HTMLElement).click();
    await Promise.resolve();
  });
  const calls = api.calls.filter((c) => c.method === "exportWrite");
  expect(calls).toHaveLength(2);
  expect(calls[0]?.args[0]).toBe(calls[1]?.args[0]);
});
