import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import {
  createFakeDecisionLoopApi,
  decisionLoopExamples,
} from "../../../clients/decisionLoop/fake";
import type { ModelErrorCode, PageView } from "../../../clients/decisionLoop/types";
import { ModelRow, PrivacyRow, type SetupRowsProps } from "./SetupRows";

afterEach(() => {
  cleanup();
});

type PagePatch = Partial<Omit<PageView, "model" | "privacy" | "today">> & {
  model?: Partial<PageView["model"]>;
  privacy?: Partial<PageView["privacy"]>;
  today?: Partial<PageView["today"]>;
};

function pageWith(overrides: PagePatch = {}): PageView {
  const page = decisionLoopExamples().page;
  const { model, privacy, today, ...rest } = overrides;
  return {
    ...page,
    ...rest,
    model: { ...page.model, ...model },
    privacy: { ...page.privacy, ...privacy },
    today: { ...page.today, ...today },
  };
}

function versionGroup() {
  return within(screen.getByRole("group", { name: "Version" }));
}

/** The Advanced disclosure is a closed `details`; open it, as a user click would, so its
 * controls can be queried. */
function openAdvanced(): void {
  for (const summary of document.querySelectorAll<HTMLElement>("#model-body details > summary")) {
    fireEvent.click(summary);
  }
}

function baseProps(overrides: Partial<SetupRowsProps> = {}): SetupRowsProps {
  return {
    page: pageWith(),
    values: {},
    writable: true,
    patch: vi.fn().mockResolvedValue(undefined),
    api: createFakeDecisionLoopApi(),
    onRefresh: vi.fn(),
    onOpenUrl: vi.fn(),
    onOpenContext: vi.fn(),
    ...overrides,
  };
}

// --- ModelRow ---

test("ModelRow's Get a key opens TypeSafe's site through onOpenUrl", () => {
  const onOpenUrl = vi.fn();
  render(<ModelRow {...baseProps({ onOpenUrl })} />);
  fireEvent.click(screen.getByRole("button", { name: "Get a key" }));
  expect(onOpenUrl).toHaveBeenCalledWith("https://typesafe.ai");
});

test("ModelRow shows a password field, never pre-filled, when no key is saved", () => {
  render(<ModelRow {...baseProps({ page: pageWith({ model: { key: "missing" } }) })} />);
  const input = screen.getByLabelText("Key") as HTMLInputElement;
  expect(input.type).toBe("password");
  expect(input.getAttribute("autocomplete")).toBe("off");
  expect(input.value).toBe("");
});

