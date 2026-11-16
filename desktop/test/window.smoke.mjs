/**
 * Window smoke: opens a real hardened window on a local page and checks, from
 * inside the page, what `window.mjs` lets it do. Run under Electron with `npm run smoke`. Prints
 * one line per check, with no URL; exits 1 when a check fails or the run passes 60 s.
 */

import { mkdtempSync, rmSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { app, BrowserWindow, Menu, session } from "electron";

import { createMagpiePanel } from "../magpie.mjs";
import { isAppUrl, spellcheckOn } from "../policy.mjs";
import { createWindow, hardenSession, installMenu } from "../window.mjs";
import { magpieView } from "./magpie-view-fixture.mjs";

const GUARD_MS = 60_000;
const SETTLE_MS = 500;
const WAIT_MS = 5_000;
const PAGE = "<!doctype html><title>smoke</title><p>smoke</p>";
const PERMISSION_STATES = [
  ["microphone", "granted"],
  ["notifications", "granted"],
  ["clipboard-write", "granted"],
  ["camera", "denied"],
  ["clipboard-read", "denied"],
];

// A fresh profile per run: Electron's default one would land in the user's app-data folder.
const userData = mkdtempSync(join(tmpdir(), "viki-smoke-"));
app.setPath("userData", userData);

/**
 * Removes the run's profile, best effort since Chromium may still hold its files, then exits.
 * @param {number} code
 */
function finish(code) {
  try {
    rmSync(userData, { recursive: true, force: true });
  } catch {
    // What Chromium still holds stays in the temp folder.
  }
  app.exit(code);
}

const guard = setTimeout(() => {
  console.log("smoke within 60 s: FAIL");
  finish(1);
}, GUARD_MS);

app.enableSandbox();
// Fake capture devices: the camera request reaches the permission handler without real hardware,
// and a broken policy opens no real camera.
app.commandLine.appendSwitch("use-fake-device-for-media-stream");
// No check reaches the network: every host except the local page fails to resolve.
app.commandLine.appendSwitch("host-resolver-rules", "MAP * ~NOTFOUND, EXCLUDE 127.0.0.1");
// The unload check closes the only window; the run must outlive it.
app.on("window-all-closed", () => {});

let failed = false;

/**
 * Prints `name: PASS` when `run` returns true, else `name: FAIL`. A thrown error fails the check
 * unprinted, since its message can hold a URL.
 * @param {string} name
 * @param {() => boolean | Promise<boolean>} run
 */
async function check(name, run) {
  let ok = false;
  try {
    ok = (await run()) === true;
  } catch {
    ok = false;
  }
  if (!ok) failed = true;
  console.log(`${name}: ${ok ? "PASS" : "FAIL"}`);
}

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function smoke() {
  let appRequests = 0;
  const server = createServer((_request, response) => {
    appRequests += 1;
    response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    response.end(PAGE);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const spellcheck = spellcheckOn(process.platform);
  const ses = session.defaultSession;
  let downloads = 0;
  ses.on("spellcheck-dictionary-download-begin", (_event, languageCode) => {
    downloads += 1;
    // Names the language a future regression tries to fetch; never a URL.
    console.log(`spellcheck dictionary download begin: ${languageCode}`);
  });
  hardenSession(ses, { origin, spellcheck });
  await check("application menu installs", () => {
    installMenu({ platform: process.platform, packaged: app.isPackaged });
    return Menu.getApplicationMenu() !== null;
  });

  let leave = false;
  let onAsked = () => {};
  const win = createWindow(`${origin}/#token=smoke`, {
    origin,
    devTools: false,
    spellcheck,
    confirmLeave: (target) => {
      onAsked(target);
      return leave;
    },
  });
  const contents = win.webContents;
  await new Promise((resolve) => contents.once("did-finish-load", resolve));
  const page = (code) => contents.executeJavaScript(code, true);

  await check("native caption is blank and window controls are enabled", () => {
    return win.getTitle() === "" && win.isMinimizable() && win.isMaximizable() && win.isClosable();
  });
  await check("the accessible title identifies VIKI Agent", () => {
    return win.accessibleTitle === "VIKI Agent";
  });
  await check("changing the page title keeps the native caption blank", async () => {
    const updated = new Promise((resolve) => win.once("page-title-updated", resolve));
    await page('document.title = "Changed page title"; true');
    await updated;
    return win.getTitle() === "";
  });

  await check(
    "page has no process or require",
    async () =>
      (await page('typeof process === "undefined" && typeof require === "undefined"')) === true,
  );
  if (process.platform === "win32" || process.platform === "darwin") {
    await check("renderer is OS-sandboxed", () => {
      const pid = contents.getOSProcessId();
      return app.getAppMetrics().find((metric) => metric.pid === pid)?.sandboxed === true;
    });
  }
  for (const [name, state] of PERMISSION_STATES) {
    await check(
      `permissions.query ${name} is ${state}`,
      async () =>
        (await page(`navigator.permissions.query({ name: "${name}" }).then((s) => s.state)`)) ===
        state,
    );
  }
  await check(
    "Notification.requestPermission is granted",
    async () => (await page("Notification.requestPermission()")) === "granted",
  );
  await check(
    "getUserMedia video rejects NotAllowedError",
    async () =>
      (await page(`navigator.mediaDevices.getUserMedia({ video: true }).then(
        (stream) => { for (const track of stream.getTracks()) track.stop(); return "resolved"; },
        (error) => error.name,
      )`)) === "NotAllowedError",
  );
  await check(
    "geolocation fails with code 1",
    async () =>
      (await page(`new Promise((resolve) => navigator.geolocation.getCurrentPosition(
        () => resolve(0), (error) => resolve(error.code), { timeout: ${WAIT_MS} },
      ))`)) === 1,
  );
  await check(
    "window.open returns null and adds no window",
    async () =>
      (await page('window.open("https://example.com/") === null')) === true &&
      BrowserWindow.getAllWindows().length === 1,
  );
  await check("navigation off the app origin keeps the app URL", async () => {
    await page('location.href = "https://example.com/"; true');
    await delay(SETTLE_MS);
    return isAppUrl(contents.getURL(), origin);
  });

  const dashboardServer = createServer((_request, response) => {
    response.writeHead(200, {
      "content-type": "text/html; charset=utf-8",
      "content-security-policy":
        "default-src 'self'; style-src 'none'; font-src data:; script-src 'self' 'unsafe-inline'; connect-src *; img-src * data:; frame-src 'none'",
    });
    response.end(`<!doctype html><title>synthetic dashboard</title>
      <div class="brand">magpie</div><nav id="nav"><button onclick="this.textContent='Selected'">Providers</button></nav>
      <button id="prefs">Settings</button><p>synthetic dashboard</p>`);
  });
  await new Promise((resolve) => dashboardServer.listen(0, "127.0.0.1", resolve));
  const dashboardOrigin = `http://127.0.0.1:${dashboardServer.address().port}`;
  const dashboardUrl = `${dashboardOrigin}/?k=${"a".repeat(64)}`;
  await page(
    'sessionStorage.setItem("window-secret", "synthetic"); document.cookie="viki=synthetic"',
  );
  const externalLinks = [];
  let panel = createMagpiePanel(win, dashboardUrl, {
    appOrigin: origin,
    onOpenExternal: (url) => externalLinks.push(url),
  });
  await Promise.all([
    new Promise((resolve) => panel.contents.once("did-finish-load", resolve)),
    new Promise((resolve) => panel.header.once("did-finish-load", resolve)),
  ]);
  const dashboardPage = (code) => panel.contents.executeJavaScript(code, true);
  await check("Magpie has a separate in-memory session without VIKI secrets or Node", async () => {
    return (
      panel.contents.session !== contents.session &&
      !panel.contents.session.isPersistent() &&
      (await dashboardPage(`typeof process === "undefined" && typeof require === "undefined" &&
        sessionStorage.getItem("window-secret") === null && !document.cookie.includes("viki=")`)) ===
        true
    );
  });
  await check("Magpie header has scripts disabled and remains above the dashboard", () => {
    const children = win.contentView.children;
    const headerView = children.find((view) => view.webContents === panel.header);
    const dashboardView = children.find((view) => view.webContents === panel.contents);
    return (
      panel.header.getLastWebPreferences().javascript === false &&
      headerView.getBounds().height === 48 &&
      dashboardView.getBounds().y === 48 &&
      dashboardView.getBounds().width === win.getContentSize()[0]
    );
  });
  await check("Magpie follows the VIKI window size", async () => {
    const resized = new Promise((resolve) => win.once("resize", resolve));
    win.setContentSize(960, 720);
    await Promise.race([resized, delay(WAIT_MS)]);
    const view = win.contentView.children.find((child) => child.webContents === panel.contents);
    const [width, height] = win.getContentSize();
    return view.getBounds().width === width && view.getBounds().height === height - 48;
  });
  await check("Magpie refuses VIKI network requests", async () => {
    const before = appRequests;
    const refused = await dashboardPage(`fetch(${JSON.stringify(origin)}, { mode: "no-cors" })
      .then(() => false, () => true)`);
    return refused === true && appRequests === before;
  });
  await check("Magpie refuses microphone, notifications and clipboard reads", async () => {
    for (const name of ["microphone", "notifications", "clipboard-read"]) {
      if (
        (await dashboardPage(`navigator.permissions.query({name:"${name}"}).then(s=>s.state)`)) !==
        "denied"
      )
        return false;
    }
    return true;
  });
  await check("Magpie cannot navigate to VIKI or open a second window", async () => {
    const noWindow = await dashboardPage('window.open("https://example.com/") === null');
    await dashboardPage(`location.href=${JSON.stringify(origin)}; true`);
    await delay(SETTLE_MS);
    return (
      noWindow === true &&
      externalLinks.length === 1 &&
      externalLinks[0] === "https://example.com/" &&
      BrowserWindow.getAllWindows().length === 1 &&
      new URL(panel.contents.getURL()).origin === dashboardOrigin
    );
  });
  await check("Magpie blob exports use Save As without writing during the smoke", async () => {
    const exported = new Promise((resolve) => {
      panel.contents.session.once("will-download", (event, item) => {
        event.preventDefault();
        resolve(item.getSaveDialogOptions().title === "Save Magpie download");
      });
    });
    await dashboardPage(`const link=document.createElement("a");
      link.href=URL.createObjectURL(new Blob(["synthetic"],{type:"text/plain"}));
      link.download="synthetic-export.txt";document.body.append(link);link.click();true`);
    return (await Promise.race([exported, delay(WAIT_MS)])) === true;
  });
  await check("Magpie Back closes both views and keeps the VIKI window", async () => {
    const closed = Promise.all([
      new Promise((resolve) => panel.contents.once("destroyed", resolve)),
      new Promise((resolve) => panel.header.once("destroyed", resolve)),
    ]);
    panel.header.focus();
    panel.header.sendInputEvent({
      type: "mouseDown",
      x: win.getContentSize()[0] - 90,
      y: 24,
      button: "left",
      clickCount: 1,
    });
    panel.header.sendInputEvent({
      type: "mouseUp",
      x: win.getContentSize()[0] - 90,
      y: 24,
      button: "left",
      clickCount: 1,
    });
    await Promise.race([closed, delay(WAIT_MS)]);
    return (
      panel.contents.isDestroyed() &&
      panel.header.isDestroyed() &&
      !win.isDestroyed() &&
      contents.isFocused()
    );
  });
  const embedded = magpieView();
  embedded.appearance.scale = 1.5;
  win.setContentSize(1200, 800);
  contents.setZoomFactor(1.25);
  panel = createMagpiePanel(win, dashboardUrl, { appOrigin: origin, view: embedded });
  await new Promise((resolve) => panel.contents.once("did-finish-load", resolve));
  await check(
    "Magpie fits VIKI's content area while navigation stays exposed at browser and text zoom",
    async () => {
      const headerView = win.contentView.children.find(
        (child) => child.webContents === panel.header,
      );
      const dashboardView = win.contentView.children.find(
        (child) => child.webContents === panel.contents,
      );
      // The host finishes asynchronous styling and layout before exposing the dashboard.
      const deadline = Date.now() + WAIT_MS;
      while (!dashboardView.getVisible()) {
        if (Date.now() >= deadline) return false;
        await delay(10);
      }
      const h = headerView.getBounds(),
        d = dashboardView.getBounds();
      return (
        h.x === 325 &&
        h.y === 120 &&
        h.width === 875 &&
        h.height === 90 &&
        d.x === 325 &&
        d.y === 210 &&
        d.width === 875 &&
        d.height === 535 &&
        panel.contents.getZoomFactor() === 1.875 &&
        panel.header.getZoomFactor() === 1.875
      );
    },
  );
  await check(
    "VIKI colors, local fonts and Magpie navigation work under restrictive stylesheet CSP",
    async () => {
      const fontLoaded = await dashboardPage(`document.fonts.load('400 14px "IBM Plex Sans"')
      .then(faces => faces.length > 0 && faces.every(face => face.status === 'loaded'))`);
      if (!fontLoaded) return false;
      return (
        (await dashboardPage(`(() => {
      const root = getComputedStyle(document.documentElement);
      document.querySelector('#nav button').click();
      return root.getPropertyValue('--bg').trim() === '#0b0b0b' &&
        getComputedStyle(document.body).backgroundColor === 'rgb(11, 11, 11)' &&
        document.fonts.check('400 14px "IBM Plex Sans"') &&
        getComputedStyle(document.querySelector('.brand')).display === 'none' &&
        document.querySelector('#nav button').textContent === 'Selected' &&
        document.querySelector('#prefs').offsetParent !== null;
    })()`)) === true
      );
    },
  );
  await check(
    "VIKI appearance updates preserve the open dashboard and notify plugin theme observers",
    async () => {
      await dashboardPage(`window.syntheticDraft='keep'; window.themeUpdates=0;
      new MutationObserver(()=>window.themeUpdates++).observe(document.documentElement,{attributes:true});true`);
      const light = structuredClone(embedded);
      light.appearance.theme = "light";
      light.appearance.palette.bg = "#f4f4f4";
      light.appearance.palette.panel = "#ffffff";
      light.appearance.palette.text = "#111111";
      panel.update(light);
      return (
        (await dashboardPage(`new Promise(resolve => {
      function check(){ if(document.documentElement.dataset.theme==='light') resolve(
        window.syntheticDraft==='keep' && window.themeUpdates>0 &&
        getComputedStyle(document.body).backgroundColor==='rgb(244, 244, 244)');
        else requestAnimationFrame(check); } check();
    })`)) === true
      );
    },
  );
  await check("stale Magpie updates are ignored and hidden native views expose VIKI", () => {
    const stale = structuredClone(embedded);
    stale.id = "c".repeat(32);
    stale.visible = false;
    if (panel.update(stale)) return false;
    const hidden = structuredClone(embedded);
    hidden.visible = false;
    const accepted = panel.update(hidden);
    const children = win.contentView.children.filter(
      (child) => child.webContents === panel.header || child.webContents === panel.contents,
    );
    const invisible = children.every((child) => !child.getVisible());
    panel.update(embedded);
    return accepted && invisible && panel.viewId === embedded.id;
  });
  await check("Escape returns from Magpie", async () => {
    const closed = Promise.all([
      new Promise((resolve) => panel.contents.once("destroyed", resolve)),
      new Promise((resolve) => panel.header.once("destroyed", resolve)),
    ]);
    panel.contents.focus();
    panel.contents.sendInputEvent({ type: "keyDown", keyCode: "Escape" });
    await Promise.race([closed, delay(WAIT_MS)]);
    return panel.contents.isDestroyed() && panel.header.isDestroyed();
  });
  contents.setZoomFactor(1);
  panel = createMagpiePanel(win, dashboardUrl, { appOrigin: origin });
  await new Promise((resolve) => panel.contents.once("did-finish-load", resolve));

  await page('addEventListener("beforeunload", (event) => event.preventDefault()); true');
  const closeAndAsk = (choice) => {
    leave = choice;
    const asked = new Promise((resolve) => {
      onAsked = resolve;
    });
    win.close();
    return Promise.race([asked, delay(WAIT_MS)]);
  };
  await check("unload blocker: Stay keeps the window", async () => {
    const target = await closeAndAsk(false);
    await delay(SETTLE_MS);
    return target === win && !win.isDestroyed();
  });
  await check("unload blocker: Leave closes the window", async () => {
    const closed = new Promise((resolve) => win.once("closed", resolve));
    const panelClosed = Promise.all([
      new Promise((resolve) => panel.contents.once("destroyed", resolve)),
      new Promise((resolve) => panel.header.once("destroyed", resolve)),
    ]);
    const target = await closeAndAsk(true);
    await Promise.race([closed, delay(WAIT_MS)]);
    await Promise.race([panelClosed, delay(WAIT_MS)]);
    return target === win && win.isDestroyed();
  });
  await check(
    "closing VIKI destroys both Magpie views",
    () => panel.contents.isDestroyed() && panel.header.isDestroyed(),
  );
  dashboardServer.close();

  await check("no spellcheck dictionary download began", () => downloads === 0);
  await check(
    "spellchecker enabled matches spellcheckOn",
    () => ses.isSpellCheckerEnabled() === spellcheck,
  );
}

app.whenReady().then(async () => {
  await check("smoke ran to the end", async () => {
    await smoke();
    return true;
  });
  clearTimeout(guard);
  finish(failed ? 1 : 0);
});
