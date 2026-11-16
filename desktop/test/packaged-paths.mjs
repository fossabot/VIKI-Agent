/**
 * @module packaged-paths
 * Where electron-builder's unpacked output puts things, computed from the packaged app's own
 * executable path. Pure path arithmetic with no filesystem access and no dependency beyond
 * node:path, so this loads with no node_modules installed — `packaged.test.mjs` imports only
 * this and node: built-ins at its top level, for the same reason.
 */
import { dirname, join } from "node:path";

/**
 * @param {string} exe the packaged app's main executable, as `VIKI_PACKAGED_APP` names it: on
 *   darwin, `<X>.app/Contents/MacOS/<name>`; elsewhere, the exe electron-builder's `--dir`
 *   output puts beside a `resources/` folder (`win-unpacked`, `linux-unpacked`)
 * @param {string} platform a `process.platform` value
 * @returns {{
 *   resources: string,
 *   backend: string,
 *   asar: string,
 *   fuseTarget: string,
 *   plist: string | null,
 * }}
 */
export function packagedPaths(exe, platform) {
  if (platform === "darwin") {
    const contents = dirname(dirname(exe)); // <X>.app/Contents, since exe is Contents/MacOS/<name>
    const resources = join(contents, "Resources");
    return {
      resources,
      backend: join(resources, "viki-agent", "viki-agent"),
      asar: join(resources, "app.asar"),
      fuseTarget: dirname(contents), // the .app folder itself
      plist: join(contents, "Info.plist"),
    };
  }
  const resources = join(dirname(exe), "resources");
  return {
    resources,
    backend: join(resources, "viki-agent", platform === "win32" ? "viki-agent.exe" : "viki-agent"),
    asar: join(resources, "app.asar"),
    fuseTarget: exe,
    plist: null,
  };
}
