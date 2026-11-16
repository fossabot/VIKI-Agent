/**
 * @module dist
 * Builds VIKI Agent's installers with electron-builder (`npm run dist`, run from `desktop/`).
 * Refuses to run with no compiled backend (`scripts/compile.py`, ADR-054) or with any argument
 * outside a fixed allowlist (`ALLOWED_ARGS`): this script always appends its own `--publish
 * never` and never publishes. Signs only from a complete, named set of environment secrets
 * (`signing`); a partial set is refused rather than silently shipping unsigned. The macOS
 * unsigned-build fallback args are refused outright on a signed build, so they can never
 * override real signing. Secrets live only in this process's environment and its one child
 * process, stripped case-insensitively before the child ever sees them (`childEnv`); none is
 * ever printed, and the macOS API key is staged to a private temp file (`stageKey`) removed
 * before this process exits, including on a failed write or a terminating signal.
 *
 * Before electron-builder runs, this script also refuses when `scripts/compile.py` has not
 * written a licence inventory (`readInventory`, ADR-054) or when the reviewed licence table
 * (`desktop/build/licences.json`) leaves any component unreviewed (`buildNotices`'s `problems`,
 * ADR-055); on success it writes `dist/notices/THIRD-PARTY-NOTICES.txt`, which electron-builder
 * then ships as `<resources>/THIRD-PARTY-NOTICES.txt`. It then deletes its own earlier
 * `dist/desktop` outputs (`cleanStaleOutputs`) and notes the time, so a release step that fails
 * later never leaves something complete-looking from an older build beside the new installers.
 * Only once electron-builder itself exits 0 does it run the release steps
 * (`writeReleaseArtifacts`, given `releaseCommands`'s steps): production CycloneDX SBOMs for the
 * user interface, the window host and the compiled server, a `SHA256SUMS-<os>.txt` over only the
 * installers electron-builder wrote or rewrote this run (`freshInstallerNames`), and a copy
 * of the notices file, all written to `dist/desktop/`. Every one of these child processes gets
 * the same stripped `childEnv` electron-builder's own child gets.
 */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import {
  capture,
  checksumText,
  cleanStaleOutputs,
  freshInstallerNames,
  NPM_SBOM_ARGS,
  osLabel,
  productionSbom,
  sha256File,
  uvExportArgs,
} from "./artifacts.mjs";
import { loadTable, loadTexts } from "./licences.mjs";
import { readMagpieBundle } from "./magpie-build.mjs";
import { buildNotices } from "./notices.mjs";
import { checkCompiledTree } from "./release-files.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const DESKTOP_ROOT = dirname(HERE);
const REPO = dirname(DESKTOP_ROOT);
const PKG = JSON.parse(readFileSync(join(DESKTOP_ROOT, "package.json"), "utf8"));

const MAC_VARS = [
  "VIKI_MAC_CERT_P12",
  "VIKI_MAC_CERT_PASSWORD",
  "VIKI_APPLE_API_KEY_P8",
  "VIKI_APPLE_API_KEY_ID",
  "VIKI_APPLE_API_ISSUER",
];
/** The six `VIKI_*` names `signing` ever reads. */
const VIKI_SIGNING_VAR_NAMES = ["VIKI_WIN_CERT_SHA1", ...MAC_VARS];

/** Case-insensitive env var name prefixes stripped from electron-builder's child env (`childEnv`):
 * ambient signing material that could make it sign or notarize on its own even though this
 * script reported "signing: off" (an ambient `WIN_CSC_LINK`, `CSC_LINK` or `CSC_NAME`/`APPLE_ID`
 * otherwise wins underneath the script's own, deliberate choice). */
const DENY_ENV_PREFIXES = ["csc_", "win_csc_", "apple_"];

/** Exact env var names, matched case-insensitively alongside `DENY_ENV_PREFIXES`: the six
 * `VIKI_*` signing names this script resolves into `signing.env`/`keyP8` instead, and the two
 * GitHub tokens that would otherwise let electron-builder auto-detect a publish target even
 * though `--publish never` is always passed too. */
