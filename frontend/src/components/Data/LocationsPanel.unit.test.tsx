/**
 * `LocationsPanel`. The catalog defaults to
 * English (`i18n/t.ts`'s module-level `current`), so every assertion here reads the `en` rows.
 */
import { act, cleanup, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, expect, test } from "vitest";
import type { ShellApi } from "../../clients/app/shellApi";
import { ShellApiError } from "../../clients/app/shellApi";
import { deferredShellApi, fakeShellApi, locationsFixture } from "../../testShell";
import { LocationsPanel } from "./LocationsPanel";

afterEach(cleanup);

function mount(api: ShellApi | null) {
  const headingRef = createRef<HTMLHeadingElement>();
  const utils = render(<LocationsPanel api={api} headingRef={headingRef} />);
  return { ...utils, headingRef };
}

async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
  });
}

test("vault rows show a not-found chip, the policy chip and a partial note", async () => {
  const api = fakeShellApi({ locations: locationsFixture() });
  api.data.locations.categories[0] = {
    id: "vaults",
    items: [
      {
        workspace_id: "w1",
        name: "My notes",
        path: "D:\\VIKI\\Vaults\\My notes",
        policy: "ordinary",
        available: false,
        bytes: 1536,
        partial: true,
      },
    ],
  };
  mount(api);
  await flush();
  expect(screen.getByText("My notes")).toBeTruthy();
  expect(screen.getByText("Not found")).toBeTruthy();
  expect(screen.getByText("Ordinary")).toBeTruthy();
  expect(screen.getByText("1.5 KB")).toBeTruthy();
  expect(screen.getByText("Partially measured")).toBeTruthy();
});

test("app-data rows show a localised schema, count, in-use and shared chips", async () => {
  const api = fakeShellApi({ locations: locationsFixture() });
  mount(api);
  await flush();
  expect(screen.getByText("Schema 4")).toBeTruthy();
  expect(screen.getByText("0 files")).toBeTruthy();
  expect(screen.getByText("In use")).toBeTruthy();
  expect(screen.getByText("Shared")).toBeTruthy();
});

test("a provider row with a null path and read_by_app false says so", async () => {
  const api = fakeShellApi({ locations: locationsFixture() });
  mount(api);
  await flush();
  expect(screen.getByText("(not shown)")).toBeTruthy();
  expect(screen.getByText("The app does not read this.")).toBeTruthy();
});

test("indexes, and any other empty category with no dedicated table, render the empty sentence", async () => {
  const api = fakeShellApi({
    locations: {
      categories: [
        { id: "vaults", items: [] },
        { id: "indexes", items: [] },
      ],
    },
  });
  mount(api);
  await flush();
  const empties = screen.getAllByText("Nothing to show.");
  expect(empties.length).toBe(2);
});

test("an assets item renders its path, size and a partial note when set", async () => {
  const api = fakeShellApi({
    locations: {
      categories: [
        { id: "vaults", items: [] },
        {
          id: "assets",
          items: [
            { path: "blobs\\viki\\ab\\ab12.jpg", bytes: 204_800, partial: false },
            { path: "blobs\\viki\\cd\\cd34.jpg", bytes: 1_048_576, partial: true },
          ],
        },
      ],
    },
  });
  mount(api);
  await flush();
  expect(screen.getByText("blobs\\viki\\ab\\ab12.jpg")).toBeTruthy();
  expect(screen.getByText("blobs\\viki\\cd\\cd34.jpg")).toBeTruthy();
  expect(screen.getByText("200.0 KB")).toBeTruthy();
  expect(screen.getByText("1.0 MB")).toBeTruthy();
  expect(screen.getByText("Partially measured")).toBeTruthy();
});

test("the assets paragraph says retention runs for every workspace, not only the active one", async () => {
  const api = fakeShellApi({
    locations: {
      categories: [
        { id: "vaults", items: [] },
        { id: "assets", items: [] },
      ],
    },
  });
  mount(api);
  await flush();
  const paragraph = screen.getByText(/expire after 24 hours/);
  expect(paragraph.textContent).toContain(
    "This runs hourly for every workspace with images, not only the active one.",
  );
  expect(paragraph.textContent).not.toContain("only for the active workspace");
});

test("the assets paragraph names the Send history reset, not a nonexistent queue reset", async () => {
  const api = fakeShellApi({
    locations: {
      categories: [
        { id: "vaults", items: [] },
        { id: "assets", items: [] },
      ],
    },
  });
  mount(api);
  await flush();
  const paragraph = screen.getByText(/expire after 24 hours/);
  expect(paragraph.textContent).toContain("Resetting Drafts or Send history");
  expect(paragraph.textContent).not.toContain("queue");
});

test("without a shell api the panel shows the generic refusal, not a crash", async () => {
  mount(null);
  await flush();
  expect(screen.getByRole("alert").textContent).toContain("The operation could not be completed.");
});

test("refresh re-reads the document", async () => {
  const api = fakeShellApi({ locations: locationsFixture() });
  mount(api);
  await flush();
  const before = api.calls.filter((c) => c.method === "dataLocations").length;
  await act(async () => {
    screen.getByText("Refresh").click();
    await Promise.resolve();
  });
  const after = api.calls.filter((c) => c.method === "dataLocations").length;
  expect(after).toBe(before + 1);
});

test("a failed read renders the error text and the heading stays reachable by ref", async () => {
  const api = fakeShellApi({ locations: locationsFixture() });
  api.fail("dataLocations", new ShellApiError(503, "busy"));
  const { headingRef } = mount(api);
  await flush();
  expect(screen.getByRole("alert").textContent).toContain("The app store is busy.");
  expect(headingRef.current).not.toBeNull();
});

test("a stale read that fails after a newer one already painted the table does not overwrite it", async () => {
  // Regression for the missing newest-wins guard: without a generation ref, this stale .catch
  // would flip status to "error" and hide the table the fresh read already painted.
  const api = deferredShellApi();
  mount(api);
  await act(async () => {
    screen.getByText("Refresh").click(); // a second, overlapping read before the first settles
  });
  expect(api.pending).toHaveLength(2);
  const [stale, fresh] = api.pending;
  await act(async () => {
    fresh?.resolve(locationsFixture());
  });
  expect(screen.getByText("Home")).toBeTruthy();
  await act(async () => {
    stale?.reject(new ShellApiError(0, "timeout"));
  });
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByText("Home")).toBeTruthy(); // still the newer table, not an error line
});

test("a refresh in flight keeps the previous table on screen instead of blanking it", async () => {
  const api = deferredShellApi();
  mount(api);
  const initial = api.pending[0];
  await act(async () => {
    initial?.resolve(locationsFixture());
  });
  expect(screen.getByText("Home")).toBeTruthy();
  await act(async () => {
    screen.getByText("Refresh").click();
  });
  expect(screen.getByRole("status").textContent).toBe("Loading…");
  expect(screen.getByText("Home")).toBeTruthy(); // the stale-but-correct table stays up
});
