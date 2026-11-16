/**
 * The real backend, for the end-to-end run: `scripts/e2e_server.py --scratch`, which is the
 * launcher's own assembly minus its window, serving a throwaway vault and home so a test never
 * reads the user's transcripts.
 *
 * Everything awkward lives on the Python side on purpose — the scratch directories, the fake
 * CLI's wiring, the vault it needs to exist at all — so this file only spawns a process and
 * waits for it. The one thing node owns is the token, because the server refuses to start
 * without it and a test is the only thing that may choose one.
 *
 * A run starts one server per test file, so the port is checked BEFORE spawning too: nothing
 * may already be answering. Without that, a server that outlived its stop() would be adopted by
 * the next file — its 200 is indistinguishable from ours — and that file would quietly drive the
 * previous fixture. stop() therefore waits for the port to go quiet, not just for the process.
 *
 * Readiness is a request, not a printed line: "listening" is printed only once the port is bound,
 * but it says nothing about the lifespan having finished or whose token the server holds.
 * Polling an authenticated route settles both questions at once —
 * whether the server is up, and whether the server answering on 8765 is this one. A real launch
 * holding the port would answer 401 to our token, and that is worth saying plainly rather than
 * letting a test drive the user's live app.
 */
import { spawn } from "node:child_process";
import { TEST_TOKEN } from "./harness";

/** The launcher's fixed port. There is no fallback, by design (hard rule 7 pins host and port). */
export const PORT = 8765;
export const ORIGIN = `http://127.0.0.1:${PORT}`;

/** The repo root, relative to frontend/, which is where vitest runs. PYTHON is resolved
 * against that cwd rather than against ours, which is why it carries no "../". */
const REPO = "..";
const PYTHON = process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python";

export interface Server {
  /** Kills the server and prints what it wrote to stderr. On Windows the launcher's Job object kills its children too; on POSIX
   * each fake CLI exits when its stdin closes with the server. */
  stop(): Promise<void>;
}

export interface StartOptions {
  /** A replay script under tests/fixtures/claude, repo-relative. Ignored when `live` is set. */
  fixture?: string;
  /** Make the scratch vault a real git repo with one uncommitted file. The only way to reach
   * anything that talks about uncommitted changes: rails.py runs real git, so with no repo
   * `dirty` is always 0 and the header's git chip has nothing dirty to show. */
  dirty?: boolean;
  /** Serve the running user's own vault, home and real CLI instead of a scratch one. Only the
   * live smoke run passes this: it spends real subscription quota and writes a real transcript. */
  live?: boolean;
  /** Register and select nothing, so the launch is chat-only and the onboarding view is
   * reachable — the shape of a first launch. The scratch vault folder is still created, so a
   * test can still open it as an existing folder. */
  noVault?: boolean;
  /** Leave an unreadable app store behind, so the launch serves the read-only recovery shell
   * instead of the app: no socket, no sessions, no rails, no data routes. */
  damagedStore?: boolean;
  /** Report git ready, so a code folder's Git routes (changes, diff, search) run real git instead
   * of answering 503 `git_unavailable`. The workspace service's identity probes stay off. */
  git?: boolean;
  /** Install fake dictation models and run the scripted voice worker (`--voice`, with scratch). */
  voice?: boolean;
  /** Bootstrap an ordinary scratch vault for Bot tests with a repository synthetic fixture. */
  bots?: boolean;
  /** With `bots`: bootstrap the Start with VIKI template, so the vault holds the Kanban pack. */
  team?: boolean;
  /** Use the reviewed CLI version for a synthetic model connection fixture. */
  modelConnections?: boolean;
  timeoutMs?: number;
}

