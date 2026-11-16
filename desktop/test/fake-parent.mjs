/**
 * A stand-in for `uv run ...` sitting between the app and the real server: spawns the leaf
 * (this folder's fake-server.mjs, normally FAKE_MODE=stubborn) as its own child -- in the same
 * process group, since it starts no session of its own -- forwards SIGTERM to it, and does not
 * exit on that signal itself, only once the leaf has exited: a well-behaved wrapper that waits
 * on the command it ran instead of dying out from under it. Used by server.test.mjs's
 * process-tree test to prove startServer()'s stop() ends the whole tree, not just this
 * direct child.
 *
 * Writes {"parent":<this process's pid>,"leaf":<the leaf's pid>} to the file
 * FAKE_PARENT_PID_FILE names once the leaf has started: a side channel kept out of the stdout
 * JSON-lines protocol the real server.mjs parses, so it is never seen as a stray message.
 */
import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));

const leaf = spawn(process.execPath, [join(HERE, "fake-server.mjs")], {
  env: process.env,
  stdio: "ignore",
  // Windows only: a plain Node spawn there wraps the child in a job object that cascades a
  // kill down to whatever it starts, which would hide the bug under test -- the real uv has no
  // such protection for its own child. POSIX needs no such thing: this leaf must stay in
  // fake-parent's process group, the same as a real server left ungrouped by uv, for stop()'s
  // group-wide SIGKILL to be what reaches it.
  detached: process.platform === "win32",
});

writeFileSync(
  process.env.FAKE_PARENT_PID_FILE,
  JSON.stringify({ parent: process.pid, leaf: leaf.pid }),
);

leaf.on("exit", (code, signal) => {
  process.exit(signal ? 1 : (code ?? 0));
});

// A cooperative "uv": pass the signal on, but do not exit because of it -- only the leaf's own
// "exit" above ends this process, the way a wrapper that waits on its command behaves.
process.on("SIGTERM", () => {
  leaf.kill("SIGTERM");
});
