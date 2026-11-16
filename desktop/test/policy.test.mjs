import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  APP_ID,
  APP_ORIGIN,
  allowsPermissionCheck,
  allowsPermissionRequest,
  contextMenuTemplate,
  isAppUrl,
  menuTemplate,
  sandboxOff,
  spellcheckOn,
  windowOptions,
} from "../policy.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const DESKTOP_ROOT = join(here, "..");

// -- isAppUrl --------------------------------------------------------------------------------

test("isAppUrl accepts only the app origin, credential-free", () => {
  assert.equal(isAppUrl("http://127.0.0.1:8765/", APP_ORIGIN), true);
  assert.equal(isAppUrl("http://127.0.0.1:8765/anything?x=1#y", APP_ORIGIN), true);
  assert.equal(isAppUrl("http://127.0.0.1:9999/", APP_ORIGIN), false, "another port");
  assert.equal(isAppUrl("http://localhost:8765/", APP_ORIGIN), false, "another host");
  assert.equal(isAppUrl("https://127.0.0.1:8765/", APP_ORIGIN), false, "https, not http");
  assert.equal(isAppUrl("file:///etc/passwd", APP_ORIGIN), false, "file: URL");
  assert.equal(
    isAppUrl("blob:http://127.0.0.1:8765/9c33b65e-0b22-4c37-9f1a-9b6b6f1f2b3a", APP_ORIGIN),
    false,
    "blob: URL reporting the app's origin",
  );
  assert.equal(isAppUrl("data:text/html,x", APP_ORIGIN), false, "data: URL");
  assert.equal(
    isAppUrl("filesystem:http://127.0.0.1:8765/temporary/x", APP_ORIGIN),
    false,
    "filesystem: URL",
  );
  assert.equal(isAppUrl(undefined, APP_ORIGIN), false, "missing URL");
  assert.equal(isAppUrl("not a url", APP_ORIGIN), false, "unparseable");
  assert.equal(isAppUrl("http://user@127.0.0.1:8765/", APP_ORIGIN), false, "embedded username");
  assert.equal(
    isAppUrl("http://user:pass@127.0.0.1:8765/", APP_ORIGIN),
    false,
    "embedded username and password",
  );
});

// -- sandboxOff (hard rule 7) -------------------------------------------------------------

test('sandboxOff is true only for an exact true from hasSwitch("no-sandbox")', () => {
  assert.equal(
    sandboxOff(() => true),
    true,
  );
  assert.equal(
    sandboxOff(() => false),
    false,
  );
  assert.equal(
    sandboxOff(() => undefined),
    false,
  );
  assert.equal(
    sandboxOff(() => 1),
    false,
    "a truthy non-boolean must not count",
  );
  assert.equal(
    sandboxOff(() => "true"),
    false,
    "a truthy string must not count",
  );
});

test('sandboxOff asks hasSwitch for "no-sandbox"', () => {
  const asked = [];
  sandboxOff((name) => {
    asked.push(name);
    return false;
  });
  assert.deepEqual(asked, ["no-sandbox"]);
});

// -- allowsPermissionRequest -----------------------------------------------------------

const OTHER_PORT = "http://127.0.0.1:9999/";
const SAME_HOST_NAME = "http://localhost:8765/";
const HTTPS = "https://127.0.0.1:8765/";
const FILE_URL = "file:///etc/passwd";

const grantedOnAppOrigin = ["notifications", "clipboard-sanitized-write"];

for (const permission of grantedOnAppOrigin) {
  test(`allowsPermissionRequest grants ${permission} on the app origin`, () => {
    assert.equal(
      allowsPermissionRequest(permission, { requestingUrl: "http://127.0.0.1:8765/" }, APP_ORIGIN),
      true,
    );
  });

  for (const [label, url] of [
    ["another port", OTHER_PORT],
    ["localhost:8765", SAME_HOST_NAME],
    ["https", HTTPS],
    ["file:", FILE_URL],
    ["a missing URL", undefined],
  ]) {
    test(`allowsPermissionRequest refuses ${permission} on ${label}`, () => {
      assert.equal(allowsPermissionRequest(permission, { requestingUrl: url }, APP_ORIGIN), false);
    });
  }
}

for (const permission of [
  "geolocation",
  "clipboard-read",
  "display-capture",
  "hid",
  "usb",
  "serial",
  "unknown-permission",
]) {
  test(`allowsPermissionRequest refuses ${permission} even on the app origin`, () => {
    assert.equal(
      allowsPermissionRequest(permission, { requestingUrl: "http://127.0.0.1:8765/" }, APP_ORIGIN),
      false,
    );
  });
}

