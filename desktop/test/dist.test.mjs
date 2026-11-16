import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { NPM_SBOM_ARGS, osLabel, uvExportArgs } from "../build/artifacts.mjs";
import {
  ALLOWED_ARGS,
  backendBinary,
  childArgs,
  childEnv,
  readInventory,
  refusedArgs,
  releaseCommands,
  signing,
  stageKey,
  writeReleaseArtifacts,
} from "../build/dist.mjs";
import { writeMagpieFixture } from "./magpie-build-fixture.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const DESKTOP_ROOT = dirname(HERE);
const REPO = dirname(DESKTOP_ROOT);
const DIST_MJS = join(DESKTOP_ROOT, "build", "dist.mjs");
const SPAWN_TIMEOUT_MS = 10_000;

// Every value below is a synthetic placeholder: this file never holds a real key or certificate.
const FAKE_SHA1_LOWER = "a".repeat(40);
const FAKE_SHA1_MIXED_CASE = "A1b2C3d4".repeat(5);
const FAKE_MAC_ENV = {
  VIKI_MAC_CERT_P12: "ZmFrZS1wMTItYmFzZTY0", // base64 of "fake-p12-base64"
  VIKI_MAC_CERT_PASSWORD: "fake-cert-password",
  VIKI_APPLE_API_KEY_P8: "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----",
  VIKI_APPLE_API_KEY_ID: "FAKEKEYID01",
  VIKI_APPLE_API_ISSUER: "00000000-fake-issuer-0000-000000000000",
};

// -- backendBinary ------------------------------------------------------------------------------

test("backendBinary appends .exe only on win32", () => {
  assert.equal(
    backendBinary("/repo", "win32"),
    join("/repo", "dist", "compiled", "viki-agent.dist", "viki-agent.exe"),
  );
  for (const platform of ["darwin", "linux"]) {
    assert.equal(
      backendBinary("/repo", platform),
      join("/repo", "dist", "compiled", "viki-agent.dist", "viki-agent"),
      platform,
    );
  }
});

// -- readInventory --------------------------------------------------------------------------------

test("readInventory returns the parsed licence inventory scripts/compile.py wrote", () => {
  const repo = mkdtempSync(join(tmpdir(), "dist-inventory-"));
  try {
    mkdirSync(join(repo, "dist", "compiled"), { recursive: true });
    const inventory = { schema: 1, python: { version: "3.14.7" }, distributions: [] };
    writeFileSync(
      join(repo, "dist", "compiled", "viki-agent-licences.json"),
      JSON.stringify(inventory),
    );
    assert.deepEqual(readInventory(repo), inventory);
  } finally {
    rmSync(repo, { recursive: true, force: true });
  }
});

test("readInventory throws when scripts/compile.py has not written an inventory yet", () => {
  const repo = mkdtempSync(join(tmpdir(), "dist-inventory-"));
  try {
    assert.throws(() => readInventory(repo), {
      message:
        "dist refused: no licence inventory, scripts/compile.py writes dist/compiled/viki-agent-licences.json",
    });
  } finally {
    rmSync(repo, { recursive: true, force: true });
  }
});

test("readInventory refuses malformed JSON without quoting its synthetic payload", () => {
  const repo = mkdtempSync(join(tmpdir(), "dist-inventory-"));
  try {
    mkdirSync(join(repo, "dist", "compiled"), { recursive: true });
    writeFileSync(
      join(repo, "dist", "compiled", "viki-agent-licences.json"),
      "SYNTHETIC_PRIVATE_INVENTORY",
    );
    assert.throws(() => readInventory(repo), {
      message: "dist refused: invalid licence inventory JSON",
    });
  } finally {
    rmSync(repo, { recursive: true, force: true });
  }
});

// -- refusedArgs ---------------------------------------------------------------------------------

test("refusedArgs accepts every argument in ALLOWED_ARGS, individually and together", () => {
  for (const allowed of ALLOWED_ARGS) {
    assert.deepEqual(refusedArgs([allowed]), [], allowed);
  }
  assert.deepEqual(refusedArgs([...ALLOWED_ARGS]), []);
  assert.deepEqual(refusedArgs([]), []);
});

