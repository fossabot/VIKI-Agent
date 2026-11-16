export const PANEL_IDS = ["sessions", "agents", "approvals", "routines", "skills"] as const;
export type PanelId = (typeof PANEL_IDS)[number];
export type PanelSide = "left" | "right";
export type PanelPlacement = Record<PanelSide, readonly PanelId[]>;

export function defaultPanelPlacement(): PanelPlacement {
  return {
    left: ["sessions", "agents"],
    right: ["approvals", "routines", "skills"],
  };
}

export function placePanel(
  current: PanelPlacement,
  id: PanelId,
  side: PanelSide,
  before: PanelId | null = null,
): PanelPlacement {
  if (before === id || (before !== null && !current[side].includes(before))) return current;
  const next = {
    left: current.left.filter((item) => item !== id),
    right: current.right.filter((item) => item !== id),
  };
  const at = before === null ? next[side].length : next[side].indexOf(before);
  next[side].splice(at, 0, id);
  return (Object.keys(next) as PanelSide[]).every(
    (key) => next[key].join(",") === current[key].join(","),
  )
    ? current
    : next;
}
