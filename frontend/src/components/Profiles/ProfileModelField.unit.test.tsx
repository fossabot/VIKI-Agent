import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, expect, test, vi } from "vitest";
import type { ProviderModelView } from "../../clients/profiles/profileLaunchApi";
import type { ConnectionLabels } from "./modelLabel";
import { ProfileModelField } from "./ProfileModelField";

afterEach(() => {
  cleanup();
});

const UUID = "11111111-1111-4111-8111-111111111111";
const MODELS: ProviderModelView = {
  provider: "claude",
  adapter_version: "1",
  native_version: "2.1.281",
  observed_at: null,
  catalog_key: null,
  status: "supported",
  models: [
    { value: "sonnet", label: "Claude Sonnet", description: "" },
    { value: "opus", label: "Claude Opus", description: "" },
  ],
};
const UNKNOWN_MODELS: readonly (readonly [
  string,
  ProviderModelView | "loading" | "failed" | null,
])[] = [
  ["not checked", null],
  ["loading", "loading"],
  ["failed", "failed"],
  ["unverified", { ...MODELS, status: "unverified", models: [] }],
];

test("offers app default and observed models without arbitrary text entry", () => {
  const onChange = vi.fn();
  render(
    <ProfileModelField
      value={null}
      models={MODELS}
      defaultModel="sonnet"
      onChange={onChange}
      onRetry={vi.fn()}
    />,
  );
  const select = screen.getByRole("combobox", { name: "Model" });
  const label = screen.getByText("Model", { selector: "label" });
  const help = screen.getByText(/Settings › Chat › Default model/);
  expect(screen.getAllByText("Model", { selector: "label" })).toHaveLength(1);
  expect(label.getAttribute("for")).toBe(select.id);
  expect(select.getAttribute("aria-describedby")).toBe(help.id);
  expect(
    within(select)
      .getAllByRole("option")
      .map((option) => option.textContent),
  ).toEqual(["App default", "Claude Sonnet", "Claude Opus"]);
  expect(screen.queryByRole("textbox")).toBeNull();
  fireEvent.change(select, { target: { value: "opus" } });
  expect(onChange).toHaveBeenCalledWith("opus");
});

test("marks an unavailable saved model and substitutes the default model in its help", () => {
  render(
    <ProfileModelField
      value="missing/model"
      models={MODELS}
      defaultModel="sonnet"
      onChange={vi.fn()}
      onRetry={vi.fn()}
    />,
  );
  const select = screen.getByRole("combobox", { name: "Model" });
  expect(within(select).getByRole("option", { name: "App default" })).toBeTruthy();
  expect(within(select).getByRole("option", { name: "missing/model (unavailable)" })).toBeTruthy();
  expect(screen.getByText(/App default is Claude Sonnet\./)).toBeTruthy();
});

test("keeps an unavailable saved model selectable without exposing connection UUID", () => {
  const ref = `viki-llm/${UUID}/missing/model`;
  render(
    <ProfileModelField
      value={ref}
      models={MODELS}
      defaultModel={`viki-llm/${UUID}/default`}
      onChange={vi.fn()}
      onRetry={vi.fn()}
    />,
  );
  const select = screen.getByRole("combobox", { name: "Model" });
  expect(
    within(select)
      .getAllByRole("option")
      .map((option) => option.textContent),
  ).toEqual(["App default", "missing/model", "Claude Sonnet", "Claude Opus"]);
  expect(screen.getByText("App default uses Claude Code's default for this profile.")).toBeTruthy();
  expect(document.body.textContent).not.toContain(UUID);
});

const REF = `viki-llm/${UUID}/vendor/model-x`;
const OTHER = `viki-llm/22222222-2222-4222-8222-222222222222/vendor/other`;
const LISTED: ConnectionLabels = {
  status: "ready",
  labels: [
    { model_ref: REF, name: "Work endpoint", model: "vendor/model-x" },
    { model_ref: OTHER, name: "Home endpoint", model: "vendor/other" },
  ],
};

function field(
  props: Partial<ComponentProps<typeof ProfileModelField>> = {},
): ComponentProps<typeof ProfileModelField> {
  return {
    value: null,
    models: MODELS,
    defaultModel: "sonnet",
    onChange: vi.fn(),
    onRetry: vi.fn(),
    ...props,
  };
}

const optionTexts = (): (string | null)[] =>
  within(screen.getByRole("combobox", { name: "Model" }))
    .getAllByRole("option")
    .map((option) => option.textContent);

