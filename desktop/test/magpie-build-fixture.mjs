import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { gzipSync } from "node:zlib";

/** A synthetic pinned sidecar; nothing here downloads or starts Magpie.
 * @param {string} repo @param {string} platform @param {string} arch
 */
export function writeMagpieFixture(repo, platform, arch) {
  const build = join(repo, "desktop", "build");
  const release = JSON.parse(readFileSync(join(build, "magpie-release.json"), "utf8"));
  const sha256 = (text) => createHash("sha256").update(text).digest("hex");
  const data = "synthetic Magpie executable";
  release.assets[`${platform}/${arch}`] = { size: data.length, sha256: sha256(data) };
  const notices = { LICENSE: "synthetic Magpie MIT", THIRD_PARTY_NOTICES: "synthetic notices" };
  release.notices = Object.fromEntries(
    Object.entries(notices).map(([name, text]) => [name, sha256(text)]),
  );
  release.dependency_notices[`${platform}/${arch}`] = sha256("synthetic dependency notices");
  const directory = join(repo, "dist", "compiled", "viki-agent.dist", "magpie");
  mkdirSync(directory, { recursive: true });
  const binary = platform === "win32" ? "magpie.exe" : "magpie";
  const manifest = {
    version: release.version,
    source_commit: release.source_commit,
    platform,
    arch,
    sha256: sha256(data),
    binary,
  };
  writeFileSync(join(directory, "magpie.gz"), gzipSync(data));
  writeFileSync(join(directory, "magpie.json"), JSON.stringify(manifest));
  for (const [name, text] of Object.entries(notices)) writeFileSync(join(directory, name), text);
  writeFileSync(join(directory, "GO-DEPENDENCY-NOTICES.txt"), "synthetic dependency notices");
  const sbom = {
    bomFormat: "CycloneDX",
    metadata: {
      component: {
        version: release.version,
        hashes: [{ alg: "SHA-256", content: manifest.sha256 }],
      },
    },
    components: [{ name: "example.invalid/synthetic-dependency", version: "v1.2.3" }],
  };
  writeFileSync(join(directory, "sbom-magpie.cdx.json"), JSON.stringify(sbom));
  release.sbom_sha256[`${platform}/${arch}`] = sha256(JSON.stringify(sbom));
  writeFileSync(join(build, "magpie-release.json"), JSON.stringify(release));
  return { directory, manifest, sbom };
}
