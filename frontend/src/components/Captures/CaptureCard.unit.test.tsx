import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Capture, CaptureSuggestion } from "../../clients/vaults/capturesApi";
import { CaptureCard } from "./CaptureCard";
import type { TriageChoice } from "./triage";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function capture(overrides: Partial<Capture> = {}): Capture {
  return {
    ref: "00-inbox/2026-10-09-idea.md",
    revision: "a".repeat(64),
    modified: "2026-10-09T12:00:00.000Z",
    captured: null,
    preview: "A loose idea about the next release.",
    cut: false,
    unread: null,
    suggestion: null,
    ...overrides,
  };
}

function suggestion(overrides: Partial<CaptureSuggestion> = {}): CaptureSuggestion {
  return {
    to: "knowledge",
    project: null,
    why: "Reads like an evergreen claim.",
    at: "2026-10-09T12:05:00.000Z",
    ...overrides,
  };
}

function renderCard(props: Partial<Parameters<typeof CaptureCard>[0]> = {}) {
  const onChoose = vi.fn();
  const view = render(
    <CaptureCard capture={capture()} choice={null} projects={[]} onChoose={onChoose} {...props} />,
  );
  return { ...view, onChoose };
}

describe("CaptureCard: header", () => {
  it('renders the root as <li class="capture-card">', () => {
    const { container } = renderCard();
    expect(container.querySelector("li.capture-card")).toBeTruthy();
  });

  it("renders the exact ref in mono", () => {
    renderCard({ capture: capture({ ref: "00-inbox/weird ref (1).md" }) });
    const ref = screen.getByText("00-inbox/weird ref (1).md");
    expect(ref.tagName).toBe("CODE");
    expect(ref.className).toContain("mono");
  });

  it("shows captured when present", () => {
    renderCard({ capture: capture({ captured: "2026-10-09" }) });
    expect(screen.getByText("2026-10-09")).toBeTruthy();
    expect(screen.queryByText(/^Modified /)).toBeNull();
  });

  it("falls back to 'Modified {modified}' when captured is null", () => {
    renderCard({ capture: capture({ captured: null, modified: "2026-10-09T12:00:00.000Z" }) });
    expect(screen.getByText("Modified 2026-10-09T12:00:00.000Z")).toBeTruthy();
  });

  it("shows the preview without an ellipsis when not cut", () => {
    renderCard({ capture: capture({ preview: "Not cut.", cut: false }) });
    expect(screen.getByText("Not cut.")).toBeTruthy();
  });

  it("appends an ellipsis to the preview when cut", () => {
    renderCard({ capture: capture({ preview: "Truncated body", cut: true }) });
    expect(screen.getByText("Truncated body…")).toBeTruthy();
  });
});

describe("CaptureCard: unread reasons", () => {
  it("reads the too_large sentence", () => {
    renderCard({ capture: capture({ unread: "too_large", preview: null }) });
    expect(screen.getByText("Too large to preview here; VIKI can still read it.")).toBeTruthy();
  });

  it("reads the budget sentence", () => {
    renderCard({ capture: capture({ unread: "budget", preview: null }) });
    expect(screen.getByText("Not previewed this time (read limit).")).toBeTruthy();
  });

  it("reads the generic sentence for any other reason code", () => {
    renderCard({ capture: capture({ unread: "note_too_large", preview: null }) });
    expect(screen.getByText("Could not be read here.")).toBeTruthy();
  });
});

describe("CaptureCard: suggestion line", () => {
  it("reads 'No suggestion yet.' without a suggestion", () => {
    renderCard({ capture: capture({ suggestion: null }) });
    expect(screen.getByText("No suggestion yet.")).toBeTruthy();
  });

  it("reads the label and puts why in a dim span", () => {
    renderCard({
      capture: capture({
        suggestion: suggestion({ to: "area", project: null, why: "Fits your health area." }),
      }),
    });
    expect(screen.getByText("VIKI suggests: Note in 20-areas/")).toBeTruthy();
    const why = screen.getByText("Fits your health area.");
    expect(why.className).toContain("dim");
  });
});

