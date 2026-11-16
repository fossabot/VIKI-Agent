import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";
import type { RoutinePreviewData } from "../../clients/automation/routinesApi";
import { t, tf } from "../../i18n/t";
import { RoutinePreview } from "./RoutinePreview";

afterEach(cleanup);

function data(over: Partial<RoutinePreviewData["resolved"]> = {}): RoutinePreviewData {
  return {
    next_five: [
      { at: null, local: "2026-10-04 09:00", zone: "Asia/Tokyo", offset_minutes: 540, note: null },
    ],
    readiness: { blockers: [], run_blockers: [], warnings: [] },
    resolved: {
      profile: {
        profile_id: "p",
        name: "Prof",
        revision: 1,
        provider: "claude",
        model: "opus",
        permission_mode: "default",
        mode_source: "profile",
        connection_pinned: false,
      },
      workspace: { workspace_id: "w", name: "Work", policy: "ordinary", active: true },
      first_turn: "hello",
      deadline_minutes: 30,
      ...over,
    },
  };
}

describe("RoutinePreview", () => {
  test("prompt disclosure is opt-in and leaves returned slots and DST warnings expanded", () => {
    const preview = data();
    preview.next_five[0] = {
      at: null,
      local: "03-29 02:30",
      zone: "Europe/Paris",
      offset_minutes: null,
      note: "dst_gap",
    };
    const { rerender } = render(<RoutinePreview kind="daily" preview={preview} />);
    expect(screen.getByText("hello").closest("details")).toBeNull();
    rerender(<RoutinePreview kind="daily" preview={preview} collapsePrompt scheduleZone={null} />);
    const disclosure = screen.getByText("hello").closest("details") as HTMLDetailsElement;
    expect(disclosure.open).toBe(false);
    expect(screen.getByText("Not specified")).toBeTruthy();
    const slot = screen.getByText("03-29 02:30 Europe/Paris");
    expect(slot.closest("details")).toBeNull();
    expect(slot.closest("ul")?.classList.contains("routine-preview-slots")).toBe(true);
    expect(screen.getByText(t("routines.preview.gap")).closest("details")).toBeNull();
    expect(screen.getAllByText(/Europe\/Paris/)).toHaveLength(1);
    fireEvent.click(disclosure.querySelector("summary") as HTMLElement);
    expect(disclosure.open).toBe(true);
    expect(screen.getByText("hello")).toBeTruthy();
  });

  test("a Bot run's system prompt shows before its first message, folded alike", () => {
    const preview = data({ first_turn: "Say hello.", system_prompt: "Be brief.\n\nMemory rule." });
    const { container, rerender } = render(<RoutinePreview kind="daily" preview={preview} />);
    const labels = [...container.querySelectorAll(".routine-preview-block h4")].map(
      (heading) => heading.textContent,
    );
    expect(labels.indexOf(t("routines.preview.system_prompt"))).toBe(
      labels.indexOf(t("routines.preview.first_turn")) - 1,
    );
    expect(
      [...container.querySelectorAll("pre.routine-preview-turn")].map((pre) => pre.textContent),
    ).toEqual(["Be brief.\n\nMemory rule.", "Say hello."]);
    rerender(<RoutinePreview kind="daily" preview={preview} collapsePrompt />);
    expect(screen.getByText(/^Be brief\./).closest("details")).not.toBeNull();
    rerender(<RoutinePreview kind="daily" preview={data({ system_prompt: null })} />);
    expect(screen.queryByText(t("routines.preview.system_prompt"))).toBeNull();
  });

  test("freshness is optional and stale results are announced", () => {
    const { rerender } = render(<RoutinePreview kind="daily" preview={data()} />);
    expect(screen.queryByRole("status")).toBeNull();
    rerender(<RoutinePreview kind="daily" preview={data()} stale />);
    expect(screen.getByRole("status").textContent).toContain("Rehearsal is stale");
    expect(screen.getByText("2026-10-04 09:00 Asia/Tokyo")).toBeTruthy();
    rerender(<RoutinePreview kind="daily" preview={data()} stale={false} />);
    expect(screen.queryByRole("status")).toBeNull();
  });

  test("renders markup-shaped strings as text", () => {
    const evil = "<img src=x onerror=alert(1)>";
    const { container } = render(
      <RoutinePreview
        kind="daily"
        preview={data({
          first_turn: evil,
          profile: {
            profile_id: "p",
            name: "<b>name</b>",
            revision: 1,
            provider: "claude",
            model: null,
            permission_mode: null,
            mode_source: null,
            connection_pinned: false,
          },
        })}
      />,
    );
    expect(container.querySelector("pre")?.textContent).toBe(evil);
    expect(screen.getByText("<b>name</b>")).toBeTruthy();
    expect(container.querySelector("img, b")).toBeNull();
  });

  test("null model reads default; settings mode is noted", () => {
    const base = data().resolved.profile;
    if (!base) throw new Error("fixture");
    render(
      <RoutinePreview
        kind="daily"
        preview={data({ profile: { ...base, model: null, mode_source: "settings" } })}
      />,
    );
    expect(
      screen.getByText(`${t("routines.preview.model")}: ${t("routines.preview.model_default")}`),
    ).toBeTruthy();
    expect(
      screen.getByText(
        `${t("routines.preview.mode")}: default ${t("routines.preview.mode_settings")}`,
      ),
    ).toBeTruthy();
  });

  test("DST notes, local time and zone", () => {
    const preview = data();
    preview.next_five = [
      {
        at: null,
        local: "03-29 02:30",
        zone: "Europe/Paris",
        offset_minutes: null,
        note: "dst_gap",
      },
      {
        at: null,
        local: "10-25 02:30",
        zone: "Europe/Paris",
        offset_minutes: 120,
        note: "dst_fold",
      },
    ];
    render(<RoutinePreview kind="daily" preview={preview} />);
    expect(screen.getByText("03-29 02:30 Europe/Paris")).toBeTruthy();
    expect(screen.getByText(t("routines.preview.gap"))).toBeTruthy();
    expect(screen.getByText(t("routines.preview.fold"))).toBeTruthy();
    expect(screen.getByText(t("routines.preview.unattended"))).toBeTruthy();
  });

  test("manual shows the manual note and no unattended note or slots", () => {
    render(<RoutinePreview kind="manual" preview={data()} />);
    expect(screen.getByText(t("routines.preview.manual"))).toBeTruthy();
    expect(screen.queryByText("2026-10-04 09:00 Asia/Tokyo")).toBeNull();
    expect(screen.queryByText(t("routines.preview.unattended"))).toBeNull();
  });

  test("the stop line replaces the unattended line, manual too, and the card line shows the seat", () => {
    const { rerender } = render(
      <RoutinePreview kind="daily" preview={data()} cardSeat="research" stopAtDraft />,
    );
    expect(screen.getByText(t("routines.preview.stop"))).toBeTruthy();
    expect(screen.queryByText(t("routines.preview.unattended"))).toBeNull();
    expect(screen.getByText(tf("routines.preview.card", { seat: "research" }))).toBeTruthy();
    rerender(<RoutinePreview kind="manual" preview={data()} stopAtDraft />);
    expect(screen.getByText(t("routines.preview.stop"))).toBeTruthy();
    expect(screen.queryByText(/owner\//)).toBeNull();
  });

  test("the folder line names the folder a scheduled time waits for", () => {
    const { rerender } = render(
      <RoutinePreview kind="daily" preview={data()} changedFolder="00-inbox" />,
    );
    expect(screen.getByText(tf("routines.preview.changed", { folder: "00-inbox" }))).toBeTruthy();
    expect(screen.queryByText(/owner\//)).toBeNull();
    rerender(<RoutinePreview kind="daily" preview={data()} />);
    expect(screen.queryByText(/00-inbox/)).toBeNull();
  });

  test("missing parts, inactive workspace, blockers and warnings", () => {
    const preview = data({ profile: null, workspace: null });
    preview.readiness = {
      blockers: ["profile_missing"],
      run_blockers: ["mystery"],
      warnings: ["workspace_not_active"],
    };
    render(<RoutinePreview kind="daily" preview={preview} />);
    expect(screen.getByText(t("routines.preview.missing_profile"))).toBeTruthy();
    expect(screen.getByText(t("routines.preview.missing_workspace"))).toBeTruthy();
    expect(screen.getByText(t("routines.reason.profile_missing"))).toBeTruthy();
    expect(screen.getByText(t("routines.warning.workspace_not_active"))).toBeTruthy();
    expect(screen.getByText("Other (mystery)", { exact: false })).toBeTruthy();
  });

  test("an inactive workspace says so", () => {
    const base = data().resolved.workspace;
    if (!base) throw new Error("fixture");
    render(
      <RoutinePreview kind="daily" preview={data({ workspace: { ...base, active: false } })} />,
    );
    expect(screen.getByText(t("routines.preview.not_active"))).toBeTruthy();
  });

  test("a pinned connection shows the subscription note with the native model and no ref", () => {
    const base = data().resolved.profile;
    if (!base) throw new Error("fixture");
    const { container } = render(
      <RoutinePreview
        kind="daily"
        preview={data({ profile: { ...base, model: "opus", connection_pinned: true } })}
      />,
    );
    expect(
      screen.getByText(tf("routines.preview.connection_native", { model: "opus" })),
    ).toBeTruthy();
    expect(container.textContent).not.toContain("viki-llm/");
    expect(screen.queryByText(tf("routines.preview.connection_native", { model: "x" }))).toBeNull();
  });

  test("no note without a pinned connection, and a ref-shaped model never shows its id", () => {
    const base = data().resolved.profile;
    if (!base) throw new Error("fixture");
    const { container, rerender } = render(<RoutinePreview kind="daily" preview={data()} />);
    expect(container.querySelector(".routine-preview-note")?.textContent).not.toContain(
      "subscription",
    );
    rerender(
      <RoutinePreview
        kind="daily"
        preview={data({
          profile: {
            ...base,
            model: "viki-llm/11111111-2222-4333-8444-555555555555/gpt-x",
            connection_pinned: true,
          },
        })}
      />,
    );
    expect(container.textContent).not.toContain("11111111-2222-4333-8444-555555555555");
    expect(container.textContent).toContain("Model: gpt-x");
  });
});
