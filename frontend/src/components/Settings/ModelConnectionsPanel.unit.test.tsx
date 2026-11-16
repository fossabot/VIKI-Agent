import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { MAGPIE_BASE_URL } from "../../clients/app/magpieApi";
import {
  ModelConnectionError,
  type ModelConnectionsApi,
  ModelConnectionsContext,
  type ModelConnectionsListing,
} from "../../clients/app/modelConnectionsApi";
import { createShellStore } from "../../state/shell";
import { AppStore } from "../../state/store";
import { fakeShellApi, settingsFixture } from "../../testShell";
import { usePrestartChip } from "../Composer/prestartChip";
import { ModelConnectionsPanel } from "./ModelConnectionsPanel";
import { ModelDefaultField } from "./ModelDefaultField";

afterEach(cleanup);
const empty: ModelConnectionsListing = { revision: 0, connections: [], default_model: null };
const id = "11111111-1111-4111-8111-111111111111";
const saved = {
  ...empty,
  revision: 1,
  connections: [
    {
      id,
      name: "My local model",
      base_url: "http://127.0.0.1:11434/v1",
      model: "example/model",
      api_mode: "chat_completions" as const,
      has_api_key: true,
      model_ref: `viki-llm/${id}/example/model`,
      is_default: false,
    },
  ],
};

function fakeApi(): ModelConnectionsApi {
  return {
    list: vi.fn(async () => empty),
    save: vi.fn(async () => saved),
    remove: vi.fn(async () => empty),
    updateKey: vi.fn(async () => ({ ...saved, revision: 2 })),
    test: vi.fn(async () => ["example/model"]),
    discover: vi.fn(async () => ["example/model"]),
  };
}

function expandAdvanced() {
  const summary = screen.getByText("Advanced — Direct connections", { exact: true });
  const details = summary.closest("details");
  if (!details) throw new Error("missing Advanced disclosure");
  if (!details.open) fireEvent.click(summary);
}

function withMagpie() {
  const state = {
    available: true,
    running: false,
    version: "0.1.3357",
    desktop: false,
    base_url: MAGPIE_BASE_URL,
  };
  return {
    ...fakeApi(),
    magpie: {
      status: vi.fn(async () => state),
      open: vi.fn(async () => ({ ...state, running: true })),
      view: vi.fn(async () => state),
      close: vi.fn(async () => state),
    },
  };
}

function magpieListing(model = "group/main", hasKey = false): ModelConnectionsListing {
  return {
    ...empty,
    revision: 1,
    connections: [
      {
        id,
        name: [...`Magpie · ${model}`].slice(0, 80).join(""),
        base_url: MAGPIE_BASE_URL,
        api_mode: "anthropic",
        model,
        has_api_key: hasKey,
        model_ref: `viki-llm/${id}/${model}`,
        is_default: false,
      },
    ],
  };
}

async function showModels(api: ModelConnectionsApi, settings = settingsFixture()) {
  const shellApi = fakeShellApi({ settings });
  const store = createShellStore(shellApi, new AppStore());
  await store.refreshSettings();
  const result = render(
    <ModelConnectionsContext.Provider value={api}>
      <ModelConnectionsPanel store={store} snapshot={store.getSnapshot()} />
    </ModelConnectionsContext.Provider>,
  );
  await act(async () => {});
  return { ...result, store, shellApi };
}

function chooseMagpie() {
  fireEvent.click(screen.getByRole("button", { name: "Choose a model or group" }));
  return within(screen.getByRole("group", { name: "Choose a model or group" }));
}

