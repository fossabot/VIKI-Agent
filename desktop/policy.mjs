/**
 * @module policy
 * Pure security policy for the VIKI Agent desktop window: origin checks, permission decisions,
 * spellcheck gating, hardened window options, and menu templates. Holds no Electron import so it
 * can be unit-tested in plain Node; `window.mjs` wires it to `session` and `BrowserWindow`.
 */

/** The only origin the desktop app's windows may load or interact with. */
export const APP_ORIGIN = "http://127.0.0.1:8765";

/** The Electron app id: used by the installers and by `main.mjs`'s Windows AppUserModelID. */
export const APP_ID = "app.viki-agent.desktop";

/**
 * @typedef {{ theme: "dark" | "light", density: "comfortable" | "compact", scale: number,
 * reduce_motion: boolean, locale: "en", palette: {
 * bg: string, panel: string, raised: string, text: string, secondary: string, muted: string,
 * faint: string, line: string, hair: string, accent: string, onAccent: string, popover: string
 * } }} MagpieAppearance
 * @typedef {{ id: string, bounds: { x: number, y: number, width: number, height: number },
 * appearance: MagpieAppearance, visible: boolean }} MagpieView
 */

/** @param {unknown} value @param {string[]} keys */
function exactKeys(value, keys) {
  return (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Object.keys(value).length === keys.length &&
    keys.every((key) => Object.hasOwn(value, key))
  );
}

/**
 * Only bounded geometry, enumerated preferences and literal colors cross the host pipe.
 * @param {unknown} value
 * @returns {MagpieView | null}
 */
export function parseMagpieView(value) {
  if (!exactKeys(value, ["id", "bounds", "appearance", "visible"])) return null;
  const { id, bounds, appearance, visible } = value;
  const palette = appearance?.palette;
  if (
    typeof id !== "string" ||
    !/^[0-9a-f]{32}$/.test(id) ||
    typeof visible !== "boolean" ||
    !exactKeys(bounds, ["x", "y", "width", "height"]) ||
    !Object.values(bounds).every(
      (n) => typeof n === "number" && Number.isFinite(n) && n >= 0 && n <= 32768,
    ) ||
    !exactKeys(appearance, ["theme", "density", "scale", "reduce_motion", "locale", "palette"]) ||
    !["dark", "light"].includes(appearance.theme) ||
    !["comfortable", "compact"].includes(appearance.density) ||
    typeof appearance.scale !== "number" ||
    !Number.isFinite(appearance.scale) ||
    appearance.scale < 0.75 ||
    appearance.scale > 2 ||
    typeof appearance.reduce_motion !== "boolean" ||
    appearance.locale !== "en" ||
    !exactKeys(palette, [
      "bg",
      "panel",
      "raised",
      "text",
      "secondary",
      "muted",
      "faint",
      "line",
      "hair",
      "accent",
      "onAccent",
      "popover",
    ]) ||
    !Object.values(palette).every(
      (color) => typeof color === "string" && /^#[0-9a-f]{6}$/i.test(color),
    )
  )
    return null;
  return value;
}

/**
 * The supervised Magpie dashboard URL, including its launch credential. Accept only
 * the literal loopback form the server emits; this URL must never be printed.
 * @param {unknown} value
 * @returns {string | null} the dashboard origin, or null for an invalid launch URL
 */
export function magpieOrigin(value) {
  if (typeof value !== "string") return null;
  const match = /^http:\/\/127\.0\.0\.1:([1-9][0-9]{0,4})\/\?k=[0-9a-f]{64}$/.exec(value);
  if (!match || match[0] !== value || Number(match[1]) > 65535 || Number(match[1]) === 8765)
    return null;
  return new URL(value).origin;
}

/**
 * Magpie loads its own HTTP API and HTTPS dashboard assets. It cannot request the VIKI
 * server, another local HTTP service, a file URL or a device protocol.
 * @param {string} value
 * @param {string} origin
 * @param {string} [appOrigin]
 * @param {string} [resourceType]
 * @returns {boolean}
 */
