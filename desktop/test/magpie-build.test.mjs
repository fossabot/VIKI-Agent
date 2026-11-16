import assert from "node:assert/strict";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { gzipSync } from "node:zlib";
import { writeMagpieFixture } from "./magpie-build-fixture.mjs";

test("packaging checks the pinned Magpie manifest, executable, notices and inventory", async () => {
  const repo = mkdtempSync(join(tmpdir(), "viki-magpie-build-"));
  try {
    const source = join(dirname(fileURLToPath(import.meta.url)), "..", "build");
    const build = join(repo, "desktop", "build");
    mkdirSync(build, { recursive: true });
    for (const name of ["magpie-build.mjs", "magpie-release.json"]) {
      copyFileSync(join(source, name), join(build, name));
    }
    const { directory, manifest, sbom } = writeMagpieFixture(repo, "win32", "x64");
    const { readMagpieBundle } = await import(pathToFileURL(join(build, "magpie-build.mjs")));
    const result = readMagpieBundle(directory, "win32", "x64");
    assert.deepEqual(result.manifest, manifest);
    assert.deepEqual(result.sbom, sbom);
    assert.match(result.notices, /synthetic Magpie MIT/);
    assert.match(result.notices, /synthetic notices/);
    assert.throws(() => readFileSync(join(directory, "magpie.exe")), /ENOENT/);
    const binary = join(directory, "magpie.gz");
    const original = readFileSync(binary);
    writeFileSync(binary, gzipSync("changed"));
    assert.throws(() => readMagpieBundle(directory, "win32", "x64"), /executable/);
    writeFileSync(binary, gzipSync("x".repeat(1000)));
    assert.throws(() => readMagpieBundle(directory, "win32", "x64"), /compressed executable/);
    writeFileSync(binary, original);
    writeFileSync(join(directory, "LICENSE"), "changed");
    assert.throws(() => readMagpieBundle(directory, "win32", "x64"), /checksum/);
    writeMagpieFixture(repo, "win32", "x64");
    writeFileSync(
      join(directory, "sbom-magpie.cdx.json"),
      JSON.stringify({ ...sbom, components: [{ name: "invented", version: "9.9.9" }] }),
    );
    assert.throws(() => readMagpieBundle(directory, "win32", "x64"), /SBOM differs/);
    writeMagpieFixture(repo, "win32", "x64");
    writeFileSync(join(directory, "magpie.json"), JSON.stringify({ ...manifest, arch: "arm64" }));
    assert.throws(() => readMagpieBundle(directory, "win32", "x64"), /manifest/);
    assert.throws(() => readMagpieBundle(directory, "win32", "arm64"), /unsupported/);
  } finally {
    rmSync(repo, { recursive: true, force: true });
  }
});