test("ModelRow's Save calls api.saveKey, clears the field and refreshes", async () => {
  const api = createFakeDecisionLoopApi();
  const onRefresh = vi.fn();
  render(
    <ModelRow {...baseProps({ page: pageWith({ model: { key: "missing" } }), api, onRefresh })} />,
  );
  const input = screen.getByLabelText("Key") as HTMLInputElement;
  fireEvent.change(input, { target: { value: "sk-live-abc123" } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  expect(api.requests).toEqual([{ method: "saveKey", args: ["sk-live-abc123"] }]);
  expect(input.value).toBe("");
});

test("ModelRow's Save button stays disabled for a blank key", () => {
  render(<ModelRow {...baseProps({ page: pageWith({ model: { key: "missing" } }) })} />);
  expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
});

test("ModelRow shows 'A key is saved.' with Replace and Remove when a key is saved", () => {
  render(<ModelRow {...baseProps({ page: pageWith({ model: { key: "saved" } }) })} />);
  expect(screen.getByText("A key is saved.")).toBeTruthy();
  expect(screen.queryByLabelText("Key")).toBeNull();
  expect(screen.getByRole("button", { name: "Replace" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Remove" })).toBeTruthy();
});

test("ModelRow's Replace shows the empty field again", () => {
  render(<ModelRow {...baseProps({ page: pageWith({ model: { key: "saved" } }) })} />);
  fireEvent.click(screen.getByRole("button", { name: "Replace" }));
  const input = screen.getByLabelText("Key") as HTMLInputElement;
  expect(input.value).toBe("");
  expect(screen.queryByText("A key is saved.")).toBeNull();
});

test("ModelRow's Remove asks to confirm before calling api.removeKey", async () => {
  const api = createFakeDecisionLoopApi();
  const onRefresh = vi.fn();
  render(
    <ModelRow {...baseProps({ page: pageWith({ model: { key: "saved" } }), api, onRefresh })} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  expect(screen.getByText(/Remove the saved key\?/)).toBeTruthy();
  expect(api.requests).toEqual([]);
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
  expect(api.requests).toEqual([{ method: "removeKey", args: [] }]);
});

test("ModelRow's Remove confirm Cancel removes nothing", () => {
  const api = createFakeDecisionLoopApi();
  render(<ModelRow {...baseProps({ page: pageWith({ model: { key: "saved" } }), api })} />);
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.getByRole("button", { name: "Remove" })).toBeTruthy();
  expect(api.requests).toEqual([]);
});

test("ModelRow's version choice labels the pinned option from page.model.version.display", () => {
  render(
    <ModelRow
      {...baseProps({
        page: pageWith({ model: { version: { setting: "pinned", display: "9.9.9" } } }),
      })}
    />,
  );
  openAdvanced();
  expect(versionGroup().getByRole("radio", { name: "Pinned 9.9.9" })).toBeTruthy();
  expect(versionGroup().getByRole("radio", { name: "Latest stable" })).toBeTruthy();
  expect(versionGroup().getByRole("radio", { name: "Preview" })).toBeTruthy();
});

test("ModelRow's version choice patches decision.model_version and refreshes", async () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  const onRefresh = vi.fn();
  render(<ModelRow {...baseProps({ patch, onRefresh })} />);
  openAdvanced();
  fireEvent.click(versionGroup().getByRole("radio", { name: "Latest stable" }));
  await waitFor(() => expect(patch).toHaveBeenCalledWith({ "decision.model_version": "latest" }));
  expect(onRefresh).toHaveBeenCalledTimes(1);
});

const TEST_ERROR_CASES: ReadonlyArray<readonly [ModelErrorCode, string]> = [
  ["key_rejected", "TypeSafe refused the key."],
  ["unreachable", "TypeSafe could not be reached."],
  ["rate_limited", "TypeSafe asked VIKI to slow down."],
  ["overloaded", "TypeSafe is busy. Try again later."],
  ["no_key", "Save a key first."],
  ["refused", "The test failed."],
  ["too_large", "The test failed."],
  ["invalid_reply", "The test failed."],
  ["failed", "The test failed."],
];

for (const [code, text] of TEST_ERROR_CASES) {
  test(`ModelRow's Test shows "${text}" for ${code}`, async () => {
    const api = createFakeDecisionLoopApi({
      test_result: { ok: false, latency_ms: null, version: null, error: code },
    });
    render(<ModelRow {...baseProps({ api })} />);
    fireEvent.click(screen.getByRole("button", { name: "Test" }));
    await screen.findByText(text);
  });
}

test("ModelRow's Test shows Answered in N ms on success", async () => {
  const api = createFakeDecisionLoopApi({
    test_result: { ok: true, latency_ms: 112, version: "1.14.0", error: null },
  });
  render(<ModelRow {...baseProps({ api })} />);
  fireEvent.click(screen.getByRole("button", { name: "Test" }));
  await screen.findByText("Answered in 112 ms");
});

test("ModelRow's Test falls back to the generic failure text when the call itself fails", async () => {
  const api = createFakeDecisionLoopApi();
  api.fail("test", "unavailable");
  render(<ModelRow {...baseProps({ api })} />);
  fireEvent.click(screen.getByRole("button", { name: "Test" }));
  await screen.findByText("The test failed.");
});

test("ModelRow refreshes the page after a test, win or lose", async () => {
  const onRefresh = vi.fn();
  render(<ModelRow {...baseProps({ onRefresh })} />);
  fireEvent.click(screen.getByRole("button", { name: "Test" }));
  await waitFor(() => expect(onRefresh).toHaveBeenCalledTimes(1));
});

test("ModelRow shows the last error in the Test's own words", () => {
  render(
    <ModelRow {...baseProps({ page: pageWith({ model: { last_error: "rate_limited" } }) })} />,
  );
  expect(screen.getByText("Last error: TypeSafe asked VIKI to slow down.")).toBeTruthy();
});

test("ModelRow shows no last-error line when there is none", () => {
  render(<ModelRow {...baseProps({ page: pageWith({ model: { last_error: null } }) })} />);
  expect(screen.queryByText(/^Last error:/)).toBeNull();
});

test("ModelRow disables its controls when not writable, with no hint line of its own", () => {
  render(
    <ModelRow {...baseProps({ writable: false, page: pageWith({ model: { key: "saved" } }) })} />,
  );
  openAdvanced();
  expect((screen.getByRole("button", { name: "Get a key" }) as HTMLButtonElement).disabled).toBe(
    true,
  );
  expect((screen.getByRole("button", { name: "Replace" }) as HTMLButtonElement).disabled).toBe(
    true,
  );
  expect((screen.getByRole("button", { name: "Remove" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "Test" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByLabelText("Daily cap") as HTMLSelectElement).disabled).toBe(true);
  const compare = within(screen.getByRole("group", { name: "Compare with another model version" }));
  expect((compare.getByRole("radio", { name: "Pinned" }) as HTMLInputElement).disabled).toBe(true);
  expect(screen.queryByText("Settings aren't ready to change right now.")).toBeNull();
});

test("ModelRow's version radios do not patch while not writable, even though jsdom still fires a disabled radio's onChange", () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  render(<ModelRow {...baseProps({ writable: false, patch })} />);
  openAdvanced();
  fireEvent.click(versionGroup().getByRole("radio", { name: "Latest stable" }));
  expect(patch).not.toHaveBeenCalled();
});

test("ModelRow shows the Decision model description", () => {
  render(<ModelRow {...baseProps()} />);
  expect(
    screen.getByText(
      "Optional and paid. A TypeSafe model helps VIKI judge what matters and when to nudge you. Without a key, VIKI uses simple rules: it lists what it notices, with no badges, nudges or alerts. This key is separate from your chat model.",
    ),
  ).toBeTruthy();
});

const MODEL_CHIPS: ReadonlyArray<readonly [PageView["model"]["state"], string]> = [
  ["ready", "Ready"],
  ["rules", "Simple rules"],
  ["no_key", "Simple rules · no key"],
  ["key_rejected", "Key rejected · using rules"],
  ["capped", "Daily cap reached · using rules"],
  ["unreachable", "Can't reach TypeSafe · using rules"],
];

for (const [state, chip] of MODEL_CHIPS) {
  test(`ModelRow's chip reads "${chip}" in the ${state} state`, () => {
    render(<ModelRow {...baseProps({ page: pageWith({ model: { state } }) })} />);
    expect(screen.getByRole("status").textContent).toBe(chip);
  });
}

test("ModelRow's Spending shows the cap, its hint and the request rate", () => {
  render(<ModelRow {...baseProps()} />);
  expect(screen.getByRole("heading", { name: "Spending", level: 4 })).toBeTruthy();
  const cap = screen.getByLabelText("Daily cap") as HTMLSelectElement;
  expect(cap.value).toBe("0.5");
  expect([...cap.options].map((option) => option.textContent)).toEqual([
    "$0.10",
    "$0.25",
    "$0.50",
    "$1.00",
    "$2.00",
  ]);
  expect(cap.getAttribute("aria-describedby")).toBe("model-cap-hint");
  expect(document.getElementById("model-cap-hint")?.textContent).toBe(
    "Once today's spend reaches this, VIKI uses simple rules until tomorrow.",
  );
  expect(screen.getByText("VIKI sends at most 10 requests a minute.")).toBeTruthy();
});

test("ModelRow's Daily cap shows $0.50 when the stored cap is not an option", () => {
  render(<ModelRow {...baseProps({ page: pageWith({ today: { cap_usd: 0.33 } }) })} />);
  expect((screen.getByLabelText("Daily cap") as HTMLSelectElement).value).toBe("0.5");
});

test("ModelRow's Daily cap patches decision.daily_cap_usd and refreshes", async () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  const onRefresh = vi.fn();
  render(<ModelRow {...baseProps({ patch, onRefresh })} />);
  fireEvent.change(screen.getByLabelText("Daily cap"), { target: { value: "1" } });
  await waitFor(() => expect(patch).toHaveBeenCalledWith({ "decision.daily_cap_usd": 1 }));
  expect(onRefresh).toHaveBeenCalledTimes(1);
});

test("ModelRow's Daily cap shows an error and does not refresh when patch rejects", async () => {
  const patch = vi.fn().mockRejectedValue(new Error("boom"));
  const onRefresh = vi.fn();
  render(<ModelRow {...baseProps({ patch, onRefresh })} />);
  fireEvent.change(screen.getByLabelText("Daily cap"), { target: { value: "2" } });
  await screen.findByText("Could not save that change. Try again.");
  expect(onRefresh).not.toHaveBeenCalled();
  expect((screen.getByLabelText("Daily cap") as HTMLSelectElement).disabled).toBe(false);
});

test("ModelRow's today line adds the comparison share while comparison requests ran", () => {
  render(
    <ModelRow
      {...baseProps({
        page: pageWith({
          today: { decisions: 1412, model_calls: 1426, shadow_calls: 6, cost_usd: 0.07 },
        }),
      })}
    />,
  );
  expect(
    screen.getByText("Today: 1,412 decisions · 1,426 model requests (6 for comparison) · $0.07"),
  ).toBeTruthy();
});

test("ModelRow's today line has no comparison share without comparison requests", () => {
  render(
    <ModelRow
      {...baseProps({
        page: pageWith({
          today: { decisions: 12, model_calls: 14, shadow_calls: 0, cost_usd: 0.004 },
        }),
      })}
    />,
  );
  expect(screen.getByText("Today: 12 decisions · 14 model requests · < $0.01")).toBeTruthy();
});

test("ModelRow's capped line shows only while today's cap is reached", () => {
  const { rerender } = render(
    <ModelRow {...baseProps({ page: pageWith({ today: { capped: true } }) })} />,
  );
  expect(screen.getByText("Daily cap reached: simple rules until tomorrow.")).toBeTruthy();
  rerender(<ModelRow {...baseProps({ page: pageWith({ today: { capped: false } }) })} />);
  expect(screen.queryByText("Daily cap reached: simple rules until tomorrow.")).toBeNull();
});

test("ModelRow's Advanced disclosure starts closed and is named by its summary", () => {
  render(<ModelRow {...baseProps()} />);
  const details = document.querySelector<HTMLDetailsElement>("#model-body details");
  expect(details?.open).toBe(false);
  expect(details?.querySelector("summary")?.textContent).toBe("Advanced: version and comparison");
});

test("ModelRow's Version options say what each one does", () => {
  render(<ModelRow {...baseProps()} />);
  openAdvanced();
  const version = versionGroup();
  expect(version.getByText("Stays on this version.")).toBeTruthy();
  expect(version.getByText("Follows TypeSafe's latest stable version.")).toBeTruthy();
  expect(version.getByText("Follows TypeSafe's preview version.")).toBeTruthy();
});

// --- Compare with another model version (Advanced) ---

function compareGroup() {
  return within(screen.getByRole("group", { name: "Compare with another model version" }));
}

test("the comparison choice defaults to Off and saves decision.shadow", async () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  const onRefresh = vi.fn();
  render(<ModelRow {...baseProps({ patch, onRefresh })} />);
  openAdvanced();
  expect((compareGroup().getByRole("radio", { name: "Off" }) as HTMLInputElement).checked).toBe(
    true,
  );
  fireEvent.click(compareGroup().getByRole("radio", { name: "Latest stable" }));
  await waitFor(() => expect(patch).toHaveBeenCalledWith({ "decision.shadow": "latest" }));
  expect(onRefresh).toHaveBeenCalledTimes(1);
});

test("the comparison choice reflects a saved decision.shadow value", () => {
  render(<ModelRow {...baseProps({ values: { "decision.shadow": "preview" } })} />);
  openAdvanced();
  expect((compareGroup().getByRole("radio", { name: "Preview" }) as HTMLInputElement).checked).toBe(
    true,
  );
});

test("explains the comparison never changes what VIKI decides, and that a stuck TypeSafe pauses live requests too", () => {
  render(<ModelRow {...baseProps()} />);
  openAdvanced();
  expect(
    screen.getByText(
      "Each new decision also goes to this version for comparison. Its answers do not change VIKI's decisions. Each comparison is an extra paid request within your daily cap and the 10-a-minute limit. If TypeSafe rejects your key or says it is busy, VIKI pauses all Decision model requests, including requests to the version in use.",
    ),
  ).toBeTruthy();
});

test("hints that nothing runs in the comparison once the choice matches the version in use", () => {
  render(
    <ModelRow
      {...baseProps({
        page: pageWith({ model: { version: { setting: "pinned", display: "1.14.0" } } }),
        values: { "decision.shadow": "pinned" },
      })}
    />,
  );
  openAdvanced();
  expect(
    screen.getByText("This is the version already in use, so no comparison requests run."),
  ).toBeTruthy();
});

test("shows no hint while the comparison choice differs from the version in use", () => {
  render(
    <ModelRow
      {...baseProps({
        page: pageWith({ model: { version: { setting: "pinned", display: "1.14.0" } } }),
        values: { "decision.shadow": "latest" },
      })}
    />,
  );
  openAdvanced();
  expect(
    screen.queryByText("This is the version already in use, so no comparison requests run."),
  ).toBeNull();
});

test("shows the comparison's own agreement once it is on", () => {
  render(<ModelRow {...baseProps()} />);
  openAdvanced();
  expect(
    screen.getByText(
      "Same attention level as the version in use on 41 of 50 decisions; same item type on 45.",
    ),
  ).toBeTruthy();
});

test("the comparison radios do not patch while not writable, even though jsdom still fires a disabled radio's onChange", () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  render(<ModelRow {...baseProps({ writable: false, patch })} />);
  openAdvanced();
  fireEvent.click(compareGroup().getByRole("radio", { name: "Pinned" }));
  expect(patch).not.toHaveBeenCalled();
});

test("a failed comparison save shows the error line and re-enables the choice", async () => {
  const patch = vi.fn().mockRejectedValue(new Error("boom"));
  render(<ModelRow {...baseProps({ patch })} />);
  openAdvanced();
  fireEvent.click(compareGroup().getByRole("radio", { name: "Latest stable" }));
  await screen.findByText("Could not save. Try again.");
  expect(
    (compareGroup().getByRole("radio", { name: "Latest stable" }) as HTMLInputElement).disabled,
  ).toBe(false);
});

// --- PrivacyRow ---

test("PrivacyRow starts with what goes to the Decision model", () => {
  render(<PrivacyRow {...baseProps()} />);
  const intro = screen.getByText(/^Titles, note paths/);
  expect(intro.textContent).toBe(
    "Titles, note paths, facts in words, and note excerpts cut to your Send level go to the Decision model, so a short note can go whole. Chats go only by their titles; their messages are not sent to that model. You can exclude folders or tags from these requests.",
  );
  expect(document.querySelector("#privacy-body > :first-child")).toBe(intro);
});

test("PrivacyRow's never-send hint gives an example of a folder and of a tag", () => {
  render(<PrivacyRow {...baseProps()} />);
  expect(document.getElementById("never-send-hint")?.textContent).toBe(
    'Add a folder ending in "/" (for example Private/) or a tag starting with "#" (for example #personal).',
  );
});

test("PrivacyRow names each Remove button after its entry", () => {
  render(
    <PrivacyRow {...baseProps({ values: { "decision.never_send": '["Private/","#tag"]' } })} />,
  );
  expect(screen.getByRole("button", { name: "Remove Private/" }).textContent).toBe("Remove");
  expect(screen.getByRole("button", { name: "Remove #tag" })).toBeTruthy();
});

test("PrivacyRow's send-level choice shows the three plain labels", () => {
  render(<PrivacyRow {...baseProps()} />);
  expect(screen.getByRole("radio", { name: "Titles only" })).toBeTruthy();
  expect(
    screen.getByRole("radio", { name: "Titles and short excerpts (up to 200 characters)" }),
  ).toBeTruthy();
  expect(
    screen.getByRole("radio", { name: "Titles and sections (up to 2,000 characters)" }),
  ).toBeTruthy();
});

test("PrivacyRow's send-level choice patches decision.send_level", async () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  render(<PrivacyRow {...baseProps({ values: { "decision.send_level": "excerpts" }, patch })} />);
  fireEvent.click(screen.getByRole("radio", { name: "Titles only" }));
  await waitFor(() => expect(patch).toHaveBeenCalledWith({ "decision.send_level": "titles" }));
});

test("PrivacyRow's send-level radios do not patch while not writable", () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  render(<PrivacyRow {...baseProps({ writable: false, patch })} />);
  fireEvent.click(screen.getByRole("radio", { name: "Titles only" }));
  expect(patch).not.toHaveBeenCalled();
});

