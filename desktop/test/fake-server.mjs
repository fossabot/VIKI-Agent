/**
 * A stand-in for the real server process, driven by server.test.mjs through
 * startServer(). Records its own argv and every stdin line it receives as
 * JSON lines in the file named by FAKE_LOG, then behaves per FAKE_MODE:
 *
 *   talk     prints an open (id 1, a #token= URL), one malformed line, then
 *            a close (id 1); exits 0 once stdin ends.
 *   stubborn ignores stdin ending; stays alive until killed.
 *   crash    exits 1 immediately.
 */
import { appendFileSync } from "node:fs";
import { createInterface } from "node:readline";

import { magpieView } from "./magpie-view-fixture.mjs";

const FAKE_LOG = process.env.FAKE_LOG;
const FAKE_MODE = process.env.FAKE_MODE;

/** @param {unknown} entry */
function log(entry) {
  appendFileSync(FAKE_LOG, JSON.stringify(entry) + "\n");
}

log({ argv: process.argv.slice(2) });

if (FAKE_MODE === "crash") {
  process.exit(1);
}

createInterface({ input: process.stdin }).on("line", (line) => log({ stdin: line }));

if (FAKE_MODE === "magpie-view") {
  const view = magpieView();
  const url = `http://127.0.0.1:3430/?k=${"a".repeat(64)}`;
  process.stdout.write(`${JSON.stringify({ type: "magpie", id: 1, url, view })}\n`);
  process.stdout.write(
    `${JSON.stringify({ type: "magpie_view", id: 1, view: { ...view, visible: false } })}\n`,
  );
  process.stdout.write(`${JSON.stringify({ type: "magpie_close", id: 1, view_id: view.id })}\n`);
  process.stdin.on("end", () => process.exit(0));
} else if (FAKE_MODE === "magpie") {
  const url = `http://127.0.0.1:3430/?k=${"a".repeat(64)}`;
  process.stdout.write(`${JSON.stringify({ type: "magpie", id: 1, url })}\n`);
  process.stdout.write(`${JSON.stringify({ type: "magpie_close", id: 1 })}\n`);
  process.stdin.on("end", () => process.exit(0));
} else if (FAKE_MODE === "talk") {
  const url = `http://127.0.0.1:8765/?ui=${"a".repeat(32)}#token=faketoken`;
  process.stdout.write(`${JSON.stringify({ type: "open", id: 1, url })}\n`);
  process.stdout.write("not valid json\n");
  process.stdout.write(`${JSON.stringify({ type: "close", id: 1 })}\n`);
  process.stdin.on("end", () => process.exit(0));
} else {
  // stubborn: stdin ending does nothing, and SIGTERM is ignored as the real server's graceful
  // handler only asks itself to stop; keep the event loop alive for an external kill to end
  // this process instead.
  process.on("SIGTERM", () => {});
  setInterval(() => {}, 1 << 30);
}
