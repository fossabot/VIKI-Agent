import { expect, test } from "vitest";
import type { ShellSnapshot } from "../../state/shell";
import { capabilitiesFixture } from "../../testShell";
import { notReadyNotice } from "./notReady";

function snapshotWith(overrides: Parameters<typeof capabilitiesFixture>[0]): ShellSnapshot {
  return {
    capabilities: capabilitiesFixture(overrides),
    capabilitiesStatus: "ready",
    settings: null,
    settingsStatus: "unavailable",
    workspaces: null,
    workspacesStatus: "unavailable",
    errors: { capabilities: "", settings: "", workspaces: "" },
    recovery: false,
    checkRunning: null,
  };
}

test("null when every capability is ready", () => {
  expect(notReadyNotice(snapshotWith({}))).toBeNull();
});

test("null while capabilities are unknown", () => {
  expect(notReadyNotice({ ...snapshotWith({}), capabilities: null })).toBeNull();
});

test("null when git is merely overridden, not missing", () => {
  expect(notReadyNotice(snapshotWith({ git: { state: "overridden" } }))).toBeNull();
});

test("null when signed out but the app could not start anyway", () => {
  expect(
    notReadyNotice(snapshotWith({ provider: { can_start: false, login: "signed_out" } })),
  ).toBeNull();
});

test("names claude signed out alone", () => {
  expect(notReadyNotice(snapshotWith({ provider: { login: "signed_out" } }))).toBe(
    "claude: signed out",
  );
});

test("names git missing alone", () => {
  expect(notReadyNotice(snapshotWith({ git: { state: "missing" } }))).toBe("git: missing");
});

test("joins both parts with a dot", () => {
  expect(
    notReadyNotice(snapshotWith({ provider: { login: "signed_out" }, git: { state: "missing" } })),
  ).toBe("claude: signed out · git: missing");
});

test("null when the claude version is merely tested", () => {
  expect(notReadyNotice(snapshotWith({ provider: { version_status: "tested" } }))).toBeNull();
});

test("null when the claude version is newer than tested", () => {
  expect(notReadyNotice(snapshotWith({ provider: { version_status: "newer" } }))).toBeNull();
});

test("names claude update needed alone", () => {
  expect(notReadyNotice(snapshotWith({ provider: { version_status: "older" } }))).toBe(
    "claude: update needed",
  );
});

test("joins signed out, update needed and git missing in that order", () => {
  expect(
    notReadyNotice(
      snapshotWith({
        provider: { login: "signed_out", version_status: "older" },
        git: { state: "missing" },
      }),
    ),
  ).toBe("claude: signed out · claude: update needed · git: missing");
});
