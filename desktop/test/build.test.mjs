import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import * as dist from "../build/dist.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const DESKTOP_ROOT = join(here, "..");
const REPO_ROOT = join(DESKTOP_ROOT, "..");
const HANG_GUARD_MS = 5000;

const pkg = JSON.parse(readFileSync(join(DESKTOP_ROOT, "package.json"), "utf8"));

/** The `[project]` table's `version` from `<repo>/pyproject.toml`. */
function pyprojectVersion() {
  const toml = readFileSync(join(REPO_ROOT, "pyproject.toml"), "utf8");
  const project = toml.split(/^\[/m).find((section) => section.startsWith("project]"));
  const match = project?.match(/^version\s*=\s*"([^"]+)"/m);
  if (!match) throw new Error("pyproject.toml has no [project] version");
  return match[1];
}

/**
 * GO-readiness problems in a candidate package.json: a configured publish feed is a problem
 * when code signing is not forced, and a problem again when a ".invalid" placeholder is still
 * named in `homepage`, `author` or anywhere in `build`. Nothing is a problem while
 * `build.publish` stays falsy (`null` until ADR-055's GO switch).
 * @param {Record<string, any>} candidate
 * @returns {string[]}
 */
function goProblems(candidate) {
  const build = candidate.build ?? {};
  if (!build.publish) return [];
  const problems = [];
  if (build.forceCodeSigning !== true) {
    problems.push("forceCodeSigning is not true while a publish feed is configured");
  }
  const invalid =
    String(candidate.homepage ?? "").includes(".invalid") ||
    String(candidate.author ?? "").includes(".invalid") ||
    JSON.stringify(build).includes(".invalid");
  if (invalid) {
    problems.push("homepage, author or build still names a .invalid placeholder");
  }
  return problems;
}

// -- top level --------------------------------------------------------------------------------

test("package.json names the publisher and the dist script", () => {
  assert.equal(pkg.author, "VIKI Agent");
  assert.equal(pkg.homepage, "https://viki-agent.invalid");
  assert.equal(pkg.scripts.dist, "node build/dist.mjs");
});

test("package.json version matches pyproject.toml's [project] version", () => {
  assert.equal(pkg.version, pyprojectVersion());
});

// -- build: identity and output -----------------------------------------------------------------

test("build pins the app id, output layout and bundled files", () => {
  assert.equal(pkg.build.appId, "app.viki-agent.desktop");
  assert.deepEqual(pkg.build.directories, {
    output: "../dist/desktop",
    buildResources: "build",
  });
  assert.deepEqual(pkg.build.files, [
    "main.mjs",
    "window.mjs",
    "policy.mjs",
    "server.mjs",
    "updates.mjs",
    "magpie.mjs",
    "magpie-theme.mjs",
    "package.json",
    {
      from: "../frontend/node_modules/@fontsource/ibm-plex-sans/files",
      to: "fonts",
      filter: [
        "ibm-plex-sans-latin-400-normal.woff2",
        "ibm-plex-sans-latin-500-normal.woff2",
        "ibm-plex-sans-latin-600-normal.woff2",
      ],
    },
    {
      from: "../frontend/node_modules/@fontsource/ibm-plex-mono/files",
      to: "fonts",
      filter: ["ibm-plex-mono-latin-400-normal.woff2", "ibm-plex-mono-latin-500-normal.woff2"],
    },
  ]);
  assert.equal(pkg.build.asar, true);
  assert.equal(pkg.build.npmRebuild, false);
  assert.equal(pkg.build.publish, null);
});

test("build carries the compiled server, the brand mark and the third-party notices as extra resources", () => {
  assert.deepEqual(pkg.build.extraResources, [
    { from: "../dist/compiled/viki-agent.dist", to: "viki-agent" },
    { from: "../frontend/public/brand/mark.png", to: "icon.png" },
    { from: "../dist/notices/THIRD-PARTY-NOTICES.txt", to: "THIRD-PARTY-NOTICES.txt" },
  ]);
});

// -- build: fuses (ADR-053) ---------------------------------------------------------------------

