import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import {
  certificateBundleProblem,
  checkCompiledTree,
  privatePathProblem,
} from "../build/release-files.mjs";

const CERTIFICATES = Buffer.from(
  "# synthetic public bundle\n-----BEGIN CERTIFICATE-----\nU3ludGhldGlj\n-----END CERTIFICATE-----\n",
);

test("certificate line grammar refuses malformed and space-heavy input", () => {
  for (const spaces of [1000, 2000]) {
    for (const payload of [
      `-----BEGIN CERTIFICATE-----\n${" ".repeat(spaces)}!`,
      `-----BEGIN CERTIFICATE-----\n${" ".repeat(spaces)}-----END CERTIFICATE-----\n`,
    ]) {
      assert.equal(
        certificateBundleProblem(Buffer.from(payload)),
        "invalid public certificate bundle",
      );
    }
  }
  for (const body of ["U3lu=\ndGhldGlj", "U3l", "U3lu dGhldGlj", "-----BEGIN CERTIFICATE-----"]) {
    assert.equal(
      certificateBundleProblem(
        Buffer.from(`-----BEGIN CERTIFICATE-----\n${body}\n-----END CERTIFICATE-----\n`),
      ),
      "invalid public certificate bundle",
    );
  }
  const multiple = `${CERTIFICATES.toString()}\n# another public certificate\n${CERTIFICATES.toString()}`;
  assert.equal(certificateBundleProblem(Buffer.from(multiple.replaceAll("\n", "\r\n"))), null);
});

test("release filename rules reject private paths and keep public build inputs", () => {
  for (const name of [
    ".env",
    ".env.local",
    "config/.ENV.production",
    "production.env",
    "production.env.local",
    "credentials.json",
    "." + "credentials",
    "config/client-credentials.json",
    "state.json",
    "sealing-key-id",
    "session.db",
    "store.sqlite",
    "store.sqlite3-wal",
    "store.db-shm",
    "store.sqlite-journal",
    "private.pem",
    "client.key",
    "signing.p8",
    "cert.p12",
    "cert.pfx",
    "debug.log",
    "assets/app.js.map",
    "nested/../file",
    "/absolute",
    "directory\\file",
    "C:/file",
  ]) {
    assert.ok(privatePathProblem(name), name);
  }
  for (const name of [
    "support.dll",
    "data.bin",
    "dot-claude/settings.json",
    ".claude/agents/review.md",
    "certifi/cacert.pem",
  ]) {
    assert.equal(privatePathProblem(name), null, name);
  }
});

test("certificate exception permits only bounded certificate PEM blocks and comments", () => {
  assert.equal(certificateBundleProblem(CERTIFICATES), null);
  for (const payload of [
    Buffer.from(""),
    Buffer.from("synthetic secret"),
    Buffer.concat([
      CERTIFICATES,
      Buffer.from(
        "# -----BEGIN RSA PRIVATE KEY-----\n# U3ludGhldGlj\n# -----END RSA PRIVATE KEY-----",
      ),
    ]),
    Buffer.concat([
      CERTIFICATES,
      Buffer.from("-----BEGIN PRIVATE KEY-----\nU3ludGhldGlj\n-----END PRIVATE KEY-----"),
    ]),
    Buffer.alloc(2_097_153, "x"),
  ]) {
    assert.match(certificateBundleProblem(payload), /public certificate bundle/);
  }
});

test("compiled tree accepts support binaries and the public certifi bundle", () => {
  const root = mkdtempSync(join(tmpdir(), "release-files-"));
  try {
    writeFileSync(join(root, "support.dll"), "binary");
    mkdirSync(join(root, "certifi"));
    writeFileSync(join(root, "certifi", "cacert.pem"), CERTIFICATES);
    checkCompiledTree(root);
    writeFileSync(join(root, "certifi", "cacert.pem"), "synthetic secret");
    assert.throws(() => checkCompiledTree(root), /invalid public certificate bundle/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("compiled tree refuses private state anywhere", () => {
  const root = mkdtempSync(join(tmpdir(), "release-files-"));
  try {
    mkdirSync(join(root, "nested"));
    writeFileSync(join(root, "nested", "state.json"), "synthetic private state");
    assert.throws(() => checkCompiledTree(root), /private release filename/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("compiled framework links stay inside the tree without a directory-link traversal", (t) => {
  const root = mkdtempSync(join(tmpdir(), "release-files-"));
  try {
    const version = join(root, "Framework", "Versions", "A");
    mkdirSync(version, { recursive: true });
    writeFileSync(join(version, "binary"), "public binary");
    try {
      symlinkSync(
        version,
        join(root, "Framework", "Versions", "Current"),
        process.platform === "win32" ? "junction" : "dir",
      );
    } catch {
      t.skip("symlink creation unavailable");
      return;
    }
    checkCompiledTree(root);
    symlinkSync(root, join(root, "root-alias"), process.platform === "win32" ? "junction" : "dir");
    assert.throws(() => checkCompiledTree(root), /outside compiled release link/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("compiled directory links refuse outside targets and root aliases", () => {
  const outer = mkdtempSync(join(tmpdir(), "release-files-"));
  const root = join(outer, "dist");
  mkdirSync(root);
  try {
    symlinkSync(outer, join(root, "outside"), process.platform === "win32" ? "junction" : "dir");
    assert.throws(() => checkCompiledTree(root), /outside compiled release link/);
    assert.throws(() => checkCompiledTree(join(root, "outside")), /compiled release root/);
  } finally {
    rmSync(outer, { recursive: true, force: true });
  }
});
