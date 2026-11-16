import { describe, expect, it } from "vitest";
import type { Capture } from "../../clients/vaults/capturesApi";
import { captureHandoffText, destinationLabel, pruneChoices, type TriageChoice } from "./triage";

describe("destinationLabel", () => {
  it("returns correct label for knowledge destination", () => {
    const destination = { to: "knowledge" as const, project: null };
    expect(destinationLabel(destination)).toBe("New note in 30-knowledge/");
  });

  it("returns correct label for source destination", () => {
    const destination = { to: "source" as const, project: null };
    expect(destinationLabel(destination)).toBe("Source note in 40-sources/");
  });

  it("returns correct label for task destination", () => {
    const destination = { to: "task" as const, project: "my-project" };
    expect(destinationLabel(destination)).toBe("Next action in 10-projects/my-project/tasks.md");
  });

  it("returns correct label for area destination", () => {
    const destination = { to: "area" as const, project: null };
    expect(destinationLabel(destination)).toBe("Note in 20-areas/");
  });

  it("returns correct label for person destination", () => {
    const destination = { to: "person" as const, project: null };
    expect(destinationLabel(destination)).toBe("Note in 70-people/");
  });

  it("returns correct label for daily destination", () => {
    const destination = { to: "daily" as const, project: null };
    expect(destinationLabel(destination)).toBe("One line in today's daily note");
  });

  it("returns correct label for discard destination", () => {
    const destination = { to: "discard" as const, project: null };
    expect(destinationLabel(destination)).toBe("Discard: archive it whole, never delete");
  });
});

describe("captureHandoffText", () => {
  it("formats handoff text correctly for three choices", () => {
    const choices: TriageChoice[] = [
      {
        ref: "capture-1.md",
        revision: "abc123",
        modified: "2026-10-09T12:00:00.000Z",
        destination: { to: "knowledge" as const, project: null },
        via: "suggestion",
      },
      {
        ref: "capture-2.md",
        revision: "def456",
        modified: "2026-10-09T12:00:00.000Z",
        destination: { to: "task" as const, project: "project-a" },
        via: "owner",
      },
      {
        ref: "capture-3.md",
        revision: "ghi789",
        modified: "2026-10-09T12:00:00.000Z",
        destination: { to: "discard" as const, project: null },
        via: "suggestion",
      },
    ];

    const result = captureHandoffText(choices);

    expect(result).toBe(
      `File these 00-inbox captures with the /inbox flow, using my choices below. File only these captures, in this order, and keep my words verbatim. A capture marked Discard is archived whole under this vault's rules, never deleted.

\`\`\`text
1. capture-1.md → New note in 30-knowledge/
2. capture-2.md → Next action in 10-projects/project-a/tasks.md
3. capture-3.md → Discard: archive it whole, never delete
\`\`\`

Skip a capture that is no longer in 00-inbox/ and say so. End with the filing report.`,
    );
  });
});

