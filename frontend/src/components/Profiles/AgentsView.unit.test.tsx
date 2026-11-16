import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { AgentsView } from "./AgentsView";

afterEach(cleanup);

it("shows the profiles panel by default, with both tabs in the tablist", () => {
  render(
    <AgentsView
      section="profiles"
      onSection={vi.fn()}
      onBack={vi.fn()}
      profiles={<p>Profiles panel</p>}
      running={<p>Running panel</p>}
    />,
  );
  expect(screen.getByRole("tablist")).not.toBeNull();
  expect(screen.getByRole("tab", { name: "Profiles" }).getAttribute("aria-selected")).toBe("true");
  expect(screen.getByRole("tab", { name: "Running work" }).getAttribute("aria-selected")).toBe(
    "false",
  );
  expect(screen.getByText("Profiles panel")).not.toBeNull();
  expect(screen.queryByText("Running panel")).toBeNull();
});

it("shows only the running panel when section is running", () => {
  render(
    <AgentsView
      section="running"
      onSection={vi.fn()}
      onBack={vi.fn()}
      profiles={<p>Profiles panel</p>}
      running={<p>Running panel</p>}
    />,
  );
  expect(screen.getByRole("tab", { name: "Running work" }).getAttribute("aria-selected")).toBe(
    "true",
  );
  expect(screen.getByText("Running panel")).not.toBeNull();
  expect(screen.queryByText("Profiles panel")).toBeNull();
});

it("clicking a tab calls onSection with its id, once", () => {
  const onSection = vi.fn();
  render(
    <AgentsView
      section="profiles"
      onSection={onSection}
      onBack={vi.fn()}
      profiles={<p>Profiles panel</p>}
      running={<p>Running panel</p>}
    />,
  );
  fireEvent.click(screen.getByRole("tab", { name: "Running work" }));
  expect(onSection).toHaveBeenCalledTimes(1);
  expect(onSection).toHaveBeenCalledWith("running");
});

it("shows only two tabs when native and tools are absent", () => {
  render(
    <AgentsView
      section="profiles"
      onSection={vi.fn()}
      onBack={vi.fn()}
      profiles={<p>Profiles panel</p>}
      running={<p>Running panel</p>}
    />,
  );
  expect(screen.getAllByRole("tab")).toHaveLength(2);
  expect(screen.queryByRole("tab", { name: "Subagents & skills" })).toBeNull();
  expect(screen.queryByRole("tab", { name: "Tools" })).toBeNull();
});

it("shows four tabs when native and tools are given, and the selected tab's node shows", () => {
  render(
    <AgentsView
      section="tools"
      onSection={vi.fn()}
      onBack={vi.fn()}
      profiles={<p>Profiles panel</p>}
      running={<p>Running panel</p>}
      native={<p>Native panel</p>}
      tools={<p>Tools panel</p>}
    />,
  );
  expect(screen.getAllByRole("tab")).toHaveLength(4);
  expect(screen.getByRole("tab", { name: "Subagents & skills" })).not.toBeNull();
  expect(screen.getByRole("tab", { name: "Tools" }).getAttribute("aria-selected")).toBe("true");
  expect(screen.getByText("Tools panel")).not.toBeNull();
  expect(screen.queryByText("Profiles panel")).toBeNull();
  expect(screen.queryByText("Running panel")).toBeNull();
  expect(screen.queryByText("Native panel")).toBeNull();
});

it("focuses the heading on mount", () => {
  render(
    <AgentsView
      section="profiles"
      onSection={vi.fn()}
      onBack={vi.fn()}
      profiles={<p>Profiles panel</p>}
      running={<p>Running panel</p>}
    />,
  );
  expect(screen.getByRole("heading", { name: "Agents" })).toBe(document.activeElement);
});

