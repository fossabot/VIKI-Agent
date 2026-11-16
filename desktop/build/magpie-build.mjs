/** Verify the separately pinned sidecar before packaging it with the compiled server. */
import { createHash } from "node:crypto";
import { lstatSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { gunzipSync } from "node:zlib";

const RELEASE = JSON.parse(readFileSync(new URL("./magpie-release.json", import.meta.url), "utf8"));
const sha256 = (data) => createHash("sha256").update(data).digest("hex");

/** @param {string} directory @param {string} platform @param {string} arch */
export function readMagpieBundle(directory, platform, arch) {
  const asset = RELEASE.assets[`${platform}/${arch}`];
  if (!asset) throw new Error("unsupported Magpie build target");
  if (lstatSync(directory).isSymbolicLink()) throw new Error("link in Magpie bundle");
  const binary = platform === "win32" ? "magpie.exe" : "magpie";
  const names = [
    "magpie.gz",
    "magpie.json",
    "LICENSE",
    "THIRD_PARTY_NOTICES",
    "GO-DEPENDENCY-NOTICES.txt",
    "sbom-magpie.cdx.json",
  ];
  if (JSON.stringify(readdirSync(directory).sort()) !== JSON.stringify(names.sort())) {
    throw new Error("unexpected or missing Magpie bundle files");
  }
  for (const name of names) {
    const stat = lstatSync(join(directory, name));
    if (!stat.isFile() || stat.isSymbolicLink())
      throw new Error("link or directory in Magpie bundle");
  }
  const manifest = JSON.parse(readFileSync(join(directory, "magpie.json"), "utf8"));
  const expected = {
    version: RELEASE.version,
    source_commit: RELEASE.source_commit,
    platform,
    arch,
    sha256: asset.sha256,
    binary,
  };
  if (
    Object.keys(manifest).length !== Object.keys(expected).length ||
    Object.entries(expected).some(([key, value]) => manifest[key] !== value)
  ) {
    throw new Error("Magpie manifest differs from the pinned release");
  }
  const payload = readFileSync(join(directory, "magpie.gz"));
  if (payload.length > asset.size + 65536) throw new Error("oversized Magpie compressed payload");
  let bytes;
  try {
    bytes = gunzipSync(payload, { maxOutputLength: asset.size });
  } catch {
    throw new Error("invalid Magpie compressed executable");
  }
  if (bytes.length !== asset.size || sha256(bytes) !== asset.sha256) {
    throw new Error("Magpie executable differs from the pinned release");
  }
  const notices = [];
  for (const [name, digest] of Object.entries(RELEASE.notices)) {
    const text = readFileSync(join(directory, name));
    if (sha256(text) !== digest) throw new Error(`Magpie checksum mismatch: ${name}`);
    notices.push(`---- Magpie ${RELEASE.version}: ${name} ----\n${text.toString("utf8")}`);
  }
  const dependencyNotices = readFileSync(join(directory, "GO-DEPENDENCY-NOTICES.txt"));
  if (sha256(dependencyNotices) !== RELEASE.dependency_notices[`${platform}/${arch}`]) {
    throw new Error("Magpie dependency notices differ from the pinned release");
  }
  notices.push(dependencyNotices.toString("utf8"));
  const inventory = readFileSync(join(directory, "sbom-magpie.cdx.json"));
  if (sha256(inventory) !== RELEASE.sbom_sha256[`${platform}/${arch}`]) {
    throw new Error("Magpie SBOM differs from the pinned release");
  }
  const sbom = JSON.parse(inventory.toString("utf8"));
  if (
    sbom.bomFormat !== "CycloneDX" ||
    sbom.metadata?.component?.version !== RELEASE.version ||
    !sbom.metadata.component.hashes?.some(
      (hash) => hash.alg === "SHA-256" && hash.content === asset.sha256,
    ) ||
    !Array.isArray(sbom.components) ||
    sbom.components.length === 0
  ) {
    throw new Error("invalid Magpie executable SBOM");
  }
  return { manifest, notices: `\n\n${notices.join("\n\n")}\n`, sbom };
}