test("refusedArgs refuses every spelling of publish, short-option aliases and any other -c.* override", () => {
  assert.deepEqual(refusedArgs(["-p", "always"]), ["-p", "always"]);
  assert.deepEqual(refusedArgs(["-wp"]), ["-wp"]);
  assert.deepEqual(refusedArgs(["--publish"]), ["--publish"]);
  assert.deepEqual(refusedArgs(["--publish=always"]), ["--publish=always"]);
  assert.deepEqual(refusedArgs(["-c.publish=x"]), ["-c.publish=x"]);
  assert.deepEqual(refusedArgs(["always"]), ["always"]);
  assert.deepEqual(
    refusedArgs(["--win", "-p", "always", "--dir"]),
    ["-p", "always"],
    "every offending arg, in order, not just the first; allowed ones excluded",
  );
});

// -- signing: win32 -------------------------------------------------------------------------------

test("signing on win32 with no cert thumbprint builds unsigned", () => {
  assert.deepEqual(signing({}, "win32"), { args: [], env: {} });
  assert.deepEqual(
    signing({ VIKI_WIN_CERT_SHA1: "" }, "win32"),
    { args: [], env: {} },
    "an empty string counts as absent",
  );
});

test("signing on win32 with a valid 40-hex thumbprint signs, case preserved", () => {
  assert.deepEqual(signing({ VIKI_WIN_CERT_SHA1: FAKE_SHA1_LOWER }, "win32"), {
    args: [`-c.win.signtoolOptions.certificateSha1=${FAKE_SHA1_LOWER}`, "-c.forceCodeSigning=true"],
    env: {},
  });
  assert.deepEqual(signing({ VIKI_WIN_CERT_SHA1: FAKE_SHA1_MIXED_CASE }, "win32"), {
    args: [
      `-c.win.signtoolOptions.certificateSha1=${FAKE_SHA1_MIXED_CASE}`,
      "-c.forceCodeSigning=true",
    ],
    env: {},
  });
});

