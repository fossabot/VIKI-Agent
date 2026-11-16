import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ProfileIdentityV1 } from "../../clients/profiles/profileApi";
import { BotHuesContext } from "../Bots/botHues";
import { BotLookField } from "./BotLookField";

const mocks = vi.hoisted(() => ({
  iconProps: null as null | { onClose(): void; onSaved(icon: string | null): void },
  editorProps: null as null | { onCancel(): void; onSaved(id: string): void },
  previewIdentity: null as null | ProfileIdentityV1,
}));

vi.mock("../Bots/BotAvatar", () => ({
  BotAvatar: ({ identity, size }: { identity: ProfileIdentityV1; size: number }) => {
    mocks.previewIdentity = identity;
    return <div data-testid="preview" data-size={size} />;
  },
}));
vi.mock("../Avatar/AvatarEditor", () => ({
  AvatarEditor: (props: (typeof mocks)["editorProps"]) => {
    mocks.editorProps = props;
    return <div role="dialog" aria-label="Edit avatar" />;
  },
}));
vi.mock("./BotIconDialog", async () => {
  const React = await import("react");
  return {
    BotIconDialog: (props: NonNullable<(typeof mocks)["iconProps"]>) => {
      const opener = React.useRef<HTMLElement | null>(null);
      React.useEffect(() => {
        opener.current = document.activeElement as HTMLElement;
        return () => opener.current?.focus();
      }, []);
      mocks.iconProps = props;
      return <div role="dialog" aria-label="Choose icon" />;
    },
  };
});

const design: ProfileIdentityV1 = {
  name: "Planner",
  purpose: "Test profile",
  avatar_asset_id: null,
  accent: "blue",
  shape: "circle",
};

function renderField(overrides: Partial<React.ComponentProps<typeof BotLookField>> = {}) {
  const onUpload = vi.fn();
  const onReset = vi.fn();
  const set = vi.fn(async () => "saved" as const);
  const view = render(
    <BotHuesContext.Provider value={{ hues: {}, icons: {}, set }}>
      <BotLookField
        profileId="profile-a"
        name="Planner"
        design={design}
        avatar={null}
        imageRemovalPending={false}
        onUpload={onUpload}
        onReset={onReset}
        {...overrides}
      />
    </BotHuesContext.Provider>,
  );
  return { ...view, onUpload, onReset, set };
}

