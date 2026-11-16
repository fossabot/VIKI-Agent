/**
 * A read-only rendering of an arbitrary JSON value as nested definition lists:
 * objects become `<dl>`, arrays become `<ol>`, and every scalar — including a string that
 * looks like a URL or contains `<script>` — is a plain text node. No link is ever created (hard
 * rule 7: server text is never trusted with markup) and nothing is set through
 * `dangerouslySetInnerHTML`; JSX already escapes every string it renders.
 *
 * `JsonTree.css` lays the map out as a two-column key/value grid; an entry whose value is itself
 * a non-empty object or array gets `json-tree-group` so its key reads as a label over an
 * indented block instead of sitting beside a scalar.
 */
import "./JsonTree.css";

export interface JsonTreeProps {
  value: unknown;
}

function Scalar({ value }: { value: string | number | boolean | null }) {
  if (value === null) return <span className="json-tree-null mono">null</span>;
  return <span className="json-tree-scalar mono">{String(value)}</span>;
}

function isGroup(value: unknown): boolean {
  if (Array.isArray(value)) return value.length > 0;
  return value !== null && typeof value === "object" && Object.keys(value).length > 0;
}

export function JsonTree({ value }: JsonTreeProps) {
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="json-tree-empty mono">[]</span>;
    return (
      <ol className="json-tree-list">
        {value.map((item, index) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: a JSON array's items have no other identity
          <li key={index}>
            <JsonTree value={item} />
          </li>
        ))}
      </ol>
    );
  }
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) return <span className="json-tree-empty mono">{"{}"}</span>;
    return (
      <dl className="json-tree-map">
        {entries.map(([key, item]) => (
          <div
            className={isGroup(item) ? "json-tree-entry json-tree-group" : "json-tree-entry"}
            key={key}
          >
            <dt className="json-tree-key mono">{key}</dt>
            <dd className="json-tree-value">
              <JsonTree value={item} />
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  return <Scalar value={value as string | number | boolean | null} />;
}