test("Claude and Magpie are the main choices while a direct default remains unchanged in collapsed Advanced", async () => {
  const api = { ...withMagpie(), list: vi.fn(async () => saved) };
  const result = await showModels(
    api,
    settingsFixture({ "chat.default_model": saved.connections[0]?.model_ref }),
  );
  const heading = screen.getByRole("heading", { name: "Chat models" });
  expect(heading.querySelector("img")).toBeNull();
  expect(result.container.querySelector('img[src*="/brand/mark.png"]')).toBeNull();
  const advanced = screen.getByText("Advanced — Direct connections").closest("details");
  expect(advanced?.open).toBe(false);
  const current = result.container.querySelector(".llm-current");
  expect(current?.textContent).toContain("My local model");
  expect(current?.textContent).toContain("Direct connection");
  expect(current?.textContent).not.toContain("Claude Code sign-in");
  expect(result.shellApi.calls.some((call) => call.method === "patchSettings")).toBe(false);
  expect(api.save).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Use Claude Code sign-in for new chats" }));
  await waitFor(
    () =>
      expect(
        result.shellApi.calls.find((call) => call.method === "patchSettings")?.args[2],
      ).toEqual({ "chat.default_model": null }),
    { timeout: 50 },
  );
});

test("a missing qualified default remains visibly unavailable without a fallback or mutation", async () => {
  const reference = `viki-llm/${id}/missing-model`;
  const api = withMagpie();
  const result = await showModels(api, settingsFixture({ "chat.default_model": reference }));
  const current = result.container.querySelector(".llm-current");
  expect(current?.textContent).toContain("Unavailable saved connection");
  expect(current?.textContent).toContain(reference);
  expect(current?.textContent).not.toContain("Claude Code sign-in");
  expect(result.shellApi.calls.some((call) => call.method === "patchSettings")).toBe(false);
  expect(api.save).not.toHaveBeenCalled();
});

test.each(["native", "__proto__", "<group>/Model:V1", `group/${"🧪".repeat(180)}`])(
  "Magpie discovers and saves the exact opaque model %s without provider fields or a default change",
  async (model) => {
    const api = {
      ...withMagpie(),
      discover: vi.fn(async () => [model]),
      save: vi.fn<ModelConnectionsApi["save"]>(async () => magpieListing(model)),
    };
    const result = await showModels(api);
    const picker = chooseMagpie();
    expect(api.discover).not.toHaveBeenCalled();
    expect(api.test).not.toHaveBeenCalled();
    expect(picker.queryByLabelText("API base URL")).toBeNull();
    expect(picker.queryByLabelText("API format")).toBeNull();
    expect(picker.queryByLabelText("Connection name")).toBeNull();
    expect(picker.queryByLabelText("API key (optional for local servers)")).toBeNull();
    expect(
      picker.getByText("Gateway access key", { selector: "summary" }).closest("details")?.open,
    ).toBe(false);
    fireEvent.click(picker.getByRole("button", { name: "List models" }));
    const selection = await picker.findByLabelText(
      "Magpie model or routing group",
      {},
      { timeout: 50 },
    );
    expect((selection as HTMLSelectElement).value).toBe(model);
    expect((selection as HTMLSelectElement).labels?.[0]?.textContent).toBe(
      "Magpie model or routing group",
    );
    expect(api.discover).toHaveBeenCalledWith(
      {
        name: "Magpie",
        base_url: MAGPIE_BASE_URL,
        api_mode: "anthropic",
        model: "",
        api_key: "",
      },
      expect.any(AbortSignal),
    );
    expect(picker.getByRole("option", { name: model }).textContent).toBe(model);
    expect(result.container.querySelector("group")).toBeNull();
    fireEvent.click(picker.getByRole("button", { name: "Add model to VIKI" }));
    await waitFor(() => expect(api.save).toHaveBeenCalledOnce(), { timeout: 50 });
    const connection = api.save.mock.calls[0]?.[1];
    expect(connection).toEqual({
      name: [...`Magpie · ${model}`].slice(0, 80).join(""),
      base_url: MAGPIE_BASE_URL,
      api_mode: "anthropic",
      model,
      api_key: "",
    });
    expect([...(connection?.name ?? "")].length).toBeLessThanOrEqual(80);
    expect(api.test).not.toHaveBeenCalled();
    expect(result.shellApi.calls.some((call) => call.method === "patchSettings")).toBe(false);
    expect(screen.queryByLabelText("Gateway access key")).toBeNull();
    await screen.findByRole("button", { name: "Use for new chats" }, { timeout: 50 });
    fireEvent.click(screen.getByRole("button", { name: "Use for new chats" }));
    await waitFor(
      () =>
        expect(
          result.shellApi.calls.find((call) => call.method === "patchSettings")?.args[2],
        ).toEqual({ "chat.default_model": magpieListing(model).connections[0]?.model_ref }),
      { timeout: 50 },
    );
  },
);