test("PrivacyRow adds a valid folder entry and saves compact JSON", async () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  render(<PrivacyRow {...baseProps({ values: { "decision.never_send": "[]" }, patch })} />);
  fireEvent.change(screen.getByLabelText("Never send"), { target: { value: "proj/" } });
  fireEvent.click(screen.getByRole("button", { name: "Add" }));
  await waitFor(() => expect(patch).toHaveBeenCalledWith({ "decision.never_send": '["proj/"]' }));
});

test("PrivacyRow accepts a tag starting with # too", async () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  render(<PrivacyRow {...baseProps({ values: { "decision.never_send": "[]" }, patch })} />);
  fireEvent.change(screen.getByLabelText("Never send"), { target: { value: "#private" } });
  fireEvent.click(screen.getByRole("button", { name: "Add" }));
  await waitFor(() =>
    expect(patch).toHaveBeenCalledWith({ "decision.never_send": '["#private"]' }),
  );
});

test("PrivacyRow's Add stays disabled for an entry that is not a folder or a tag", () => {
  render(<PrivacyRow {...baseProps({ values: { "decision.never_send": "[]" } })} />);
  fireEvent.change(screen.getByLabelText("Never send"), { target: { value: "not-a-folder" } });
  expect((screen.getByRole("button", { name: "Add" }) as HTMLButtonElement).disabled).toBe(true);
});