test("signing on win32 refuses a thumbprint that is not exactly 40 hex characters", () => {
  for (const bad of ["a".repeat(39), "a".repeat(41), "g".repeat(40), "not-hex-at-all"]) {
    assert.throws(
      () => signing({ VIKI_WIN_CERT_SHA1: bad }, "win32"),
      (error) => {
        assert.ok(error instanceof Error, bad);
        assert.doesNotMatch(error.message, new RegExp(bad.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
        return true;
      },
    );
  }
});

// -- signing: darwin ------------------------------------------------------------------------------

test("signing on darwin with none of its secrets set builds unsigned, with auto-discovery off", () => {
  assert.deepEqual(signing({}, "darwin"), {
    args: [],
    env: { CSC_IDENTITY_AUTO_DISCOVERY: "false" },
  });
  const allEmpty = Object.fromEntries(Object.keys(FAKE_MAC_ENV).map((name) => [name, ""]));
  assert.deepEqual(
    signing(allEmpty, "darwin"),
    { args: [], env: { CSC_IDENTITY_AUTO_DISCOVERY: "false" } },
    "empty strings count as absent, same as unset",
  );
});

test("signing on darwin with the complete secret set signs and maps every name", () => {
  const result = signing(FAKE_MAC_ENV, "darwin");
  assert.deepEqual(result.args, ["-c.forceCodeSigning=true"]);
  assert.deepEqual(result.env, {
    CSC_LINK: FAKE_MAC_ENV.VIKI_MAC_CERT_P12,
    CSC_KEY_PASSWORD: FAKE_MAC_ENV.VIKI_MAC_CERT_PASSWORD,
    APPLE_API_KEY_ID: FAKE_MAC_ENV.VIKI_APPLE_API_KEY_ID,
    APPLE_API_ISSUER: FAKE_MAC_ENV.VIKI_APPLE_API_ISSUER,
  });
  assert.equal(result.keyP8, FAKE_MAC_ENV.VIKI_APPLE_API_KEY_P8);
});

// Guards the partial-secrets refusal: remove the "some missing" check in `signing` and this
// fails, because darwin would then sign with `undefined` entries instead of refusing.
test("signing on darwin with a partial secret set throws, naming only the missing variables", () => {
  const partial = { ...FAKE_MAC_ENV };
  delete partial.VIKI_MAC_CERT_PASSWORD;
  delete partial.VIKI_APPLE_API_KEY_ID;

  assert.throws(
    () => signing(partial, "darwin"),
    (error) => {
      assert.match(error.message, /VIKI_MAC_CERT_PASSWORD/);
      assert.match(error.message, /VIKI_APPLE_API_KEY_ID/);
      for (const present of [
        "VIKI_MAC_CERT_P12",
        "VIKI_APPLE_API_KEY_P8",
        "VIKI_APPLE_API_ISSUER",
      ]) {
        assert.doesNotMatch(
          error.message,
          new RegExp(present),
          `must not name the present ${present}`,
        );
      }
      for (const value of Object.values(FAKE_MAC_ENV)) {
        assert.ok(!error.message.includes(value), "must not carry any secret's value");
      }
      return true;
    },
  );
});

test("signing on darwin with exactly one secret missing throws naming just that one", () => {
  const partial = { ...FAKE_MAC_ENV, VIKI_APPLE_API_ISSUER: "" };
  assert.throws(() => signing(partial, "darwin"), /VIKI_APPLE_API_ISSUER/);
});

// -- signing: linux --------------------------------------------------------------------------------

test("signing on linux never signs, whatever the environment holds", () => {
  assert.deepEqual(signing({}, "linux"), { args: [], env: {} });
  assert.deepEqual(
    signing({ VIKI_WIN_CERT_SHA1: FAKE_SHA1_LOWER, ...FAKE_MAC_ENV }, "linux"),
    { args: [], env: {} },
    "linux has no signing concept, so even a full set of secrets changes nothing",
  );
});

// -- childEnv -------------------------------------------------------------------------------------

test("childEnv strips signing and token names case-insensitively, keeps everything else, and lets extraEnv win", () => {
  const baseEnv = {
    PATH: "/usr/bin",
    SOME_OTHER_VAR: "kept",
    gh_token: "leak-gh-token",
    GitHub_Token: "leak-github-token",
    Csc_Link: "leak-ambient-csc-link",
    WIN_CSC_KEY_PASSWORD: "leak-win-csc-password",
    apple_id: "leak-apple-id",
    APPLE_APP_SPECIFIC_PASSWORD: "leak-apple-password",
    viki_mac_cert_p12: "leak-viki-mac-cert",
  };
  const extraEnv = { CSC_LINK: "resolved-cert-path", APPLE_API_KEY: "/tmp/AuthKey.p8" };

  assert.deepEqual(childEnv(baseEnv, extraEnv), {
    PATH: "/usr/bin",
    SOME_OTHER_VAR: "kept",
    CSC_LINK: "resolved-cert-path",
    APPLE_API_KEY: "/tmp/AuthKey.p8",
  });
});

test("childEnv with an empty extraEnv just strips", () => {
  assert.deepEqual(childEnv({ PATH: "/bin", GH_TOKEN: "leak" }, {}), { PATH: "/bin" });
});

// -- childArgs --------------------------------------------------------------------------------------

test("childArgs orders cli, --publish never, signing args, then the caller's args", () => {
  assert.deepEqual(
    childArgs(
      "/path/to/electron-builder/cli.js",
      ["-c.forceCodeSigning=true"],
      ["--mac", "--arm64"],
    ),
    [
      "/path/to/electron-builder/cli.js",
      "--publish",
      "never",
      "-c.forceCodeSigning=true",
      "--mac",
      "--arm64",
    ],
  );
});

test("childArgs with no signing args or caller args still forces --publish never", () => {
  assert.deepEqual(childArgs("/cli.js", [], []), ["/cli.js", "--publish", "never"]);
});

// -- stageKey -----------------------------------------------------------------------------------

test("stageKey writes the key to AuthKey.p8, 0600 in a 0700 folder on POSIX, and cleanup removes it", () => {
  const FAKE_KEY = "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----";
  const { file, cleanup } = stageKey(FAKE_KEY);

  assert.equal(basename(file), "AuthKey.p8");
  assert.equal(readFileSync(file, "utf8"), FAKE_KEY);
  if (process.platform !== "win32") {
    assert.equal(statSync(file).mode & 0o777, 0o600, "key file mode");
    assert.equal(statSync(dirname(file)).mode & 0o777, 0o700, "key folder mode");
  }

  cleanup();
  assert.ok(!existsSync(dirname(file)), "cleanup removes the staging folder");
  assert.doesNotThrow(cleanup, "cleanup is idempotent");
});

// -- releaseCommands -------------------------------------------------------------------------------

test("releaseCommands throws naming the missing npm_execpath when npmCli is not set", () => {
  for (const npmCli of [undefined, ""]) {
    assert.throws(
      () => releaseCommands({ repo: "/repo", npmCli, env: {} }),
      (error) => {
        assert.ok(error instanceof Error);
        assert.match(error.message, /npm_execpath/);
        return true;
      },
    );
  }
});

test("releaseCommands returns sbom-frontend, sbom-desktop and sbom-python, each ready for capture()", () => {
  const repo = join("/repo");
  const npmCli = join("/path/to/npm-cli.js");
  const steps = releaseCommands({ repo, npmCli, env: { PATH: "/usr/bin" } });

  assert.deepEqual(
    steps.map((step) => step.name),
    ["sbom-frontend", "sbom-desktop", "sbom-python"],
  );

  const frontend = steps.find((step) => step.name === "sbom-frontend");
  assert.equal(frontend.command, process.execPath);
  assert.deepEqual(frontend.args, [npmCli, ...NPM_SBOM_ARGS]);
  assert.equal(frontend.cwd, join(repo, "frontend"));
  assert.equal(frontend.lockFile, join(repo, "frontend", "package-lock.json"));
  assert.equal(frontend.outFile, join(repo, "dist", "desktop", "sbom-frontend.cdx.json"));

  const desktop = steps.find((step) => step.name === "sbom-desktop");
  assert.equal(desktop.cwd, join(repo, "desktop"));
  assert.equal(desktop.lockFile, join(repo, "desktop", "package-lock.json"));
  assert.equal(desktop.outFile, join(repo, "dist", "desktop", "sbom-desktop.cdx.json"));

  const python = steps.find((step) => step.name === "sbom-python");
  const pythonOut = join(repo, "dist", "desktop", "sbom-python.cdx.json");
  assert.equal(python.command, "uv");
  assert.deepEqual(python.args, uvExportArgs(pythonOut));
  assert.equal(python.cwd, repo);
  assert.equal(python.lockFile, null, "uv writes its own output via -o; nothing to post-process");
  assert.equal(python.outFile, pythonOut);
});

// Guards the stripped step environment: remove the childEnv call in releaseCommands and this
// fails, because every step's env would carry the signing secrets and tokens straight through.
test("releaseCommands strips every VIKI_* signing secret and GitHub token from every step's env", () => {
  const leakyEnv = {
    PATH: "/usr/bin",
    VIKI_WIN_CERT_SHA1: "leak-win-cert",
    VIKI_MAC_CERT_P12: "leak-mac-cert",
    VIKI_MAC_CERT_PASSWORD: "leak-mac-password",
    VIKI_APPLE_API_KEY_P8: "leak-apple-key",
    VIKI_APPLE_API_KEY_ID: "leak-apple-key-id",
    VIKI_APPLE_API_ISSUER: "leak-apple-issuer",
    GH_TOKEN: "leak-gh-token",
    GITHUB_TOKEN: "leak-github-token",
  };
  const steps = releaseCommands({ repo: "/repo", npmCli: "/npm-cli.js", env: leakyEnv });
  assert.ok(steps.length > 0);
  for (const step of steps) {
    assert.deepEqual(step.env, { PATH: "/usr/bin" }, step.name);
  }
});

// -- main(), spawned as a real process --------------------------------------------------------------
//
// These paths exit before dist.mjs ever imports electron-builder, so spawning the real script to
// prove their exit codes never runs electron-builder itself. The third documented throw path (a
// partial signing secret set) is proved above as a pure function instead, since reaching it
// through main() needs a compiled backend to exist first.

/** A clean slate: every signing variable this script reads, explicitly unset. */
const NO_SIGNING_ENV = {
  ...process.env,
  VIKI_WIN_CERT_SHA1: "",
  VIKI_MAC_CERT_P12: "",
  VIKI_MAC_CERT_PASSWORD: "",
  VIKI_APPLE_API_KEY_P8: "",
  VIKI_APPLE_API_KEY_ID: "",
  VIKI_APPLE_API_ISSUER: "",
};

/**
 * @param {string[]} args
 * @param {NodeJS.ProcessEnv} env
 */
function runDist(args, env) {
  return spawnSync(process.execPath, [DIST_MJS, ...args], {
    cwd: DESKTOP_ROOT,
    env,
    encoding: "utf8",
    timeout: SPAWN_TIMEOUT_MS,
  });
}

test("main refuses an unsupported argument before touching anything else (exit 2, names it)", () => {
  const result = runDist(["-p", "always"], NO_SIGNING_ENV);
  assert.equal(result.status, 2);
  assert.equal(result.stderr.trim(), "dist refused: unsupported argument(s): -p, always");
});

test("main refuses with no compiled backend (exit 1, names scripts/compile.py and the path)", (t) => {
  const backend = backendBinary(REPO, process.platform);
  if (existsSync(backend)) {
    t.skip(`a compiled backend exists at ${backend}; this checkout is expected not to have one`);
    return;
  }
  const result = runDist(["--dir"], NO_SIGNING_ENV);
  assert.equal(result.status, 1);
  assert.equal(
    result.stderr.trim(),
    `dist refused: build the compiled server first (scripts/compile.py): ${backend}`,
  );
});

test("main refuses a macOS unsigned-build fallback once the build is actually signed (exit 2)", (t) => {
  // Linux has no signing concept (`signing` always returns empty args there), so this build can
  // never be "signed" and the scenario cannot be reached.
  const signedEnv =
    process.platform === "win32"
      ? { VIKI_WIN_CERT_SHA1: FAKE_SHA1_LOWER }
      : process.platform === "darwin"
        ? { ...FAKE_MAC_ENV }
        : null;
  if (!signedEnv) {
    t.skip('linux has no signing concept; this build can never be "signed"');
    return;
  }
  // main() checks for the compiled backend before it reads signing, so only a run with one
  // (after `scripts/compile.py` has built it) reaches the fallback refusal.
  const backend = backendBinary(REPO, process.platform);
  if (!existsSync(backend)) {
    t.skip(`no compiled backend at ${backend}; main refuses before the fallback check`);
    return;
  }
  const result = runDist(["-c.mac.hardenedRuntime=false"], { ...NO_SIGNING_ENV, ...signedEnv });
  assert.equal(result.status, 2);
  assert.equal(
    result.stderr.trim(),
    "dist refused: the -c.mac fallbacks are for unsigned builds only",
  );
});

// -- main(): notices.problems refuses before electron-builder is ever resolved ------------------
//
// A real dist.mjs, plus the sibling modules its relative imports need, copied into a temp repo
// with a stub backend, a trivial pair of npm lockfiles and a reviewed-licence table that covers
// nothing at all — so CPython and Nuitka (both `requiresRow`) are guaranteed unreviewed. The
// copy carries no node_modules at all, so even a version of this script that skipped the refusal
// could never actually resolve, let alone run, electron-builder here.

/** @returns {{distMjs: string, cleanup: () => void}} */
function buildNoticesRefusalFixture() {
  const repo = mkdtempSync(join(tmpdir(), "dist-notices-refusal-"));
  const desktopDir = join(repo, "desktop");
  const buildDir = join(desktopDir, "build");
  mkdirSync(buildDir, { recursive: true });
  for (const name of [
    "dist.mjs",
    "artifacts.mjs",
    "licences.mjs",
    "notices.mjs",
    "release-files.mjs",
    "magpie-build.mjs",
    "magpie-release.json",
  ]) {
    writeFileSync(join(buildDir, name), readFileSync(join(DESKTOP_ROOT, "build", name)));
  }
  mkdirSync(join(repo, "scripts"));
  writeFileSync(
    join(repo, "scripts", "private-files.json"),
    readFileSync(join(REPO, "scripts", "private-files.json")),
  );
  writeFileSync(
    join(desktopDir, "package.json"),
    JSON.stringify({ version: "0.0.0-test", homepage: "https://example.invalid" }),
  );
  writeFileSync(
    join(buildDir, "licences.json"),
    JSON.stringify({ allowed: [], texts: {}, components: {} }),
  );
  for (const tree of ["frontend", "desktop"]) {
    const dir = join(repo, tree);
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "package-lock.json"), JSON.stringify({ packages: { "": {} } }));
  }
  const backend = backendBinary(repo, process.platform);
  mkdirSync(dirname(backend), { recursive: true });
  writeFileSync(backend, "stub");
  mkdirSync(join(repo, "dist", "compiled"), { recursive: true });
  writeFileSync(
    join(repo, "dist", "compiled", "viki-agent-licences.json"),
    JSON.stringify({
      schema: 1,
      python: { version: "9.9.9", licence: "fake CPython licence text" },
      nuitka: { version: "9.9.9", files: {} },
      distributions: [],
      unattributed: [],
    }),
  );
  return {
    distMjs: join(buildDir, "dist.mjs"),
    cleanup: () => rmSync(repo, { recursive: true, force: true }),
  };
}