test("build hardens every Electron fuse", () => {
  assert.deepEqual(pkg.build.electronFuses, {
    runAsNode: false,
    enableCookieEncryption: true,
    enableNodeOptionsEnvironmentVariable: false,
    enableNodeCliInspectArguments: false,
    enableEmbeddedAsarIntegrityValidation: true,
    onlyLoadAppFromAsar: true,
    grantFileProtocolExtraPrivileges: false,
    resetAdHocDarwinSignature: true,
  });
});

// -- build: per-OS targets ----------------------------------------------------------------------

test("build targets Windows with a one-click, per-user NSIS installer", () => {
  assert.deepEqual(pkg.build.win, {
    target: [{ target: "nsis", arch: ["x64"] }],
    icon: "../frontend/public/favicon.ico",
  });
  assert.deepEqual(pkg.build.nsis, {
    oneClick: true,
    perMachine: false,
    deleteAppDataOnUninstall: false,
  });
});

test("build targets macOS with a dmg and zip, hardened and entitled", () => {
  assert.deepEqual(pkg.build.mac, {
    target: [
      { target: "dmg", arch: ["arm64"] },
      { target: "zip", arch: ["arm64"] },
    ],
    icon: "../frontend/public/brand/mark.png",
    category: "public.app-category.productivity",
    hardenedRuntime: true,
    entitlements: "build/entitlements.mac.plist",
    entitlementsInherit: "build/entitlements.mac.plist",
    extendInfo: {
      NSMicrophoneUsageDescription:
        "VIKI Agent uses the microphone only while you dictate a message or hold a hands-free conversation.",
      NSDocumentsFolderUsageDescription:
        "VIKI Agent and the Claude Code it runs for you read and save files in the folders you choose.",
      NSDesktopFolderUsageDescription:
        "VIKI Agent and the Claude Code it runs for you read and save files in the folders you choose.",
      NSDownloadsFolderUsageDescription:
        "VIKI Agent and the Claude Code it runs for you read and save files in the folders you choose.",
      NSRemovableVolumesUsageDescription:
        "VIKI Agent and the Claude Code it runs for you read and save files in the folders you choose.",
      NSNetworkVolumesUsageDescription:
        "VIKI Agent and the Claude Code it runs for you read and save files in the folders you choose.",
    },
  });
});

test("build targets Linux with a deb only, named and described", () => {
  assert.deepEqual(pkg.build.linux, {
    target: [{ target: "deb" }],
    icon: "../frontend/public/brand/mark.png",
    executableName: "viki-agent",
    category: "Office",
    maintainer: "VIKI Agent <support@viki-agent.invalid>",
    description: "A local chat for your notes vault that runs your own Claude Code.",
  });
  assert.equal(pkg.build.appImage, undefined, "there is no AppImage target");
  assert.equal(pkg.build.toolsets, undefined, "no toolset override is needed without an AppImage");
});

test("the deb depends on the programs the app runs on Linux, besides electron-builder's own", (t) => {
  if (!existsSync(join(DESKTOP_ROOT, "node_modules"))) {
    t.skip("desktop/node_modules is not installed");
    return;
  }
  // Setting `depends` replaces electron-builder's deb defaults, so they are listed first, read
  // from the copy of app-builder-lib the pinned electron-builder builds with: an upgrade that
  // adds a library to them fails here until package.json lists it too.
  const desktopRequire = createRequire(join(DESKTOP_ROOT, "package.json"));
  const builderRequire = createRequire(desktopRequire.resolve("electron-builder/package.json"));
  const FpmTarget = builderRequire("app-builder-lib/out/targets/FpmTarget.js").default;
  const electronBuilderDefaults = FpmTarget.prototype.getDefaultDepends("deb");
  assert.ok(electronBuilderDefaults.includes("libgtk-3-0"), "the builder's deb defaults were read");
  assert.deepEqual(pkg.build.deb, {
    depends: [
      ...electronBuilderDefaults,
      "libsecret-tools", // secret-tool: keeps the sealing key in the desktop keyring
      "git",
      "zenity | kdialog", // the folder picker
    ],
  });
});

test("native installer arguments select the host architecture on each supported host", () => {
  for (const [platform, arch] of [
    ["win32", "x64"],
    ["darwin", "arm64"],
    ["linux", "x64"],
    ["linux", "arm64"],
  ]) {
    assert.deepEqual(dist.nativeBuildArgs([], platform, arch), [`--${arch}`]);
    assert.deepEqual(dist.nativeBuildArgs(["--dir"], platform, arch), ["--dir", `--${arch}`]);
    assert.deepEqual(dist.nativeBuildArgs([`--${arch}`], platform, arch), [`--${arch}`]);
  }
});

