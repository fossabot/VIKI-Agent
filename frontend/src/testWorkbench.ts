import { act, fireEvent, screen, within } from "@testing-library/react";

/** Use the same explicit Workbench entry in docked rails and narrow drawers. */
export async function openWorkbench(): Promise<void> {
  // A same-test remount can restore the full view; its old primary entry was a noop too.
  if (
    document.querySelector('[data-region="view"]') &&
    JSON.parse(window.sessionStorage.getItem("viki.view") ?? "null")?.view === "code"
  )
    return;
  if (!screen.queryByRole("tablist", { name: "Right sidebar" }))
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "VIKI panel" }));
    });
  await act(async () => {
    fireEvent.click(
      within(screen.getByRole("tablist", { name: "Right sidebar" })).getByRole("tab", {
        name: "Code",
      }),
    );
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Open workbench" }));
  });
}
