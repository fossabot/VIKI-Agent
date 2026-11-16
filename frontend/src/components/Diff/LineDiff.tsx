/** A line-level diff of two texts: one row per line, marked `+ `, `- ` or two spaces. */
import { personaDiff } from "../Settings/persona";
import "./LineDiff.css";

const MARKER = { add: "+ ", del: "- ", same: "  " } as const;

export function LineDiff({ before, after }: { before: string; after: string }) {
  return (
    <div className="line-diff">
      {personaDiff(before, after).map((entry, index) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: a line-level diff's entries can repeat the same text (e.g. two blank lines); index is the only stable key.
        <div key={index} className={`line-diff-${entry.kind}`}>
          {MARKER[entry.kind]}
          {entry.text}
        </div>
      ))}
    </div>
  );
}
