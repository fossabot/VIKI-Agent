/** The bundled Magpie dashboard, isolated from the VIKI renderer and its window token. */
import { randomUUID } from "node:crypto";
import { basename } from "node:path";

import { session, WebContentsView } from "electron";

import {
  DEFAULT_MAGPIE_APPEARANCE,
  magpieBounds,
  magpieHeader,
  magpieStyle,
} from "./magpie-theme.mjs";
import {
  APP_ORIGIN,
  allowsMagpieDownload,
  allowsMagpieOpenUrl,
  allowsMagpiePermission,
  allowsMagpieRequest,
  isAppUrl,
  magpieOrigin,
  parseMagpieView,
} from "./policy.mjs";

const BACK = "https://viki-magpie.invalid/back";

/**
 * Shows Magpie under an always-visible, script-free Back header. The dashboard has a fresh
 * in-memory browser session, with no preload or renderer IPC. Neither page receives VIKI's token.
 * @param {import("electron").BrowserWindow} win
 * @param {string} url - supervised launch URL; contains a credential and must not be printed
 * @param {{ appOrigin?: string, view?: import("./policy.mjs").MagpieView, onClose?: () => void,
 *   onOpenExternal?: (url: string) => void }} [options]
 * @returns {{ close(): void, update(view: import("./policy.mjs").MagpieView): boolean,
 *   readonly viewId: string | null, contents: import("electron").WebContents,
 *   header: import("electron").WebContents }}
 */
