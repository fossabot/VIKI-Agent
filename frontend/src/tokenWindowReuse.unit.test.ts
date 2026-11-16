/** Synthetic document delivery; these tests do not establish native Edge reuse. */
import { Children, isValidElement, type ReactNode } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { FakeSocket } from "./accept/m3b/fakes";
import type { Rest } from "./api";
import type { Viki } from "./state/viki";

const bootstrap = vi.hoisted(() => ({ render: vi.fn() }));
vi.mock("react-dom/client", () => ({ createRoot: () => ({ render: bootstrap.render }) }));
vi.mock("./App", () => ({ App: () => null }));
vi.mock("./components/Conversation/markdownParser", () => ({
  createWorkerParser: () => ({}),
  MarkdownParserContext: { Provider: "fixture-parser" },
}));

const sockets: FakeSocket[] = [];
const clients: Viki[] = [];

function appProps(node: ReactNode): { viki: Viki; rest: Rest } {
  if (!isValidElement<{ children?: ReactNode; viki?: Viki; rest?: Rest }>(node)) {
    throw new Error("missing app");
  }
  if (node.props.viki && node.props.rest) return { viki: node.props.viki, rest: node.props.rest };
  return appProps(Children.toArray(node.props.children)[0]);
}

async function loadDocument() {
  await import("./main");
  const app = appProps(bootstrap.render.mock.calls.at(-1)?.[0]);
  clients.push(app.viki);
  sockets.at(-1)?.open();
  return app;
}

beforeEach(() => {
  vi.resetModules();
  vi.useFakeTimers();
  bootstrap.render.mockClear();
  sockets.length = 0;
  sessionStorage.clear();
  document.body.innerHTML = '<div id="root"></div>';
  vi.stubGlobal(
    "WebSocket",
    class extends FakeSocket {
      constructor() {
        super(false);
        sockets.push(this);
      }
    },
  );
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("{}", { status: 200 })),
  );
});

afterEach(() => {
  for (const viki of clients.splice(0)) viki.client.close();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  window.history.replaceState(null, "", "/");
  document.body.innerHTML = "";
});

test("a new document consumes the launch fragment for WS and REST and strips it", async () => {
  sessionStorage.setItem("viki.token", "fixture-old-launch");
  window.history.replaceState(null, "", "/#token=fixture-current-launch");
  const { rest } = await loadDocument();
  expect(window.location.hash).toBe("");
  expect(sessionStorage.getItem("viki.token")).toBe("fixture-current-launch");
  expect(sockets[0]?.sent).toEqual([{ type: "auth", token: "fixture-current-launch", v: 2 }]);
  await rest.sessions();
  expect(fetch).toHaveBeenCalledWith(
    "/api/sessions",
    expect.objectContaining({
      headers: { authorization: "Bearer " + "fixture-current-launch" },
    }),
  );
});

test("hash-only delivery to a reused document leaves its existing clients on the old token", async () => {
  window.history.replaceState(null, "", "/#token=fixture-old-launch");
  const { rest } = await loadDocument();
  window.history.replaceState(null, "", "/#token=fixture-current-launch");
  window.dispatchEvent(new HashChangeEvent("hashchange"));
  expect(sockets).toHaveLength(1);
  expect(window.location.hash).toBe("#token=fixture-current-launch");
  expect(sessionStorage.getItem("viki.token")).toBe("fixture-old-launch");
  expect(sockets[0]?.sent).toEqual([{ type: "auth", token: "fixture-old-launch", v: 2 }]);
  await rest.sessions();
  expect(fetch).toHaveBeenCalledWith(
    "/api/sessions",
    expect.objectContaining({
      headers: { authorization: "Bearer " + "fixture-old-launch" },
    }),
  );
  sockets[0]?.onclose?.({ code: 4401 });
  vi.runAllTimers();
  expect(sockets).toHaveLength(1); // Stale auth does not trigger a connection or turn retry.
});
