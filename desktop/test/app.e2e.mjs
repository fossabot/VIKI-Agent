/**
 * Desktop app end to end, run by `npm run e2e`: starts the real app, which starts the real
 * server, and checks both from outside, through Electron's remote debugging port. With
 * VIKI_PACKAGED_APP set, it drives that installed exe instead of running this checkout under
 * Electron: no DESKTOP folder argument, the compiled backend in place of `uv run`, and (win32,
 * darwin) a one-byte-tampered app.asar must stop the app before it ever opens a window. Prints
 * one line per check, never a URL, token or command line. Exits 0 when every check passes and 1
 * at the first that fails, after killing what it started. Exits 2 before starting anything
 * outside CI or while port 8765 accepts a connection, so it never wakes or reads a VIKI Agent
 * already running there.
 */

import { execFile, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { connect, createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { fileURLToPath } from "node:url";

import { APP_ORIGIN, isAppUrl } from "../policy.mjs";
import { packagedPaths } from "./packaged-paths.mjs";

const DESKTOP = dirname(dirname(fileURLToPath(import.meta.url)));
const REPO = dirname(DESKTOP);
const SERVER_PORT = Number(new URL(APP_ORIGIN).port);
const TOKEN_IN_ARGV = /token=[A-Za-z0-9_-]{20,}/; // a launch token on a command line
const GUARD_MS = 15 * 60_000;
// Packaged mode: VIKI_PACKAGED_APP names the installed exe to drive instead of
// running this checkout under Electron. PACKAGED is null in source mode.
const PACKAGED_APP = process.env.VIKI_PACKAGED_APP;
const PACKAGED = PACKAGED_APP ? packagedPaths(PACKAGED_APP, process.platform) : null;
// Runs in the page; it returns the status and whether the socket opened, never the token.
const AUTH_PROBE = `(async () => {
  const token = sessionStorage.getItem("viki.token");
  const response = await fetch("/api/sessions", { headers: { Authorization: "Bearer " + token } });
  const opened = await new Promise((resolve) => {
    const socket = new WebSocket("ws://127.0.0.1:8765/ws");
    socket.onopen = () => {
      socket.close();
      resolve(true);
    };
    socket.onerror = () => resolve(false);
    setTimeout(() => resolve(false), 10000);
  });
  return { status: response.status, opened };
})()`;

if (!process.env.CI || (await accepts(SERVER_PORT))) {
  console.log("e2e skipped: it runs only in CI, with port 8765 free");
  process.exit(2);
}

class Failed extends Error {}

// realpathSync matters on macOS: tmpdir() sits under /var, a symlink to /private/var, and the
// store's storage.check_file refuses a symlinked ancestor, serving the recovery shell instead.
const env = {
  ...process.env,
  VIKI_DATA_DIR: realpathSync(mkdtempSync(join(tmpdir(), "viki-e2e-"))),
};
const PROFILE = join(env.VIKI_DATA_DIR, "electron-profile");
delete env.ELECTRON_RUN_AS_NODE;
// Importing "electron" launches nothing by itself, but a packaged run has no use for it (its
// own exe already is the app) and must not need it on the module search path.
const electron = PACKAGED_APP ? null : (await import("electron")).default;
/** The app itself: the packaged exe directly, or Electron running this checkout. */
const appCommand = PACKAGED_APP ?? electron;
/** @type {Set<import("node:child_process").ChildProcess>} what to kill when the run fails */
const children = new Set();
let appOutput = "";

/**
 * @param {string} name
 * @param {boolean} ok
 */
function check(name, ok) {
  console.log(`${name}: ${ok ? "PASS" : "FAIL"}`);
  if (!ok) throw new Failed();
}

/**
 * Resolves true when something accepts a TCP connection on 127.0.0.1:`port`. Sends nothing.
 * @param {number} port
 * @returns {Promise<boolean>}
 */
function accepts(port) {
  return new Promise((resolve) => {
    const socket = connect({ host: "127.0.0.1", port });
    socket.setTimeout(3_000);
    socket.once("connect", () => {
      socket.destroy();
      resolve(true);
    });
    socket.once("timeout", () => {
      socket.destroy();
      resolve(false);
    });
    socket.once("error", () => resolve(false));
  });
}

/** @returns {Promise<number>} a port nothing listens on now */
function freePort() {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

/**
 * Polls `probe` until it returns a truthy value and resolves with that value, or with null once
 * `timeoutMs` has passed. A probe that throws has not succeeded yet.
 * @template T
 * @param {() => Promise<T> | T} probe
 * @param {number} timeoutMs
 * @returns {Promise<T | null>}
 */
async function waitFor(probe, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      const value = await probe();
      if (value) return value;
    } catch {
      // not yet
    }
    if (Date.now() >= deadline) return null;
    await sleep(250);
  }
}

/**
 * @template T
 * @param {Promise<T>} promise
 * @param {number} ms
 * @returns {Promise<T | null>} its value, or null when `ms` passes first
 */
function within(promise, ms) {
  let timer;
  const late = new Promise((resolve) => {
    timer = setTimeout(resolve, ms, null);
  });
  return Promise.race([promise, late]).finally(() => clearTimeout(timer));
}

/**
 * Starts a child with the run's environment; an Electron child's output is kept for the token
 * check and never printed.
 * @param {string} command
 * @param {string[]} args
 * @param {{ keepOutput?: boolean }} [options]
 */
function start(command, args, { keepOutput = false } = {}) {
  const output = keepOutput ? "pipe" : "ignore";
  const child = spawn(command, args, { env, stdio: ["ignore", output, output] });
  children.add(child);
  if (keepOutput) {
    for (const stream of [child.stdout, child.stderr]) {
      stream.on("data", (chunk) => {
        appOutput += chunk;
      });
    }
  }
  /** @type {Promise<{ code: number | null, signal: NodeJS.Signals | null }>} */
  const exited = new Promise((resolve) => {
    child.once("exit", (code, signal) => resolve({ code, signal }));
    child.once("error", () => resolve({ code: null, signal: null }));
  }).finally(() => children.delete(child));
  return { child, exited };
}

/**
 * The app's pages that DevTools on `port` lists on the app origin.
 * @param {number} port
 * @returns {Promise<{ id: string, url: string, title: string, webSocketDebuggerUrl: string }[]>}
 */
async function appPages(port) {
  const response = await fetch(`http://127.0.0.1:${port}/json/list`, {
    signal: AbortSignal.timeout(5_000),
  });
  const targets = await response.json();
  return targets.filter((target) => target.type === "page" && isAppUrl(target.url, APP_ORIGIN));
}

/**
 * Waits up to `timeoutMs` for exactly `count` app pages.
 * @param {number} port
 * @param {number} count
 * @param {number} timeoutMs
 */
async function pagesReach(port, count, timeoutMs) {
  return (await waitFor(async () => (await appPages(port)).length === count, timeoutMs)) !== null;
}

/**
 * Runs `expression` in a page over its DevTools WebSocket.
 * @param {string} debuggerUrl
 * @param {string} expression
 * @returns {Promise<unknown>} its value, or null when it fails or takes over 30 s
 */
async function evaluate(debuggerUrl, expression) {
  const socket = new WebSocket(debuggerUrl);
  const reply = new Promise((resolve) => {
    socket.onopen = () =>
      socket.send(
        JSON.stringify({
          id: 1,
          method: "Runtime.evaluate",
          params: { expression, awaitPromise: true, returnByValue: true },
        }),
      );
    socket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.id !== 1) return;
      resolve(message.result?.exceptionDetails ? null : (message.result?.result?.value ?? null));
    };
    socket.onerror = () => resolve(null);
  });
  try {
    return await within(reply, 30_000);
  } finally {
    socket.close();
  }
}

