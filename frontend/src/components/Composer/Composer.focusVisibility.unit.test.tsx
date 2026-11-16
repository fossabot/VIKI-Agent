import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { Composer } from "./Composer";

afterEach(cleanup);

test("focusing the editor or Send exposes the control through nested scrollers without editing", () => {
  const onSend = vi.fn(async () => false);
  const onEdit = vi.fn();
  const { container } = render(
    <Composer
      running={false}
      value="Retained draft"
      onEdit={onEdit}
      onSend={onSend}
      onStop={vi.fn()}
    />,
  );
  const box = container.querySelector("textarea");
  const send = container.querySelector<HTMLButtonElement>(".composer-submit");
  if (!box || !send) throw new Error("Composer controls are missing");
  const revealEditor = vi.fn();
  const revealSend = vi.fn();
  box.scrollIntoView = revealEditor;
  send.scrollIntoView = revealSend;
  box.setSelectionRange(2, 5);

  fireEvent.focus(box);
  expect(revealEditor).toHaveBeenCalledExactlyOnceWith({ block: "nearest", inline: "nearest" });
  fireEvent.focus(send);
  expect(revealSend).toHaveBeenCalledExactlyOnceWith({ block: "nearest", inline: "nearest" });
  expect(box.value).toBe("Retained draft");
  expect([box.selectionStart, box.selectionEnd]).toEqual([2, 5]);
  expect(onEdit).not.toHaveBeenCalled();
  expect(onSend).not.toHaveBeenCalled();
});
