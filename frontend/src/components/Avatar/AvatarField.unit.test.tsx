import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AvatarField } from "./AvatarField";
import { DEFAULT_IDENTITY, IdentityContext } from "./identity";

vi.mock("./AvatarEditor", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./AvatarEditor")>();
  return { ...actual, AvatarEditor: vi.fn(actual.AvatarEditor) };
});

afterEach(cleanup);

const FIXED_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";

function setButton(): HTMLElement {
  return screen.getByRole("button", { name: /^set$|^replace$/i });
}

function resetButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: /reset/i }) as HTMLButtonElement;
}

describe("AvatarField", () => {
  it("uses an optional fallback and shows empty text only for an unset avatar", () => {
    const { container, rerender } = render(
      <AvatarField
        label="Avatar"
        name="Ada"
        accent="slate"
        shape="circle"
        avatarId={null}
        disabled={false}
        fallback={false}
        emptyText="Not set"
        onChange={vi.fn()}
      />,
    );
    const preview = container.querySelector(".avatar-field-preview");
    expect(preview?.textContent).toBe("Not set");
    expect(preview?.querySelector("[role='img']")).toBeNull();
    expect(preview?.querySelector(".avatar-initials")).toBeNull();

    const urls = {
      acquire: vi.fn(() => () => {}),
      get: vi.fn(() => "blob:avatar"),
      dispose: vi.fn(),
    };
    rerender(
      <IdentityContext.Provider value={{ ...DEFAULT_IDENTITY, urls }}>
        <AvatarField
          label="Avatar"
          name="Ada"
          accent="slate"
          shape="circle"
          avatarId={FIXED_UUID}
          disabled={false}
          fallback={false}
          emptyText="Not set"
          onChange={vi.fn()}
        />
      </IdentityContext.Provider>,
    );
    expect(container.querySelector(".avatar-field-preview")?.textContent).toBe("");
    expect(container.querySelector(".avatar-field-preview img")).not.toBeNull();

    rerender(
      <AvatarField
        label="Avatar"
        name="Ada"
        accent="slate"
        shape="circle"
        avatarId={null}
        disabled={false}
        fallback={false}
        emptyText="Not set"
        onChange={vi.fn()}
      />,
    );
    expect(container.querySelector(".avatar-field-empty")?.textContent).toBe("Not set");
  });

  it("keeps the default initial when callers omit the optional preview props", () => {
    const { container } = render(
      <AvatarField
        label="Avatar"
        name="Ada"
        accent="slate"
        shape="circle"
        avatarId={null}
        disabled={false}
        onChange={vi.fn()}
      />,
    );
    expect(container.querySelector(".avatar-field .avatar-initials")?.textContent).toBe("A");
    expect(container.querySelector(".avatar-field-empty")).toBeNull();
  });

  it("Set opens the real editor's dialog; its Cancel closes it with no onChange", async () => {
    const onChange = vi.fn();
    render(
      <AvatarField
        label="Avatar"
        name="Ada"
        accent="slate"
        shape="circle"
        avatarId={null}
        disabled={false}
        onChange={onChange}
      />,
    );
    fireEvent.click(setButton());
    await screen.findByRole("dialog");

    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(onChange).not.toHaveBeenCalled();
  });

  it("a saved editor sends the id to onChange and closes", async () => {
    const { AvatarEditor } = await import("./AvatarEditor");
    vi.mocked(AvatarEditor).mockImplementationOnce(({ onSaved }) => {
      useEffect(() => {
        onSaved(FIXED_UUID);
      }, [onSaved]);
      return <div data-testid="stub-editor" />;
    });
    const onChange = vi.fn();
    render(
      <AvatarField
        label="Avatar"
        name="Ada"
        accent="slate"
        shape="circle"
        avatarId={null}
        disabled={false}
        onChange={onChange}
      />,
    );
    fireEvent.click(setButton());

    await waitFor(() => expect(onChange).toHaveBeenCalledWith(FIXED_UUID));
    expect(screen.queryByTestId("stub-editor")).toBeNull();
  });

  it("Reset sends null", () => {
    const onChange = vi.fn();
    render(
      <AvatarField
        label="Avatar"
        name="Ada"
        accent="slate"
        shape="circle"
        avatarId={FIXED_UUID}
        disabled={false}
        onChange={onChange}
      />,
    );
    fireEvent.click(resetButton());
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("the label switches between Set and Replace", () => {
    const { rerender } = render(
      <AvatarField
        label="Avatar"
        name="Ada"
        accent="slate"
        shape="circle"
        avatarId={null}
        disabled={false}
        onChange={vi.fn()}
      />,
    );
    screen.getByRole("button", { name: /^set$/i });

    rerender(
      <AvatarField
        label="Avatar"
        name="Ada"
        accent="slate"
        shape="circle"
        avatarId={FIXED_UUID}
        disabled={false}
        onChange={vi.fn()}
      />,
    );
    screen.getByRole("button", { name: /^replace$/i });
  });

  it("disabled disables both buttons", () => {
    render(
      <AvatarField
        label="Avatar"
        name="Ada"
        accent="slate"
        shape="circle"
        avatarId={FIXED_UUID}
        disabled={true}
        onChange={vi.fn()}
      />,
    );
    expect((setButton() as HTMLButtonElement).disabled).toBe(true);
    expect(resetButton().disabled).toBe(true);
  });

  it("Reset is disabled when there is no avatar", () => {
    render(
      <AvatarField
        label="Avatar"
        name="Ada"
        accent="slate"
        shape="circle"
        avatarId={null}
        disabled={false}
        onChange={vi.fn()}
      />,
    );
    expect(resetButton().disabled).toBe(true);
  });

  it("focus returns to Set after opening and cancelling the editor", async () => {
    render(
      <AvatarField
        label="Avatar"
        name="Ada"
        accent="slate"
        shape="circle"
        avatarId={null}
        disabled={false}
        onChange={vi.fn()}
      />,
    );
    const button = setButton();
    button.focus();
    fireEvent.click(button);
    await screen.findByRole("dialog");

    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(button);
  });

  it("renders its own label span when the caller gives none, and describes both buttons by it", () => {
    const { container } = render(
      <AvatarField
        label="Avatar"
        name="Ada"
        accent="slate"
        shape="circle"
        avatarId={null}
        disabled={false}
        onChange={vi.fn()}
      />,
    );
    const label = container.querySelector(".avatar-field > span:not(.avatar)");
    expect(label?.textContent).toBe("Avatar");
    const labelId = label?.id;
    expect(labelId).toBeTruthy();
    expect(setButton().getAttribute("aria-describedby")).toBe(labelId);
    expect(resetButton().getAttribute("aria-describedby")).toBe(labelId);
  });

  it("renders no label span when the caller supplies labelId, and describes both buttons by it", () => {
    const { container } = render(
      <AvatarField
        label="Avatar"
        labelId="external-label"
        name="Ada"
        accent="slate"
        shape="circle"
        avatarId={null}
        disabled={false}
        onChange={vi.fn()}
      />,
    );
    expect(container.querySelector(".avatar-field > span:not(.avatar)")).toBeNull();
    expect(setButton().getAttribute("aria-describedby")).toBe("external-label");
    expect(resetButton().getAttribute("aria-describedby")).toBe("external-label");
  });

  it("a read-only notice keeps both buttons focusable and their click a no-op", () => {
    const onChange = vi.fn();
    render(
      <AvatarField
        label="Avatar"
        name="Ada"
        accent="slate"
        shape="circle"
        avatarId={FIXED_UUID}
        disabled={false}
        readOnlyNoticeId="notice"
        onChange={onChange}
      />,
    );
    const set = setButton() as HTMLButtonElement;
    const reset = resetButton();
    expect(set.disabled).toBe(false);
    expect(set.getAttribute("aria-disabled")).toBe("true");
    expect(set.getAttribute("aria-describedby")).toContain("notice");
    fireEvent.click(set);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(reset.disabled).toBe(false);
    expect(reset.getAttribute("aria-disabled")).toBe("true");
    fireEvent.click(reset);
    expect(onChange).not.toHaveBeenCalled();
  });
});