/**
 * One CDP connection to `debuggerUrl`, sending each `{method, params}` in `calls` in order and
 * waiting for its reply before the next: `Page.navigate` must start only once the network
 * condition ahead of it has taken effect. Ignores every reply's contents; the caller needs only
 * the ordering. A call without a reply within 10 s is skipped, not retried.
 * @param {string} debuggerUrl
 * @param {{ method: string, params?: object }[]} calls
 */
async function cdpSequence(debuggerUrl, calls) {
  const socket = new WebSocket(debuggerUrl);
  let nextId = 1;
  /** @type {Map<number, (value: unknown) => void>} */
  const pending = new Map();
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    const resolve = pending.get(message.id);
    if (resolve) {
      pending.delete(message.id);
      resolve(message);
    }
  };
  await within(
    new Promise((resolve) => {
      socket.onopen = resolve;
    }),
    10_000,
  );
  try {
    for (const { method, params } of calls) {
      const id = nextId++;
      const reply = new Promise((resolve) => pending.set(id, resolve));
      socket.send(JSON.stringify({ id, method, params }));
      await within(reply, 10_000);
    }
  } finally {
    socket.close();
  }
}

/**
 * Makes the window at `debuggerUrl` fail one navigation to a same-origin URL carrying a
 * fake token, over CDP rather than the page's own `location` (the `will-navigate` guard does
 * not fire for a CDP navigation, so this exercises the other guard, against a failed-load
 * warning that would print the token: the warning listener in main.mjs). The stdout/stderr
 * token scan, run after every check in this file, is what actually verifies nothing leaked.
 * @param {string} debuggerUrl
 */