test("main refuses a contaminated compiled tree before signing, notices or builder work", () => {
  const fixture = buildNoticesRefusalFixture();
  const repo = dirname(dirname(dirname(fixture.distMjs)));
  try {
    writeFileSync(
      join(dirname(backendBinary(repo, process.platform)), ".env.local"),
      "SYNTHETIC_PRIVATE_BACKEND",
    );
    const result = spawnSync(process.execPath, [fixture.distMjs, "--dir"], {
      env: { ...NO_SIGNING_ENV, ...FAKE_MAC_ENV, VIKI_WIN_CERT_SHA1: FAKE_SHA1_LOWER },
      encoding: "utf8",
      timeout: SPAWN_TIMEOUT_MS,
    });
    assert.equal(result.status, 1, result.stderr);
    assert.match(result.stderr, /dist refused: private release filename/);
    assert.doesNotMatch(
      result.stdout + result.stderr,
      /signing:|notices:|SYNTHETIC_PRIVATE_BACKEND/,
    );
    assert.equal(existsSync(join(repo, "dist", "notices")), false);
  } finally {
    fixture.cleanup();
  }
});

test("main refuses malformed inventory without echoing payload or starting signing", () => {
  const fixture = buildNoticesRefusalFixture();
  const repo = dirname(dirname(dirname(fixture.distMjs)));
  try {
    writeFileSync(
      join(repo, "dist", "compiled", "viki-agent-licences.json"),
      "SYNTHETIC_PRIVATE_INVENTORY",
    );
    const result = spawnSync(process.execPath, [fixture.distMjs, "--dir"], {
      env: NO_SIGNING_ENV,
      encoding: "utf8",
      timeout: SPAWN_TIMEOUT_MS,
    });
    assert.equal(result.status, 1, result.stderr);
    assert.equal(result.stderr.trim(), "dist refused: invalid licence inventory JSON");
    assert.doesNotMatch(result.stdout + result.stderr, /signing:|SYNTHETIC_PRIVATE_INVENTORY/);
  } finally {
    fixture.cleanup();
  }
});