test("an optional gateway key belongs only to Magpie discovery/save and clears after saving", async () => {
  const api = { ...withMagpie(), save: vi.fn(async () => magpieListing("example/model", true)) };
  await showModels(api);
  expandAdvanced();
  fireEvent.change(screen.getByLabelText("API key (optional for local servers)"), {
    target: { value: "other-key" },
  });
  const picker = chooseMagpie();
  fireEvent.click(picker.getByText("Gateway access key", { selector: "summary" }));
  fireEvent.change(picker.getByLabelText("Gateway access key"), {
    target: { value: "gateway-key" },
  });
  fireEvent.click(picker.getByRole("button", { name: "List models" }));
  await picker.findByLabelText("Magpie model or routing group", {}, { timeout: 50 });
  expect(api.discover).toHaveBeenCalledWith(
    expect.objectContaining({ api_key: "gateway-key" }),
    expect.any(AbortSignal),
  );
  fireEvent.click(picker.getByRole("button", { name: "Add model to VIKI" }));
  await waitFor(
    () =>
      expect(api.save).toHaveBeenCalledWith(0, expect.objectContaining({ api_key: "gateway-key" })),
    { timeout: 50 },
  );
  expect(
    (screen.getByLabelText("API key (optional for local servers)") as HTMLInputElement).value,
  ).toBe("other-key");
  expect(screen.queryByLabelText("Gateway access key")).toBeNull();
  const reopened = chooseMagpie();
  expect((reopened.getByLabelText("Gateway access key") as HTMLInputElement).value).toBe("");
});

test("an existing managed Responses route is kept as Magpie and duplicate selection never recreates it", async () => {
  const listing = magpieListing("example/model", true);
  const existing = listing.connections[0];
  if (!existing) throw new Error("missing managed connection");
  existing.api_mode = "responses";
  const api = { ...withMagpie(), list: vi.fn(async () => listing) };
  const result = await showModels(api);
  const row = result.container.querySelector(".llm-connection");
  expect(row?.closest(".llm-advanced")).toBeNull();
  expect(
    screen.getByRole("button", { name: "Update gateway key: Magpie · example/model" }),
  ).toBeTruthy();
  const picker = chooseMagpie();
  fireEvent.click(picker.getByRole("button", { name: "List models" }));
  await picker.findByText("This model or group is already added to VIKI.", {}, { timeout: 50 });
  expect(picker.getByRole("button", { name: "Add model to VIKI" }).matches(":disabled")).toBe(true);
  fireEvent.submit(
    picker.getByRole("button", { name: "Add model to VIKI" }).closest("form") as HTMLFormElement,
  );
  expect(api.save).not.toHaveBeenCalled();
  expect(api.updateKey).not.toHaveBeenCalled();
  expect(listing.connections[0]?.api_mode).toBe("responses");
  expect(listing.connections[0]?.has_api_key).toBe(true);
  expect(result.shellApi.calls.some((call) => call.method === "patchSettings")).toBe(false);
});

test.each(["read-only", "capacity", "loading"])(
  "Magpie selection respects %s fences",
  async (kind) => {
    const direct = saved.connections[0];
    if (!direct) throw new Error("missing direct connection");
    const listing = {
      ...empty,
      connections: Array.from({ length: 8 }, (_, index) => {
        const connectionId = `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`;
        return {
          ...direct,
          id: connectionId,
          name: `Direct ${index}`,
          model_ref: `viki-llm/${connectionId}/example/model`,
        };
      }),
    };
    const api = {
      ...withMagpie(),
      list: vi.fn<ModelConnectionsApi["list"]>(async () => (kind === "capacity" ? listing : empty)),
    };
    if (kind === "loading") api.list.mockImplementation(() => new Promise(() => {}));
    await showModels(api, settingsFixture({}, { read_only: kind === "read-only" }));
    expect(
      screen.getByRole("button", { name: "Choose a model or group" }).matches(":disabled"),
    ).toBe(true);
    expect(screen.getByRole("button", { name: "Open Magpie" }).matches(":disabled")).toBe(false);
    expect(api.discover).not.toHaveBeenCalled();
    expect(api.save).not.toHaveBeenCalled();
  },
);