it("holds the title, intro and actions in one page head", () => {
  const { container } = render(
    <AgentsView
      section="native"
      onSection={vi.fn()}
      onBack={vi.fn()}
      onBots={vi.fn()}
      onSkills={vi.fn()}
      profiles={<p>Profiles panel</p>}
      running={<p>Running panel</p>}
      native={<p>Native panel</p>}
    />,
  );
  const head = container.querySelector("header.page-head");
  expect(head?.querySelector("h1.page-title")?.textContent).toBe("Agents");
  expect(head?.querySelector("p.page-intro")?.textContent).not.toBe("");
  const actions = head?.querySelector("div.page-actions");
  expect(
    Array.from(actions?.querySelectorAll("button") ?? []).map((button) => button.className),
  ).toEqual(["btn alt", "btn txt sm"]);
  expect(container.querySelector(".agents-view-note")).toBeNull();
});

it("the back button calls onBack, once", () => {
  const onBack = vi.fn();
  render(
    <AgentsView
      section="profiles"
      onSection={vi.fn()}
      onBack={onBack}
      profiles={<p>Profiles panel</p>}
      running={<p>Running panel</p>}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Back to chat" }));
  expect(onBack).toHaveBeenCalledTimes(1);
});

it.each(["native", "callback"] as const)(
  "omits the Skills entry when its %s is absent",
  (missing) => {
    const onSkills = vi.fn();
    const entry = missing === "native" ? { onSkills } : { native: <p>Native panel</p> };
    render(
      <AgentsView
        section="profiles"
        onSection={vi.fn()}
        onBack={vi.fn()}
        profiles={<p>Profiles panel</p>}
        running={<p>Running panel</p>}
        {...entry}
      />,
    );
    expect(screen.queryByRole("button", { name: "Skills" })).toBeNull();
    expect(onSkills).not.toHaveBeenCalled();
  },
);

it("the Skills entry calls once without changing native content or tabs", () => {
  const onSkills = vi.fn();
  const onSection = vi.fn();
  const onBack = vi.fn();
  render(
    <AgentsView
      section="native"
      onSection={onSection}
      onBack={onBack}
      onSkills={onSkills}
      profiles={<p>Profiles panel</p>}
      running={<p>Running panel</p>}
      native={
        <div>
          <p>Native agent body</p>
          <p>A skill's description</p>
        </div>
      }
      tools={<p>Tools panel</p>}
    />,
  );
  const button = screen.getByRole("button", { name: "Skills" });
  expect(button.closest('[role="tablist"]')).toBeNull();
  expect(button.getAttribute("type")).toBe("button");
  expect(screen.getAllByRole("tab")).toHaveLength(4);
  expect(screen.queryByRole("tab", { name: "Skills" })).toBeNull();
  expect(onSkills).not.toHaveBeenCalled();
  fireEvent.click(button);
  expect(onSkills).toHaveBeenCalledTimes(1);
  expect(onSection).not.toHaveBeenCalled();
  expect(onBack).not.toHaveBeenCalled();
  expect(screen.getByRole("tabpanel").textContent).toBe("Native agent bodyA skill's description");
  expect(screen.getAllByRole("tab")).toHaveLength(4);
});

it("a supplied Back replaces the page button and calls only the current onBack once", () => {
  const onBack = vi.fn();
  const onSection = vi.fn();
  render(
    <AgentsView
      section="profiles"
      onSection={onSection}
      onBack={onBack}
      profiles={<p>Profiles panel</p>}
      running={<p>Running panel</p>}
      renderBack={(back) => (
        <button type="button" onClick={back}>
          Back
        </button>
      )}
    />,
  );
  expect(screen.getAllByRole("button")).toHaveLength(1);
  expect(screen.queryByRole("button", { name: "Back to chat" })).toBeNull();
  expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Agents" }));
  expect(screen.getByRole("tabpanel").textContent).toBe("Profiles panel");
  expect(onBack).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Back" }));
  expect(onBack).toHaveBeenCalledTimes(1);
  expect(onSection).not.toHaveBeenCalled();
});