test("main refuses on an unreviewed component before electron-builder is ever resolved, so main never ignores notices.problems", () => {
  const fixture = buildNoticesRefusalFixture();
  try {
    const result = spawnSync(process.execPath, [fixture.distMjs, "--dir"], {
      env: NO_SIGNING_ENV,
      encoding: "utf8",
      timeout: SPAWN_TIMEOUT_MS,
    });
    assert.equal(result.status, 1, result.stderr);
    assert.match(result.stderr, /^notices: /m);
  } finally {
    fixture.cleanup();
  }
});

// -- main(): cleanStaleOutputs runs before electron-builder, and a failed release step still
// exits 1 ---------------------------------------------------------------------------------------
//
// Same technique as buildNoticesRefusalFixture above (a real dist.mjs, plus its sibling modules,
// copied into a temp repo with a stub backend and an inventory), but this time with a fully
// reviewed table so the build runs past the notices refusal: a fake electron-builder cli.js,
// resolved the same way dist.mjs resolves the real one, that exits 0 without doing anything (no
// app starts, no port binds, nothing under dist/desktop is ever really built); and a fake
// npm_execpath script that fails only the "sbom" subcommand, so the release step itself — not
// electron-builder, not notices — is what fails.

/** @returns {{distMjs: string, npmExecpath: string, staleChecksums: string, staleSbom: string,
 *   cleanup: () => void}} */
