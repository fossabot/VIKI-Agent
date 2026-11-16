const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export type SessionHandoff =
  | { kind: "none" }
  | { kind: "target"; sessionId: string }
  | { kind: "invalid" };

/** Call before readToken removes the launch fragment. This is a target, not admission proof. */
export function readSessionHandoff(hash = window.location.hash): SessionHandoff {
  const values = new URLSearchParams(hash.replace(/^#/, "")).getAll("session");
  if (!values.length) return { kind: "none" };
  const sessionId = values[0];
  return values.length === 1 && typeof sessionId === "string" && UUID.test(sessionId)
    ? { kind: "target", sessionId }
    : { kind: "invalid" };
}