test("Bot settings offers Your connections after native models with exact refs", () => {
  const onChange = vi.fn();
  render(<ProfileModelField {...field({ botSettings: true, connections: LISTED, onChange })} />);
  const select = screen.getByRole("combobox", { name: "Model" });
  const groups = Array.from(select.querySelectorAll("optgroup"), (group) => group.label);
  expect(groups).toEqual(["Claude Code models", "Your connections"]);
  expect(optionTexts()).toEqual([
    "App default",
    "Claude Sonnet",
    "Claude Opus",
    "Work endpoint · vendor/model-x",
    "Home endpoint · vendor/other",
  ]);
  const group = select.querySelectorAll("optgroup")[1] as HTMLOptGroupElement;
  expect(Array.from(group.querySelectorAll("option"), (option) => option.value)).toEqual([
    REF,
    OTHER,
  ]);
  fireEvent.change(select, { target: { value: OTHER } });
  expect(onChange).toHaveBeenCalledWith(OTHER);
  expect(document.body.textContent).not.toContain(UUID);
});

test("offers no connection choice outside Bot settings, even with a loaded list", () => {
  render(<ProfileModelField {...field({ connections: LISTED })} />);
  expect(optionTexts()).toEqual(["App default", "Claude Sonnet", "Claude Opus"]);
  expect(screen.queryByText("Your connections", { selector: "optgroup" })).toBeNull();
});

test("keeps a loaded saved connection selected by name and model in both views", () => {
  const { rerender } = render(
    <ProfileModelField {...field({ value: REF, botSettings: true, connections: LISTED })} />,
  );
  const select = screen.getByRole("combobox", { name: "Model" }) as HTMLSelectElement;
  expect(select.value).toBe(REF);
  expect(select.selectedOptions[0]?.textContent).toBe("Work endpoint · vendor/model-x");
  expect(screen.queryByText("Saved connection (removed or unreadable)")).toBeNull();
  // Agents › Profiles names the saved ref but offers no other connection.
  rerender(<ProfileModelField {...field({ value: REF, connections: LISTED })} />);
  expect(optionTexts()).toEqual([
    "App default",
    "Work endpoint · vendor/model-x",
    "Claude Sonnet",
    "Claude Opus",
  ]);
  expect((screen.getByRole("combobox", { name: "Model" }) as HTMLSelectElement).value).toBe(REF);
  expect(screen.queryByText("Saved connection (removed or unreadable)")).toBeNull();
  expect(document.body.textContent).not.toContain(UUID);
});

test.each([true, false])(
  "keeps a removed saved connection selected and says so only after a successful list (bot %s)",
  (botSettings) => {
    const removed = `viki-llm/${UUID}/gone/model`;
    const { rerender } = render(
      <ProfileModelField
        {...field({
          value: removed,
          botSettings,
          connections: { status: "ready", labels: [LISTED.labels[1] as never] },
        })}
      />,
    );
    const select = screen.getByRole("combobox", { name: "Model" }) as HTMLSelectElement;
    expect(select.value).toBe(removed);
    expect(select.selectedOptions[0]?.textContent).toBe("gone/model");
    expect(screen.getByText("Saved connection (removed or unreadable)")).toBeTruthy();
    for (const status of ["loading", "failed"] as const) {
      rerender(
        <ProfileModelField
          {...field({ value: removed, botSettings, connections: { status, labels: [] } })}
        />,
      );
      expect((screen.getByRole("combobox", { name: "Model" }) as HTMLSelectElement).value).toBe(
        removed,
      );
      expect(screen.queryByText("Saved connection (removed or unreadable)")).toBeNull();
    }
    expect(document.body.textContent).not.toContain(UUID);
  },
);

test.each([true, false])(
  "tells loading, failed, removed and listed saved connections apart (bot %s)",
  (botSettings) => {
    const loading = "Loading saved connection names…";
    const failed = "Saved connection names could not be loaded.";
    const removedLine = "Saved connection (removed or unreadable)";
    const lines = [loading, failed, removedLine];
    const shown = () => lines.filter((line) => screen.queryByText(line) !== null);
    const gone = `viki-llm/${UUID}/gone/model`;
    const states: readonly [ConnectionLabels, string, string[]][] = [
      [{ status: "loading", labels: [] }, REF, [loading]],
      [{ status: "failed", labels: [] }, REF, [failed]],
      [{ status: "ready", labels: [LISTED.labels[1] as never] }, gone, [removedLine]],
      [LISTED, REF, []],
    ];
    const { rerender } = render(<ProfileModelField {...field({ value: REF, botSettings })} />);
    for (const [connections, value, expected] of states) {
      rerender(<ProfileModelField {...field({ value, botSettings, connections })} />);
      expect(shown()).toEqual(expected);
    }
    // A native or null value gets no connection status line, except that Bot settings says a
    // failed list failed, so it is not mistaken for "no saved connections".
    for (const value of [null, "sonnet"]) {
      for (const status of ["loading", "failed", "ready"] as const) {
        rerender(
          <ProfileModelField
            {...field({ value, botSettings, connections: { status, labels: [] } })}
          />,
        );
        expect(shown()).toEqual(botSettings && status === "failed" ? [failed] : []);
      }
    }
  },
);