const DENY_ENV_NAMES = new Set(
  [...VIKI_SIGNING_VAR_NAMES, "GH_TOKEN", "GITHUB_TOKEN"].map((name) => name.toLowerCase()),
);

/** The two `-c.mac.*` overrides that make up the unsigned-build fallback (for a failed ad-hoc
 * re-sign): forcing no identity and no hardened runtime. Allowed as arguments, but
 * refused in `main` when the build is actually signed, so they can never downgrade a signed
 * build to unsigned. */
const MAC_UNSIGNED_FALLBACK_ARGS = ["-c.mac.identity=-", "-c.mac.hardenedRuntime=false"];

/** Every argument `npm run dist` may forward to electron-builder. Anything else — every spelling
 * of `--publish`, a short-option cluster such as `-wp` or `-p`, or any other `-c.*` override — is
 * refused by `refusedArgs` rather than forwarded. */
export const ALLOWED_ARGS = Object.freeze([
  "--win",
  "--mac",
  "--linux",
  "--x64",
  "--arm64",
  "--dir",
  ...MAC_UNSIGNED_FALLBACK_ARGS,
]);

/**
 * @param {string} repo absolute path to the repository root
 * @param {string} platform a `process.platform` value
 * @returns {string} where `scripts/compile.py` (ADR-054) puts the compiled server
 */
export function backendBinary(repo, platform) {
  const name = platform === "win32" ? "viki-agent.exe" : "viki-agent";
  return join(repo, "dist", "compiled", "viki-agent.dist", name);
}

/**
 * Reads the compiled server's licence inventory (ADR-054, ADR-055), the fact
 * base `buildNotices` composes `THIRD-PARTY-NOTICES.txt` from. `scripts/compile.py` writes it
 * only once a compiled binary exists, so its absence means the same thing `backendBinary`'s own
 * check means: build the server first.
 * @param {string} repo absolute path to the repository root
 * @returns {object} the parsed inventory JSON
 * @throws {Error} with message "dist refused: no licence inventory, scripts/compile.py writes
 *   dist/compiled/viki-agent-licences.json" when compile.py has not written one yet
 */
export function readInventory(repo) {
  const file = join(repo, "dist", "compiled", "viki-agent-licences.json");
  if (!existsSync(file)) {
    throw new Error(
      "dist refused: no licence inventory, scripts/compile.py writes dist/compiled/viki-agent-licences.json",
    );
  }
  const text = readFileSync(file, "utf8");
  try {
    return JSON.parse(text);
  } catch {
    throw new Error("dist refused: invalid licence inventory JSON");
  }
}

/**
 * @param {string[]} args
 * @returns {string[]} the args that are not exactly one of `ALLOWED_ARGS`, in their given order;
 *   empty when every arg is allowed
 */
export function refusedArgs(args) {
  return args.filter((arg) => !ALLOWED_ARGS.includes(arg));
}

/**
 * The bundled compiled server is native to this build host. Refuse cross-target flags and pass
 * its architecture explicitly so the Electron window host matches the server.
 * @param {string[]} args already-allowlisted caller arguments
 * @param {string} platform a `process.platform` value
 * @param {string} arch a `process.arch` value
 * @returns {string[]} electron-builder arguments for the native host
 */
export function nativeBuildArgs(args, platform, arch) {
  const supported =
    (platform === "win32" && arch === "x64") ||
    (platform === "darwin" && arch === "arm64") ||
    (platform === "linux" && (arch === "x64" || arch === "arm64"));
  if (!supported) {
    throw new Error(`unsupported native build host: ${platform}/${arch}`);
  }
  const platformFlag = platform === "win32" ? "--win" : platform === "darwin" ? "--mac" : "--linux";
  if (args.some((arg) => ["--win", "--mac", "--linux"].includes(arg) && arg !== platformFlag)) {
    throw new Error("target must match the native platform");
  }
  const archFlag = `--${arch}`;
  if (args.some((arg) => ["--x64", "--arm64"].includes(arg) && arg !== archFlag)) {
    throw new Error("target must match the native architecture");
  }
  return args.includes(archFlag) ? [...args] : [...args, archFlag];
}