function buildReleaseWiringFixture() {
  const repo = mkdtempSync(join(tmpdir(), "dist-release-wiring-"));
  const desktopDir = join(repo, "desktop");
  const buildDir = join(desktopDir, "build");
  mkdirSync(buildDir, { recursive: true });
  for (const name of [
    "dist.mjs",
    "artifacts.mjs",
    "licences.mjs",
    "notices.mjs",
    "release-files.mjs",
    "magpie-build.mjs",
    "magpie-release.json",
  ]) {
    writeFileSync(join(buildDir, name), readFileSync(join(DESKTOP_ROOT, "build", name)));
  }
  mkdirSync(join(repo, "scripts"));
  writeFileSync(
    join(repo, "scripts", "private-files.json"),
    readFileSync(join(REPO, "scripts", "private-files.json")),
  );
  writeFileSync(
    join(desktopDir, "package.json"),
    JSON.stringify({ version: "0.0.0-test", homepage: "https://example.invalid" }),
  );

  // Both `requiresRow` components (CPython, Nuitka) get a row whose own `licence` is the review,
  // and the inventory's platform matches this process's — so buildNotices reports
  // zero problems and main() runs on, past the notices refusal.
  writeFileSync(
    join(buildDir, "licences.json"),
    JSON.stringify({
      allowed: [],
      texts: {},
      components: {
        "python:cpython@9.9.9": { licence: "PSF-2.0 (test)" },
        "pypi:nuitka@9.9.9": { licence: "AGPL-3.0 with exception (test)" },
      },
    }),
  );
  for (const tree of ["frontend", "desktop"]) {
    const dir = join(repo, tree);
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "package-lock.json"), JSON.stringify({ packages: { "": {} } }));
  }

  const backend = backendBinary(repo, process.platform);
  mkdirSync(dirname(backend), { recursive: true });
  writeFileSync(backend, "stub");
  writeMagpieFixture(repo, process.platform, process.arch);

  mkdirSync(join(repo, "dist", "compiled"), { recursive: true });
  writeFileSync(
    join(repo, "dist", "compiled", "viki-agent-licences.json"),
    JSON.stringify({
      schema: 1,
      platform: process.platform,
      python: { version: "9.9.9", licence: "fake CPython licence text" },
      nuitka: {
        version: "9.9.9",
        files: { "LICENSE-RUNTIME.txt": "fake nuitka runtime licence text" },
      },
      distributions: [],
      unattributed: [],
    }),
  );

  // The fake electron-builder: resolved by dist.mjs's own
  // createRequire(import.meta.url).resolve("electron-builder/cli.js"), which climbs up from the
  // copied dist.mjs's own directory (desktop/build/) looking for a node_modules folder — found
  // here, one level up, exactly where npm would really install it. It exits 0 doing nothing: no
  // app starts, no port binds, nothing under dist/desktop is ever really built.
  const electronBuilderDir = join(desktopDir, "node_modules", "electron-builder");
  mkdirSync(electronBuilderDir, { recursive: true });
  writeFileSync(
    join(electronBuilderDir, "package.json"),
    JSON.stringify({ name: "electron-builder", version: "0.0.0-test" }),
  );
  writeFileSync(join(electronBuilderDir, "cli.js"), "process.exit(0);\n");

  // The fake npm_execpath: releaseCommands() runs it as `<node> <this file> sbom ...`, so a
  // step's own command never needs a real npm or network access.
  const npmExecpath = join(repo, "fake-npm-execpath.cjs");
  writeFileSync(npmExecpath, 'process.exit(process.argv.includes("sbom") ? 1 : 0);\n');

  const distDesktop = join(repo, "dist", "desktop");
  mkdirSync(distDesktop, { recursive: true });
  const staleChecksums = join(distDesktop, `SHA256SUMS-${osLabel(process.platform)}.txt`);
  const staleSbom = join(distDesktop, "sbom-frontend.cdx.json");
  writeFileSync(staleChecksums, "stale checksums left by an earlier build\n");
  writeFileSync(staleSbom, '{"stale": true}');

  return {
    distMjs: join(buildDir, "dist.mjs"),
    npmExecpath,
    staleChecksums,
    staleSbom,
    cleanup: () => rmSync(repo, { recursive: true, force: true }),
  };
}

