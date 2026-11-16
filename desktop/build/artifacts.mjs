/**
 * @module artifacts
 * Helpers for `npm run dist`'s release artifacts (ADR-055 supply chain): trimming a full
 * `npm sbom` CycloneDX document down to the packages a lockfile actually ships in production
 * (`productionSbom`, using `./licences.mjs`'s `prodPackages`: `npm sbom`'s own
 * `--omit dev` and `scope` fields disagree with the lockfile for a package reachable from both a
 * production and a dev path, such as desktop's semver@7.7.4); the argument lists for
 * `npm sbom` and for `uv export`'s CycloneDX output; running a child process and capturing its
 * output without a shell (`capture`); and the installer checksum file's pieces
 * (`installerNames`, `sha256File`, `checksumText`, `osLabel`). `cleanStaleOutputs` deletes this
 * script's own earlier top-level outputs from `dist/desktop` before electron-builder runs, and
 * `freshInstallerNames` then limits the checksum file to only the installers this run's
 * electron-builder actually wrote or rewrote. Used by `dist.mjs`, both just before
 * electron-builder runs and, on success, after.
 */
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { createReadStream, existsSync, readdirSync, rmSync, statSync } from "node:fs";
import { join } from "node:path";

import { prodPackages } from "./licences.mjs";

// -- productionSbom -----------------------------------------------------------------------------

/** The npm SBOM property name marking a component reachable from a dev path (present even on
 * a component also reachable from a production path, such as desktop's semver@7.7.4). */
const DEV_PROPERTY = "cdx:npm:package:development";

/**
 * A `npm sbom --sbom-format cyclonedx --package-lock-only` document trimmed to the packages
 * `lock` actually ships in production (`prodPackages`). Every kept component's `scope` becomes
 * `"required"` and its `cdx:npm:package:development` property, if present, is dropped; other
 * properties are kept. `dependencies` is pruned to the refs this keeps, plus
 * `metadata.component`'s own `bom-ref` (the application component, never itself a member of
 * `components`): entries for a dropped ref are removed outright, and every kept entry's
 * `dependsOn` is filtered to the same ref set.
 * @param {{metadata?: {component?: {"bom-ref"?: string}}, components?: {name: string,
 *   version: string, "bom-ref": string, scope?: string, properties?: {name: string, value:
 *   string}[]}[], dependencies?: {ref: string, dependsOn?: string[]}[]}} sbom a parsed
 *   CycloneDX document
 * @param {{packages: object}} lock a parsed `package-lock.json`
 * @returns {object} `sbom` with only `components` and `dependencies` replaced; every other field
 *   (`bomFormat`, `specVersion`, `serialNumber`, `metadata`, …) is kept as given
 */
export function productionSbom(sbom, lock) {
  const prodKeys = new Set(prodPackages(lock).map((pkg) => `${pkg.name}@${pkg.version}`));

  const components = (sbom.components ?? [])
    .filter((component) => prodKeys.has(`${component.name}@${component.version}`))
    .map((component) => {
      const next = { ...component, scope: "required" };
      if (component.properties) {
        next.properties = component.properties.filter((prop) => prop.name !== DEV_PROPERTY);
      }
      return next;
    });

  const rootRef = sbom.metadata?.component?.["bom-ref"];
  const keptRefs = new Set(components.map((component) => component["bom-ref"]));
  if (rootRef !== undefined) keptRefs.add(rootRef);

  const dependencies = (sbom.dependencies ?? [])
    .filter((dep) => keptRefs.has(dep.ref))
    .map((dep) => ({
      ...dep,
      dependsOn: (dep.dependsOn ?? []).filter((ref) => keptRefs.has(ref)),
    }));

  return { ...sbom, components, dependencies };
}

// -- NPM_SBOM_ARGS / uvExportArgs ----------------------------------------------------------------

/** `npm sbom`'s arguments for a full, offline CycloneDX document: lock-only (no network, no
 * `node_modules` walk) and unfiltered by `--omit dev` or npm's own `scope` field, both of which
 * disagree with the lockfile — `productionSbom` does the filtering instead. */
export const NPM_SBOM_ARGS = ["sbom", "--sbom-format", "cyclonedx", "--package-lock-only"];

/**
 * `uv export`'s arguments for the server's production CycloneDX SBOM (uv 0.11.11's
 * CycloneDX export is an experimental feature that otherwise warns on stderr;
 * `--preview-features sbom-export` acknowledges it instead; `--frozen` and `--no-dev` so the
 * lockfile alone decides the output, with no network or resolution).
 * @param {string} outFile where `-o` writes the document
 * @returns {string[]}
 */
export function uvExportArgs(outFile) {
  return [
    "export",
    "--format",
    "cyclonedx1.5",
    "--preview-features",
    "sbom-export",
    "--no-dev",
    "--frozen",
    "--quiet",
    "-o",
    outFile,
  ];
}

// -- capture --------------------------------------------------------------------------------

/**
 * Runs `command` and collects its output, without a shell.
 * @param {string} command
 * @param {string[]} args
 * @param {{cwd?: string, env?: Record<string, string>}} [options]
 * @returns {Promise<{code: number|null, stdout: string, stderr: string}>} resolves once the
 *   child closes; `code` is its exit code, or `null` when it was killed by a signal instead
 * @throws rejects with the child's own `"error"` event (for example an unresolvable command)
 */