describe("CaptureCard: Accept", () => {
  it("is disabled without a suggestion", () => {
    renderCard({ capture: capture({ suggestion: null }) });
    const accept = screen.getByRole("button", { name: "Accept" }) as HTMLButtonElement;
    expect(accept.disabled).toBe(true);
  });

  it("emits the suggestion's destination with via: suggestion", () => {
    const theCapture = capture({
      ref: "00-inbox/a.md",
      revision: "r1",
      suggestion: suggestion({ to: "task", project: "garden" }),
    });
    const { onChoose } = renderCard({ capture: theCapture });
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    expect(onChoose).toHaveBeenCalledTimes(1);
    expect(onChoose).toHaveBeenCalledWith({
      ref: "00-inbox/a.md",
      revision: "r1",
      modified: "2026-10-09T12:00:00.000Z",
      destination: { to: "task", project: "garden" },
      via: "suggestion",
    } satisfies TriageChoice);
  });
});

describe("CaptureCard: Discard", () => {
  it("emits the discard destination with via: owner", () => {
    const theCapture = capture({ ref: "00-inbox/b.md", revision: "r2" });
    const { onChoose } = renderCard({ capture: theCapture });
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(onChoose).toHaveBeenCalledWith({
      ref: "00-inbox/b.md",
      revision: "r2",
      modified: "2026-10-09T12:00:00.000Z",
      destination: { to: "discard", project: null },
      via: "owner",
    } satisfies TriageChoice);
  });
});

describe("CaptureCard: Change", () => {
  it("opens the destination and project selects, hiding Accept/Change/Discard", () => {
    renderCard();
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    expect(screen.getByLabelText("Destination")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Discard" })).toBeNull();
  });

  it("disables the task option and names the reason when there are no projects", () => {
    renderCard({ projects: [] });
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    const destination = screen.getByLabelText("Destination") as HTMLSelectElement;
    const task = [...destination.options].find((option) => option.value === "task");
    expect(task?.disabled).toBe(true);
    expect(task?.textContent).toBe("No projects found in 10-projects/.");
  });

  it("enables the task option and names it when projects exist", () => {
    renderCard({ projects: ["garden"] });
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    const destination = screen.getByLabelText("Destination") as HTMLSelectElement;
    const task = [...destination.options].find((option) => option.value === "task");
    expect(task?.disabled).toBe(false);
    expect(task?.textContent).toBe("Next action in a project (10-projects/)");
  });

  it("defaults to a non-task destination, where Use is enabled with no project chosen", () => {
    const theCapture = capture({ ref: "00-inbox/c.md", revision: "r3" });
    const { onChoose } = renderCard({ capture: theCapture, projects: ["garden"] });
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    const use = screen.getByRole("button", { name: "Use" }) as HTMLButtonElement;
    expect(use.disabled).toBe(false);
    fireEvent.click(use);
    expect(onChoose).toHaveBeenCalledWith({
      ref: "00-inbox/c.md",
      revision: "r3",
      modified: "2026-10-09T12:00:00.000Z",
      destination: { to: "knowledge", project: null },
      via: "owner",
    } satisfies TriageChoice);
    // the change form closes and the default row returns.
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
  });

  it("requires a project for task: Use stays disabled until one is chosen", () => {
    const theCapture = capture({ ref: "00-inbox/d.md", revision: "r4" });
    const { onChoose } = renderCard({ capture: theCapture, projects: ["garden", "launch"] });
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    fireEvent.change(screen.getByLabelText("Destination"), { target: { value: "task" } });
    const use = screen.getByRole("button", { name: "Use" }) as HTMLButtonElement;
    expect(use.disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("Project"), { target: { value: "launch" } });
    expect(use.disabled).toBe(false);
    fireEvent.click(use);
    expect(onChoose).toHaveBeenCalledWith({
      ref: "00-inbox/d.md",
      revision: "r4",
      modified: "2026-10-09T12:00:00.000Z",
      destination: { to: "task", project: "launch" },
      via: "owner",
    } satisfies TriageChoice);
  });

  it("lists exactly the given projects in the project select, in order", () => {
    renderCard({ projects: ["alpha", "beta"] });
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    fireEvent.change(screen.getByLabelText("Destination"), { target: { value: "task" } });
    const project = screen.getByLabelText("Project") as HTMLSelectElement;
    expect([...project.options].map((option) => option.value)).toEqual(["", "alpha", "beta"]);
  });

  it("Cancel closes the form without emitting a choice", () => {
    const { onChoose } = renderCard();
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onChoose).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
  });
});

