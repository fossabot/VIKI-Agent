import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { JsonTree } from "./JsonTree";

afterEach(cleanup);

test("nested objects render as definition lists with every key as text", () => {
  const { container } = render(<JsonTree value={{ app: { mode: "normal" } }} />);
  const dl = container.querySelector("dl.json-tree-map");
  expect(dl).not.toBeNull();
  expect(container.querySelector("dt")?.textContent).toBe("app");
  // The nested object under "app" is itself a <dl>, reachable from the outer <dd>.
  expect(container.querySelectorAll("dl").length).toBe(2);
  expect(container.textContent).toContain("mode");
  expect(container.textContent).toContain("normal");
});

test("arrays render as ordered lists, one <li> per item", () => {
  const { container } = render(<JsonTree value={["a", "b", "c"]} />);
  const items = container.querySelectorAll("ol.json-tree-list > li");
  expect(items.length).toBe(3);
  expect(items[0]?.textContent).toBe("a");
});

test("scalars render as text: strings, numbers, booleans and null", () => {
  const { container: strings } = render(<JsonTree value="plain text" />);
  expect(strings.textContent).toBe("plain text");

  const { container: numbers } = render(<JsonTree value={42} />);
  expect(numbers.textContent).toBe("42");

  const { container: bools } = render(<JsonTree value={false} />);
  expect(bools.textContent).toBe("false");

  const { container: nulls } = render(<JsonTree value={null} />);
  expect(nulls.textContent).toBe("null");
});

test("a hostile string containing a script tag renders as text and creates no element", () => {
  const hostile = "<script>window.pwned = true</script>";
  const { container } = render(<JsonTree value={{ note: hostile }} />);
  expect(container.querySelector("script")).toBeNull();
  expect(container.textContent).toContain(hostile);
});

test("a URL-shaped string never becomes an anchor", () => {
  const { container } = render(<JsonTree value={{ path: "https://example.invalid/x" }} />);
  expect(container.querySelector("a")).toBeNull();
  expect(container.textContent).toContain("https://example.invalid/x");
});

test("empty objects and arrays render a placeholder, not an empty container", () => {
  const { container: obj } = render(<JsonTree value={{}} />);
  expect(obj.textContent).toBe("{}");
  expect(obj.querySelector("dl")).toBeNull();

  const { container: arr } = render(<JsonTree value={[]} />);
  expect(arr.textContent).toBe("[]");
  expect(arr.querySelector("ol")).toBeNull();
});

test("an entry's value that is a non-empty object or array also gets json-tree-group; scalars and empty values do not", () => {
  const { container } = render(
    <JsonTree
      value={{
        nestedObject: { a: 1 },
        nestedArray: [1, 2],
        emptyObject: {},
        emptyArray: [],
        scalar: "text",
        nullValue: null,
      }}
    />,
  );
  const entries = Array.from(container.querySelectorAll(".json-tree-entry"));
  const classesFor = (key: string) =>
    entries.find((entry) => entry.querySelector(".json-tree-key")?.textContent === key)?.className;
  expect(classesFor("nestedObject")).toBe("json-tree-entry json-tree-group");
  expect(classesFor("nestedArray")).toBe("json-tree-entry json-tree-group");
  expect(classesFor("emptyObject")).toBe("json-tree-entry");
  expect(classesFor("emptyArray")).toBe("json-tree-entry");
  expect(classesFor("scalar")).toBe("json-tree-entry");
  expect(classesFor("nullValue")).toBe("json-tree-entry");
});