test.each(["empty", "unavailable", "stale_revision"])(
  "Magpie reports %s without inventing a saved model or retaining its key",
  async (outcome) => {
    const api = {
      ...withMagpie(),
      discover: vi.fn(async () => (outcome === "empty" ? [] : ["example/model"])),
      save: vi.fn(async () => {
        throw new ModelConnectionError("stale_revision");
      }),
    };
    if (outcome === "unavailable")
      api.discover.mockRejectedValue(new ModelConnectionError("endpoint_unreachable"));
    await showModels(api);
    const picker = chooseMagpie();
    fireEvent.change(picker.getByLabelText("Gateway access key"), {
      target: { value: "gateway-key" },
    });
    fireEvent.click(picker.getByRole("button", { name: "List models" }));
    if (outcome === "empty") {
      await picker.findByText(/Magpie returned no models/, {}, { timeout: 50 });
      expect(picker.queryByRole("button", { name: "Add model to VIKI" })).toBeNull();
      fireEvent.click(picker.getByRole("button", { name: "Cancel" }));
      expect(screen.queryByLabelText("Gateway access key")).toBeNull();
    } else {
      if (outcome === "stale_revision") {
        await picker.findByLabelText("Magpie model or routing group", {}, { timeout: 50 });
        fireEvent.click(picker.getByRole("button", { name: "Add model to VIKI" }));
      }
      await screen.findByRole("alert", {}, { timeout: 50 });
      expect((picker.getByLabelText("Gateway access key") as HTMLInputElement).value).toBe("");
      expect(screen.getByRole("alert").textContent).toContain(
        outcome === "stale_revision" ? "Settings changed" : "Couldn't complete",
      );
    }
    expect(api.test).not.toHaveBeenCalled();
  },
);

test("Magpie opens on request and its model picker does not reuse the direct connection key", async () => {
  const state = {
    available: true,
    running: false,
    version: "0.1.3357",
    desktop: false,
    base_url: "http://127.0.0.1:3426/v1",
  };
  const magpie = {
    status: vi.fn(async () => state),
    open: vi.fn(async () => ({ ...state, running: true })),
    view: vi.fn(async () => state),
    close: vi.fn(async () => state),
  };
  const api = { ...fakeApi(), magpie };
  const store = createShellStore(fakeShellApi(), new AppStore());
  await store.refreshSettings();
  render(
    <ModelConnectionsContext.Provider value={api}>
      <ModelConnectionsPanel store={store} snapshot={store.getSnapshot()} />
    </ModelConnectionsContext.Provider>,
  );
  const open = await screen.findByRole("button", { name: "Open Magpie" }, { timeout: 50 });
  await waitFor(() => expect(open.matches(":disabled")).toBe(false), { timeout: 50 });
  expect(magpie.open).not.toHaveBeenCalled();
  expandAdvanced();
  fireEvent.change(screen.getByLabelText("API key (optional for local servers)"), {
    target: { value: "synthetic-other-provider-key" },
  });
  fireEvent.change(screen.getByLabelText("Model ID"), { target: { value: "another-model" } });
  fireEvent.click(screen.getByRole("button", { name: "Choose a model or group" }));
  const picker = within(screen.getByRole("group", { name: "Choose a model or group" }));
  expect(picker.queryByLabelText("Connection name")).toBeNull();
  expect(picker.queryByLabelText("API base URL")).toBeNull();
  expect(picker.queryByLabelText("API format")).toBeNull();
  expect(picker.queryByLabelText("API key (optional for local servers)")).toBeNull();
  expect((picker.getByLabelText("Gateway access key") as HTMLInputElement).value).toBe("");
  expect((screen.getByLabelText("Model ID") as HTMLInputElement).value).toBe("another-model");
  expect(
    (screen.getByLabelText("API key (optional for local servers)") as HTMLInputElement).value,
  ).toBe("synthetic-other-provider-key");
  fireEvent.click(open);
  await waitFor(() => expect(magpie.open).toHaveBeenCalledOnce(), { timeout: 50 });
  expect(magpie.open).toHaveBeenCalledWith(null, expect.any(AbortSignal));
  expect(api.save).not.toHaveBeenCalled();
  expect(api.test).not.toHaveBeenCalled();
  expect(api.discover).not.toHaveBeenCalled();
});

