import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { startUpdates, UPDATE_PLATFORMS } from "../updates.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const DESKTOP_ROOT = dirname(HERE);
const UPDATES_SOURCE = readFileSync(join(DESKTOP_ROOT, "updates.mjs"), "utf8");

const FEED_FILE = "/fake/resources/app-update.yml";

/** A `load` that fails the test if startUpdates ever calls it, for the false branches. */
function loadMustNotRun() {
  return () => {
    throw new Error("load must not be called on a false branch");
  };
}

/**
 * A fake updater shaped like the real update library's default export, for the true branch:
 * counts `checkForUpdatesAndNotify` calls and resolves each one like a successful check.
 */
function fakeUpdater() {
  const state = { calls: 0 };
  state.autoUpdater = {
    checkForUpdatesAndNotify() {
      state.calls++;
      return Promise.resolve(null);
    },
  };
  return state;
}

/**
 * Captures `console.warn` calls for the test's duration, restoring the original after.
 * @param {import("node:test").TestContext} t
 * @returns {unknown[][]} each call's arguments, in order
 */
function captureWarnings(t) {
  const warnings = [];
  const original = console.warn;
  console.warn = (...args) => warnings.push(args);
  t.after(() => {
    console.warn = original;
  });
  return warnings;
}

// -- UPDATE_PLATFORMS ----------------------------------------------------------------------------

test("UPDATE_PLATFORMS is frozen and exactly win32 and darwin", () => {
  assert.deepEqual(UPDATE_PLATFORMS, ["win32", "darwin"]);
  assert.ok(Object.isFrozen(UPDATE_PLATFORMS));
});

// -- startUpdates: false branches, load never called ---------------------------------------------

test("startUpdates returns false and never loads when not packaged", async () => {
  const result = await startUpdates({
    packaged: false,
    platform: "win32",
    feedFile: FEED_FILE,
    exists: () => true,
    load: loadMustNotRun(),
  });
  assert.equal(result, false);
});

test("startUpdates returns false and never loads on a platform the update check does not cover", async () => {
  const result = await startUpdates({
    packaged: true,
    platform: "linux",
    feedFile: FEED_FILE,
    exists: () => true,
    load: loadMustNotRun(),
  });
  assert.equal(result, false);
});

test("startUpdates returns false and never loads when the feed file is missing", async () => {
  const result = await startUpdates({
    packaged: true,
    platform: "win32",
    feedFile: FEED_FILE,
    exists: () => false,
    load: loadMustNotRun(),
  });
  assert.equal(result, false);
});

// -- startUpdates: the true branch ----------------------------------------------------------------

for (const platform of UPDATE_PLATFORMS) {
  test(`startUpdates on ${platform}, packaged, with a feed file starts exactly one check`, async () => {
    const updater = fakeUpdater();
    const result = await startUpdates({
      packaged: true,
      platform,
      feedFile: FEED_FILE,
      exists: () => true,
      load: async () => updater,
    });
    assert.equal(result, true);
    assert.equal(updater.calls, 1);
  });
}

// -- startUpdates: failures are caught, never escape, and log only the fixed text -----------------

test("a rejecting check logs only the fixed text and does not throw", async (t) => {
  const warnings = captureWarnings(t);
  let checkResult;
  const updater = {
    autoUpdater: {
      checkForUpdatesAndNotify() {
        checkResult = Promise.reject(new Error("network is down, 10.0.0.1"));
        return checkResult;
      },
    },
  };

  const result = await startUpdates({
    packaged: true,
    platform: "win32",
    feedFile: FEED_FILE,
    exists: () => true,
    load: async () => updater,
  });

  assert.equal(result, true);
  // Reactions on one promise run in the order they were attached. startUpdates's own `.catch`
  // (registered inside the call above, before this line runs) was attached first, so by the time
  // this settles, its console.warn has already run: no timer or sleep needed.
  await checkResult.catch(() => {});
  assert.deepEqual(warnings, [["update check failed"]]);
});

test("a throwing load logs only the fixed text and does not throw", async (t) => {
  const warnings = captureWarnings(t);
  const result = await startUpdates({
    packaged: true,
    platform: "win32",
    feedFile: FEED_FILE,
    exists: () => true,
    load: async () => {
      throw new Error("could not load the update library, https://example.invalid/secret");
    },
  });
  assert.equal(result, true);
  assert.deepEqual(warnings, [["update check failed"]]);
});

// -- the module itself: no real update library at the top level -----------------------------------

test("updates.mjs's only static import is node:fs", () => {
  const importLines = UPDATES_SOURCE.split("\n").filter((line) => /^\s*import\b/.test(line));
  assert.equal(importLines.length, 1, "exactly one static import line");
  assert.match(importLines[0], /from\s+"node:fs"/);
});

test("updates.mjs names electron-updater exactly once, in the dynamic import inside loadUpdater", () => {
  const occurrences = UPDATES_SOURCE.split("electron-updater").length - 1;
  assert.equal(occurrences, 1, "electron-updater must be named exactly once in this file");

  const lines = UPDATES_SOURCE.split("\n");
  const loadUpdaterLine = lines.findIndex((line) => /function\s+loadUpdater\s*\(/.test(line));
  const importLine = lines.findIndex((line) => line.includes("electron-updater"));
  assert.ok(loadUpdaterLine !== -1, "loadUpdater must be declared");
  assert.ok(
    importLine > loadUpdaterLine,
    "the only mention of electron-updater must come after loadUpdater's declaration",
  );
  assert.match(lines[importLine], /await import\("electron-updater"\)/);
});
