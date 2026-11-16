/**
 * Gates VIKI Agent's update check behind a configured feed. `startUpdates` is the only entry
 * point main.mjs calls: it decides whether a check should run at all, and the real update
 * library loads only once that decision says yes. No build ships a feed file until ADR-055's GO
 * switch, so in practice this never loads it.
 */
import { existsSync } from "node:fs";

/**
 * Platforms the update check covers. Linux has no in-app updates: users install the new
 * package.
 */
export const UPDATE_PLATFORMS = Object.freeze(["win32", "darwin"]);

/**
 * @param {{
 *   packaged: boolean,
 *   platform: string,
 *   feedFile: string,
 *   exists?: (path: string) => boolean,
 *   load?: () => Promise<{ autoUpdater: { checkForUpdatesAndNotify(): Promise<unknown> } }>,
 * }} args
 * @returns {Promise<boolean>} true when a packaged win32 or darwin build has a feed file, so a
 *   check was attempted (a failed load or check only warns); false otherwise, with nothing loaded
 */
export async function startUpdates({
  packaged,
  platform,
  feedFile,
  exists = existsSync,
  load = loadUpdater,
}) {
  if (!packaged || !UPDATE_PLATFORMS.includes(platform) || !exists(feedFile)) {
    return false;
  }
  try {
    const updater = await load();
    // Fire-and-forget: a network check must never hold up startup, so this attaches its own
    // catch instead of being awaited before startUpdates returns.
    updater.autoUpdater.checkForUpdatesAndNotify().catch(() => {
      console.warn("update check failed");
    });
  } catch {
    console.warn("update check failed");
  }
  return true;
}

/**
 * Loads the real update library. Its own function, called only from inside
 * {@link startUpdates} and never at this module's top level, so importing this file never
 * loads it.
 * @returns {Promise<{ autoUpdater: { checkForUpdatesAndNotify(): Promise<unknown> } }>}
 */
export async function loadUpdater() {
  return (await import("electron-updater")).default;
}
