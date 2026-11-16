import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { OverviewNote } from "../../state/vaultApi";
import { RelatedNotes } from "./RelatedNotes";

describe("RelatedNotes", () => {
  it("shows exact refs on compact cards and excludes unsafe and external links", () => {
    const onOpen = vi.fn();
    const overview = new Map<string, OverviewNote>([
      [
        "30-knowledge/target.md",
        {
          ref: "30-knowledge/target.md",
          state: "ready",
          title: "Shared title",
          type: null,
          status: null,
          confidence: null,
          tags: null,
          created: null,
          modified: null,
          summary: null,
          links: null,
          complete: true,
        },
      ],
    ]);
    render(
      <RelatedNotes
        links={[
          { target: "target", state: "resolved", candidates: ["30-knowledge/target.md"] },
          { target: "unknown", state: "missing", candidates: [] },
          { target: "bad", state: "unsafe", candidates: ["bad.md"] },
          { target: "web", state: "external", candidates: ["web.md"] },
        ]}
        backlinks={[{ source: "40-sources/target.md", line: 3 }]}
        backlinkPhase="ready"
        backlinkStatus={null}
        listing={new Map()}
        overview={overview}
        onOpen={onOpen}
      />,
    );
    const links = within(screen.getByRole("region", { name: "Links" }));
    const target = links.getByRole("button", { name: "Shared title" });
    expect(target.getAttribute("data-vault-ref")).toBe("30-knowledge/target.md");
    expect(links.getByText("30-knowledge/target.md")).toBeTruthy();
    expect(links.getByText("unknown (no note yet)")).toBeTruthy();
    expect(links.queryByText("bad.md")).toBeNull();
    expect(links.queryByText("web.md")).toBeNull();
    fireEvent.click(target);
    expect(onOpen).toHaveBeenCalledExactlyOnceWith("30-knowledge/target.md", "link");
    const backlinks = within(screen.getByRole("region", { name: "Backlinks" }));
    expect(backlinks.getByText("40-sources/target.md")).toBeTruthy();
    fireEvent.click(backlinks.getByRole("button", { name: "target" }));
    expect(onOpen).toHaveBeenLastCalledWith("40-sources/target.md", "backlink");
  });
});
