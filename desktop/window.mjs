/**
 * @module window
 * Electron wiring for the VIKI Agent desktop window: applies `policy.mjs` to a session, the
 * application menu and each `BrowserWindow`. Windows load no preload script; `windowOptions`
 * keeps the page context-isolated, sandboxed and without Node.
 */

import { BrowserWindow, dialog, Menu } from "electron";

import {
  allowsPermissionCheck,
  allowsPermissionRequest,
  contextMenuTemplate,
  isAppUrl,
  menuTemplate,
  windowOptions,
} from "./policy.mjs";

/**
 * Applies the permission and spellcheck policy to `ses`.
 * @param {import("electron").Session} ses
 * @param {{ origin: string, spellcheck: boolean }} options
 */
export function hardenSession(ses, { origin, spellcheck }) {
  ses.setPermissionRequestHandler((_contents, permission, callback, details) => {
    callback(allowsPermissionRequest(permission, details, origin));
  });
  ses.setPermissionCheckHandler((_contents, permission, requestingOrigin, details) =>
    allowsPermissionCheck(permission, requestingOrigin, details, origin),
  );
  ses.setDevicePermissionHandler(() => false);
  if (!spellcheck) {
    // Electron still fetches a Hunspell dictionary on Windows and Linux for whatever languages
    // this session holds unless the list itself is emptied first; disabling the spellchecker
    // alone does not stop the download.
    ses.setSpellCheckerLanguages([]);
  }
  ses.setSpellCheckerEnabled(spellcheck);
}

/**
 * @param {{ platform: string, packaged: boolean }} options
 */
export function installMenu({ platform, packaged }) {
  Menu.setApplicationMenu(Menu.buildFromTemplate(menuTemplate({ platform, packaged })));
}

/**
 * Asks whether to leave a page whose `beforeunload` handler blocks the unload.
 * @param {import("electron").BrowserWindow} win
 * @returns {boolean} true for Leave; Stay, Escape and closing the box keep the page
 */
export function askToLeave(win) {
  return (
    dialog.showMessageBoxSync(win, {
      type: "question",
      buttons: ["Leave", "Stay"],
      defaultId: 1,
      cancelId: 1,
      title: "VIKI Agent",
      message: "Leave without saving?",
      detail: "Changes you made may not be saved.",
    }) === 0
  );
}

/**
 * Opens a hardened window on `url`: no new windows, no webviews, and no navigation or redirect
 * off `origin`.
 * @param {string} url - may carry the launch token in its fragment: do not print it
 * @param {{ origin: string, icon?: string, devTools: boolean, spellcheck: boolean,
 *   confirmLeave?: (win: import("electron").BrowserWindow) => boolean }} options
 * @returns {import("electron").BrowserWindow}
 */
export function createWindow(
  url,
  { origin, icon, devTools, spellcheck, confirmLeave = askToLeave },
) {
  const win = new BrowserWindow(windowOptions({ icon, devTools, spellcheck }));
  win.accessibleTitle = "VIKI Agent";
  // Page titles, including a synthesized URL, must not fill the native caption.
  win.on("page-title-updated", (event) => event.preventDefault());
  const contents = win.webContents;
  contents.setWindowOpenHandler(() => ({ action: "deny" }));
  const stayInApp = (event) => {
    if (!isAppUrl(event.url, origin)) event.preventDefault();
  };
  contents.on("will-navigate", stayInApp);
  contents.on("will-redirect", stayInApp);
  contents.on("will-attach-webview", (event) => event.preventDefault());
  // Here preventDefault overrides the page's beforeunload handler: the page unloads.
  contents.on("will-prevent-unload", (event) => {
    if (confirmLeave(win)) event.preventDefault();
  });
  contents.on("context-menu", (_event, params) => {
    const template = contextMenuTemplate(params, (suggestion) =>
      contents.replaceMisspelling(suggestion),
    );
    if (template.length > 0) Menu.buildFromTemplate(template).popup({ window: win });
  });
  win.loadURL(url);
  return win;
}
