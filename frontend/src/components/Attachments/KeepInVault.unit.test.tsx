import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { KnowledgeError } from "../../state/vaultKnowledgeApi";
import { KeepInVault, type KeepInVaultContext } from "./KeepInVault";

const asset = {
  asset_id: "123e4567-e89b-12d3-a456-426614174000",
  media_type: "image/png" as const,
  byte_size: 2,
  width: 2,
  height: 1,
  name: "screen.png",
};
const make = (keepAttachment: ReturnType<typeof vi.fn>): KeepInVaultContext => ({
  api: { keepAttachment } as unknown as KeepInVaultContext["api"],
  workspaceId: "223e4567-e89b-12d3-a456-426614174000",
  policyRevision: 3,
  rootPhysicalId: "1:2",
});

afterEach(cleanup);

function open(keep: ReturnType<typeof vi.fn>, onDone = vi.fn()) {
  render(<KeepInVault attachment={asset} vault={make(keep)} onDone={onDone} />);
  fireEvent.click(screen.getByRole("button", { name: "Save to vault" }));
}

test("claims nothing before the answer, then shows the ref as text", async () => {
  let resolve: (value: unknown) => void = () => {};
  const keep = vi.fn(
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  const onDone = vi.fn();
  open(keep, onDone);
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(screen.queryByText(/Saved to vault/)).toBeNull();
  const [workspace, request, command] = keep.mock.calls[0] as unknown[];
  expect(workspace).toBe("223e4567-e89b-12d3-a456-426614174000");
  expect(request).toEqual({
    policyRevision: 3,
    rootPhysicalId: "1:2",
    assetId: asset.asset_id,
    name: "screen.png",
  });
  expect(command).toMatch(/^[0-9a-f-]{36}$/);
  resolve({ ref: "attachments/<b>x</b>.png", reused: false, byte_size: 2 });
  await screen.findByText(/Saved to vault/);
  expect(screen.getByText("attachments/<b>x</b>.png").tagName).toBe("CODE");
  expect(onDone).toHaveBeenCalledWith("attachments/<b>x</b>.png");
});

test("reused says the existing copy is used, not that a copy was written", async () => {
  open(vi.fn().mockResolvedValue({ ref: "attachments/a.png", reused: true, byte_size: 2 }));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText(/Using the existing copy/);
  expect(screen.queryByText(/Saved to vault/)).toBeNull();
});

test.each([
  ["vault_protected", "protected"],
  ["vault_read_only", "read-only"],
  ["asset_unavailable", "no longer available"],
  ["something_new", "Couldn't save to vault."],
])("refusal %s shows a typed sentence without echoing the code", async (code, part) => {
  open(vi.fn().mockRejectedValue(new KnowledgeError(code)));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain(part);
  expect(alert.textContent).not.toContain(code);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Save" })).toHaveProperty("disabled", false),
  );
});

test.each([
  [
    "a result",
    vi.fn().mockResolvedValue({ ref: "attachments/a.png", reused: false, byte_size: 2 }),
  ],
  ["an error", vi.fn().mockRejectedValue(new KnowledgeError("conflict"))],
])("after %s, Close closes the dialog and refocuses the opener", async (_label, keep) => {
  open(keep);
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Close" })).toBeTruthy());
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Close" })).toHaveProperty("disabled", false),
  );
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  const opener = screen.getByRole("button", { name: "Save to vault" });
  expect(screen.queryByRole("group")).toBeNull();
  expect(document.activeElement).toBe(opener);
});

test("an empty name shows the Image placeholder", () => {
  open(vi.fn());
  const input = screen.getByRole("textbox");
  fireEvent.change(input, { target: { value: "" } });
  expect(input.getAttribute("placeholder")).toBe("Image");
});

test("reopening after a result shows a fresh form", async () => {
  open(vi.fn().mockResolvedValue({ ref: "attachments/a.png", reused: false, byte_size: 2 }));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await screen.findByText(/Saved to vault/);
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  fireEvent.click(screen.getByRole("button", { name: "Save to vault" }));
  expect(screen.getByRole("button", { name: "Save" })).toBeTruthy();
  expect(screen.queryByText(/Saved to vault/)).toBeNull();
});

test("the result's Close button sits outside the status element", async () => {
  open(vi.fn().mockResolvedValue({ ref: "attachments/a.png", reused: false, byte_size: 2 }));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  const status = await screen.findByRole("status");
  expect(status.querySelector("button")).toBeNull();
  expect(screen.getByRole("button", { name: "Close" })).toBeTruthy();
});