test("PrivacyRow's Add ignores a duplicate entry", () => {
  render(<PrivacyRow {...baseProps({ values: { "decision.never_send": '["x/"]' } })} />);
  fireEvent.change(screen.getByLabelText("Never send"), { target: { value: "x/" } });
  expect((screen.getByRole("button", { name: "Add" }) as HTMLButtonElement).disabled).toBe(true);
});

test("PrivacyRow caps the list at 32 entries", () => {
  const full = JSON.stringify(Array.from({ length: 32 }, (_, i) => `#t${i}`));
  render(<PrivacyRow {...baseProps({ values: { "decision.never_send": full } })} />);
  fireEvent.change(screen.getByLabelText("Never send"), { target: { value: "#new" } });
  expect((screen.getByRole("button", { name: "Add" }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText("You've listed the most VIKI allows (32).")).toBeTruthy();
});

test("PrivacyRow removes one entry by saving the list without it", async () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  render(
    <PrivacyRow {...baseProps({ values: { "decision.never_send": '["x/","#tag"]' }, patch })} />,
  );
  const row = screen.getByText("x/").closest("li") as HTMLElement;
  fireEvent.click(row.querySelector("button") as HTMLButtonElement);
  await waitFor(() => expect(patch).toHaveBeenCalledWith({ "decision.never_send": '["#tag"]' }));
});

test("PrivacyRow's damaged never_send: shows the warning, starts the editor empty, and adding an entry replaces the sentinel", async () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  render(
    <PrivacyRow {...baseProps({ values: { "decision.never_send": "!unreadable" }, patch })} />,
  );
  expect(screen.getByText(/Your never-send list could not be read\./)).toBeTruthy();
  expect(screen.queryByRole("listitem")).toBeNull();
  fireEvent.change(screen.getByLabelText("Never send"), { target: { value: "proj/" } });
  fireEvent.click(screen.getByRole("button", { name: "Add" }));
  await waitFor(() => expect(patch).toHaveBeenCalledWith({ "decision.never_send": '["proj/"]' }));
});

