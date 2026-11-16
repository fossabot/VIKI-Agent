/**
 * The Markdown parse worker entry. Loaded only as a module Worker (see vite.config.ts and
 * main.tsx); the `worker` export condition keeps every dependency off `document` at load.
 */
import type { ParseRequest, ParseResponse } from "./markdownParser";
import { parseMarkdown } from "./markdownTree";

interface WorkerScope {
  onmessage: ((event: MessageEvent<ParseRequest>) => void) | null;
  postMessage(message: ParseResponse): void;
}

const scope = self as unknown as WorkerScope;

scope.onmessage = (event) => {
  const { id, text } = event.data;
  if (!Number.isSafeInteger(id) || typeof text !== "string") return;
  scope.postMessage({ id, result: parseMarkdown(text) });
};
