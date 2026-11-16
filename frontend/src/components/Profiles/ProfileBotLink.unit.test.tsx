import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { type BotRow, type BotsApi, BotsApiError } from "../../clients/messaging/botsApi";
import { ProfileBotLink } from "./ProfileBotLink";

afterEach(cleanup);
const bot = { bot_id: "bot", profile_id: "profile", workspace_id: "vault" } as BotRow;
function setup(rows: BotRow[]) {
  const api = {
    list: vi.fn().mockResolvedValue({ bots: rows }),
    create: vi.fn().mockResolvedValue({ bot }),
  } as unknown as BotsApi;
  const onOpen = vi.fn();
  const props = {
    api,
    onOpen,
    profileId: "profile",
    revision: 1,
    workspaceId: "vault",
    disabled: false,
  };
  const view = render(<ProfileBotLink {...props} />);
  return { ...props, view };
}

it("reuses a Bot by exact profile and vault identity", async () => {
  const props = setup([bot]);
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Open Bot" }));
  expect(props.onOpen).toHaveBeenCalledWith("bot");
  expect(props.api.create).not.toHaveBeenCalled();
});

it("creates the vault's Bot and adopts a concurrent creation after a conflict", async () => {
  const props = setup([{ ...bot, workspace_id: "other-vault" }]);
  await act(async () => {});
  vi.mocked(props.api.create).mockRejectedValue(new BotsApiError(409, "conflict"));
  vi.mocked(props.api.list).mockResolvedValue({
    bots: [bot],
    scheduler: { state: "running", attended: true, paused: false },
  });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Use as Bot" })));
  expect(props.api.create).toHaveBeenCalledTimes(1);
  expect(props.api.create).toHaveBeenCalledWith(
    expect.any(String),
    "profile",
    1,
    "vault",
    expect.any(AbortSignal),
  );
  expect(props.onOpen).toHaveBeenCalledWith("bot");
});

it("disables the link while the profile has unsaved changes", async () => {
  const props = setup([bot]);
  await act(async () => {});
  props.view.rerender(<ProfileBotLink {...props} disabled />);
  expect(screen.getByRole<HTMLButtonElement>("button", { name: "Open Bot" }).disabled).toBe(true);
  expect(props.onOpen).not.toHaveBeenCalled();
});
