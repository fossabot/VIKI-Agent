import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { SessionMetadata } from "../../clients/conversations/sessionMetadataApi";
import type { SessionRow } from "../../protocol";
import { ConversationHeader } from "../Conversation/ConversationHeader";
import { SessionList } from "../LeftRail/SessionList";
import { SessionTabs } from "../Sessions/Sessions";
import { RootLabel } from "./RootLabel";

afterEach(cleanup);

const row: SessionRow = {
  session_id: "123e4567-e89b-12d3-a456-426614174000",
  title: "Chat",
  modified: "2026-10-03T00:00:00Z",
} as SessionRow;
const meta: SessionMetadata = {
  session_id: row.session_id,
  revision: 1,
  title: "Chat",
  pinned: false,
  archived: false,
};

function list(folderOf?: (id: string) => string | null) {
  return render(
    <SessionList
      groups={[{ label: "Today", rows: [row] }]}
      other={[]}
      selectedId={null}
      liveIds={new Set()}
      nowMs={Date.parse("2026-10-03T01:00:00Z")}
      onOpen={vi.fn()}
      {...(folderOf ? { folderOf } : {})}
    />,
  );
}

test("a session row shows its folder", () => {
  const { container } = list(() => "code");
  expect(container.querySelector(".sess .root-label")?.textContent).toBe("code");
});

test("a row shows no label without folderOf or when it answers null", () => {
  expect(list().container.querySelector(".root-label")).toBeNull();
  cleanup();
  expect(list(() => null).container.querySelector(".root-label")).toBeNull();
});

test("a script folder name stays text", () => {
  const evil = "<script>alert(1)</script>";
  const { container } = render(<RootLabel text={evil} />);
  expect(container.querySelector("script")).toBeNull();
  expect(container.querySelector(".root-label")?.textContent).toBe(evil);
  expect(container.querySelector(".root-label")?.getAttribute("title")).toBe(evil);
});

test("the tab's accessible name includes the label", () => {
  const props = {
    sessions: [meta],
    selectedId: meta.session_id,
    onSelect: vi.fn(),
    onClose: vi.fn(),
  };
  render(<SessionTabs {...props} folderOf={() => "code"} />);
  expect(screen.getByRole("tab", { name: "Chat code" })).toBeTruthy();
  cleanup();
  render(<SessionTabs {...props} />);
  expect(screen.getByRole("tab", { name: "Chat" })).toBeTruthy();
});

test("the header shows the folder beside the title, and nothing without it", () => {
  const props = { title: "Chat" };
  const { container } = render(<ConversationHeader {...props} folder="code · viki/a" />);
  expect(container.querySelector(".root-label")?.textContent).toBe("code · viki/a");
  cleanup();
  expect(
    render(<ConversationHeader {...props} />).container.querySelector(".root-label"),
  ).toBeNull();
});