/** @param {string | undefined} value */
function isSet(value) {
  // An empty string counts as absent, the same as an unset variable.
  return typeof value === "string" && value.length > 0;
}

/**
 * Decides whether, and how, to sign the build, from this process's own environment only —
 * never a file, never printed. A platform with none of its secrets set builds unsigned; a
 * platform with all of them set signs; a platform with some but not all of them set throws,
 * naming only the missing variables, since shipping that build unsigned would be silently
 * wrong. Linux has no signing concept, so it always builds unsigned regardless of env.
 * @param {NodeJS.ProcessEnv} env
 * @param {string} platform a `process.platform` value
 * @returns {{ args: string[], env: NodeJS.ProcessEnv, keyP8?: string }} extra electron-builder
 *   CLI args, extra environment for its process, and (macOS only) the Apple API key's own
 *   contents, for the caller to stage to a file — this function never touches the filesystem
 */
export function signing(env, platform) {
  if (platform === "win32") {
    const sha1 = env.VIKI_WIN_CERT_SHA1;
    if (!isSet(sha1)) {
      return { args: [], env: {} };
    }
    if (!/^[0-9a-fA-F]{40}$/.test(sha1)) {
      throw new Error("VIKI_WIN_CERT_SHA1 must be exactly 40 hex characters");
    }
    return {
      args: [`-c.win.signtoolOptions.certificateSha1=${sha1}`, "-c.forceCodeSigning=true"],
      env: {},
    };
  }

  if (platform === "darwin") {
    const have = MAC_VARS.filter((name) => isSet(env[name]));
    if (have.length === 0) {
      return { args: [], env: { CSC_IDENTITY_AUTO_DISCOVERY: "false" } };
    }
    const missing = MAC_VARS.filter((name) => !isSet(env[name]));
    if (missing.length > 0) {
      throw new Error(`missing: ${missing.join(", ")}`);
    }
    return {
      args: ["-c.forceCodeSigning=true"],
      env: {
        CSC_LINK: env.VIKI_MAC_CERT_P12,
        CSC_KEY_PASSWORD: env.VIKI_MAC_CERT_PASSWORD,
        APPLE_API_KEY_ID: env.VIKI_APPLE_API_KEY_ID,
        APPLE_API_ISSUER: env.VIKI_APPLE_API_ISSUER,
      },
      keyP8: env.VIKI_APPLE_API_KEY_P8,
    };
  }

  return { args: [], env: {} }; // linux: no signing concept
}

/**
 * Builds electron-builder's child environment: every `baseEnv` entry except the ones that could
 * let it sign or publish on its own, matched case-insensitively against `DENY_ENV_PREFIXES` and
 * `DENY_ENV_NAMES` (Windows and macOS env var names are conventionally upper-case, but nothing
 * stops a lower- or mixed-case ambient one from reaching the child otherwise), with `extraEnv`'s
 * entries layered on top so this script's own resolved signing variables always survive the
 * strip.
 * @param {NodeJS.ProcessEnv} baseEnv
 * @param {NodeJS.ProcessEnv} extraEnv
 * @returns {NodeJS.ProcessEnv}
 */
export function childEnv(baseEnv, extraEnv) {
  const kept = {};
  for (const [name, value] of Object.entries(baseEnv)) {
    const lower = name.toLowerCase();
    const denied =
      DENY_ENV_NAMES.has(lower) || DENY_ENV_PREFIXES.some((prefix) => lower.startsWith(prefix));
    if (!denied) {
      kept[name] = value;
    }
  }
  return { ...kept, ...extraEnv };
}

