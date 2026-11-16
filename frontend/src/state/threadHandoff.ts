export interface ThreadHandoff {
  to: string | null;
  objective: string;
  artifacts: string[];
  decisions: string[];
  questions: string[];
  next_gate: "work" | "review" | "done";
  criteria: string[];
  evidence: string[];
  verdict: "PASS" | "FAIL" | null;
  progress: string;
}
export function readHandoff(text: string): ThreadHandoff | null {
  const last = text.trimEnd().split("\n").at(-1)?.trim() ?? "";
  if (!last.startsWith("VIKI_RESULT ") || last.length > 16000) return null;
  try {
    const value: unknown = JSON.parse(last.slice(12));
    if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
    const row = value as Record<string, unknown>;
    if (
      Object.keys(row).sort().join(",") !==
      "artifacts,criteria,decisions,evidence,next_gate,objective,progress,questions,to,verdict"
    )
      return null;
    if (row.to !== null && (typeof row.to !== "string" || !/^[a-z0-9-]{1,64}$/.test(row.to)))
      return null;
    if (
      !["work", "review", "done"].includes(row.next_gate as string) ||
      ![null, "PASS", "FAIL"].includes(row.verdict as string | null)
    )
      return null;
    if (
      !["objective", "progress"].every(
        (key) =>
          typeof row[key] === "string" &&
          (row[key] as string).trim().length > 0 &&
          (row[key] as string).length <= 2000,
      )
    )
      return null;
    if (
      !["artifacts", "decisions", "questions", "criteria", "evidence"].every(
        (key) =>
          Array.isArray(row[key]) &&
          row[key].length <= 16 &&
          row[key].every(
            (item: unknown) =>
              typeof item === "string" && item.trim().length > 0 && item.length <= 2000,
          ),
      )
    )
      return null;
    return row as unknown as ThreadHandoff;
  } catch {
    return null;
  }
}
export function handoffFingerprint(value: ThreadHandoff): string {
  return JSON.stringify([
    value.objective,
    value.artifacts,
    value.decisions,
    value.evidence,
    value.progress,
    value.verdict,
  ]);
}
export function promptContext(
  prompt: string,
): { request: string; handoff: ThreadHandoff | null } | null {
  const at = prompt.lastIndexOf("\nVIKI_THREAD_CONTEXT ");
  if (at < 0) return null;
  try {
    const value: unknown = JSON.parse(prompt.slice(at + 21));
    if (
      typeof value !== "object" ||
      value === null ||
      !("request" in value) ||
      typeof value.request !== "string" ||
      value.request.length > 8000
    )
      return null;
    if (!("handoff" in value) || Object.keys(value).sort().join(",") !== "handoff,request")
      return null;
    const handoff =
      value.handoff === null ? null : readHandoff(`VIKI_RESULT ${JSON.stringify(value.handoff)}`);
    if (value.handoff !== null && handoff === null) return null;
    return { request: value.request, handoff };
  } catch {
    return null;
  }
}
