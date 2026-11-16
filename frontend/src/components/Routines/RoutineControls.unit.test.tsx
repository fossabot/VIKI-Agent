import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { t } from "../../i18n/t";
import { RoutineControls, type RoutineControlValues } from "./RoutineControls";

afterEach(cleanup);

const BASE: RoutineControlValues = {
  paused: false,
  notificationsEnabled: false,
  quietStart: null,
  quietEnd: null,
  permission: "granted",
};

function setup(controls: Partial<RoutineControlValues> = {}, over: { onSettings?: never } = {}) {
  const order: string[] = [];
  const onSettings = vi.fn(async () => {
    order.push("settings");
  });
  const onRequestPermission = vi.fn(async () => {
    order.push("permission");
  });
  const onRefresh = vi.fn();
  render(
    <RoutineControls
      scheduler={{ state: "running", attended: true, paused: false }}
      controls={{ ...BASE, ...controls }}
      onSettings={over.onSettings ?? onSettings}
      onRequestPermission={onRequestPermission}
      onRefresh={onRefresh}
    />,
  );
  return { onSettings, onRequestPermission, onRefresh, order };
}

const click = async (element: HTMLElement) => act(async () => void fireEvent.click(element));

describe("RoutineControls", () => {
  test("the pause toggle patches routines.paused from the current value", async () => {
    const first = setup();
    await click(screen.getByRole("button", { name: t("routines.controls.pause_all") }));
    expect(first.onSettings).toHaveBeenCalledWith({ "routines.paused": true });
    expect(first.onRefresh).toHaveBeenCalledTimes(1);
    cleanup();
    const second = setup({ paused: true });
    await click(screen.getByRole("button", { name: t("routines.controls.resume") }));
    expect(second.onSettings).toHaveBeenCalledWith({ "routines.paused": false });
  });

  test("checking notify with permission granted patches without asking", async () => {
    const { onSettings, onRequestPermission } = setup();
    await click(screen.getByRole("checkbox"));
    expect(onRequestPermission).not.toHaveBeenCalled();
    expect(onSettings).toHaveBeenCalledWith({ "notifications.enabled": true });
  });

  test("checking notify with permission undecided asks first, then patches", async () => {
    const { onSettings, order } = setup({ permission: "default" });
    await click(screen.getByRole("checkbox"));
    expect(order).toEqual(["permission", "settings"]);
    expect(onSettings).toHaveBeenCalledWith({ "notifications.enabled": true });
  });

  test("unchecking notify does not ask for permission", async () => {
    const { onSettings, onRequestPermission } = setup({
      permission: "default",
      notificationsEnabled: true,
    });
    await click(screen.getByRole("checkbox"));
    expect(onRequestPermission).not.toHaveBeenCalled();
    expect(onSettings).toHaveBeenCalledWith({ "notifications.enabled": false });
  });

  test("quiet selects offer none and the 48 half hours and patch both keys", async () => {
    const { onSettings } = setup({ quietStart: "22:00" });
    const from = screen.getByLabelText(t("routines.controls.quiet_from")) as HTMLSelectElement;
    const options = Array.from(from.options).map((option) => option.value);
    expect(options).toHaveLength(49);
    expect(options[0]).toBe("");
    expect(options[1]).toBe("00:00");
    expect(options[48]).toBe("23:30");
    expect(from.value).toBe("22:00");
    expect(from.options[0]?.textContent).toBe(t("routines.controls.quiet_none"));
    await act(async () => void fireEvent.change(from, { target: { value: "23:30" } }));
    expect(onSettings).toHaveBeenCalledWith({ "notifications.quiet_start": "23:30" });
    const to = screen.getByLabelText(t("routines.controls.quiet_to"));
    await act(async () => void fireEvent.change(to, { target: { value: "06:00" } }));
    expect(onSettings).toHaveBeenCalledWith({ "notifications.quiet_end": "06:00" });
    await act(async () => void fireEvent.change(from, { target: { value: "" } }));
    expect(onSettings).toHaveBeenCalledWith({ "notifications.quiet_start": null });
  });

  test("permission notices show only when enabled and not granted", async () => {
    setup({ notificationsEnabled: false, permission: "denied" });
    expect(screen.queryByText(t("routines.controls.permission_denied"))).toBeNull();
    cleanup();
    setup({ notificationsEnabled: true, permission: "granted" });
    expect(screen.queryByText(/./, { selector: ".routines-controls-note" })).toBeNull();
    cleanup();
    setup({ notificationsEnabled: true, permission: "denied" });
    expect(screen.getByText(t("routines.controls.permission_denied"))).toBeTruthy();
    cleanup();
    setup({ notificationsEnabled: true, permission: "unsupported" });
    expect(screen.getByText(t("routines.controls.permission_unsupported"))).toBeTruthy();
  });

  test("the undecided notice offers a request button that asks and refreshes", async () => {
    const { onRequestPermission, onSettings, onRefresh } = setup({
      notificationsEnabled: true,
      permission: "default",
    });
    expect(
      screen.getByText(t("routines.controls.permission_default"), { exact: false }),
    ).toBeTruthy();
    await click(screen.getByRole("button", { name: t("routines.controls.permission_request") }));
    expect(onRequestPermission).toHaveBeenCalledTimes(1);
    expect(onSettings).not.toHaveBeenCalled();
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  test("a rejection shows save_failed and does not refresh", async () => {
    const onSettings = vi.fn().mockRejectedValue(new Error("nope"));
    const { onRefresh } = setup({}, { onSettings: onSettings as never });
    await click(screen.getByRole("button", { name: t("routines.controls.pause_all") }));
    expect(screen.getByRole("alert").textContent).toBe(t("routines.controls.save_failed"));
    expect(onRefresh).not.toHaveBeenCalled();
  });
});