/**
 * @param {string} cli absolute path to electron-builder's own CLI entry point
 * @param {string[]} signingArgs extra electron-builder args from `signing()` (plus any allowed
 *   macOS unsigned-build fallback args the caller passed)
 * @param {string[]} args the caller's own, already-allowlisted arguments
 * @returns {string[]} argv for the spawned electron-builder process; publish is always forced off
 */
export function childArgs(cli, signingArgs, args) {
  return [cli, "--publish", "never", ...signingArgs, ...args];
}

/**
 * Stages the macOS Apple API key to a private temp file, since electron-builder's
 * `APPLE_API_KEY` takes a file path rather than key material. The folder is created with
 * `mkdtemp` (mode 0700 on POSIX) and the key file is written 0600 inside it; if the write
 * itself fails (ENOSPC, EIO), the folder is removed before the error is rethrown, so no partial
 * key ever survives a failed write. `cleanup()` removes the whole folder and is safe to call
 * more than once.
 * @param {string} keyP8 the Apple API key's own contents (never printed, never logged)
 * @returns {{ file: string, cleanup: () => void }}
 */
export function stageKey(keyP8) {
  const dir = mkdtempSync(join(tmpdir(), "viki-agent-dist-"));
  const cleanup = () => rmSync(dir, { recursive: true, force: true });
  try {
    const file = join(dir, "AuthKey.p8");
    writeFileSync(file, keyP8, { mode: 0o600 });
    return { file, cleanup };
  } catch (error) {
    cleanup();
    throw error;
  }
}

/**
 * The three child-process steps that produce this build's CycloneDX SBOMs (ADR-055), run in
 * order only after electron-builder itself exits 0. A pure function — it neither
 * spawns anything nor touches the filesystem — so a caller runs each step with `capture` and, for
 * the two npm steps, filters the result through `productionSbom` before writing it to `outFile`
 * (the uv step writes its own output directly, via `uvExportArgs`'s `-o`, so its `lockFile` is
 * `null`). Every step's `env` is already `childEnv(env, {})`: stripped of every signing secret
 * and GitHub token this script itself resolves, exactly like electron-builder's own child env
 * (`main`), so neither a `VIKI_*` variable nor `GH_TOKEN`/`GITHUB_TOKEN` ever reaches one of these
 * children either.
 * @param {{repo: string, npmCli: string|undefined, env: NodeJS.ProcessEnv}} args `npmCli` is
 *   `process.env.npm_execpath`, the absolute path to npm's own CLI script that npm sets for any
 *   lifecycle script it runs (`npm run dist`); running `node build/dist.mjs` directly has none.
 * @returns {{name: string, command: string, args: string[], cwd: string, env: NodeJS.ProcessEnv,
 *   lockFile: string|null, outFile: string}[]}
 * @throws {Error} naming the missing `npm_execpath` when `npmCli` is not set
 */
export function releaseCommands({ repo, npmCli, env }) {
  if (!npmCli) {
    throw new Error("no npm_execpath to run npm sbom with (run through npm, e.g. npm run dist)");
  }
  const distDesktop = join(repo, "dist", "desktop");
  const strippedEnv = childEnv(env, {});
  const npmSbomStep = (name, tree) => ({
    name,
    command: process.execPath,
    args: [npmCli, ...NPM_SBOM_ARGS],
    cwd: join(repo, tree),
    env: strippedEnv,
    lockFile: join(repo, tree, "package-lock.json"),
    outFile: join(distDesktop, `${name}.cdx.json`),
  });
  const pythonOutFile = join(distDesktop, "sbom-python.cdx.json");
  return [
    npmSbomStep("sbom-frontend", "frontend"),
    npmSbomStep("sbom-desktop", "desktop"),
    {
      name: "sbom-python",
      command: "uv",
      args: uvExportArgs(pythonOutFile),
      cwd: repo,
      env: strippedEnv,
      lockFile: null,
      outFile: pythonOutFile,
    },
  ];
}