export function allowsMagpieRequest(value, origin, appOrigin = APP_ORIGIN, resourceType) {
  try {
    const url = new URL(value);
    if (url.username || url.password || url.origin === appOrigin) return false;
    if (url.protocol === "http:") return url.origin === origin;
    if (url.protocol === "blob:") return url.origin === origin;
    if (url.protocol === "data:") {
      return (
        (resourceType === "image" && /^data:image\//i.test(value)) ||
        (resourceType === "font" &&
          value.length <= 192 * 1024 &&
          /^data:font\/woff2;base64,[a-z0-9+/]+=*$/i.test(value))
      );
    }
    if (url.protocol === "ws:" || url.protocol === "wss:") {
      return new URL(value.replace(/^ws/, "http")).origin === origin;
    }
    if (url.protocol !== "https:") return false;
    const host = url.hostname.toLowerCase();
    return !(
      host === "localhost" ||
      host.endsWith(".localhost") ||
      host.endsWith(".local") ||
      host.includes(":") ||
      /^[0-9.]+$/.test(host)
    );
  } catch {
    return false;
  }
}

/**
 * Configuration exports may download only from this dashboard, including its generated blobs.
 * @param {string} value
 * @param {string} origin
 * @returns {boolean}
 */
export function allowsMagpieDownload(value, origin) {
  try {
    const url = new URL(value);
    return (
      (url.protocol === "http:" || url.protocol === "blob:") &&
      url.origin === origin &&
      url.username === "" &&
      url.password === ""
    );
  } catch {
    return false;
  }
}

/**
 * Links clicked in Magpie open through the server's external opener, without credentials.
 * @param {unknown} value
 * @param {string} [appOrigin]
 * @returns {boolean}
 */
export function allowsMagpieOpenUrl(value, appOrigin = APP_ORIGIN) {
  if (
    typeof value !== "string" ||
    value.length > 2048 ||
    !/^https?:\/\//i.test(value) ||
    value.includes("\\") ||
    /\s/u.test(value) ||
    [...value].some(
      (char) => char.charCodeAt(0) < 32 || (char.charCodeAt(0) >= 127 && char.charCodeAt(0) <= 159),
    )
  )
    return false;
  try {
    const url = new URL(value);
    return (
      (url.protocol === "http:" || url.protocol === "https:") &&
      url.username === "" &&
      url.password === "" &&
      url.origin !== appOrigin &&
      !(url.port === "8765" && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname))
    );
  } catch {
    return false;
  }
}

/**
 * Magpie's isolated view gets only clipboard writes, on its own origin.
 * @param {string} permission
 * @param {string | undefined} url
 * @param {string} origin
 * @returns {boolean}
 */
export function allowsMagpiePermission(permission, url, origin) {
  return permission === "clipboard-sanitized-write" && isAppUrl(url, origin);
}

/**
 * True iff `url` parses, is `http:`, and names exactly `origin`, with no embedded username or
 * password. The protocol check matters on its own: a `blob:` URL's origin (per the URL Standard)
 * is the origin of the context that created it, so `blob:http://127.0.0.1:8765/<uuid>` reports
 * this origin while never being an actual page of it. Compares only the origin otherwise (host,
 * port): path, query and hash never matter, since the app's own pages all share one origin.
 * @param {string | undefined | null} url
 * @param {string} origin
 * @returns {boolean}
 */
export function isAppUrl(url, origin) {
  try {
    const parsed = new URL(url);
    return (
      parsed.protocol === "http:" &&
      parsed.origin === origin &&
      parsed.username === "" &&
      parsed.password === ""
    );
  } catch {
    return false;
  }
}

/**
 * True iff Chromium's sandbox was turned off with `--no-sandbox` (hard rule 7). `hasSwitch` is
 * `app.commandLine.hasSwitch`, passed in so this stays plain-Node testable.
 * @param {(name: string) => boolean} hasSwitch
 * @returns {boolean}
 */