test("a build without Magpie shows its setup instruction and disables its controls", async () => {
  const magpie = {
    status: vi.fn(async () => ({
      available: false,
      running: false,
      version: "0.1.3357",
      desktop: false,
      base_url: "http://127.0.0.1:3426/v1",
    })),
    open: vi.fn(),
    view: vi.fn(),
    close: vi.fn(),
  };
  const store = createShellStore(fakeShellApi(), new AppStore());
  await store.refreshSettings();
  render(
    <ModelConnectionsContext.Provider value={{ ...fakeApi(), magpie }}>
      <ModelConnectionsPanel store={store} snapshot={store.getSnapshot()} />
    </ModelConnectionsContext.Provider>,
  );
  await screen.findByText(/Magpie is missing from this build/, {}, { timeout: 50 });
  expect(screen.getByRole("button", { name: "Open Magpie" }).matches(":disabled")).toBe(true);
  expect(screen.getByRole("button", { name: "Choose a model or group" }).matches(":disabled")).toBe(
    true,
  );
  expect(magpie.open).not.toHaveBeenCalled();
});

test("Magpie cannot replace the draft while another endpoint's model discovery is pending", async () => {
  let complete!: (models: string[]) => void;
  const api = {
    ...fakeApi(),
    magpie: {
      status: vi.fn(async () => ({
        available: true,
        running: false,
        version: "0.1.3357",
        desktop: true,
        base_url: "http://127.0.0.1:3426/v1",
      })),
      open: vi.fn(),
      view: vi.fn(),
      close: vi.fn(),
    },
    discover: vi.fn<ModelConnectionsApi["discover"]>(
      () =>
        new Promise((resolve) => {
          complete = resolve;
        }),
    ),
  };
  const store = createShellStore(fakeShellApi(), new AppStore());
  await store.refreshSettings();
  render(
    <ModelConnectionsContext.Provider value={api}>
      <ModelConnectionsPanel store={store} snapshot={store.getSnapshot()} />
    </ModelConnectionsContext.Provider>,
  );
  const connect = await screen.findByRole(
    "button",
    { name: "Choose a model or group" },
    { timeout: 50 },
  );
  await waitFor(() => expect(connect.matches(":disabled")).toBe(false), { timeout: 50 });
  expandAdvanced();
  fireEvent.click(screen.getByRole("button", { name: "Local server" }));
  fireEvent.click(screen.getByRole("button", { name: "List models" }));
  expect(connect.matches(":disabled")).toBe(true);
  fireEvent.click(connect);
  expect((screen.getByLabelText("API base URL") as HTMLInputElement).value).toBe(
    "http://127.0.0.1:11434/v1",
  );
  await act(async () => complete(["local-model"]));
  await waitFor(() => expect(connect.matches(":disabled")).toBe(false), { timeout: 50 });
  fireEvent.click(connect);
  expect((screen.getByLabelText("API base URL") as HTMLInputElement).value).toBe(
    "http://127.0.0.1:11434/v1",
  );
  expect(screen.getByRole("group", { name: "Choose a model or group" })).toBeTruthy();
  expect((screen.getByLabelText("Model ID") as HTMLInputElement).value).toBe("local-model");
});

