import type { CodeDiff } from "../../clients/code/codeApi";
import "./CodeViews.css";

const REASONS = {
  binary: "Binary file: no text diff.",
  too_large: "This file is over 1 MiB, so no diff is shown.",
  no_commits: "The repository has no commits yet.",
  no_diff: "No changes against HEAD.",
};

// Header lines (`--- a/x`) look like changed lines, so only text after a file's first `@@` is
// classified by its first character.
function lineClasses(lines: string[]): string[] {
  let inHunk = false;
  return lines.map((line) => {
    if (line.startsWith("diff ")) inHunk = false;
    if (line.startsWith("@@")) inHunk = true;
    if (!inHunk) return "diff-meta";
    if (line.startsWith("@@")) return "diff-hunk";
    if (line.startsWith("+")) return "diff-add";
    if (line.startsWith("-")) return "diff-del";
    if (line.startsWith("\\")) return "diff-meta";
    return "diff-ctx";
  });
}

export function DiffView({ diff }: { diff: CodeDiff }) {
  if (diff.text === null) {
    return (
      <section className="code-diff">
        {diff.reason !== null ? <p className="code-reason">{REASONS[diff.reason]}</p> : null}
      </section>
    );
  }
  const lines = diff.text.split(/\r\n|\r|\n/);
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  const classes = lineClasses(lines);
  return (
    <section className="code-diff">
      {lines.map((line, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: the lines are a fixed split of one text
        <pre key={i} className={classes[i]}>
          {line}
        </pre>
      ))}
    </section>
  );
}