test("lists no connection group while the list is loading or failed", () => {
  const { rerender } = render(
    <ProfileModelField
      {...field({ botSettings: true, connections: { status: "loading", labels: [] } })}
    />,
  );
  expect(optionTexts()).toEqual(["App default", "Claude Sonnet", "Claude Opus"]);
  rerender(
    <ProfileModelField
      {...field({ botSettings: true, connections: { status: "failed", labels: [] } })}
    />,
  );
  expect(optionTexts()).toEqual(["App default", "Claude Sonnet", "Claude Opus"]);
});

test("distinguishes empty supported and unverified catalogs", () => {
  const empty = { ...MODELS, models: [] };
  const { rerender } = render(
    <ProfileModelField
      value={null}
      models={empty}
      defaultModel={null}
      onChange={vi.fn()}
      onRetry={vi.fn()}
    />,
  );
  expect(screen.getByText("Claude Code reported no models.")).toBeTruthy();
  rerender(
    <ProfileModelField
      value={null}
      models={{ ...MODELS, status: "unverified" }}
      defaultModel={null}
      onChange={vi.fn()}
      onRetry={vi.fn()}
    />,
  );
  expect(screen.getByText("Claude Code models are unverified.")).toBeTruthy();
});

test("distinguishes unchecked, loading and failed catalogs; Retry calls onRetry", () => {
  const onRetry = vi.fn();
  const { rerender } = render(
    <ProfileModelField
      value={null}
      models={null}
      defaultModel={null}
      onChange={vi.fn()}
      onRetry={onRetry}
    />,
  );
  expect(screen.getByText("Claude Code model availability has not been checked.")).toBeTruthy();
  rerender(
    <ProfileModelField
      value={null}
      models="loading"
      defaultModel={null}
      onChange={vi.fn()}
      onRetry={onRetry}
    />,
  );
  expect(screen.getByText("Loading Claude Code models…")).toBeTruthy();
  rerender(
    <ProfileModelField
      value={null}
      models="failed"
      defaultModel={null}
      onChange={vi.fn()}
      onRetry={onRetry}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect(onRetry).toHaveBeenCalledOnce();
  expect(screen.queryByRole("alert")).toBeNull();
});

test.each(UNKNOWN_MODELS)(
  "keeps saved model visible without an unavailable claim while catalog is %s",
  (_state, models) => {
    render(
      <ProfileModelField
        value="sonnet"
        models={models}
        defaultModel={null}
        onChange={vi.fn()}
        onRetry={vi.fn()}
      />,
    );
    const select = screen.getByRole("combobox", { name: "Model" });
    expect((select as HTMLSelectElement).value).toBe("sonnet");
    expect(within(select).getByRole("option", { name: "sonnet" })).toBeTruthy();
    expect(within(select).queryByRole("option", { name: "sonnet (unavailable)" })).toBeNull();
  },
);

test("marks a saved model unavailable only after a supported catalog confirms absence", () => {
  const { rerender } = render(
    <ProfileModelField
      value="sonnet"
      models={{ ...MODELS, models: [] }}
      defaultModel={null}
      onChange={vi.fn()}
      onRetry={vi.fn()}
    />,
  );
  let select = screen.getByRole("combobox", { name: "Model" });
  expect((select as HTMLSelectElement).value).toBe("sonnet");
  expect(within(select).getByRole("option", { name: "sonnet (unavailable)" })).toBeTruthy();

  rerender(
    <ProfileModelField
      value="sonnet"
      models={MODELS}
      defaultModel={null}
      onChange={vi.fn()}
      onRetry={vi.fn()}
    />,
  );
  select = screen.getByRole("combobox", { name: "Model" });
  expect((select as HTMLSelectElement).value).toBe("sonnet");
  expect(within(select).getByRole("option", { name: "Claude Sonnet" })).toBeTruthy();
  expect(within(select).queryByRole("option", { name: "sonnet (unavailable)" })).toBeNull();
});