test("native installer arguments accept only the host platform and architecture", () => {
  for (const [platform, arch, flag] of [
    ["win32", "x64", "--win"],
    ["darwin", "arm64", "--mac"],
    ["linux", "x64", "--linux"],
    ["linux", "arm64", "--linux"],
  ]) {
    const args = [flag, `--${arch}`, "--dir"];
    assert.deepEqual(dist.nativeBuildArgs(args, platform, arch), args);
    assert.deepEqual(args, [flag, `--${arch}`, "--dir"], "caller arguments stay unchanged");
    for (const foreign of ["--win", "--mac", "--linux"].filter((value) => value !== flag)) {
      assert.throws(() => dist.nativeBuildArgs([flag, foreign], platform, arch), /native platform/);
    }
    const foreignArch = arch === "x64" ? "--arm64" : "--x64";
    assert.throws(() => dist.nativeBuildArgs([foreignArch], platform, arch), /native architecture/);
    assert.throws(
      () => dist.nativeBuildArgs([`--${arch}`, foreignArch], platform, arch),
      /native architecture/,
    );
  }
});

test("native installer arguments refuse unsupported host combinations", () => {
  for (const [platform, arch] of [
    ["win32", "arm64"],
    ["darwin", "x64"],
    ["linux", "ia32"],
    ["linux", "riscv64"],
    ["freebsd", "x64"],
  ]) {
    assert.throws(() => dist.nativeBuildArgs([], platform, arch), /unsupported native build host/);
  }
});

test("dist refuses a foreign target before looking for the compiled backend", () => {
  const repo = mkdtempSync(join(tmpdir(), "viki-native-build-"));
  try {
    const build = join(repo, "desktop", "build");
    mkdirSync(build, { recursive: true });
    for (const name of [
      "dist",
      "artifacts",
      "licences",
      "notices",
      "release-files",
      "magpie-build",
    ]) {
      writeFileSync(
        join(build, `${name}.mjs`),
        readFileSync(join(DESKTOP_ROOT, "build", `${name}.mjs`)),
      );
    }
    writeFileSync(
      join(build, "magpie-release.json"),
      readFileSync(join(DESKTOP_ROOT, "build", "magpie-release.json")),
    );
    mkdirSync(join(repo, "scripts"));
    writeFileSync(
      join(repo, "scripts", "private-files.json"),
      readFileSync(join(REPO_ROOT, "scripts", "private-files.json")),
    );
    writeFileSync(join(repo, "desktop", "package.json"), JSON.stringify(pkg));
    const foreign = process.platform === "win32" ? "--linux" : "--win";
    const result = spawnSync(process.execPath, [join(build, "dist.mjs"), foreign], {
      encoding: "utf8",
      env: {},
      timeout: HANG_GUARD_MS, // a guard on the short refusal process, not a build timeout
    });
    assert.equal(result.status, 2, result.stderr);
    assert.match(result.stderr, /native platform/);
    assert.doesNotMatch(result.stderr, /compiled server|signing:|notices:/);
  } finally {
    rmSync(repo, { recursive: true, force: true });
  }
});

// -- goProblems --------------------------------------------------------------------------------

test("goProblems finds nothing wrong while publish stays null", () => {
  assert.deepEqual(goProblems(pkg), []);
});

test("goProblems flags an unsigned, still-placeholder feed", () => {
  const feedPkg = { ...pkg, build: { ...pkg.build, publish: { provider: "github" } } };
  assert.deepEqual(goProblems(feedPkg), [
    "forceCodeSigning is not true while a publish feed is configured",
    "homepage, author or build still names a .invalid placeholder",
  ]);
});

test("goProblems clears once code signing is forced and the identity is real", () => {
  const readyPkg = {
    ...pkg,
    author: "Acme",
    homepage: "https://viki-agent.example",
    build: {
      ...pkg.build,
      publish: { provider: "github" },
      forceCodeSigning: true,
      linux: { ...pkg.build.linux, maintainer: "Acme <support@example.com>" },
    },
  };
  assert.deepEqual(goProblems(readyPkg), []);
});
