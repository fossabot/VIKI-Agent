/**
 * The one place that reads which OS the browser is running on. Every other
 * module — the key handler, the shortcut labels, the onboarding forms — asks this one instead of
 * touching `navigator` itself.
 */
import type { Key } from "./i18n/index";

export interface NavigatorLike {
  platform?: string;
  userAgentData?: { platform?: string };
}

export type PlatformKind = "windows" | "mac" | "linux";

/** `userAgentData.platform` first (the modern, spec-preferred source), else `platform`, falling
 * through an empty string from either. Anything that does not match a known OS — including
 * jsdom's own empty string — reads as `"windows"`, the platform whose behaviour is unchanged; the
 * server still refuses what the UI offers. */
export function platformKind(nav: NavigatorLike = navigator): PlatformKind {
  const raw = nav.userAgentData?.platform || nav.platform || "";
  if (/mac/i.test(raw)) return "mac";
  if (/linux|x11|cros/i.test(raw)) return "linux";
  return "windows";
}

export function isMac(nav?: NavigatorLike): boolean {
  return platformKind(nav) === "mac";
}

/** The per-OS example path shown beside the vault path field (Create and Open forms share it). */
export const EXAMPLE_PATH_KEY: Readonly<Record<PlatformKind, Key>> = {
  windows: "field.example_path",
  mac: "field.example_path_mac",
  linux: "field.example_path_linux",
};