describe("BotLookField", () => {
  afterEach(cleanup);

  beforeEach(() => {
    mocks.iconProps = null;
    mocks.editorProps = null;
    mocks.previewIdentity = null;
  });

  it("shows a 64px preview and opens the icon dialog", () => {
    renderField();
    expect(screen.getByTestId("preview").getAttribute("data-size")).toBe("64");
    fireEvent.click(screen.getByRole("button", { name: "Choose icon…" }));
    expect(screen.getByRole("dialog", { name: "Choose icon" })).not.toBeNull();
  });

  it("uses the full Look row without a duplicate preview when compact", () => {
    renderField({ compact: true });
    const look = screen.getByRole("region", { name: "Look" });
    expect(look.classList.contains("bot-look-compact")).toBe(true);
    expect(screen.queryByTestId("preview")).toBeNull();
    expect(screen.getByRole("button", { name: "Choose icon…" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Upload image…" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reset" })).toBeTruthy();
    expect(screen.getByText("An uploaded image takes priority over the chosen icon.")).toBeTruthy();
    expect(screen.getByRole("status")).toBeTruthy();
  });

  it("announces only confirmed icon save after dialog unmount and focus return", async () => {
    const { rerender, onReset } = renderField();
    const opener = screen.getByRole("button", { name: "Choose icon…" });
    opener.focus();
    fireEvent.click(opener);
    mocks.iconProps?.onSaved("gear");
    mocks.iconProps?.onClose();
    await waitFor(() => expect(document.activeElement).toBe(opener));
    expect(screen.getByRole("status").textContent).toBe("Icon changed to Gear.");
    expect(screen.queryByRole("dialog")).toBeNull();
    rerender(
      <BotHuesContext.Provider value={{ hues: {}, icons: {}, set: vi.fn() }}>
        <BotLookField
          profileId="profile-a"
          name="Planner"
          design={design}
          avatar={null}
          imageRemovalPending
          onUpload={vi.fn()}
          onReset={onReset}
        />
      </BotHuesContext.Provider>,
    );
    expect(screen.getByText("Save the profile to finish removing its image.")).not.toBeNull();
    rerender(
      <BotHuesContext.Provider value={{ hues: {}, icons: {}, set: vi.fn() }}>
        <BotLookField
          profileId="profile-a"
          name="Planner"
          design={design}
          avatar={null}
          imageRemovalPending={false}
          onUpload={vi.fn()}
          onReset={onReset}
        />
      </BotHuesContext.Provider>,
    );
    expect(screen.queryByText("Save the profile to finish removing its image.")).toBeNull();
    expect(screen.getByRole("status").textContent).toBe("Icon changed to Gear.");
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    expect(onReset).toHaveBeenCalledOnce();
    expect(screen.getByRole("status").textContent).toBe("");
  });

  it("does not announce stale profile save after navigation", async () => {
    const { rerender } = renderField();
    const opener = screen.getByRole("button", { name: "Choose icon…" });
    opener.focus();
    fireEvent.click(opener);
    rerender(
      <BotHuesContext.Provider value={{ hues: {}, icons: {}, set: vi.fn() }}>
        <BotLookField
          profileId="profile-b"
          name="Other"
          design={design}
          avatar={null}
          imageRemovalPending={false}
          onUpload={vi.fn()}
          onReset={vi.fn()}
        />
      </BotHuesContext.Provider>,
    );
    mocks.iconProps?.onSaved("gear");
    mocks.iconProps?.onClose();
    await waitFor(() => expect(document.activeElement).toBe(opener));
    expect(screen.queryByRole("status")?.textContent).toBe("");
  });

  it("uploads as an unsaved profile draft and Reset delegates without writing the icon", () => {
    const { onUpload, onReset, set } = renderField({
      avatar: "draft-asset",
      imageRemovalPending: true,
    });
    expect(mocks.previewIdentity).toMatchObject({
      avatar_asset_id: "draft-asset",
      accent: "blue",
      shape: "circle",
    });
    fireEvent.click(screen.getByRole("button", { name: "Upload image…" }));
    mocks.editorProps?.onSaved("asset-draft");
    expect(onUpload).toHaveBeenCalledWith("asset-draft");
    expect(
      screen.getByText("An uploaded image takes priority over the chosen icon."),
    ).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    expect(onReset).toHaveBeenCalledOnce();
    expect(set).not.toHaveBeenCalled();
    expect(screen.getByText("Save the profile to finish removing its image.")).not.toBeNull();
  });

  it("disables only draft image actions while a profile draft cannot be saved", () => {
    renderField({ draftDisabled: true });
    expect(
      (screen.getByRole("button", { name: "Choose icon…" }) as HTMLButtonElement).disabled,
    ).toBe(false);
    expect(
      (screen.getByRole("button", { name: "Upload image…" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect((screen.getByRole("button", { name: "Reset" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  it("hides removal notice for an already unset or discarded-only image", () => {
    const { rerender } = renderField({ avatar: null, imageRemovalPending: false });
    expect(screen.queryByText("Save the profile to finish removing its image.")).toBeNull();
    rerender(
      <BotHuesContext.Provider value={{ hues: {}, icons: {}, set: vi.fn() }}>
        <BotLookField
          profileId="profile-a"
          name="Planner"
          design={design}
          avatar="draft-only-asset"
          imageRemovalPending={false}
          onUpload={vi.fn()}
          onReset={vi.fn()}
        />
      </BotHuesContext.Provider>,
    );
    expect(screen.queryByText("Save the profile to finish removing its image.")).toBeNull();
    rerender(
      <BotHuesContext.Provider value={{ hues: {}, icons: {}, set: vi.fn() }}>
        <BotLookField
          profileId="profile-a"
          name="Planner"
          design={design}
          avatar={null}
          imageRemovalPending={false}
          onUpload={vi.fn()}
          onReset={vi.fn()}
        />
      </BotHuesContext.Provider>,
    );
    expect(screen.queryByText("Save the profile to finish removing its image.")).toBeNull();
  });

  it("does not upload or reset on cancel", () => {
    const { onUpload, onReset } = renderField();
    fireEvent.click(screen.getByRole("button", { name: "Upload image…" }));
    mocks.editorProps?.onCancel();
    expect(onUpload).not.toHaveBeenCalled();
    expect(onReset).not.toHaveBeenCalled();
  });

  it("does not announce when icon save has no confirmation", () => {
    renderField();
    fireEvent.click(screen.getByRole("button", { name: "Choose icon…" }));
    mocks.iconProps?.onClose();
    expect(screen.getByRole("status").textContent).toBe("");
  });
});
