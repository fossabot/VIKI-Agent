import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  capture,
  checksumText,
  cleanStaleOutputs,
  FS_TIMESTAMP_SLACK_MS,
  freshInstallerNames,
  INSTALLER_SUFFIXES,
  installerNames,
  NPM_SBOM_ARGS,
  osLabel,
  productionSbom,
  sha256File,
  uvExportArgs,
} from "../build/artifacts.mjs";
import { prodPackages } from "../build/licences.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const DESKTOP_ROOT = dirname(HERE);
const REPO_ROOT = dirname(DESKTOP_ROOT);
const FRONTEND_ROOT = join(REPO_ROOT, "frontend");

// -- productionSbom -------------------------------------------------------------------------

test("productionSbom keeps a component reached by both a production and a dev path, forcing scope to required and dropping only the development property", () => {
  const lock = {
    packages: {
      "": {},
      "node_modules/semver": { version: "7.7.4", license: "ISC" },
      "node_modules/dev-tool/node_modules/semver": {
        version: "7.7.4",
        license: "ISC",
        dev: true,
      },
    },
  };
  const component = {
    "bom-ref": "semver@7.7.4",
    type: "library",
    name: "semver",
    version: "7.7.4",
    scope: "optional",
    properties: [
      { name: "cdx:npm:package:development", value: "true" },
      { name: "cdx:npm:package:bundled", value: "true" },
    ],
  };
  const sbom = {
    bomFormat: "CycloneDX",
    metadata: { component: { "bom-ref": "root@1.0.0" } },
    components: [component],
    dependencies: [{ ref: "root@1.0.0", dependsOn: ["semver@7.7.4"] }],
  };

  const result = productionSbom(sbom, lock);

  assert.deepEqual(result.components, [
    {
      ...component,
      scope: "required",
      properties: [{ name: "cdx:npm:package:bundled", value: "true" }],
    },
  ]);
});

test("productionSbom drops a dev-only component entirely", () => {
  const lock = {
    packages: {
      "": {},
      "node_modules/dev-only": { version: "1.0.0", license: "MIT", dev: true },
    },
  };
  const sbom = {
    metadata: { component: { "bom-ref": "root@1.0.0" } },
    components: [{ "bom-ref": "dev-only@1.0.0", name: "dev-only", version: "1.0.0" }],
    dependencies: [{ ref: "root@1.0.0", dependsOn: ["dev-only@1.0.0"] }],
  };

  assert.deepEqual(productionSbom(sbom, lock).components, []);
});

test("productionSbom prunes dependencies to the refs it keeps, plus the root component, and leaves other fields untouched", () => {
  const lock = {
    packages: {
      "": {},
      "node_modules/kept": { version: "1.0.0", license: "MIT" },
      "node_modules/dropped": { version: "1.0.0", license: "MIT", dev: true },
    },
  };
  const sbom = {
    bomFormat: "CycloneDX",
    specVersion: "1.5",
    serialNumber: "urn:uuid:test",
    metadata: { component: { "bom-ref": "root@1.0.0" } },
    components: [
      { "bom-ref": "kept@1.0.0", name: "kept", version: "1.0.0" },
      { "bom-ref": "dropped@1.0.0", name: "dropped", version: "1.0.0" },
    ],
    dependencies: [
      { ref: "root@1.0.0", dependsOn: ["kept@1.0.0", "dropped@1.0.0"] },
      { ref: "kept@1.0.0", dependsOn: [] },
      { ref: "dropped@1.0.0", dependsOn: [] },
    ],
  };

  const result = productionSbom(sbom, lock);

  assert.deepEqual(result.dependencies, [
    { ref: "root@1.0.0", dependsOn: ["kept@1.0.0"] },
    { ref: "kept@1.0.0", dependsOn: [] },
  ]);
  assert.equal(result.bomFormat, "CycloneDX");
  assert.equal(result.specVersion, "1.5");
  assert.equal(result.serialNumber, "urn:uuid:test");
  assert.deepEqual(result.metadata, sbom.metadata);
});

// -- NPM_SBOM_ARGS / uvExportArgs -----------------------------------------------------------

test("NPM_SBOM_ARGS requests a lock-only CycloneDX document, unfiltered by npm", () => {
  assert.deepEqual(NPM_SBOM_ARGS, ["sbom", "--sbom-format", "cyclonedx", "--package-lock-only"]);
});

test("uvExportArgs requests a frozen, no-dev CycloneDX export at the given output path", () => {
  assert.deepEqual(uvExportArgs("dist/desktop/sbom-python.cdx.json"), [
    "export",
    "--format",
    "cyclonedx1.5",
    "--preview-features",
    "sbom-export",
    "--no-dev",
    "--frozen",
    "--quiet",
    "-o",
    "dist/desktop/sbom-python.cdx.json",
  ]);
});

// -- capture ----------------------------------------------------------------------------------