test("PrivacyRow's damaged never_send: Clear the damaged list asks to confirm before saving an empty list", async () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  render(
    <PrivacyRow {...baseProps({ values: { "decision.never_send": "!unreadable" }, patch })} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Clear the damaged list" }));
  expect(
    screen.getByText(
      "VIKI will send titles and excerpts again, with no never-send list. Clear it?",
    ),
  ).toBeTruthy();
  expect(patch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Clear" }));
  await waitFor(() => expect(patch).toHaveBeenCalledWith({ "decision.never_send": "[]" }));
});

test("PrivacyRow's damaged never_send: Cancel on the clear confirm saves nothing", () => {
  const patch = vi.fn().mockResolvedValue(undefined);
  render(
    <PrivacyRow {...baseProps({ values: { "decision.never_send": "!unreadable" }, patch })} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Clear the damaged list" }));
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(patch).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "Clear the damaged list" })).toBeTruthy();
});

test("PrivacyRow's held-back count renders in words", () => {
  const { rerender } = render(
    <PrivacyRow {...baseProps({ page: pageWith({ privacy: { held_back_today: 0 } }) })} />,
  );
  expect(screen.getByText("Nothing has been held back today.")).toBeTruthy();

  rerender(<PrivacyRow {...baseProps({ page: pageWith({ privacy: { held_back_today: 1 } }) })} />);
  expect(
    screen.getByText("Your never-send list held something back from 1 request today."),
  ).toBeTruthy();

  rerender(<PrivacyRow {...baseProps({ page: pageWith({ privacy: { held_back_today: 4 } }) })} />);
  expect(
    screen.getByText("Your never-send list held something back from 4 requests today."),
  ).toBeTruthy();
});

test("PrivacyRow's See the last state sent opens the Context tab", () => {
  const onOpenContext = vi.fn();
  render(<PrivacyRow {...baseProps({ onOpenContext })} />);
  fireEvent.click(screen.getByRole("button", { name: "See the last state sent" }));
  expect(onOpenContext).toHaveBeenCalledTimes(1);
});

test("PrivacyRow disables its controls when not writable, with no hint line of its own", () => {
  render(
    <PrivacyRow {...baseProps({ writable: false, values: { "decision.never_send": '["x/"]' } })} />,
  );
  expect((screen.getByLabelText("Never send") as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "Add" }) as HTMLButtonElement).disabled).toBe(true);
  expect(
    (screen.getByRole("button", { name: "See the last state sent" }) as HTMLButtonElement).disabled,
  ).toBe(true);
  const row = screen.getByText("x/").closest("li") as HTMLElement;
  expect((row.querySelector("button") as HTMLButtonElement).disabled).toBe(true);
  expect(screen.queryByText("Settings aren't ready to change right now.")).toBeNull();
});
