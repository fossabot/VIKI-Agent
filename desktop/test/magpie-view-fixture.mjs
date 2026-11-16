import { DEFAULT_MAGPIE_APPEARANCE } from "../magpie-theme.mjs";

export function magpieView() {
  return {
    id: "b".repeat(32),
    bounds: { x: 260, y: 96, width: 700, height: 500 },
    appearance: structuredClone(DEFAULT_MAGPIE_APPEARANCE),
    visible: true,
  };
}
