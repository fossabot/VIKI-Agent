import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { type CreateVaultInput, ShellApiError } from "../../clients/app/shellApi";
import { createShellStore } from "../../state/shell";
import { AppStore } from "../../state/store";
import { FAKE_WORKSPACE_ID, fakeShellApi } from "../../testShell";
import { CreateVaultForm } from "./CreateVaultForm";

async function flush(): Promise<void> {
  await act(async () => {
    for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

it("explains when setup must start again, then cancels and creates with new choices", async () => {
  const api = fakeShellApi();
  const store = createShellStore(api, new AppStore());
  api.fail(
    "createVault",
    new ShellApiError(409, "destination_not_empty", {
      hint: "cancel_bootstrap",
      extra: { workspace_id: FAKE_WORKSPACE_ID },
    }),
  );
  render(
    <CreateVaultForm
      api={api}
      store={store}
      onBack={vi.fn()}
      onDone={vi.fn()}
      onGoToWorkspaces={vi.fn()}
      onOpenExisting={vi.fn()}
    />,
  );
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Trip" } });
  fireEvent.blur(screen.getByLabelText("Name"));
  await flush();
  fireEvent.click(screen.getByText("Create"));
  await flush();

  screen.getByText(
    "Cancel this setup to choose again. If the starter files or setup details changed, start a new setup.",
  );
  expect((screen.getByLabelText("Blank folder") as HTMLInputElement).disabled).toBe(true);
  fireEvent.click(screen.getByText("Cancel"));
  await flush();
  expect(api.calls.find((call) => call.method === "cancelBootstrap")?.args[1]).toBe(
    FAKE_WORKSPACE_ID,
  );
  expect((screen.getByLabelText("Blank folder") as HTMLInputElement).disabled).toBe(false);
  fireEvent.click(screen.getByLabelText("Blank folder"));
  fireEvent.click(screen.getByText("Create"));
  await flush();

  const calls = api.calls.filter((call) => call.method === "createVault");
  expect(calls).toHaveLength(2);
  expect(calls[1]?.args[0]).not.toBe(calls[0]?.args[0]);
  const secondInput = calls[1]?.args[1] as CreateVaultInput;
  expect(secondInput.template).toBe("blank");
});