test("capture runs process.execPath -e and resolves with its exit code, stdout and stderr", async () => {
  const result = await capture(
    process.execPath,
    ["-e", "process.stdout.write('hello'); process.stderr.write('warn')"],
    {},
  );
  assert.equal(result.code, 0);
  assert.equal(result.stdout, "hello");
  assert.equal(result.stderr, "warn");
});

test("capture resolves (not rejects) a non-zero exit code", async () => {
  const result = await capture(process.execPath, ["-e", "process.exit(3)"], {});
  assert.equal(result.code, 3);
});

test("capture passes cwd and env through to the child, with no shell involved", async () => {
  const dir = mkdtempSync(join(tmpdir(), "artifacts-capture-"));
  try {
    const result = await capture(
      process.execPath,
      ["-e", "process.stdout.write(process.cwd() + '|' + process.env.ARTIFACTS_TEST_VAR)"],
      { cwd: dir, env: { ARTIFACTS_TEST_VAR: "set" } },
    );
    const [printedCwd, printedVar] = result.stdout.split("|");
    assert.equal(printedVar, "set");
    // Resolve both sides the same way (realpath-equivalent) since Windows may report a different
    // drive-letter case or short/long form for the same tmp directory.
    assert.equal(printedCwd.toLowerCase(), dir.toLowerCase());
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// -- INSTALLER_SUFFIXES / installerNames ----------------------------------------------------

test("installerNames keeps only the top-level installer suffixes, in their given order", () => {
  assert.deepEqual(INSTALLER_SUFFIXES, [".exe", ".dmg", ".zip", ".AppImage", ".deb"]);
  const names = [
    "VIKI Agent Setup 0.1.0.exe",
    "VIKI Agent-0.1.0.dmg",
    "VIKI Agent-0.1.0-arm64-mac.zip",
    "viki-agent-0.1.0.AppImage",
    "viki-agent_0.1.0_amd64.deb",
    "latest.yml",
    "builder-debug.yml",
    "viki-agent-0.1.0.exe.blockmap",
  ];
  assert.deepEqual(installerNames(names), [
    "VIKI Agent Setup 0.1.0.exe",
    "VIKI Agent-0.1.0.dmg",
    "VIKI Agent-0.1.0-arm64-mac.zip",
    "viki-agent-0.1.0.AppImage",
    "viki-agent_0.1.0_amd64.deb",
  ]);
});

// -- sha256File -------------------------------------------------------------------------------

test("sha256File streams the file and returns the SHA-256 of 'abc'", async (t) => {
  const dir = mkdtempSync(join(tmpdir(), "artifacts-sha256-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const file = join(dir, "abc.txt");
  writeFileSync(file, "abc");
  const expected = createHash("sha256").update("abc").digest("hex");
  assert.equal(await sha256File(file), expected);
});

// -- checksumText -----------------------------------------------------------------------------

test("checksumText sorts by name and writes the sha256sum text-mode line format", () => {
  assert.equal(
    checksumText([
      { name: "b-second.exe", hex: "bb" },
      { name: "a-first.deb", hex: "aa" },
    ]),
    "aa  a-first.deb\nbb  b-second.exe\n",
  );
});

// -- osLabel ----------------------------------------------------------------------------------

test("osLabel maps the three supported process.platform values", () => {
  assert.equal(osLabel("win32"), "windows");
  assert.equal(osLabel("darwin"), "macos");
  assert.equal(osLabel("linux"), "linux");
});

test("osLabel throws, naming the platform, for anything else", () => {
  assert.throws(() => osLabel("sunos"), /sunos/);
});

// -- cleanStaleOutputs ------------------------------------------------------------------------

test("cleanStaleOutputs deletes only sbom-*.cdx.json, SHA256SUMS-*.txt and THIRD-PARTY-NOTICES-*.txt, nothing else", () => {
  const dir = mkdtempSync(join(tmpdir(), "artifacts-clean-"));
  try {
    const stale = [
      "sbom-frontend.cdx.json",
      "sbom-desktop.cdx.json",
      "sbom-python.cdx.json",
      "SHA256SUMS-windows.txt",
      "THIRD-PARTY-NOTICES-windows.txt",
    ];
    const kept = [
      "viki-agent-0.1.0.exe",
      "builder-debug.yml",
      "my-sbom-extra.cdx.json", // does not start with "sbom-": must survive
    ];
    for (const name of [...stale, ...kept]) {
      writeFileSync(join(dir, name), "x");
    }
    mkdirSync(join(dir, "sbom-subfolder")); // a directory: never deleted, even if its name matches

    cleanStaleOutputs(dir);

    assert.deepEqual(readdirSync(dir).sort(), [...kept, "sbom-subfolder"].sort());
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("cleanStaleOutputs on a missing directory is a no-op", () => {
  const parent = mkdtempSync(join(tmpdir(), "artifacts-clean-missing-"));
  try {
    assert.doesNotThrow(() => cleanStaleOutputs(join(parent, "dist-desktop")));
  } finally {
    rmSync(parent, { recursive: true, force: true });
  }
});

// -- freshInstallerNames ----------------------------------------------------------------------

test("freshInstallerNames keeps only installers modified at or after sinceMs, within the slack", () => {
  const dir = mkdtempSync(join(tmpdir(), "artifacts-fresh-"));
  try {
    // A whole second: utimesSync stores seconds as a float, so a millisecond value can read back a
    // fraction of a millisecond early and fall just outside the exact slack boundary below.
    const sinceMs = Math.floor(Date.now() / 1000) * 1000;
    const setMtime = (file, ms) => {
      writeFileSync(file, "x");
      utimesSync(file, new Date(ms), new Date(ms));
    };

    const old = join(dir, "viki-agent-old-0.0.9.exe");
    const fresh = join(dir, "viki-agent-new-0.1.0.exe");
    const atSlackEdge = join(dir, "viki-agent-edge-0.1.0.exe");
    const ignoredSuffix = join(dir, "latest.yml"); // not an installer suffix at all

    setMtime(old, sinceMs - 10_000);
    setMtime(fresh, sinceMs);
    setMtime(atSlackEdge, sinceMs - FS_TIMESTAMP_SLACK_MS); // exactly at the slack boundary: kept
    setMtime(ignoredSuffix, sinceMs);

    assert.deepEqual(
      freshInstallerNames(dir, sinceMs).sort(),
      ["viki-agent-edge-0.1.0.exe", "viki-agent-new-0.1.0.exe"].sort(),
    );
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("freshInstallerNames excludes every installer when none is fresh (a --dir build after a full one)", () => {
  const dir = mkdtempSync(join(tmpdir(), "artifacts-fresh-none-"));
  try {
    const sinceMs = Date.now();
    const old = join(dir, "viki-agent-old-0.0.9.exe");
    writeFileSync(old, "x");
    utimesSync(old, new Date(sinceMs - 10_000), new Date(sinceMs - 10_000));

    assert.deepEqual(freshInstallerNames(dir, sinceMs), []);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// -- productionSbom against a real npm sbom run ----------------------------------------------
//
// npm sets npm_execpath (the absolute path to its own CLI script) for any script it runs, so
// this is skipped under a bare `node --test` and runs for real under `npm run test`. It needs
// no network: NPM_SBOM_ARGS is --package-lock-only.

const npmExecpath = process.env.npm_execpath;

test("productionSbom, on a real npm sbom run in frontend/, keeps exactly the lockfile's production set including react, react-dom and scheduler, none of it a dev package", {
  skip: npmExecpath ? false : "npm_execpath is not set outside npm run test",
}, async () => {
  const lock = JSON.parse(readFileSync(join(FRONTEND_ROOT, "package-lock.json"), "utf8"));
  const result = await capture(process.execPath, [npmExecpath, ...NPM_SBOM_ARGS], {
    cwd: FRONTEND_ROOT,
  });
  assert.equal(result.code, 0, result.stderr);
  const sbom = productionSbom(JSON.parse(result.stdout), lock);

  assert.equal(sbom.components.length, prodPackages(lock).length);
  for (const name of ["react", "react-dom", "scheduler"]) {
    const found = sbom.components.filter((c) => c.name === name);
    assert.equal(found.length, 1, name);
  }
  assert.equal(sbom.components.find((c) => c.name === "react").version, "19.2.8");
  assert.equal(sbom.components.find((c) => c.name === "react-dom").version, "19.2.8");
  assert.equal(sbom.components.find((c) => c.name === "scheduler").version, "0.27.0");
  assert.ok(sbom.components.every((c) => c.scope === "required"));
  assert.ok(
    sbom.components.every(
      (c) => !(c.properties ?? []).some((p) => p.name === "cdx:npm:package:development"),
    ),
  );
});

test("productionSbom, on a real npm sbom run in desktop/, keeps exactly the lockfile's 16 production packages with semver@7.7.4 required", {
  skip: npmExecpath ? false : "npm_execpath is not set outside npm run test",
}, async () => {
  const lock = JSON.parse(readFileSync(join(DESKTOP_ROOT, "package-lock.json"), "utf8"));
  const result = await capture(process.execPath, [npmExecpath, ...NPM_SBOM_ARGS], {
    cwd: DESKTOP_ROOT,
  });
  assert.equal(result.code, 0, result.stderr);
  const sbom = productionSbom(JSON.parse(result.stdout), lock);

  assert.equal(sbom.components.length, 16);
  assert.ok(sbom.components.every((c) => c.scope === "required"));
  assert.ok(
    sbom.components.every(
      (c) => !(c.properties ?? []).some((p) => p.name === "cdx:npm:package:development"),
    ),
  );
  const semver = sbom.components.find((c) => c.name === "semver");
  assert.ok(semver, "semver@7.7.4 is reached by a production path and must be kept");
  assert.equal(semver.version, "7.7.4");
});