test("opening setup makes no provider test; saving clears the key and choosing a default is separate", async () => {
  const api = fakeApi();
  const shellApi = fakeShellApi();
  const store = createShellStore(shellApi, new AppStore());
  await store.refreshSettings();
  render(
    <ModelConnectionsContext.Provider value={api}>
      <ModelConnectionsPanel store={store} snapshot={store.getSnapshot()} />
    </ModelConnectionsContext.Provider>,
  );
  await screen.findByText("Add connection", {}, { timeout: 50 });
  expandAdvanced();
  await waitFor(
    () => expect(screen.getByLabelText("Connection name").matches(":disabled")).toBe(false),
    { timeout: 50 },
  );
  expect(api.test).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("Connection name"), {
    target: { value: "My local model" },
  });
  fireEvent.click(screen.getByText("Local server"));
  fireEvent.change(screen.getByLabelText("Model ID"), { target: { value: "example/model" } });
  fireEvent.change(screen.getByLabelText("API key (optional for local servers)"), {
    target: { value: "synthetic-api-key" },
  });
  fireEvent.click(screen.getByText("Test connection"));
  await screen.findByText("The endpoint returned a complete response.", {}, { timeout: 50 });
  expect(api.test).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByText("Save connection"));
  await screen.findByText("My local model", {}, { timeout: 50 });
  expect(
    (screen.getByLabelText("API key (optional for local servers)") as HTMLInputElement).value,
  ).toBe("");
  expect(shellApi.calls.some((call) => call.method === "patchSettings")).toBe(false);
  fireEvent.click(screen.getByText("Use for new chats"));
  await waitFor(
    () => expect(shellApi.calls.some((call) => call.method === "patchSettings")).toBe(true),
    { timeout: 50 },
  );
});

test("the default model field shows the connection name, with an opaque route kept out of its label", async () => {
  const api = { ...fakeApi(), list: vi.fn(async () => saved) };
  const onChange = vi.fn();
  render(
    <ModelConnectionsContext.Provider value={api}>
      <span id="model-label">Default model</span>
      <ModelDefaultField
        value={`viki-llm/${id}/example/model`}
        labelId="model-label"
        disabled={false}
        onChange={onChange}
      />
    </ModelConnectionsContext.Provider>,
  );
  await screen.findByText("My local model · example/model", {}, { timeout: 50 });
  expect(screen.queryByText(`viki-llm/${id}/example/model`)).toBeNull();
  fireEvent.change(screen.getByLabelText("Default model"), { target: { value: "native" } });
  expect(onChange).toHaveBeenCalledWith(null);
});

test("a new chat can pick a connection without changing the global default", async () => {
  const api = { ...fakeApi(), list: vi.fn(async () => saved) };
  const prefs = {
    get: vi.fn(async () => ({ model: null, effort: "high" })),
    save: vi.fn(async (_key: string, changes: { model?: string; effort?: string }) => ({
      model: changes.model ?? null,
      effort: "high",
    })),
  };
  const wrapper = ({ children }: { children: ReactNode }) => (
    <ModelConnectionsContext.Provider value={api}>{children}</ModelConnectionsContext.Provider>
  );
  const { result } = renderHook(
    () =>
      usePrestartChip({ api: prefs, models: undefined, chatKey: id, enabled: true, refreshKey: 0 }),
    { wrapper },
  );
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(result.current.chip?.models[0]?.label).toBe("My local model · example/model");
  act(() => result.current.hold("model", `viki-llm/${id}/example/model`));
  expect(prefs.save).not.toHaveBeenCalled();
  await act(async () => {
    expect(await result.current.commit(id)).toBe(true);
  });
  expect(prefs.save).toHaveBeenCalledWith(id, { model: `viki-llm/${id}/example/model` });
  expect(api.save).not.toHaveBeenCalled();
});

test("a late default-model save preserves a newer draft, and an external choice switches fields", async () => {
  let reply!: () => void;
  const pending = new Promise<void>((resolve) => {
    reply = resolve;
  });
  const onChange = vi.fn((value: string | null) => {
    void pending.then(() => rerender(field(value ?? "")));
  });
  const field = (value: string) => (
    <ModelDefaultField value={value} labelId="label" disabled={false} onChange={onChange} />
  );
  const { rerender } = render(field("first"));
  fireEvent.change(screen.getByLabelText("Model ID"), { target: { value: "second" } });
  fireEvent.blur(screen.getByLabelText("Model ID"));
  expect(onChange).toHaveBeenLastCalledWith("second");
  fireEvent.change(screen.getByLabelText("Model ID"), { target: { value: "third" } });
  await act(async () => reply()); // the first save's delayed response
  expect((screen.getByLabelText("Model ID") as HTMLInputElement).value).toBe("third");
  fireEvent.blur(screen.getByLabelText("Model ID"));
  expect(onChange).toHaveBeenLastCalledWith("third");
  rerender(field(`viki-llm/${id}/example/model`));
  expect(screen.queryByLabelText("Model ID")).toBeNull();
  rerender(field("other-native"));
  expect((screen.getByLabelText("Model ID") as HTMLInputElement).value).toBe("other-native");
});