export function capture(command, args, { cwd, env } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd, env, shell: false });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.once("error", reject);
    child.once("close", (code) => resolve({ code, stdout, stderr }));
  });
}

// -- INSTALLER_SUFFIXES / installerNames ---------------------------------------------------------

/** Top-level installer file suffixes a build writes; a `--dir` build writes none of these,
 * so `installerNames` then returns an empty array and no `SHA256SUMS-<os>.txt` is written. */
export const INSTALLER_SUFFIXES = [".exe", ".dmg", ".zip", ".AppImage", ".deb"];

/**
 * @param {string[]} names file names (not paths) from a build's top-level output folder
 * @returns {string[]} the ones ending in one of `INSTALLER_SUFFIXES`, in their given order
 */
export function installerNames(names) {
  return names.filter((name) => INSTALLER_SUFFIXES.some((suffix) => name.endsWith(suffix)));
}

// -- sha256File -----------------------------------------------------------------------------

/**
 * @param {string} path
 * @returns {Promise<string>} the file's SHA-256, lowercase hex, computed by streaming it rather
 *   than reading it whole (an installer can be hundreds of megabytes)
 */
export function sha256File(path) {
  return new Promise((resolve, reject) => {
    const hash = createHash("sha256");
    const stream = createReadStream(path);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("end", () => resolve(hash.digest("hex")));
    stream.on("error", reject);
  });
}

// -- checksumText ---------------------------------------------------------------------------

/**
 * The `SHA256SUMS-<os>.txt` body: coreutils' `sha256sum` text-mode format, so
 * `sha256sum -c`, `shasum -a 256 -c` and `Get-FileHash` all read it back unmodified.
 * @param {{name: string, hex: string}[]} entries
 * @returns {string} `"<hex>  <name>\n"` per entry, sorted by `name`, two spaces between the hex
 *   digest and the name
 */
export function checksumText(entries) {
  return entries
    .slice()
    .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
    .map((entry) => `${entry.hex}  ${entry.name}\n`)
    .join("");
}

// -- osLabel --------------------------------------------------------------------------------

/** `process.platform` to the label `SHA256SUMS-<os>.txt` and `THIRD-PARTY-NOTICES-<os>.txt`
 * use. */
const OS_LABELS = { win32: "windows", darwin: "macos", linux: "linux" };

/**
 * @param {string} platform a `process.platform` value
 * @returns {string} `"windows"`, `"macos"` or `"linux"`
 * @throws {Error} naming `platform`, for anything else
 */
export function osLabel(platform) {
  const label = OS_LABELS[platform];
  if (label === undefined) {
    throw new Error(`osLabel: unknown platform "${platform}"`);
  }
  return label;
}

// -- cleanStaleOutputs ------------------------------------------------------------------------

/** This script's own top-level output file names in `dist/desktop/` a previous run may have left
 * behind: the release SBOMs, the checksum file and the notices copy. Never an installer, and
 * never a subfolder. */
const STALE_OUTPUT_RES = [
  /^sbom-.*\.cdx\.json$/,
  /^SHA256SUMS-.*\.txt$/,
  /^THIRD-PARTY-NOTICES-.*\.txt$/,
];

/**
 * Deletes `dist.mjs`'s own earlier top-level outputs from `dir` before electron-builder runs,
 * so a release step that fails partway through this run never leaves something
 * complete-looking from an older build beside the new installers. Deletes only regular files
 * matching `STALE_OUTPUT_RES`; a missing `dir` (the very first build) is a no-op.
 * @param {string} dir absolute path to `dist/desktop`
 * @returns {void}
 */
export function cleanStaleOutputs(dir) {
  if (!existsSync(dir)) return;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.isFile() && STALE_OUTPUT_RES.some((re) => re.test(entry.name))) {
      rmSync(join(dir, entry.name));
    }
  }
}

// -- freshInstallerNames ----------------------------------------------------------------------

/** Filesystem timestamp slack `freshInstallerNames` allows: some filesystems keep only
 * coarse (for example 2-second) mtime resolution, so an installer electron-builder wrote at or
 * after `sinceMs` can still stat a little earlier than that. */
export const FS_TIMESTAMP_SLACK_MS = 2000;

/**
 * The installer file names in `dir` (`installerNames`) last modified at or after `sinceMs`,
 * within `FS_TIMESTAMP_SLACK_MS`'s slack — the ones electron-builder actually wrote or
 * rewrote this run. An older installer left beside them from an earlier build is excluded, so
 * `SHA256SUMS-<os>.txt` never vouches for it; with none fresh (for example a `--dir` build run
 * after a full one), the result is empty.
 * @param {string} dir absolute path to `dist/desktop`
 * @param {number} sinceMs the time noted just before electron-builder started
 * @returns {string[]} in `installerNames`'s own order
 */
export function freshInstallerNames(dir, sinceMs) {
  const cutoff = sinceMs - FS_TIMESTAMP_SLACK_MS;
  return installerNames(readdirSync(dir)).filter(
    (name) => statSync(join(dir, name)).mtimeMs >= cutoff,
  );
}