const mediaRows = [
  ["audio", ["audio"], true],
  ["video", ["video"], false],
  ["both audio and video", ["audio", "video"], false],
  ["empty", [], false],
  ["an unknown type", ["screen"], false],
];

for (const [label, mediaTypes, expected] of mediaRows) {
  test(`allowsPermissionRequest media ${label} on the app origin is ${expected}`, () => {
    assert.equal(
      allowsPermissionRequest(
        "media",
        { requestingUrl: "http://127.0.0.1:8765/", mediaTypes },
        APP_ORIGIN,
      ),
      expected,
    );
  });
}

test("allowsPermissionRequest refuses media audio off the app origin", () => {
  assert.equal(
    allowsPermissionRequest(
      "media",
      { requestingUrl: OTHER_PORT, mediaTypes: ["audio"] },
      APP_ORIGIN,
    ),
    false,
  );
});

test("allowsPermissionRequest refuses media audio from a blob URL reporting the app's origin", () => {
  assert.equal(
    allowsPermissionRequest(
      "media",
      {
        requestingUrl: "blob:http://127.0.0.1:8765/9c33b65e-0b22-4c37-9f1a-9b6b6f1f2b3a",
        mediaTypes: ["audio"],
      },
      APP_ORIGIN,
    ),
    false,
  );
});

// -- allowsPermissionCheck -------------------------------------------------------------

for (const permission of grantedOnAppOrigin) {
  test(`allowsPermissionCheck grants ${permission} on the app origin`, () => {
    assert.equal(
      allowsPermissionCheck(permission, "http://127.0.0.1:8765/", undefined, APP_ORIGIN),
      true,
    );
  });

  test(`allowsPermissionCheck grants ${permission} with a trailing slash`, () => {
    assert.equal(
      allowsPermissionCheck(permission, "http://127.0.0.1:8765/", undefined, APP_ORIGIN),
      true,
    );
  });

  for (const [label, origin] of [
    ["another port", OTHER_PORT],
    ["localhost:8765", SAME_HOST_NAME],
    ["https", HTTPS],
    ["file:", FILE_URL],
    ["a missing URL", undefined],
  ]) {
    test(`allowsPermissionCheck refuses ${permission} on ${label}`, () => {
      assert.equal(allowsPermissionCheck(permission, origin, undefined, APP_ORIGIN), false);
    });
  }
}

for (const permission of [
  "geolocation",
  "clipboard-read",
  "display-capture",
  "hid",
  "usb",
  "serial",
  "unknown-permission",
]) {
  test(`allowsPermissionCheck refuses ${permission} even on the app origin`, () => {
    assert.equal(
      allowsPermissionCheck(permission, "http://127.0.0.1:8765/", undefined, APP_ORIGIN),
      false,
    );
  });
}

const mediaCheckRows = [
  ["audio", "audio", true],
  ["video", "video", false],
  ["unknown", "unknown", false],
  ["missing", undefined, false],
];

for (const [label, mediaType, expected] of mediaCheckRows) {
  test(`allowsPermissionCheck media ${label} on the app origin is ${expected}`, () => {
    assert.equal(
      allowsPermissionCheck("media", "http://127.0.0.1:8765/", { mediaType }, APP_ORIGIN),
      expected,
    );
  });
}

test("allowsPermissionCheck refuses media audio off the app origin", () => {
  assert.equal(
    allowsPermissionCheck("media", OTHER_PORT, { mediaType: "audio" }, APP_ORIGIN),
    false,
  );
});

// -- spellcheckOn -----------------------------------------------------------------------

test("spellcheckOn is true only on darwin", () => {
  assert.equal(spellcheckOn("darwin"), true);
  assert.equal(spellcheckOn("win32"), false);
  assert.equal(spellcheckOn("linux"), false);
});

// -- windowOptions -----------------------------------------------------------------------

test("windowOptions matches the hardened contract exactly", () => {
  assert.deepEqual(windowOptions({ devTools: false, spellcheck: true }), {
    width: 1440,
    height: 900,
    title: "",
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      nodeIntegrationInWorker: false,
      nodeIntegrationInSubFrames: false,
      webSecurity: true,
      allowRunningInsecureContent: false,
      webviewTag: false,
      accessibleTitle: "VIKI Agent",
      spellcheck: true,
      devTools: false,
    },
  });
});

test("windowOptions includes icon only when given", () => {
  assert.equal("icon" in windowOptions({ devTools: false, spellcheck: false }), false);
  assert.equal(
    windowOptions({ icon: "icon.ico", devTools: true, spellcheck: false }).icon,
    "icon.ico",
  );
});

// -- menuTemplate (platform, packaged) -----------------------------------------------------------

