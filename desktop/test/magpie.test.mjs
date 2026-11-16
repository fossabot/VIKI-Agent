import assert from "node:assert/strict";
import { test } from "node:test";

import {
  APP_ORIGIN,
  allowsMagpieDownload,
  allowsMagpieOpenUrl,
  allowsMagpiePermission,
  allowsMagpieRequest,
  magpieOrigin,
} from "../policy.mjs";
import { parseServerLine } from "../server.mjs";

const ORIGIN = "http://127.0.0.1:3430";
const URL = `${ORIGIN}/?k=${"a".repeat(64)}`;

test("Magpie launch messages accept only the supervised URL and exact fields", () => {
  assert.equal(magpieOrigin(URL), ORIGIN);
  assert.deepEqual(parseServerLine(JSON.stringify({ type: "magpie", id: 1, url: URL })), {
    type: "magpie",
    id: 1,
    url: URL,
  });
  assert.deepEqual(parseServerLine('{"type":"magpie_close","id":1}'), {
    type: "magpie_close",
    id: 1,
  });
  for (const url of [
    URL.replace("127.0.0.1", "localhost"),
    URL.replace("127.0.0.1", "127.1"),
    URL.replace("http:", "https:"),
    URL.replace(":3430", ":8765"),
    URL.replace(":3430", ":0"),
    URL.replace(":3430", ":65536"),
    URL.replace(":3430", ":03430"),
    URL.replace("/?", "/admin?"),
    URL.replace("127.0.0.1", "user@127.0.0.1"),
    URL.replace("?k=", "?key="),
    URL.replace("a".repeat(64), "a".repeat(63)),
    URL.replace("a".repeat(64), "A".repeat(64)),
    `${URL}&other=1`,
    `${URL}#fragment`,
    `${URL}\n`,
  ]) {
    assert.equal(magpieOrigin(url), null);
    assert.equal(parseServerLine(JSON.stringify({ type: "magpie", id: 1, url })), null);
  }
  for (const message of [
    { type: "magpie", id: 1, url: URL, extra: true },
    { type: "magpie", id: true, url: URL },
    { type: "magpie", id: 0, url: URL },
    { type: "magpie", id: 1 },
    { type: "magpie_close", id: 1, url: URL },
  ]) {
    assert.equal(parseServerLine(JSON.stringify(message)), null);
  }
});

test("dashboard requests stay off VIKI, file and other local services", () => {
  for (const url of [
    `${ORIGIN}/api/config`,
    "ws://127.0.0.1:3430/plugin/service",
    `blob:${ORIGIN}/synthetic-export`,
    "https://cdn.jsdelivr.net/npm/vue@3/dist/vue.global.prod.js",
  ]) {
    assert.equal(allowsMagpieRequest(url, ORIGIN), true);
  }
  for (const url of [
    `${APP_ORIGIN}/api/v2/sessions`,
    "http://localhost:8765/",
    "http://127.0.0.1:9000/",
    "https://127.0.0.1:8765/",
    "https://localhost/",
    "https://x.localhost/",
    "https://router.local/",
    "https://[::1]/",
    "https://192.168.1.1/",
    "ws://127.0.0.1:8765/ws",
    "ws://127.0.0.1:9000/service",
    "wss://127.0.0.1:3430/service",
    "wss://example.com/service",
    "https://user:password@example.com/",
    "file:///tmp/a",
    "javascript:alert(1)",
    "data:text/html,hello",
    "vscode://file/a",
    "not a URL",
  ]) {
    assert.equal(allowsMagpieRequest(url, ORIGIN), false);
  }
});

test("inline dashboard image masks are allowed only as image resources", () => {
  const mask = "data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%3E%3C/svg%3E";
  assert.equal(allowsMagpieRequest(mask, ORIGIN, APP_ORIGIN, "image"), true);
  for (const kind of [undefined, "mainFrame", "subFrame", "script", "xhr"]) {
    assert.equal(allowsMagpieRequest(mask, ORIGIN, APP_ORIGIN, kind), false);
  }
  assert.equal(allowsMagpieRequest("data:text/html,hello", ORIGIN, APP_ORIGIN, "image"), false);
});

test("downloads accept dashboard exports and reject remote or VIKI sources", () => {
  assert.equal(allowsMagpieDownload(`${ORIGIN}/export`, ORIGIN), true);
  assert.equal(allowsMagpieDownload(`blob:${ORIGIN}/export`, ORIGIN), true);
  for (const url of [
    APP_ORIGIN,
    `blob:${APP_ORIGIN}/export`,
    "https://example.test/export",
    "file:///a",
  ]) {
    assert.equal(allowsMagpieDownload(url, ORIGIN), false);
  }
});

test("external links require absolute web URLs without credentials or VIKI targets", () => {
  assert.equal(allowsMagpieOpenUrl("https://example.test/oauth?state=synthetic"), true);
  assert.equal(allowsMagpieOpenUrl("http://example.test/guide"), true);
  for (const url of [
    APP_ORIGIN,
    "http://localhost:8765/",
    "http://[::1]:8765/",
    "https://user:password@example.com/",
    "https://example.test/has space",
    "https://example.test/\n",
    "https://example.test/\u0000",
    "https://example.test/\\path",
    "http:example.test",
    "file:///a",
    "javascript:alert(1)",
    "/relative",
    `https://example.test/${"a".repeat(2048)}`,
    null,
  ]) {
    assert.equal(allowsMagpieOpenUrl(url), false);
  }
});

test("dashboard permissions allow only clipboard writes from its own HTTP origin", () => {
  assert.equal(allowsMagpiePermission("clipboard-sanitized-write", `${ORIGIN}/`, ORIGIN), true);
  for (const permission of ["clipboard-read", "notifications", "media", "geolocation", "usb"]) {
    assert.equal(allowsMagpiePermission(permission, `${ORIGIN}/`, ORIGIN), false);
  }
  for (const url of [APP_ORIGIN, undefined, `blob:${ORIGIN}/id`, "https://example.test/"]) {
    assert.equal(allowsMagpiePermission("clipboard-sanitized-write", url, ORIGIN), false);
  }
});
