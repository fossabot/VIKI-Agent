/**
 * A subagent's geometric face (src/accept/m3b/agents.accept.test.tsx pins the contract).
 * The design draws VIKI's four agents with distinct shapes; everything else gets the neutral face.
 *
 * Rules:
 * - An inline SVG, `size` px square (default 24), inside a `.face` span (position relative) so a
 *   presence dot can sit on it. `data-shape` on the span: researcher → "circle", librarian →
 *   "square", curator → "hexagon", connector → "diamond" (dashed stroke), any other name →
 *   "neutral" (a circle with the dim stroke). Fill var(--g3), stroke 1.2, two eye dots as in the
 *   design.
 * - `active` (default false): the stroke brightens to var(--t2) and a breathing
 *   presence dot (`.face-presence.breathe`, 8px, offset -2px bottom-right) appears; inactive
 *   faces use var(--t3), the dashed diamond var(--face-dim).
 * - Purely decorative: `aria-hidden`.
 */
import type { ReactElement } from "react";
// Reused verbatim: the .face positioning box and the .face-presence.breathe dot are the same
// shapes Face.tsx already draws for VIKI's own portrait.
import "../Face/Face.css";

export interface AgentFaceProps {
  name: string;
  size?: number;
  active?: boolean;
}

type Shape = "circle" | "square" | "hexagon" | "diamond" | "neutral";

const SHAPE_BY_NAME: Readonly<Record<string, Shape>> = {
  researcher: "circle",
  librarian: "square",
  curator: "hexagon",
  connector: "diamond",
};

function shapeOf(name: string): Shape {
  // Own keys only: an agent named "constructor" or "toString" must not read Object.prototype.
  return Object.hasOwn(SHAPE_BY_NAME, name) ? (SHAPE_BY_NAME[name] ?? "neutral") : "neutral";
}

/** The shape's own outline, centred in a 16x16 viewBox; neutral shares the circle's outline. */
function outline(shape: Shape): ReactElement {
  switch (shape) {
    case "square":
      return <rect x="2.5" y="2.5" width="11" height="11" rx="1.5" />;
    case "hexagon":
      return <polygon points="8,2 13.5,5.2 13.5,10.8 8,14 2.5,10.8 2.5,5.2" />;
    case "diamond":
      return <rect x="3.5" y="3.5" width="9" height="9" transform="rotate(45 8 8)" />;
    default:
      return <circle cx="8" cy="8" r="5.8" />;
  }
}

const PRESENCE_SIZE = 8;
const PRESENCE_OFFSET = -2;

/** A subagent's geometric mark: shape by name, stroke and presence by activity. */
export function AgentFace({ name, size = 24, active = false }: AgentFaceProps): ReactElement {
  const shape = shapeOf(name);
  const dashed = shape === "diamond";
  const inactiveStroke = dashed ? "var(--face-dim)" : "var(--t3)";
  const stroke = active ? "var(--t2)" : inactiveStroke;

  return (
    <span
      className="face"
      data-shape={shape}
      style={{ width: size, height: size }}
      aria-hidden="true"
    >
      <svg width={size} height={size} viewBox="0 0 16 16" aria-hidden="true">
        <g
          fill="var(--g3)"
          stroke={stroke}
          strokeWidth={1.2}
          strokeDasharray={dashed ? "2 1.5" : undefined}
        >
          {outline(shape)}
        </g>
        <circle cx="6" cy="7.2" r="0.6" fill={stroke} />
        <circle cx="10" cy="7.2" r="0.6" fill={stroke} />
      </svg>
      {active ? (
        <span
          className="face-presence breathe"
          style={{
            width: PRESENCE_SIZE,
            height: PRESENCE_SIZE,
            right: PRESENCE_OFFSET,
            bottom: PRESENCE_OFFSET,
          }}
        />
      ) : null}
    </span>
  );
}