test("main cleans stale dist/desktop outputs before electron-builder runs, and a failed release step still exits 1", () => {
  const fixture = buildReleaseWiringFixture();
  try {
    const result = spawnSync(process.execPath, [fixture.distMjs, "--dir"], {
      env: { ...NO_SIGNING_ENV, npm_execpath: fixture.npmExecpath },
      encoding: "utf8",
      timeout: SPAWN_TIMEOUT_MS,
    });
    assert.equal(result.status, 1, `stdout: ${result.stdout}\nstderr: ${result.stderr}`);
    assert.match(result.stderr, /^release: /m);
    assert.equal(
      existsSync(fixture.staleChecksums),
      false,
      "mutant M6b: cleanStaleOutputs must run before electron-builder",
    );
    assert.equal(
      existsSync(fixture.staleSbom),
      false,
      "mutant M6b: cleanStaleOutputs must run before electron-builder",
    );
  } finally {
    fixture.cleanup();
  }
});

// -- writeReleaseArtifacts -----------------------------------------------------------------------
//
// Runs the extracted post-build sequence directly, with a fake runCommand standing in for
// capture(): this never spawns electron-builder, starts Electron or binds a port.

/** @returns {{repo: string, distDesktop: string, cleanup: () => void}} */
function setupDistDesktop() {
  const repo = mkdtempSync(join(tmpdir(), "dist-release-artifacts-"));
  const distDesktop = join(repo, "dist", "desktop");
  mkdirSync(distDesktop, { recursive: true });
  return { repo, distDesktop, cleanup: () => rmSync(repo, { recursive: true, force: true }) };
}