describe("CaptureCard: with a choice", () => {
  it("reads 'Accepted: {label}' for a suggestion choice, hiding Accept/Change/Discard", () => {
    const choice: TriageChoice = {
      ref: "00-inbox/e.md",
      revision: "r5",
      modified: "2026-10-09T12:00:00.000Z",
      destination: { to: "source", project: null },
      via: "suggestion",
    };
    renderCard({ choice });
    expect(screen.getByText("Accepted: Source note in 40-sources/")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Change" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Discard" })).toBeNull();
  });

  it("reads 'Chosen: {label}' for an owner choice", () => {
    const choice: TriageChoice = {
      ref: "00-inbox/f.md",
      revision: "r6",
      modified: "2026-10-09T12:00:00.000Z",
      destination: { to: "task", project: "garden" },
      via: "owner",
    };
    renderCard({ choice });
    expect(screen.getByText("Chosen: Next action in 10-projects/garden/tasks.md")).toBeTruthy();
  });

  it("Undo emits null", () => {
    const choice: TriageChoice = {
      ref: "00-inbox/g.md",
      revision: "r7",
      modified: "2026-10-09T12:00:00.000Z",
      destination: { to: "daily", project: null },
      via: "owner",
    };
    const { onChoose } = renderCard({ choice });
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(onChoose).toHaveBeenCalledWith(null);
  });
});

describe("CaptureCard: Open note", () => {
  it("is absent without onOpenNote", () => {
    renderCard();
    expect(screen.queryByRole("button", { name: "Open note" })).toBeNull();
  });

  it("calls onOpenNote when clicked", () => {
    const onOpenNote = vi.fn();
    renderCard({ onOpenNote });
    fireEvent.click(screen.getByRole("button", { name: "Open note" }));
    expect(onOpenNote).toHaveBeenCalledOnce();
  });

  it("stays available even once a capture has a choice", () => {
    const onOpenNote = vi.fn();
    const choice: TriageChoice = {
      ref: "00-inbox/h.md",
      revision: "r8",
      modified: "2026-10-09T12:00:00.000Z",
      destination: { to: "person", project: null },
      via: "owner",
    };
    renderCard({ choice, onOpenNote });
    expect(screen.getByRole("button", { name: "Open note" })).toBeTruthy();
  });
});

// A real parent (CaptureTriage) feeds `onChoose`'s result straight back as the next `choice`
// prop, in the same tick React batches with this card's own `setChanging`. ControlledCard mimics
// that round trip so these focus assertions see the same swap the user sees, instead of the
// frozen `choice` a bare vi.fn() would leave behind.
function ControlledCard({
  capture: theCapture,
  projects = ["garden"],
  initialChoice = null,
}: {
  capture: Capture;
  projects?: readonly string[];
  initialChoice?: TriageChoice | null;
}) {
  const [choice, setChoice] = useState<TriageChoice | null>(initialChoice);
  return (
    <CaptureCard capture={theCapture} choice={choice} projects={projects} onChoose={setChoice} />
  );
}

describe("CaptureCard: focus follows each swap", () => {
  it("Accept moves focus to Undo", () => {
    render(<ControlledCard capture={capture({ suggestion: suggestion() })} />);
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Undo" }));
  });

  it("Discard moves focus to Undo", () => {
    render(<ControlledCard capture={capture()} />);
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Undo" }));
  });

  it("Use moves focus to Undo", () => {
    render(<ControlledCard capture={capture()} />);
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    fireEvent.click(screen.getByRole("button", { name: "Use" }));
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Undo" }));
  });

  it("Change moves focus to the Destination select", () => {
    renderCard();
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    expect(document.activeElement).toBe(screen.getByLabelText("Destination"));
  });

  it("Cancel moves focus back to Change", () => {
    renderCard();
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Change" }));
  });

  it("Undo moves focus back to Change", () => {
    const choice: TriageChoice = {
      ref: "00-inbox/undo-focus.md",
      revision: "r9",
      modified: "2026-10-09T12:00:00.000Z",
      destination: { to: "daily", project: null },
      via: "owner",
    };
    render(
      <ControlledCard
        capture={capture({ ref: choice.ref, revision: choice.revision })}
        initialChoice={choice}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Change" }));
  });
});