export function sandboxOff(hasSwitch) {
  return hasSwitch("no-sandbox") === true;
}

/**
 * Decides a `session.setPermissionRequestHandler` request.
 * @param {string} permission
 * @param {{ requestingUrl?: string, mediaTypes?: string[] } | undefined} details
 * @param {string} origin
 * @returns {boolean}
 */
export function allowsPermissionRequest(permission, details, origin) {
  if (!isAppUrl(details?.requestingUrl, origin)) return false;
  if (permission === "notifications" || permission === "clipboard-sanitized-write") return true;
  if (permission === "media") {
    const types = details?.mediaTypes;
    return Array.isArray(types) && types.length > 0 && types.every((type) => type === "audio");
  }
  return false;
}

/**
 * Decides a `session.setPermissionCheckHandler` check.
 * @param {string} permission
 * @param {string | undefined} requestingOrigin
 * @param {{ mediaType?: string } | undefined} details
 * @param {string} origin
 * @returns {boolean}
 */
export function allowsPermissionCheck(permission, requestingOrigin, details, origin) {
  if (!isAppUrl(requestingOrigin, origin)) return false;
  if (permission === "notifications" || permission === "clipboard-sanitized-write") return true;
  if (permission === "media") return details?.mediaType === "audio";
  return false;
}

/**
 * @param {string} platform - a `process.platform` value
 * @returns {boolean} true only on macOS, where the OS spellchecker needs no download
 */
export function spellcheckOn(platform) {
  return platform === "darwin";
}

/**
 * @param {{ icon?: string, devTools: boolean, spellcheck: boolean }} options
 * @returns {object} `BrowserWindow` constructor options, hardened as hard rule 7 describes:
 *   context isolation, the renderer sandbox and no Node.js in the page
 */
export function windowOptions({ icon, devTools, spellcheck }) {
  return {
    width: 1440,
    height: 900,
    title: "",
    autoHideMenuBar: true,
    ...(icon ? { icon } : {}),
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
      spellcheck,
      devTools,
    },
  };
}

/**
 * @param {{ platform: string, packaged: boolean }} options
 * @returns {object[]} an Electron `Menu.buildFromTemplate` template
 */
export function menuTemplate({ platform, packaged }) {
  return [
    ...(platform === "darwin" ? [{ role: "appMenu" }] : []),
    { role: "editMenu" },
    {
      label: "View",
      submenu: [
        { role: "reload" },
        { role: "resetZoom" },
        { role: "zoomIn" },
        { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" },
        ...(packaged ? [] : [{ role: "toggleDevTools" }]),
      ],
    },
    { role: "windowMenu" },
  ];
}

/**
 * Builds a context-menu template for a `WebContents#context-menu` event's `params`.
 * @param {{ dictionarySuggestions?: string[], isEditable?: boolean, editFlags?: object, selectionText?: string }} params
 * @param {(suggestion: string) => void} replaceMisspelling
 * @returns {object[]} empty when nothing applies
 */
export function contextMenuTemplate(params, replaceMisspelling) {
  const items = [];
  const suggestions = params.dictionarySuggestions ?? [];
  for (const suggestion of suggestions) {
    items.push({
      label: suggestion,
      click: () => replaceMisspelling(suggestion),
    });
  }
  if (suggestions.length > 0) {
    items.push({ type: "separator" });
  }
  if (params.isEditable) {
    const flags = params.editFlags ?? {};
    items.push(
      { role: "cut", enabled: flags.canCut },
      { role: "copy", enabled: flags.canCopy },
      { role: "paste", enabled: flags.canPaste },
      { role: "selectAll", enabled: flags.canSelectAll },
    );
  } else if (params.selectionText && params.selectionText.length > 0) {
    items.push({ role: "copy" });
  }
  return items;
}