export async function startServer({
  fixture,
  dirty = false,
  live = false,
  noVault = false,
  damagedStore = false,
  git = false,
  voice = false,
  bots = false,
  team = false,
  modelConnections = false,
  timeoutMs = 30_000,
}: StartOptions): Promise<Server> {
  if (bots && live) throw new Error("Bot tests require scratch mode; live + bots is refused.");
  if (team && !bots) throw new Error("Team vaults need bots mode.");
  if (modelConnections && (live || !fixture))
    throw new Error("Model connection tests require scratch mode and a synthetic fixture.");
  // Nothing may be answering before we spawn. A run starts a server per test file, so a
  // previous one that outlived its stop() would otherwise be adopted here: its 200 looks exactly
  // like ours, the child we just spawned would fail to bind and die unnoticed, and the test would
  // drive the PREVIOUS file's fixture and fail with something that reads like a UI bug. The port
  // is fixed with no fallback (hard rule 7), so "free before we start" is the
  // only thing that makes a later 200 proof of our own server.
  const before = await probe(damagedStore);
  if (before !== 0) {
    throw new Error(
      `Port ${PORT} is already serving (${before === 401 ? "someone else's VIKI" : "an earlier e2e server that did not exit"}) — ` +
        `close VIKI Agent, or let the previous test file's stop() finish, and run again.`,
    );
  }
  const args = live
    ? ["scripts/e2e_server.py"]
    : [
        "scripts/e2e_server.py",
        "--scratch",
        ...(dirty ? ["--dirty"] : []),
        ...(noVault ? ["--no-vault"] : []),
        ...(damagedStore ? ["--damaged-store"] : []),
        ...(git ? ["--git"] : []),
        ...(voice ? ["--voice"] : []),
        ...(bots ? ["--bots"] : []),
        ...(team ? ["--bots-team"] : []),
        ...(modelConnections ? ["--model-connections"] : []),
        "--fixture",
        fixture ?? "",
      ];
  const child = spawn(PYTHON, args, {
    cwd: REPO,
    env: { ...process.env, VIKI_TEST_TOKEN: TEST_TOKEN, PYTHONUTF8: "1" },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });

  let stderr = "";
  let exited: number | null | undefined;
  child.stderr?.on("data", (chunk) => {
    stderr += String(chunk);
  });
  child.on("exit", (code) => {
    exited = code;
  });

  const stop = async (): Promise<void> => {
    child.kill();
    // Wait for the process AND for the port to go quiet. The exit alone is not enough: the next
    // file's startServer treats any 200 as its own server, so a socket still accepting here would
    // be adopted there. Ten seconds is far longer than a kill takes; if it is still answering
    // after that, the next file would fail confusingly, so fail here where the cause is visible.
    for (let i = 0; i < 100 && exited === undefined; i++) await sleep(100);
    // The only record of what the server refused; the live smoke run keeps its output as it was.
    if (!live && stderr.trim() !== "")
      console.error(`e2e server stderr (${fixture || "no fixture"}):\n${stderr.trimEnd()}`);
    for (let i = 0; i < 100; i++) {
      if ((await probe(damagedStore)) === 0) return;
      await sleep(100);
    }
    throw new Error(`the e2e server on ${ORIGIN} did not stop answering after being killed`);
  };

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (exited !== undefined) {
      throw new Error(`e2e server exited with ${exited} before serving\n${stderr}`);
    }
    const status = await probe(damagedStore);
    if (status === 200) return { stop };
    if (status === 401) {
      await stop().catch(() => {});
      throw new Error(
        `Something else is already serving ${ORIGIN} and it is not this harness — close ` +
          `VIKI Agent (or whatever holds port ${PORT}) and run the end-to-end suite again.`,
      );
    }
    await sleep(150);
  }
  await stop().catch(() => {});
  throw new Error(`e2e server did not answer within ${timeoutMs}ms\n${stderr}`);
}

/** 200 = ours, 401 = someone else's, 0 = nothing listening yet.
 *
 * `/api/rails` answers in both modes: with no vault it is served against the scratch working
 * directory the launch resolved for itself, exactly as a first launch does. */
async function probe(recovery = false): Promise<number> {
  try {
    // A recovery app mounts no rails route; capabilities is the one authenticated read both
    // shells answer, so the "is it ours" question stays the same question in both modes.
    const response = await fetch(`${ORIGIN}${recovery ? "/api/v2/capabilities" : "/api/rails"}`, {
      headers: { Authorization: `Bearer ${TEST_TOKEN}` },
      // A harness that binds but then stalls would hold fetch open past the start deadline.
      signal: AbortSignal.timeout(2_000),
    });
    return response.status;
  } catch {
    return 0;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}