/**
 * The steps `main` runs only once electron-builder itself has exited 0: the
 * three release SBOMs, `SHA256SUMS-<os>.txt` over only the installers *this* run's
 * electron-builder actually wrote or rewrote (an older installer left beside them from an
 * earlier build is never vouched for, and a `--dir` build writes no checksum file at all), and a
 * copy of the notices file, all written to `dist/desktop/`. Takes `steps` already computed
 * (`releaseCommands`) rather than computing them itself, so `main`'s own "no npm_execpath"
 * refusal keeps its exact wording. Extracted out of `main` with an injectable `runCommand`
 * (defaults to `capture`) so a test can prove a failing step still refuses the build, without
 * spawning electron-builder, starting Electron, or binding a port.
 * @param {{steps: {name: string, command: string, args: string[], cwd: string,
 *   env: NodeJS.ProcessEnv, lockFile: string|null, outFile: string}[], repo: string,
 *   platform: string, noticesText: string, buildStartedAtMs: number,
 *   runCommand?: typeof capture, magpieSbom?: object}} args `buildStartedAtMs` is the time `main` noted just before
 *   electron-builder started
 * @returns {Promise<void>}
 * @throws {Error} naming the failing step, in the form `main` prints it:
 *   `release: <name> failed: <message>`
 */
export async function writeReleaseArtifacts({
  steps,
  repo,
  platform,
  noticesText,
  buildStartedAtMs,
  runCommand = capture,
  magpieSbom,
}) {
  const distDesktop = join(repo, "dist", "desktop");

  for (const step of steps) {
    const result = await runCommand(step.command, step.args, { cwd: step.cwd, env: step.env });
    if (result.code !== 0) {
      throw new Error(
        `release: ${step.name} failed: ${result.stderr.trim() || `exited ${result.code}`}`,
      );
    }
    if (step.lockFile) {
      const lock = JSON.parse(readFileSync(step.lockFile, "utf8"));
      const sbom = productionSbom(JSON.parse(result.stdout), lock);
      writeFileSync(step.outFile, `${JSON.stringify(sbom, null, 2)}\n`);
    }
  }
  if (magpieSbom) {
    writeFileSync(
      join(distDesktop, "sbom-magpie.cdx.json"),
      `${JSON.stringify(magpieSbom, null, 2)}\n`,
    );
  }

  const label = osLabel(platform);
  const fresh = freshInstallerNames(distDesktop, buildStartedAtMs);
  if (fresh.length === 0) {
    console.log("checksums: no installer in dist/desktop");
  } else {
    const entries = [];
    for (const name of fresh) {
      entries.push({ name, hex: await sha256File(join(distDesktop, name)) });
    }
    writeFileSync(join(distDesktop, `SHA256SUMS-${label}.txt`), checksumText(entries));
  }
  writeFileSync(join(distDesktop, `THIRD-PARTY-NOTICES-${label}.txt`), noticesText);
}