// Guards the refusal on a failed release step: remove the "result.code !== 0" check in
// writeReleaseArtifacts and this fails, because a failing step would be silently treated as a
// success instead of refusing the build.
test("writeReleaseArtifacts throws naming the failing step, so main never ignores a failed release step", async () => {
  const { repo, cleanup } = setupDistDesktop();
  try {
    const steps = [
      {
        name: "sbom-frontend",
        command: "unused",
        args: [],
        cwd: repo,
        env: {},
        lockFile: null,
        outFile: join(repo, "sbom-frontend.cdx.json"),
      },
    ];
    const runCommand = async () => ({ code: 1, stdout: "", stderr: "boom" });

    await assert.rejects(
      writeReleaseArtifacts({
        steps,
        repo,
        platform: "linux",
        noticesText: "notices",
        buildStartedAtMs: Date.now(),
        runCommand,
      }),
      /release: sbom-frontend failed: boom$/,
    );
  } finally {
    cleanup();
  }
});

test("writeReleaseArtifacts writes each lockFile step's production SBOM, checksums only fresh installers, and copies the notices text", async () => {
  const { repo, distDesktop, cleanup } = setupDistDesktop();
  try {
    const buildStartedAtMs = Date.now();

    const oldInstaller = join(distDesktop, "viki-agent-old-0.0.9.exe");
    writeFileSync(oldInstaller, "old");
    utimesSync(
      oldInstaller,
      new Date(buildStartedAtMs - 10_000),
      new Date(buildStartedAtMs - 10_000),
    );

    const newInstaller = join(distDesktop, "viki-agent-0.1.0.exe");
    writeFileSync(newInstaller, "new");
    utimesSync(newInstaller, new Date(buildStartedAtMs), new Date(buildStartedAtMs));

    const lockFile = join(repo, "package-lock.json");
    writeFileSync(
      lockFile,
      JSON.stringify({ packages: { "": {}, "node_modules/kept": { version: "1.0.0" } } }),
    );
    const sbomOut = join(distDesktop, "sbom-desktop.cdx.json");
    const fakeSbom = {
      metadata: { component: { "bom-ref": "root@1.0.0" } },
      components: [{ "bom-ref": "kept@1.0.0", name: "kept", version: "1.0.0" }],
      dependencies: [],
    };
    const steps = [
      {
        name: "sbom-desktop",
        command: "unused",
        args: [],
        cwd: repo,
        env: {},
        lockFile,
        outFile: sbomOut,
      },
    ];
    const runCommand = async () => ({ code: 0, stdout: JSON.stringify(fakeSbom), stderr: "" });

    await writeReleaseArtifacts({
      steps,
      repo,
      platform: "win32",
      noticesText: "notices text",
      buildStartedAtMs,
      runCommand,
      magpieSbom: { bomFormat: "CycloneDX", components: [{ name: "Magpie" }] },
    });

    const sbom = JSON.parse(readFileSync(sbomOut, "utf8"));
    assert.deepEqual(
      JSON.parse(readFileSync(join(distDesktop, "sbom-magpie.cdx.json"), "utf8")).components,
      [{ name: "Magpie" }],
    );
    assert.deepEqual(
      sbom.components.map((c) => c.name),
      ["kept"],
    );

    const checksums = readFileSync(join(distDesktop, "SHA256SUMS-windows.txt"), "utf8");
    assert.match(checksums, /viki-agent-0\.1\.0\.exe/);
    assert.doesNotMatch(checksums, /viki-agent-old-0\.0\.9\.exe/);

    assert.equal(
      readFileSync(join(distDesktop, "THIRD-PARTY-NOTICES-windows.txt"), "utf8"),
      "notices text",
    );
  } finally {
    cleanup();
  }
});

test('writeReleaseArtifacts prints "checksums: no installer in dist/desktop" and writes no checksum file when nothing is fresh (--dir after a full build)', async () => {
  const { repo, distDesktop, cleanup } = setupDistDesktop();
  try {
    const buildStartedAtMs = Date.now();
    const oldInstaller = join(distDesktop, "viki-agent-old-0.0.9.exe");
    writeFileSync(oldInstaller, "old");
    utimesSync(
      oldInstaller,
      new Date(buildStartedAtMs - 10_000),
      new Date(buildStartedAtMs - 10_000),
    );

    const logs = [];
    const originalLog = console.log;
    console.log = (line) => logs.push(line);
    try {
      await writeReleaseArtifacts({
        steps: [],
        repo,
        platform: "linux",
        noticesText: "notices text",
        buildStartedAtMs,
        runCommand: async () => ({ code: 0, stdout: "", stderr: "" }),
      });
    } finally {
      console.log = originalLog;
    }

    assert.ok(logs.includes("checksums: no installer in dist/desktop"));
    assert.equal(existsSync(join(distDesktop, "SHA256SUMS-linux.txt")), false);
  } finally {
    cleanup();
  }
});
