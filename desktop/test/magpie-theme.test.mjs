import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  MAGPIE_FONTS,
  magpieBounds,
  magpieFonts,
  magpieHeader,
  magpieStyle,
} from "../magpie-theme.mjs";
import { APP_ORIGIN, allowsMagpieRequest, parseMagpieView } from "../policy.mjs";
import { parseServerLine } from "../server.mjs";
import { magpieView } from "./magpie-view-fixture.mjs";

test("embedded views accept exact bounded presentation data, including a hidden zero-area view", () => {
  const view = magpieView();
  assert.deepEqual(parseMagpieView(view), view);
  view.bounds = { x: 0, y: 0, width: 0, height: 0 };
  view.visible = false;
  assert.deepEqual(parseMagpieView(view), view);
  const invalid = [
    (v) => {
      v.extra = "ignored";
    },
    (v) => {
      v.id = "B".repeat(32);
    },
    (v) => {
      v.id = "b".repeat(31);
    },
    (v) => {
      v.visible = 1;
    },
    (v) => {
      v.bounds.x = -1;
    },
    (v) => {
      v.bounds.y = Infinity;
    },
    (v) => {
      v.bounds.width = NaN;
    },
    (v) => {
      v.bounds.height = 32769;
    },
    (v) => {
      v.bounds.css = "position:fixed";
    },
    (v) => {
      v.appearance.theme = "system";
    },
    (v) => {
      v.appearance.density = "tight";
    },
    (v) => {
      v.appearance.scale = 0.74;
    },
    (v) => {
      v.appearance.scale = 2.01;
    },
    (v) => {
      v.appearance.scale = NaN;
    },
    (v) => {
      v.appearance.locale = "zh";
    },
    (v) => {
      v.appearance.locale = "ja";
    },
    (v) => {
      v.appearance.reduce_motion = "false";
    },
    (v) => {
      v.appearance.palette.bg = "url(https://example.test/)";
    },
    (v) => {
      v.appearance.palette.text = "#fff";
    },
    (v) => {
      v.appearance.palette.text = "#ffffff00";
    },
    (v) => {
      delete v.appearance.palette.panel;
    },
    (v) => {
      v.appearance.palette.css = "body{display:none}";
    },
    (v) => {
      v.appearance.preload = "arbitrary.js";
    },
  ];
  for (const change of invalid) {
    const bad = magpieView();
    change(bad);
    assert.equal(parseMagpieView(bad), null);
  }
  for (const bad of [null, [], {}, { ...magpieView(), appearance: null }])
    assert.equal(parseMagpieView(bad), null);
});

test("host messages validate view metadata and preserve separate show, sync and ownership-bound close", () => {
  const view = magpieView();
  for (const message of [
    { type: "magpie", id: 4, url: `http://127.0.0.1:3431/?k=${"a".repeat(64)}`, view },
    { type: "magpie_view", id: 4, view },
    { type: "magpie_close", id: 4, view_id: view.id },
  ])
    assert.deepEqual(parseServerLine(JSON.stringify(message)), message);
  for (const message of [
    { type: "magpie_view", id: 4, view: { ...view, visible: 1 } },
    { type: "magpie_view", id: 4, view, url: "http://example.test" },
    { type: "magpie_view", id: 4 },
    { type: "magpie_close", id: 4, view_id: "not-an-id" },
    { type: "magpie_close", id: 4, view_id: view.id, extra: true },
    { type: "magpie", id: 4, url: `http://127.0.0.1:3431/?k=${"a".repeat(64)}&theme=dark`, view },
  ])
    assert.equal(parseServerLine(JSON.stringify(message)), null);
});

test("embedded bounds keep VIKI navigation exposed and account for browser zoom only once", () => {
  const view = magpieView();
  assert.deepEqual(magpieBounds(view, [1200, 800], 1), {
    header: { x: 260, y: 96, width: 700, height: 48 },
    dashboard: { x: 260, y: 144, width: 700, height: 452 },
  });
  view.appearance.scale = 1.5;
  assert.deepEqual(magpieBounds(view, [1200, 800], 1.25), {
    header: { x: 325, y: 120, width: 875, height: 90 },
    dashboard: { x: 325, y: 210, width: 875, height: 535 },
  });
  assert.deepEqual(magpieBounds(view, [300, 130], 1.25), {
    header: { x: 300, y: 120, width: 0, height: 10 },
    dashboard: { x: 300, y: 130, width: 0, height: 0 },
  });
});

test("VIKI presentation embeds local fonts and keeps Magpie controls and data-color semantics", () => {
  const appearance = magpieView().appearance;
  const style = magpieStyle(appearance);
  assert.equal((magpieFonts().match(/@font-face/g) ?? []).length, 5);
  assert.match(style, /--bg:#0b0b0b !important/);
  assert.match(style, /--accent:#f8f8ff !important/);
  assert.match(style, /--ui-font:"IBM Plex Sans"/);
  assert.match(style, /--code-font:"IBM Plex Mono"/);
  assert.doesNotMatch(style, /(?:https?:|file:|--c1:|--red:|--green:|#nav\s*\{|#prefs\s*\{)/);
  assert.ok(decodeURIComponent(magpieHeader(appearance)).includes("Providers and routing"));
  assert.ok(decodeURIComponent(magpieHeader(appearance)).includes("Back to chat models"));
  assert.ok(decodeURIComponent(magpieHeader(appearance, true)).includes("Return to chat models"));
  appearance.reduce_motion = true;
  assert.match(magpieStyle(appearance), /animation:none !important/);
});

test("font resources accept only bounded inline WOFF2 fonts, and installed mappings include every face", () => {
  const origin = "http://127.0.0.1:3431";
  const font = "data:font/woff2;base64,d09GMg==";
  assert.equal(allowsMagpieRequest(font, origin, APP_ORIGIN, "font"), true);
  for (const type of ["script", "image", "xhr", "mainFrame", "subFrame", undefined])
    assert.equal(allowsMagpieRequest(font, origin, APP_ORIGIN, type), false);
  for (const url of [
    "data:application/javascript;base64,AAAA",
    "data:font/ttf;base64,AAAA",
    `${font};x`,
    `data:font/woff2;base64,${"A".repeat(192 * 1024)}`,
    `${APP_ORIGIN}/font.woff2`,
  ])
    assert.equal(allowsMagpieRequest(url, origin, APP_ORIGIN, "font"), false);
  const root = dirname(dirname(fileURLToPath(import.meta.url)));
  const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
  for (const [name, , weight] of MAGPIE_FONTS) {
    const filename = `${name}-latin-${weight}-normal.woff2`;
    assert.ok(
      pkg.build.files.some(
        (entry) =>
          typeof entry === "object" &&
          entry.to === "fonts" &&
          entry.from.endsWith(`/${name}/files`) &&
          entry.filter.includes(filename),
      ),
    );
  }
});