describe("pruneChoices", () => {
  it("keeps all choices when revisions match", () => {
    const choices: TriageChoice[] = [
      {
        ref: "note-1.md",
        revision: "abc123",
        modified: "2026-10-09T12:00:00.000Z",
        destination: { to: "knowledge" as const, project: null },
        via: "suggestion",
      },
      {
        ref: "note-2.md",
        revision: "def456",
        modified: "2026-10-09T12:00:00.000Z",
        destination: { to: "source" as const, project: null },
        via: "owner",
      },
    ];

    const captures: Capture[] = [
      {
        ref: "note-1.md",
        revision: "abc123",
        modified: "2026-10-09T12:00:00.000Z",
        captured: null,
        preview: null,
        cut: false,
        unread: null,
        suggestion: null,
      },
      {
        ref: "note-2.md",
        revision: "def456",
        modified: "2026-10-09T12:00:00.000Z",
        captured: null,
        preview: null,
        cut: false,
        unread: null,
        suggestion: null,
      },
    ];

    const result = pruneChoices(choices, captures);

    expect(result).toBe(choices);
  });

  it("drops choices with mismatched revisions", () => {
    const choices: TriageChoice[] = [
      {
        ref: "note-1.md",
        revision: "old_revision",
        modified: "2026-10-09T12:00:00.000Z",
        destination: { to: "knowledge" as const, project: null },
        via: "suggestion",
      },
      {
        ref: "note-2.md",
        revision: "def456",
        modified: "2026-10-09T12:00:00.000Z",
        destination: { to: "source" as const, project: null },
        via: "owner",
      },
    ];

    const captures: Capture[] = [
      {
        ref: "note-1.md",
        revision: "new_revision",
        modified: "2026-10-09T12:00:00.000Z",
        captured: null,
        preview: null,
        cut: false,
        unread: null,
        suggestion: null,
      },
      {
        ref: "note-2.md",
        revision: "def456",
        modified: "2026-10-09T12:00:00.000Z",
        captured: null,
        preview: null,
        cut: false,
        unread: null,
        suggestion: null,
      },
    ];

    const result = pruneChoices(choices, captures);

    expect(result).toEqual([choices[1]]);
  });

  it("drops choices for missing captures", () => {
    const choices: TriageChoice[] = [
      {
        ref: "note-1.md",
        revision: "abc123",
        modified: "2026-10-09T12:00:00.000Z",
        destination: { to: "knowledge" as const, project: null },
        via: "suggestion",
      },
      {
        ref: "note-2.md",
        revision: "def456",
        modified: "2026-10-09T12:00:00.000Z",
        destination: { to: "source" as const, project: null },
        via: "owner",
      },
    ];

    const captures: Capture[] = [
      {
        ref: "note-2.md",
        revision: "def456",
        modified: "2026-10-09T12:00:00.000Z",
        captured: null,
        preview: null,
        cut: false,
        unread: null,
        suggestion: null,
      },
    ];

    const result = pruneChoices(choices, captures);

    expect(result).toEqual([choices[1]]);
  });

  it("returns the same object when nothing is dropped", () => {
    const choices: TriageChoice[] = [
      {
        ref: "note-1.md",
        revision: "abc123",
        modified: "2026-10-09T12:00:00.000Z",
        destination: { to: "knowledge" as const, project: null },
        via: "suggestion",
      },
    ];

    const captures: Capture[] = [
      {
        ref: "note-1.md",
        revision: "abc123",
        modified: "2026-10-09T12:00:00.000Z",
        captured: null,
        preview: null,
        cut: false,
        unread: null,
        suggestion: null,
      },
    ];

    const result = pruneChoices(choices, captures);

    expect(result).toBe(choices);
  });

  it("keeps a choice on an unread capture through an unchanged modified stamp", () => {
    const choices: TriageChoice[] = [
      {
        ref: "note-1.md",
        revision: null,
        modified: "2026-10-09T12:00:00.000Z",
        destination: { to: "knowledge" as const, project: null },
        via: "suggestion",
      },
    ];

    const captures: Capture[] = [
      {
        ref: "note-1.md",
        revision: null,
        modified: "2026-10-09T12:00:00.000Z",
        captured: null,
        preview: null,
        cut: false,
        unread: "too_large",
        suggestion: null,
      },
    ];

    const result = pruneChoices(choices, captures);

    expect(result).toBe(choices);
  });

  it("drops a choice on an unread capture once a later scan shows a new modified stamp", () => {
    const choices: TriageChoice[] = [
      {
        ref: "note-1.md",
        revision: null,
        modified: "2026-10-09T12:00:00.000Z",
        destination: { to: "knowledge" as const, project: null },
        via: "suggestion",
      },
    ];

    const captures: Capture[] = [
      {
        ref: "note-1.md",
        revision: null,
        modified: "2026-10-10T08:00:00.000Z",
        captured: null,
        preview: null,
        cut: false,
        unread: "too_large",
        suggestion: null,
      },
    ];

    const result = pruneChoices(choices, captures);

    expect(result).toEqual([]);
  });
});
