import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ShellApiError } from "../../clients/app/shellApi";
import { fakeShellApi } from "../../testShell";
import { FolderPickerProvider } from "./FolderPicker";
import { PathForm } from "./PathForm";

afterEach(cleanup);

/** Settles the microtasks a picker call and its state updates need, inside `act` so React never
 * warns about an update outside it (VaultForms.unit.test.tsx's own helper, copied here for the
 * same reason that file has it: a chain of plain awaits, not a timer). */
async function flush(): Promise<void> {
  await act(async () => {
    for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
  });
}

describe("PathForm", () => {
  it("nothing submits natively: Enter calls the handler, not a form submit event", () => {
    const onEnter = vi.fn();
    const submitSpy = vi.fn();
    render(
      <div onSubmitCapture={submitSpy}>
        <PathForm id="p" label="Path" value="" onChange={() => {}} onEnter={onEnter} />
      </div>,
    );
    fireEvent.keyDown(screen.getByLabelText("Path"), { key: "Enter", keyCode: 13 });
    expect(onEnter).toHaveBeenCalledTimes(1);
    expect(submitSpy).not.toHaveBeenCalled();
  });

  it("does not call onEnter while composing (IME guard)", () => {
    const onEnter = vi.fn();
    render(<PathForm id="p" label="Path" value="" onChange={() => {}} onEnter={onEnter} />);
    fireEvent.keyDown(screen.getByLabelText("Path"), {
      key: "Enter",
      keyCode: 229,
      isComposing: true,
    });
    expect(onEnter).not.toHaveBeenCalled();
  });

  it("reports every change through onChange", () => {
    const onChange = vi.fn();
    render(<PathForm id="p" label="Path" value="" onChange={onChange} onEnter={() => {}} />);
    fireEvent.change(screen.getByLabelText("Path"), { target: { value: "D:\\x" } });
    expect(onChange).toHaveBeenCalledWith("D:\\x");
  });

  it("shows the example line and points aria-describedby at it when there is no error", () => {
    render(
      <PathForm
        id="p"
        label="Path"
        value=""
        onChange={() => {}}
        onEnter={() => {}}
        exampleText={"D:\\VIKI\\Vaults\\My notes"}
      />,
    );
    const input = screen.getByLabelText("Path");
    expect(input.getAttribute("aria-describedby")).toBe("p-example");
    screen.getByText("D:\\VIKI\\Vaults\\My notes"); // throws if absent
  });

  it("an error line is role=alert and wins aria-describedby over the example", () => {
    render(
      <PathForm
        id="p"
        label="Path"
        value=""
        onChange={() => {}}
        onEnter={() => {}}
        exampleText={"D:\\VIKI\\Vaults\\My notes"}
        error="Not a usable path."
      />,
    );
    const input = screen.getByLabelText("Path");
    expect(input.getAttribute("aria-describedby")).toBe("p-error");
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByRole("alert").textContent).toBe("Not a usable path.");
  });

  it("never uses a browse action: no file/directory input anywhere", () => {
    const { container } = render(
      <PathForm id="p" label="Path" value="" onChange={() => {}} onEnter={() => {}} />,
    );
    expect(container.querySelector("input[webkitdirectory]")).toBeNull();
    expect(container.querySelector('input[type="file"]')).toBeNull();
  });

  it("browse without an available picker leaves the markup unchanged", async () => {
    const { unmount } = render(
      <PathForm id="p" label="Path" value="" onChange={() => {}} onEnter={() => {}} browse />,
    );
    expect(screen.queryByRole("button", { name: "Browse…" })).toBeNull();
    unmount();

    const api = fakeShellApi({
      picker: { available: false, multiple: false, reason: "no_display" },
    });
    render(
      <FolderPickerProvider api={api}>
        <PathForm id="p2" label="Path" value="" onChange={() => {}} onEnter={() => {}} browse />
      </FolderPickerProvider>,
    );
    await flush();
    expect(screen.queryByRole("button", { name: "Browse…" })).toBeNull();
  });

  it("Browse… fills the field", async () => {
    const onChange = vi.fn();
    const api = fakeShellApi({
      picker: { available: true, multiple: false, reason: null },
      picked: ["D:\\VIKI\\Vaults\\My notes"],
    });
    render(
      <FolderPickerProvider api={api}>
        <PathForm id="p" label="Path" value="" onChange={onChange} onEnter={() => {}} browse />
      </FolderPickerProvider>,
    );
    const button = await screen.findByRole("button", { name: "Browse…" });
    fireEvent.click(button);
    await flush();
    expect(onChange).toHaveBeenCalledWith("D:\\VIKI\\Vaults\\My notes");
    // title = label, single pick.
    expect(api.calls).toContainEqual({ method: "pickFolders", args: [false, "Path"] });
  });

  it("cancel (an empty pick) leaves the field untouched", async () => {
    const onChange = vi.fn();
    const api = fakeShellApi({
      picker: { available: true, multiple: false, reason: null },
      picked: [],
    });
    render(
      <FolderPickerProvider api={api}>
        <PathForm id="p" label="Path" value="" onChange={onChange} onEnter={() => {}} browse />
      </FolderPickerProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Browse…" }));
    await flush();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("a picker failure shows its own field-error line via errorText", async () => {
    const api = fakeShellApi({ picker: { available: true, multiple: false, reason: null } });
    api.fail("pickFolders", new ShellApiError(503, "picker_failed"));
    render(
      <FolderPickerProvider api={api}>
        <PathForm id="p" label="Path" value="" onChange={() => {}} onEnter={() => {}} browse />
      </FolderPickerProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Browse…" }));
    await flush();
    expect(screen.getByRole("alert").textContent).toBe(
      "The folder picker couldn't open. Type the path instead.",
    );
  });

  it("aria-disabled on Browse… while a pick is in flight", async () => {
    const api = fakeShellApi({ picker: { available: true, multiple: false, reason: null } });
    render(
      <FolderPickerProvider api={api}>
        <PathForm id="p" label="Path" value="" onChange={() => {}} onEnter={() => {}} browse />
      </FolderPickerProvider>,
    );
    const button = await screen.findByRole("button", { name: "Browse…" });
    fireEvent.click(button);
    expect(button.getAttribute("aria-disabled")).toBe("true");
    await flush();
    expect(button.getAttribute("aria-disabled")).toBe("false");
  });
});
