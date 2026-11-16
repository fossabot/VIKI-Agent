export function clampResize(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, Math.round(value)));
}