/** Runs electron-builder; never called when this module is only imported (see the guard below). */
async function main() {
  let args = process.argv.slice(2);

  const refused = refusedArgs(args);
  if (refused.length > 0) {
    console.error(`dist refused: unsupported argument(s): ${refused.join(", ")}`);
    process.exit(2);
    return;
  }

  try {
    args = nativeBuildArgs(args, process.platform, process.arch);
  } catch (error) {
    console.error(`dist refused: ${error.message}`);
    process.exit(2);
    return;
  }

  const backend = backendBinary(REPO, process.platform);
  if (!existsSync(backend)) {
    console.error(`dist refused: build the compiled server first (scripts/compile.py): ${backend}`);
    process.exit(1);
    return;
  }

  try {
    checkCompiledTree(dirname(backend));
  } catch (error) {
    console.error(`dist refused: ${error.message}`);
    process.exit(1);
    return;
  }

  let inventory;
  try {
    inventory = readInventory(REPO);
  } catch (error) {
    console.error(error.message);
    process.exit(1);
    return;
  }

  let signed;
  try {
    signed = signing(process.env, process.platform);
  } catch (error) {
    console.error(`dist refused: ${error.message}`);
    process.exit(1);
    return;
  }
  const isSigned = signed.args.length > 0;
  console.log(isSigned ? "signing: on" : "signing: off (no signing secrets)");

  if (isSigned && args.some((arg) => MAC_UNSIGNED_FALLBACK_ARGS.includes(arg))) {
    console.error("dist refused: the -c.mac fallbacks are for unsigned builds only");
    process.exit(2);
    return;
  }

  let noticesText;
  let magpie;
  try {
    const table = loadTable(join(DESKTOP_ROOT, "build", "licences.json"));
    const texts = loadTexts(table, join(DESKTOP_ROOT, "build"));
    const notices = buildNotices({
      repo: REPO,
      inventory,
      table,
      texts,
      platform: process.platform,
      version: PKG.version,
      homepage: PKG.homepage,
    });
    if (notices.problems.length > 0) {
      for (const problem of notices.problems) {
        console.error(`notices: ${problem}`);
      }
      process.exit(1);
      return;
    }
    magpie = readMagpieBundle(join(dirname(backend), "magpie"), process.platform, process.arch);
    noticesText = notices.text + magpie.notices;
  } catch (error) {
    console.error(`dist refused: ${error.message}`);
    process.exit(1);
    return;
  }

  const noticesDir = join(REPO, "dist", "notices");
  mkdirSync(noticesDir, { recursive: true });
  writeFileSync(join(noticesDir, "THIRD-PARTY-NOTICES.txt"), noticesText);

  // Delete this script's own earlier dist/desktop outputs before electron-builder runs, so
  // a release step that fails partway through this run never leaves something complete-looking
  // from an older build beside the new installers; note the time right before electron-builder
  // starts, so only the installers it writes or rewrites this run are ever checksummed.
  cleanStaleOutputs(join(REPO, "dist", "desktop"));
  const buildStartedAtMs = Date.now();

  // Everything from here on must run inside try/finally, and process.exit() must stay out of
  // the try: it ends the process at once, before a finally attached to its own try can run.
  let staged = null;
  const extraEnv = { ...signed.env };

  let code;
  try {
    if (signed.keyP8) {
      staged = stageKey(signed.keyP8);
      extraEnv.APPLE_API_KEY = staged.file;
    }

    const cli = createRequire(import.meta.url).resolve("electron-builder/cli.js");
    const child = spawn(process.execPath, childArgs(cli, signed.args, args), {
      cwd: DESKTOP_ROOT,
      env: childEnv(process.env, extraEnv),
      stdio: "inherit",
    });

    // Node's default handling of these signals exits at once, skipping this try's finally and
    // leaving the staged key behind. Listening here replaces that default
    // with: forward the signal to the child, then wait for its real exit, so finally still runs.
    const signalHandlers = ["SIGINT", "SIGTERM", "SIGHUP"].map((signal) => {
      const handler = () => child.kill(signal);
      process.on(signal, handler);
      return [signal, handler];
    });

    try {
      code = await new Promise((resolve, reject) => {
        child.on("error", reject);
        child.on("exit", (exitCode) => resolve(exitCode !== null ? exitCode : 1));
      });
    } finally {
      for (const [signal, handler] of signalHandlers) {
        process.off(signal, handler);
      }
    }
  } finally {
    if (staged) {
      staged.cleanup();
    }
  }

  if (code === 0) {
    let steps;
    try {
      steps = releaseCommands({ repo: REPO, npmCli: process.env.npm_execpath, env: process.env });
    } catch (error) {
      console.error(`dist refused: ${error.message}`);
      process.exit(1);
      return;
    }

    try {
      await writeReleaseArtifacts({
        steps,
        repo: REPO,
        platform: process.platform,
        noticesText,
        buildStartedAtMs,
        magpieSbom: magpie.sbom,
      });
    } catch (error) {
      console.error(error.message);
      process.exit(1);
      return;
    }
  }

  process.exit(code);
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  main().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