export function createMagpiePanel(
  win,
  url,
  { appOrigin = APP_ORIGIN, view, onClose = () => {}, onOpenExternal = () => {} } = {},
) {
  const origin = magpieOrigin(url);
  if (origin === null || origin === appOrigin) throw new Error("Invalid Magpie launch.");
  let currentView = view === undefined ? null : parseMagpieView(view);
  if (view !== undefined && currentView === null) throw new Error("Invalid Magpie view.");
  const owner = win.webContents;
  const viewId = currentView?.id ?? null;
  const appearance = () => currentView?.appearance ?? DEFAULT_MAGPIE_APPEARANCE;
  const partition = randomUUID();
  const ses = session.fromPartition(`viki-magpie-${partition}`, { cache: false });
  const headerSession = session.fromPartition(`viki-magpie-header-${partition}`, { cache: false });
  headerSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  headerSession.setPermissionCheckHandler(() => false);
  headerSession.setDevicePermissionHandler(() => false);
  headerSession.webRequest.onBeforeRequest((details, callback) => {
    callback({
      cancel:
        !details.url.startsWith("data:text/html;charset=utf-8,") &&
        !(
          details.resourceType === "font" &&
          details.url.startsWith("data:") &&
          allowsMagpieRequest(details.url, origin, appOrigin, details.resourceType)
        ),
    });
  });
  ses.setPermissionRequestHandler((_contents, permission, callback, details) => {
    callback(allowsMagpiePermission(permission, details?.requestingUrl, origin));
  });
  ses.setPermissionCheckHandler((_contents, permission, requestingOrigin) =>
    allowsMagpiePermission(permission, requestingOrigin, origin),
  );
  ses.setDevicePermissionHandler(() => false);
  ses.setSpellCheckerLanguages([]);
  ses.setSpellCheckerEnabled(false);
  ses.on("will-download", (event, item, source) => {
    const chain = item.getURLChain();
    if (
      source !== contents ||
      chain.length === 0 ||
      !chain.every((address) => allowsMagpieDownload(address, origin))
    ) {
      event.preventDefault();
      return;
    }
    item.setSaveDialogOptions({
      title: "Save Magpie download",
      defaultPath: basename(item.getFilename().replaceAll("\\", "/")),
    });
  });
  ses.webRequest.onBeforeRequest((details, callback) => {
    callback({
      cancel: !allowsMagpieRequest(details.url, origin, appOrigin, details.resourceType),
    });
  });
  ses.webRequest.onBeforeSendHeaders((details, callback) => {
    const headers = { ...details.requestHeaders };
    for (const name of Object.keys(headers)) {
      if (name.toLowerCase() === "referer") delete headers[name];
    }
    callback({ requestHeaders: headers });
  });

  const preferences = {
    session: ses,
    contextIsolation: true,
    sandbox: true,
    nodeIntegration: false,
    nodeIntegrationInWorker: false,
    nodeIntegrationInSubFrames: false,
    webSecurity: true,
    allowRunningInsecureContent: false,
    webviewTag: false,
    spellcheck: false,
    devTools: false,
  };
  const dashboard = new WebContentsView({ webPreferences: preferences });
  // The header has no credentials, scripts, preload or network access.
  const header = new WebContentsView({
    webPreferences: { ...preferences, session: headerSession, javascript: false },
  });
  const contents = dashboard.webContents;
  let closed = false;
  let documentReady = false;
  let presentationReady = false;
  let failed = false;
  let lastAppearance = "";
  let styleKey = null;
  let generation = 0;

  async function styleDashboard() {
    const current = ++generation;
    const next = appearance();
    try {
      const key = await contents.insertCSS(magpieStyle(next), { cssOrigin: "user" });
      if (closed || current !== generation) {
        if (!contents.isDestroyed()) void contents.removeInsertedCSS(key).catch(() => {});
        return;
      }
      const previous = styleKey;
      styleKey = key;
      if (previous !== null) void contents.removeInsertedCSS(previous).catch(() => {});
      // Magpie's own plugin bridge observes root attributes and forwards its computed palette.
      await contents.executeJavaScript(`(() => {
        const appearance = ${JSON.stringify(next)};
        const root = document.documentElement;
        root.dataset.theme = appearance.theme;
        root.lang = "en";
        root.style.setProperty("--viki-appearance", ${JSON.stringify(JSON.stringify(next))});
        if (typeof params !== "undefined") {
          const relang = params.get("locale") !== appearance.locale;
          params.set("theme", appearance.theme);
          params.set("locale", appearance.locale);
          if (relang && typeof load === "function") void load();
        }
      })()`);
      if (closed || current !== generation) return;
      const firstPresentation = !presentationReady;
      presentationReady = true;
      layout();
      if (firstPresentation && (currentView?.visible ?? true)) contents.focus();
    } catch {
      if (!closed && current === generation) {
        presentationReady = true;
        failed = true;
        void header.webContents.loadURL(magpieHeader(appearance(), true)).catch(() => {});
        layout();
      }
    }
  }

  function refreshAppearance(force = false) {
    const next = appearance();
    const zoom = owner.getZoomFactor() * next.scale;
    contents.setZoomFactor(zoom);
    header.webContents.setZoomFactor(zoom);
    const stamp = JSON.stringify(next);
    if (!force && stamp === lastAppearance) return;
    lastAppearance = stamp;
    void header.webContents.loadURL(magpieHeader(next, failed)).catch(() => {});
    if (documentReady) void styleDashboard();
  }

  function layout() {
    if (closed || win.isDestroyed()) return;
    const bounds = magpieBounds(currentView, win.getContentSize(), owner.getZoomFactor());
    header.setBounds(bounds.header);
    dashboard.setBounds(bounds.dashboard);
    const visible = currentView?.visible ?? true;
    header.setVisible(visible && bounds.header.width > 0 && bounds.header.height > 0);
    dashboard.setVisible(
      visible && presentationReady && bounds.dashboard.width > 0 && bounds.dashboard.height > 0,
    );
    refreshAppearance();
  }

  function update(next) {
    const parsed = parseMagpieView(next);
    if (closed || parsed === null || parsed.id !== viewId) return false;
    const wasVisible = currentView?.visible ?? true;
    currentView = parsed;
    layout();
    if (!wasVisible && parsed.visible) contents.focus();
    else if (!parsed.visible && (contents.isFocused() || header.webContents.isFocused()))
      owner.focus();
    return true;
  }

  function close() {
    if (closed) return;
    closed = true;
    generation += 1;
    win.removeListener("resize", layout);
    win.removeListener("closed", close);
    owner.removeListener("zoom-changed", zoomChanged);
    owner.removeListener("did-start-navigation", mainNavigation);
    for (const view of [dashboard, header]) {
      if (!win.isDestroyed()) win.contentView.removeChildView(view);
      if (!view.webContents.isDestroyed()) view.webContents.close({ waitForBeforeUnload: false });
    }
    for (const browserSession of [ses, headerSession]) {
      void browserSession.clearStorageData().catch(() => {});
      void browserSession.closeAllConnections().catch(() => {});
    }
    if (!win.isDestroyed() && !owner.isDestroyed()) owner.focus();
    onClose();
  }

  const zoomChanged = () => queueMicrotask(layout);
  const mainNavigation = (_event, _url, inPlace, mainFrame) => {
    if (mainFrame && !inPlace) close();
  };

  for (const page of [contents, header.webContents]) {
    page.setWindowOpenHandler(({ url: target }) => {
      if (page === contents) {
        if (isAppUrl(target, origin)) void contents.loadURL(target).catch(() => {});
        else if (allowsMagpieOpenUrl(target, appOrigin)) onOpenExternal(target);
      }
      return { action: "deny" };
    });
    page.on("will-attach-webview", (event) => event.preventDefault());
    page.on("before-input-event", (event, input) => {
      if (input.type === "keyDown" && input.key === "Escape") {
        event.preventDefault();
        close();
      }
    });
  }
  contents.on("will-frame-navigate", (event) => {
    if (isAppUrl(event.url, origin)) return;
    event.preventDefault();
    if (event.isMainFrame && allowsMagpieOpenUrl(event.url, appOrigin)) onOpenExternal(event.url);
  });
  contents.on("will-redirect", (event) => {
    if (!isAppUrl(event.url, origin)) event.preventDefault();
  });
  contents.on("did-start-navigation", (_event, _target, inPlace, mainFrame) => {
    if (mainFrame && !inPlace) {
      documentReady = false;
      presentationReady = false;
      styleKey = null;
      generation += 1;
      layout();
    }
  });
  contents.on("dom-ready", () => {
    documentReady = true;
    failed = false;
    refreshAppearance(true);
  });
  const headerNavigation = (event) => {
    event.preventDefault();
    if (event.url === BACK) close();
  };
  header.webContents.on("will-frame-navigate", headerNavigation);
  header.webContents.on("will-navigate", headerNavigation);
  contents.on("did-fail-load", (_event, code, _description, _url, isMainFrame) => {
    if (!closed && isMainFrame && code !== -3) {
      failed = true;
      void header.webContents.loadURL(magpieHeader(appearance(), true)).catch(() => {});
    }
  });
  contents.on("render-process-gone", () => {
    if (!closed) void header.webContents.loadURL(magpieHeader(appearance(), true)).catch(() => {});
  });

  win.contentView.addChildView(header);
  win.contentView.addChildView(dashboard);
  win.on("resize", layout);
  win.once("closed", close);
  owner.on("zoom-changed", zoomChanged);
  owner.on("did-start-navigation", mainNavigation);
  layout();
  const target = new URL(url);
  target.searchParams.set("theme", appearance().theme);
  target.searchParams.set("locale", appearance().locale);
  target.searchParams.set("view", "providers");
  void contents.loadURL(target.href).catch(() => {});
  if (currentView?.visible ?? true) contents.focus();
  return { close, update, viewId, contents, header: header.webContents };
}