test.each([null, "stale_revision", "authentication_failed"])(
  "renewal keeps the saved identity and reports only a completed save (%s)",
  async (failure) => {
    const api = { ...fakeApi(), list: vi.fn(async () => saved) };
    if (failure)
      api.updateKey = vi.fn(async () => {
        throw new ModelConnectionError(failure);
      });
    const store = createShellStore(fakeShellApi(), new AppStore());
    await store.refreshSettings();
    render(
      <ModelConnectionsContext.Provider value={api}>
        <ModelConnectionsPanel store={store} snapshot={store.getSnapshot()} />
      </ModelConnectionsContext.Provider>,
    );
    await screen.findByText("My local model", {}, { timeout: 50 });
    expandAdvanced();
    fireEvent.click(screen.getByRole("button", { name: "Update API key: My local model" }));
    const input = screen.getByLabelText("New API key: My local model") as HTMLInputElement;
    expect(input.type).toBe("password");
    expect(input.value).toBe("");
    fireEvent.change(input, { target: { value: "synthetic-replacement-key" } });
    fireEvent.click(screen.getByRole("button", { name: "Save API key" }));
    await waitFor(
      () =>
        expect(api.updateKey).toHaveBeenCalledWith(
          id,
          1,
          "synthetic-replacement-key",
          expect.any(AbortSignal),
        ),
      { timeout: 50 },
    );
    await act(async () => Promise.resolve());
    expect(screen.getByText("My local model")).toBeTruthy();
    expect(screen.queryByText("synthetic-replacement-key")).toBeNull();
    if (failure) {
      expect(screen.queryByText("API key updated.")).toBeNull();
      expect(screen.getByRole("alert").textContent).toBe(
        failure === "stale_revision"
          ? "Settings changed in another window. Reload the connections before saving again."
          : "Couldn't complete this request. Check the connection and try again.",
      );
    } else {
      expect(screen.getByText("API key updated.")).toBeTruthy();
      expect(screen.queryByLabelText("New API key: My local model")).toBeNull();
      expect(api.save).not.toHaveBeenCalled();
      expect(api.remove).not.toHaveBeenCalled();
    }
  },
);

test("leaving an unfinished key renewal aborts it and does not report a completed save", async () => {
  let resolve!: (value: ModelConnectionsListing) => void;
  const api = {
    ...fakeApi(),
    list: vi.fn(async () => saved),
    updateKey: vi.fn<ModelConnectionsApi["updateKey"]>(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    ),
  };
  const store = createShellStore(fakeShellApi(), new AppStore());
  await store.refreshSettings();
  const panel = () => (
    <ModelConnectionsContext.Provider value={api}>
      <ModelConnectionsPanel store={store} snapshot={store.getSnapshot()} />
    </ModelConnectionsContext.Provider>
  );
  const first = render(panel());
  await screen.findByText("My local model", {}, { timeout: 50 });
  expandAdvanced();
  fireEvent.click(screen.getByRole("button", { name: "Update API key: My local model" }));
  fireEvent.change(screen.getByLabelText("New API key: My local model"), {
    target: { value: "synthetic-replacement-key" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save API key" }));
  const signal = api.updateKey.mock.calls[0]?.[3];
  expect(signal?.aborted).toBe(false);
  first.unmount();
  expect(signal?.aborted).toBe(true);
  render(panel());
  await act(async () => resolve(saved));
  expect(screen.queryByText("API key updated.")).toBeNull();
  expect(screen.queryByLabelText("New API key: My local model")).toBeNull();
});
