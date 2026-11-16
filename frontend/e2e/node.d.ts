/**
 * The sliver of Node this end-to-end harness uses, declared by hand.
 *
 * Why not @types/node: it is a global types package, and its globals contradict the DOM's in
 * exactly the place this app relies on them — `setTimeout` would start returning NodeJS.Timeout
 * while `window.setTimeout` still returns number, which breaks src/ (SearchPalette's debounce,
 * the socket's retry timer) to buy types for one test file. This is a browser app with one
 * process-spawning harness; the harness is the exception, so the exception is what gets
 * declared. Everything else the harness needs (fetch, URL, AbortController) is already in the
 * DOM lib the app itself compiles against.
 *
 * Only what server.ts actually calls is here. If a later harness needs more of Node, add the
 * member rather than the package.
 */
declare module "node:child_process" {
  export interface ChildProcessLike {
    readonly pid: number | undefined;
    readonly stdout: { on(event: "data", listener: (chunk: unknown) => void): void } | null;
    readonly stderr: { on(event: "data", listener: (chunk: unknown) => void): void } | null;
    on(event: "exit", listener: (code: number | null) => void): void;
    kill(): boolean;
  }
  export function spawn(
    command: string,
    args: readonly string[],
    options: {
      cwd?: string;
      env?: Record<string, string | undefined>;
      stdio?: readonly ("ignore" | "pipe" | "inherit")[];
      windowsHide?: boolean;
    },
  ): ChildProcessLike;
}

declare const process: {
  readonly env: Record<string, string | undefined>;
  readonly platform: string;
};