async function offlineNavigationLeaksNoToken(debuggerUrl) {
  const fakeToken = [...randomBytes(30)]
    .map((byte) => String.fromCharCode(97 + (byte % 26)))
    .join("");
  const offline = { offline: true, latency: 0, downloadThroughput: -1, uploadThroughput: -1 };
  const online = { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 };
  await cdpSequence(debuggerUrl, [
    { method: "Network.enable" },
    { method: "Network.emulateNetworkConditions", params: offline },
    { method: "Page.navigate", params: { url: `${APP_ORIGIN}/?ui=0#token=${fakeToken}` } },
    { method: "Network.emulateNetworkConditions", params: online },
  ]);
  // Gives Electron's load-failure warning (if any) time to reach stderr before the token scan
  // right after this runs; both scenarios' output is checked again, once, at the very end.
  await sleep(500);
}

/** @returns {Promise<string[]>} every process's command line, from the OS's process list */
function commandLines() {
  const [command, args] =
    process.platform === "win32"
      ? [
          "powershell",
          ["-NoProfile", "-Command", "Get-CimInstance Win32_Process | ForEach-Object CommandLine"],
        ]
      : ["ps", ["-axww", "-o", "command="]];
  return new Promise((resolve, reject) => {
    execFile(
      command,
      args,
      { maxBuffer: 64 * 1024 * 1024, timeout: 60_000, windowsHide: true },
      (error, stdout) => (error ? reject(error) : resolve(stdout.split(/\r?\n/))),
    );
  });
}

/**
 * No command line holds a token, while a positive control is found. The control's marker
 * comes last on a long command line, so a scan that cuts lines short cannot find it.
 */
async function scanForTokens() {
  const letters = [...randomBytes(30)].map((byte) => String.fromCharCode(97 + (byte % 26)));
  const marker = `token=${letters.join("")}`;
  const control = start(process.execPath, [
    "-e",
    "setInterval(() => {}, 60000)",
    "x".repeat(2000),
    marker,
  ]);
  await new Promise((resolve) => {
    control.child.once("spawn", resolve);
    control.child.once("error", resolve);
  });
  const lines = await commandLines().catch(() => []);
  control.child.kill();
  const hits = lines.filter((line) => TOKEN_IN_ARGV.test(line));
  check(
    "the process scan finds its positive control",
    hits.some((line) => line.includes(marker)),
  );
  check(
    "no other process command line holds a token",
    hits.every((line) => line.includes(marker)),
  );
}

/** @param {number} port */
function appArgs(port) {
  const base = [
    `--user-data-dir=${PROFILE}`,
    `--remote-debugging-port=${port}`,
    "--remote-allow-origins=*",
  ];
  // A packaged exe takes no folder argument; source mode needs DESKTOP to find this checkout.
  return PACKAGED_APP ? base : [...base, DESKTOP];
}

/** @returns {string[]} args for launching the app again while an instance already runs */
function secondLaunchArgs() {
  const base = [`--user-data-dir=${PROFILE}`];
  return PACKAGED_APP ? base : [...base, DESKTOP];
}

async function serverPortRefusesWithin10s() {
  return (await waitFor(async () => !(await accepts(SERVER_PORT)), 10_000)) !== null;
}

/** @param {number} port @returns {Promise<boolean>} true if no app page is open, now or ever */
async function noAppPage(port) {
  try {
    return (await appPages(port)).length === 0;
  } catch {
    return true; // DevTools never answered: the app never got far enough to open a page
  }
}

