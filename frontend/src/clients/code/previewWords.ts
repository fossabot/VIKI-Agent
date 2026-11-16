import { rootRefusal } from "../../components/Workbench/Worktrees";
import { checksRefusal } from "./checkWords";
import type { PreviewJson } from "./previewsApi";

const REFUSALS: Record<string, string> = {
  invalid_port: "Use a port from 1024 to 65535.",
  port_reserved: "Port 8765 is VIKI's own. Choose another.",
  port_in_use: "Another preview uses this port.",
  port_taken: "Another program uses this port, so VIKI does not open it.",
  preview_limit: "Two previews are running. Stop one first.",
  window_limit: "Four preview windows are open. Close one first.",
  not_listening: "Nothing listens on this port yet. Wait for the server, then try again.",
  preview_running: "Stop the preview first.",
  unknown_preview: "This preview no longer exists. Refresh the list.",
  browser_missing: "No browser for preview windows was found.",
  start_failed: "The command could not start. Check that the folder still exists.",
  unsupported_platform: "Previews work on Windows in this version.",
};
// These three read the same as Checks' words.
const AS_CHECKS = new Set(["invalid_command", "worktree_not_ready", "unknown_worktree"]);

export function previewRefusal(code: string): string {
  if (AS_CHECKS.has(code)) return checksRefusal(code);
  const words = Object.hasOwn(REFUSALS, code) ? REFUSALS[code] : undefined;
  return words ?? rootRefusal(code);
}

export function previewStateWords(preview: PreviewJson): string {
  const { port } = preview;
  switch (preview.state) {
    case "starting":
      return `Waiting for port ${port}…`;
    case "listening":
      return `Listening on port ${port} (process ${preview.listener_pid})`;
    case "port_taken":
      return `Port ${port} is used by another program`;
    case "stopped":
      return "Stopped";
    case "exited":
      return `Ended (exit code ${preview.exit_code})`;
  }
}
