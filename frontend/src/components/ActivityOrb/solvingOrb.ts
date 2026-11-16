// Adapted from thinking-orbs 0.3.1 by Jakub Antalik (MIT License; the notice ships in
// public/licenses/libraries-dev.txt). Only the 64px "solving" preset that ActivityOrb draws is
// kept: a dotted sphere that scrambles 14 quarter turns like a puzzle cube, undoes them and rests
// solved. Every step keeps the library's arithmetic order, so each frame matches the original.

const SIZE = 64;
const SPEED = 1.82;
const RINGS = 9;
const RING_DOTS = 24;
const DOT_BASE = 0.63;
const DOT_DEPTH = 1.785;
const DOT_TURNING = 0.315;
const DOT_MIN = 0.3;
const INK_FAR = 0.62;
const INK_SPAN = 0.54;
const TURN_SECONDS = 0.42;
const SOLVED_SECONDS = 1.2;

type Turn = { axis: 0 | 1 | 2; from: number; to: number; angle: number };
type Progress = { amounts: number[]; current: number };
type Dot = { x: number; y: number; z: number; r: number; ink: number };

function hash(a: number, b: number): number {
  const value = Math.sin(a * 12.9898 + b * 78.233) * 43758.5453;
  return value - Math.floor(value);
}

/** Each quarter turn rotates one half-unit slab of the sphere about one axis. */
const TURNS: Turn[] = Array.from({ length: 14 }, (_, index) => {
  const from = -1 + 0.5 * Math.min(3, Math.floor(hash(index, 5.9) * 4));
  return {
    axis: Math.min(2, Math.floor(hash(index, 2.3) * 3)) as Turn["axis"],
    from,
    to: from + 0.5,
    angle: ((hash(index, 7.7) < 0.5 ? 1 : -1) * Math.PI) / 2,
  };
});

/** How far each turn has gone: one turn per step, then each undone in reverse, then a rest. */
function turnProgress(time: number): Progress {
  const count = TURNS.length;
  const phase = time % (2 * count * TURN_SECONDS + SOLVED_SECONDS);
  const amounts = new Array<number>(count).fill(0);
  let current = -1;
  if (phase < 2 * count * TURN_SECONDS) {
    const step = Math.floor(phase / TURN_SECONDS);
    const within = (phase - step * TURN_SECONDS) / TURN_SECONDS;
    const eased = 1 - (1 - Math.min(1, within / 0.7)) ** 3;
    if (step < count) {
      amounts.fill(1, 0, step);
      amounts[step] = eased;
      current = step;
    } else {
      const undo = 2 * count - 1 - step;
      amounts.fill(1, 0, undo);
      amounts[undo] = 1 - eased;
      current = undo;
    }
  }
  return { amounts, current };
}

/** Rotates a unit-sphere point through every started turn whose slab holds it. */
function turned(
  [startX, startY, startZ]: [number, number, number],
  progress: Progress,
): [number, number, number, boolean] {
  let x = startX;
  let y = startY;
  let z = startZ;
  let moving = false;
  for (const [index, turn] of TURNS.entries()) {
    const amount = progress.amounts[index] ?? 0;
    if (amount <= 0) continue;
    const along = turn.axis === 0 ? x : turn.axis === 1 ? y : z;
    if (along < turn.from || along >= turn.to) continue;
    if (index === progress.current) moving = true;
    const angle = turn.angle * amount;
    const cos = Math.cos(angle);
    const sin = Math.sin(angle);
    if (turn.axis === 0) {
      const next = y * cos - z * sin;
      z = y * sin + z * cos;
      y = next;
    } else if (turn.axis === 1) {
      const next = x * cos + z * sin;
      z = -x * sin + z * cos;
      x = next;
    } else {
      const next = x * cos - y * sin;
      y = x * sin + y * cos;
      x = next;
    }
  }
  return [x, y, z, moving];
}

/** Paints one frame of the orb on a 64 px canvas, `seconds` into its animation. */
export function paintSolvingOrb(
  ctx: CanvasRenderingContext2D,
  seconds: number,
  dark: boolean,
): void {
  const time = seconds * SPEED;
  const center = SIZE / 2;
  const radius = (SIZE / 2) * 0.82;
  const yaw = time * 0.55;
  const tilt = 0.35 + 0.1 * Math.sin(time * 0.9);
  const sinTilt = Math.sin(tilt);
  const cosTilt = Math.cos(tilt);
  const sinYaw = Math.sin(yaw);
  const cosYaw = Math.cos(yaw);
  const scale = (SIZE / 300) ** 0.6;
  const progress = turnProgress(time);
  const dots: Dot[] = [];
  for (let ring = 0; ring <= RINGS; ring++) {
    const latitude = -Math.PI / 2 + (ring / RINGS) * Math.PI;
    const cosLatitude = Math.cos(latitude);
    const sinLatitude = Math.sin(latitude);
    const count = Math.max(1, Math.round(Math.abs(cosLatitude) * RING_DOTS));
    for (let dot = 0; dot < count; dot++) {
      const longitude = (dot / count) * 2 * Math.PI;
      const [x, y, z, moving] = turned(
        [cosLatitude * Math.cos(longitude), sinLatitude, cosLatitude * Math.sin(longitude)],
        progress,
      );
      const across = x * cosYaw + z * sinYaw;
      const deep = -x * sinYaw + z * cosYaw;
      const up = y * cosTilt - deep * sinTilt;
      const depth = y * sinTilt + deep * cosTilt;
      const near = (depth + 1) / 2;
      dots.push({
        x: center + across * radius,
        y: center - up * radius,
        z: depth,
        r: Math.max(DOT_MIN, (DOT_BASE + DOT_DEPTH * near + (moving ? DOT_TURNING : 0)) * scale),
        ink: INK_FAR - INK_SPAN * near - (moving ? 0.14 : 0),
      });
    }
  }
  // Far dots first, so nearer ones paint over them.
  dots.sort((a, b) => a.z - b.z);
  for (const { x, y, r, ink } of dots) {
    const white = Math.min(1, Math.max(0, ink));
    const shade = Math.round((dark ? 1 - white : white) * 255);
    ctx.fillStyle = `rgba(${shade},${shade},${shade},1)`;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();
  }
}