/**
 * Packaged only: the app's child server is the compiled backend binary, not uv, found by
 * the same process-list scan scanForTokens uses.
 */
async function serverIsBundledBinary() {
  const lines = await commandLines().catch(() => []);
  check(
    "a command line holds the bundled backend and --window-host=",
    lines.some((line) => line.includes(PACKAGED.backend) && line.includes("--window-host=")),
  );
}

/**
 * Packaged only: flips one byte inside `asarPath`'s single copy of a known, unique
 * string, runs `attempt`, then restores the byte whatever happens. The string is main.mjs's own
 * MSG_NOT_RUN text, chosen only because the asar packs it verbatim and it is guaranteed to
 * appear exactly once (checked below) — corrupting it is not the point, corrupting one byte of
 * the asar is: with EnableEmbeddedAsarIntegrityValidation on, Electron must refuse to start.
 * @param {string} asarPath
 * @param {() => Promise<void>} attempt
 */
async function withTamperedAsarByte(asarPath, attempt) {
  const needle = Buffer.from("can't start: its server", "utf8");
  const bytes = readFileSync(asarPath);
  const first = bytes.indexOf(needle);
  const second = first === -1 ? -1 : bytes.indexOf(needle, first + 1);
  check("the tamper marker appears exactly once in app.asar", first !== -1 && second === -1);
  const original = bytes[first];
  bytes[first] = original ^ 0xff;
  writeFileSync(asarPath, bytes);
  try {
    await attempt();
  } finally {
    bytes[first] = original;
    writeFileSync(asarPath, bytes);
  }
}

/**
 * Runs first, before scenario A (every OS, nothing else running yet). With --no-sandbox added,
 * main.mjs's sandboxOff check (policy.mjs, hard rule 7) must stop the app before it opens a
 * window or its server binds `SERVER_PORT`, whether or not this OS shows the dialog box main.mjs
 * tries first.
 */
async function scenarioSandboxOff() {
  const port = await freePort();
  const refused = start(appCommand, [...appArgs(port), "--no-sandbox"], { keepOutput: true });
  const appeared = await waitFor(async () => (await appPages(port)).length > 0, 20_000);
  check("with --no-sandbox no app page opens within 20 s", appeared === null);
  check("with --no-sandbox port 8765 refuses", await serverPortRefusesWithin10s());
  const hasExited = refused.child.exitCode !== null || refused.child.signalCode !== null;
  if (hasExited) {
    check("with --no-sandbox the app exits non-zero", refused.child.exitCode !== 0);
    if (process.platform === "linux") {
      check(
        "with --no-sandbox its output names the sandbox refusal",
        appOutput.includes("can't start with Chromium's sandbox turned off"),
      );
    }
  } else {
    // Windows and macOS show this refusal as a modal box, which blocks the app until someone
    // dismisses it; nothing here will click it, so stop the app rather than wait on that exit.
    refused.child.kill("SIGKILL");
    await refused.exited;
    console.log("with --no-sandbox: stopped the still-running app after the checks above");
  }
}

/**
 * Scenario A (every OS): the first window and its token handling, a second launch and a
 * relaunched server each adding a window, and a clean exit once every window is closed.
 */