test("menuTemplate on darwin starts with appMenu and ends with windowMenu", () => {
  const template = menuTemplate({ platform: "darwin", packaged: false });
  assert.equal(template[0].role, "appMenu");
  assert.equal(template[1].role, "editMenu");
  assert.equal(template.at(-1).role, "windowMenu");
});

test("menuTemplate on win32 and linux has no appMenu", () => {
  for (const platform of ["win32", "linux"]) {
    const template = menuTemplate({ platform, packaged: false });
    assert.equal(template[0].role, "editMenu");
    assert.equal(
      template.some((item) => item.role === "appMenu"),
      false,
    );
  }
});

test("menuTemplate View submenu includes toggleDevTools only when unpackaged", () => {
  const dev = menuTemplate({ platform: "win32", packaged: false }).find(
    (item) => item.label === "View",
  );
  const roles = dev.submenu.map((item) => item.role ?? item.type);
  assert.deepEqual(roles, [
    "reload",
    "resetZoom",
    "zoomIn",
    "zoomOut",
    "separator",
    "togglefullscreen",
    "toggleDevTools",
  ]);

  const packaged = menuTemplate({ platform: "win32", packaged: true }).find(
    (item) => item.label === "View",
  );
  const packagedRoles = packaged.submenu.map((item) => item.role ?? item.type);
  assert.deepEqual(packagedRoles, [
    "reload",
    "resetZoom",
    "zoomIn",
    "zoomOut",
    "separator",
    "togglefullscreen",
  ]);
});

// -- contextMenuTemplate ---------------------------------------------------------------------

test("contextMenuTemplate is empty when nothing applies", () => {
  assert.deepEqual(
    contextMenuTemplate({}, () => {}),
    [],
  );
});

test("contextMenuTemplate offers dictionary suggestions with a trailing separator", () => {
  const clicked = [];
  const template = contextMenuTemplate(
    { dictionarySuggestions: ["teh", "the"], isEditable: true, editFlags: {} },
    (s) => clicked.push(s),
  );
  assert.equal(template[0].label, "teh");
  assert.equal(template[1].label, "the");
  assert.equal(template[2].type, "separator");
  template[0].click();
  assert.deepEqual(clicked, ["teh"]);
});

test("contextMenuTemplate on editable text offers cut/copy/paste/selectAll per editFlags", () => {
  const template = contextMenuTemplate(
    {
      isEditable: true,
      editFlags: {
        canCut: false,
        canCopy: true,
        canPaste: true,
        canSelectAll: false,
      },
    },
    () => {},
  );
  assert.deepEqual(template, [
    { role: "cut", enabled: false },
    { role: "copy", enabled: true },
    { role: "paste", enabled: true },
    { role: "selectAll", enabled: false },
  ]);
});

test("contextMenuTemplate on non-editable selected text offers only copy", () => {
  assert.deepEqual(
    contextMenuTemplate({ isEditable: false, selectionText: "hello" }, () => {}),
    [{ role: "copy" }],
  );
});

test("contextMenuTemplate on non-editable empty selection offers nothing", () => {
  assert.deepEqual(
    contextMenuTemplate({ isEditable: false, selectionText: "" }, () => {}),
    [],
  );
});

// -- package.json ----------------------------------------------------------------------

const pkg = JSON.parse(readFileSync(join(DESKTOP_ROOT, "package.json"), "utf8"));

test("package.json pins exactly two devDependencies and one dependency", () => {
  assert.deepEqual(pkg.devDependencies, { electron: "44.9.3", "electron-builder": "26.17.0" });
  assert.deepEqual(pkg.dependencies, { "electron-updater": "6.8.10" });
});

test("package.json overrides only minimatch's vulnerable range, with its first fixed release", () => {
  assert.deepEqual(pkg.overrides, { "minimatch@>=7.0.0 <11.0.1": "11.0.1" });
});

test("package.json has no install hooks", () => {
  for (const hook of ["preinstall", "install", "postinstall", "prepare"]) {
    assert.equal(hook in pkg.scripts, false, hook);
  }
});

test("package.json names the product", () => {
  assert.equal(pkg.productName, "VIKI Agent");
  assert.equal(pkg.name, "viki-agent-desktop");
  assert.equal(pkg.private, true);
});

test("APP_ID matches package.json's build.appId", () => {
  assert.equal(APP_ID, pkg.build.appId);
});

// -- .npmrc -----------------------------------------------------------------------------

const npmrc = readFileSync(join(DESKTOP_ROOT, ".npmrc"), "utf8");

test(".npmrc pins the supply-chain gates", () => {
  assert.match(npmrc, /^ignore-scripts=true$/m);
  assert.match(npmrc, /^save-exact=true$/m);
  assert.match(npmrc, /^before=2026-10-28T00:00:00Z$/m);
});
