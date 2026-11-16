/** Filename checks for the standalone tree copied by electron-builder. */
import {
  closeSync,
  lstatSync,
  openSync,
  readdirSync,
  readFileSync,
  readSync,
  realpathSync,
  statSync,
} from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const rules = JSON.parse(
  readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), "../../scripts/private-files.json"),
    "utf8",
  ),
);
const PRIVATE = rules.private_patterns.map((pattern) => new RegExp(pattern));
const CERTIFICATE_LIMIT = rules.certificate_limit;
const PRIVATE_DIRECTORIES = new Set(rules.private_directories);

/** @param {string} name @returns {string | null} */
export function privatePathProblem(name) {
  const parts = name.replace(/\/$/, "").split("/");
  if (parts.some((part) => ["", ".", ".."].includes(part) || /[\\:]/.test(part))) {
    return "unsafe release path";
  }
  if (
    name !== "certifi/cacert.pem" &&
    parts.some((part) => PRIVATE.some((p) => p.test(part.toLowerCase())))
  ) {
    return "private release filename";
  }
  return null;
}

/** @param {Buffer} data @returns {string | null} */
export function certificateBundleProblem(data) {
  if (data.length > CERTIFICATE_LIMIT) return "oversized public certificate bundle";
  if (/-----(?:BEGIN|END) [A-Z0-9 ]*PRIVATE KEY-----/i.test(data.toString("latin1"))) {
    return "invalid public certificate bundle";
  }
  let blocks = 0;
  let body = null;
  for (const rawLine of data.toString("latin1").split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    if (line === "-----BEGIN CERTIFICATE-----" && body === null) {
      body = [];
    } else if (line === "-----END CERTIFICATE-----" && body !== null) {
      const payload = body.join("");
      if (!/^[A-Za-z0-9+/]+={0,2}$/.test(payload) || payload.length % 4)
        return "invalid public certificate bundle";
      blocks += 1;
      body = null;
    } else if (body !== null && /^[A-Za-z0-9+/]+={0,2}$/.test(line)) {
      body.push(line);
    } else {
      return "invalid public certificate bundle";
    }
  }
  return blocks && body === null ? null : "invalid public certificate bundle";
}

/** @param {string} root @param {string} target */
function contained(root, target) {
  const name = relative(root, target);
  return name !== "" && name !== ".." && !name.startsWith(`..${sep}`) && !isAbsolute(name);
}

/** Permit contained Nuitka framework links without following directory links during the walk.
 * @param {string} root
 */
export function checkCompiledTree(root) {
  root = resolve(root);
  if (lstatSync(root).isSymbolicLink() || relative(root, realpathSync(root)) !== "") {
    throw new Error("link in compiled release root");
  }
  const pending = [root];
  while (pending.length) {
    for (const entry of readdirSync(pending.pop(), { withFileTypes: true })) {
      const file = join(entry.parentPath, entry.name);
      const name = relative(root, file).split(sep).join("/");
      const problem = privatePathProblem(name);
      if (problem) throw new Error(`${problem}: ${name}`);
      if (name.split("/").some((part) => PRIVATE_DIRECTORIES.has(part.toLowerCase()))) {
        throw new Error(`private release directory: ${name}`);
      }
      const linked = lstatSync(file).isSymbolicLink();
      let target;
      try {
        target = linked ? realpathSync(file) : file;
      } catch {
        throw new Error(`dangling compiled release link: ${name}`);
      }
      if (!contained(root, target)) throw new Error(`outside compiled release link: ${name}`);
      const targetProblem = privatePathProblem(relative(root, target).split(sep).join("/"));
      if (targetProblem) throw new Error(`${targetProblem}: ${name}`);
      if (
        relative(root, target)
          .split(sep)
          .some((part) => PRIVATE_DIRECTORIES.has(part.toLowerCase()))
      ) {
        throw new Error(`private release directory: ${name}`);
      }
      const type = statSync(target);
      if (type.isDirectory()) {
        if (!linked) pending.push(file);
      } else if (type.isFile()) {
        if (name === "certifi/cacert.pem") {
          const fd = openSync(file, "r");
          let certificateProblem;
          try {
            const data = Buffer.alloc(CERTIFICATE_LIMIT + 1);
            const length = readSync(fd, data, 0, data.length, null);
            certificateProblem = certificateBundleProblem(data.subarray(0, length));
          } finally {
            closeSync(fd);
          }
          if (certificateProblem) throw new Error(`${certificateProblem}: ${name}`);
        }
      } else {
        throw new Error(`unexpected compiled release file: ${name}`);
      }
    }
  }
}