async function scenarioA() {
  const port = await freePort();
  const first = start(appCommand, appArgs(port), { keepOutput: true });
  const page = await waitFor(async () => (await appPages(port))[0], 120_000);
  check("a window opens on the app origin within 120 s", page !== null);
  const ready = await waitFor(async () => {
    const target = (await appPages(port)).find((candidate) => candidate.id === page.id);
    return target && !target.url.includes("token=") && target.title === "\u2060" ? target : null;
  }, 30_000);
  check("within 30 s its URL holds no token and its page title is visually blank", ready !== null);
  const auth = await evaluate(ready.webSocketDebuggerUrl, AUTH_PROBE);
  check(
    `the window's Bearer fetch of /api/sessions answers 200 (got ${auth?.status ?? "none"})`,
    auth?.status === 200,
  );
  check("the window's WebSocket to /ws opens", auth?.opened === true);
  if (PACKAGED_APP) await serverIsBundledBinary();
  await offlineNavigationLeaksNoToken(ready.webSocketDebuggerUrl);
  await scanForTokens();

  const second = start(appCommand, secondLaunchArgs(), { keepOutput: true });
  check(
    "a second app launch exits 0 within 20 s",
    (await within(second.exited, 20_000))?.code === 0,
  );
  check("the second launch adds a window", await pagesReach(port, 2, 30_000));
  const relaunch = PACKAGED_APP
    ? { command: PACKAGED.backend, args: [], label: "relaunching the bundled server" }
    : {
        command: "uv",
        args: ["run", "--directory", REPO, "viki-agent"],
        label: "uv run viki-agent",
      };
  const relaunched = start(relaunch.command, relaunch.args);
  check(
    `${relaunch.label} exits 0 within 60 s`,
    (await within(relaunched.exited, 60_000))?.code === 0,
  );
  check(`${relaunch.label} adds a window in the app`, await pagesReach(port, 3, 30_000));

  for (const target of await appPages(port)) {
    await fetch(`http://127.0.0.1:${port}/json/close/${target.id}`, {
      signal: AbortSignal.timeout(5_000),
    })
      .then((response) => response.text())
      .catch(() => {}); // the exit check below tells whether the windows closed
  }
  check(
    "with every window closed the app exits 0 within 60 s",
    (await within(first.exited, 60_000))?.code === 0,
  );
  check("port 8765 refuses within 10 s", await serverPortRefusesWithin10s());
  check("Electron's profile folder exists", existsSync(PROFILE));
}

/** Scenario B (macOS, Linux): the app ended by a signal. */
async function scenarioB() {
  const port = await freePort();
  const again = start(appCommand, appArgs(port), { keepOutput: true });
  const page = await waitFor(async () => (await appPages(port))[0], 120_000);
  check("started again, a window opens within 120 s", page !== null);
  again.child.kill("SIGTERM");
  check("after SIGTERM the app exits within 30 s", (await within(again.exited, 30_000)) !== null);
  check("after SIGTERM port 8765 refuses within 10 s", await serverPortRefusesWithin10s());
}

/** Scenario C (packaged, win32 and darwin only): embedded ASAR integrity. */
async function scenarioC() {
  await withTamperedAsarByte(PACKAGED.asar, async () => {
    const port = await freePort();
    const tampered = start(appCommand, appArgs(port), { keepOutput: true });
    const ended = await within(tampered.exited, 60_000);
    // A still-running process would hold app.asar open, and withTamperedAsarByte's finally
    // needs to rewrite it to restore the byte.
    if (ended === null) {
      tampered.child.kill("SIGKILL");
      await tampered.exited; // Windows keeps app.asar open until the process is really gone
    }
    check(
      "a tampered app.asar makes the app exit non-zero or signalled, within 60 s",
      ended !== null && (ended.code !== 0 || ended.signal !== null),
    );
    check("the tampered app shows no app page", await noAppPage(port));
    check("port 8765 refuses after the tampered launch", await serverPortRefusesWithin10s());
  });
}

/** @param {number} code */
function finish(code) {
  for (const child of children) child.kill("SIGKILL");
  try {
    rmSync(env.VIKI_DATA_DIR, { recursive: true, force: true, maxRetries: 3 });
  } catch {
    // A server still stopping may hold a file there; the runner's temp folder goes anyway.
  }
  // Ends once what was killed has gone: process.exit() at once can abort on Windows (libuv's
  // async.c assertion, Node 24.15) while a DevTools socket's last work is still in flight.
  process.exitCode = code;
  setTimeout(() => process.exit(code), 10_000).unref();
}

const guard = setTimeout(() => {
  console.log("e2e within 15 min: FAIL");
  for (const child of children) child.kill("SIGKILL");
  process.exit(1);
}, GUARD_MS);
let failed = false;
try {
  await scenarioSandboxOff();
  await scenarioA();
  if (process.platform !== "win32") await scenarioB();
  if (PACKAGED_APP && process.platform !== "linux") await scenarioC();
  check("the app's stdout and stderr hold no token=", !appOutput.includes("token="));
} catch (error) {
  failed = true;
  // An unexpected error's message can hold a URL: only the fact is printed.
  if (!(error instanceof Failed)) console.log("e2e ran to the end: FAIL");
}
clearTimeout(guard);
finish(failed ? 1 : 0);
