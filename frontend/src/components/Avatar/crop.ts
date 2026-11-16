export type Rotation = 0 | 90 | 180 | 270;

export interface Crop {
  x: number;
  y: number;
  edge: number;
}

export function rotatedSize(
  width: number,
  height: number,
  rotation: Rotation,
): { width: number; height: number } {
  if (rotation === 90 || rotation === 270) {
    return { width: height, height: width };
  }
  return { width, height };
}

export function initialCrop(width: number, height: number): Crop {
  const edge = Math.min(width, height);
  return {
    x: Math.floor((width - edge) / 2),
    y: Math.floor((height - edge) / 2),
    edge,
  };
}

export function moveCrop(crop: Crop, dx: number, dy: number, width: number, height: number): Crop {
  return {
    x: Math.min(Math.max(crop.x + dx, 0), width - crop.edge),
    y: Math.min(Math.max(crop.y + dy, 0), height - crop.edge),
    edge: crop.edge,
  };
}

// The zoom keeps the crop's centre fixed so the view stays where the user is looking.
export function zoomCrop(crop: Crop, direction: 1 | -1, width: number, height: number): Crop {
  const step = Math.max(1, Math.floor(crop.edge / 10));
  const newEdge = Math.min(Math.max(crop.edge - direction * step, 1), Math.min(width, height));
  return moveCrop(
    {
      x: Math.round(crop.x + crop.edge / 2 - newEdge / 2),
      y: Math.round(crop.y + crop.edge / 2 - newEdge / 2),
      edge: newEdge,
    },
    0,
    0,
    width,
    height,
  );
}

export function rotateBy(rotation: Rotation, turn: 1 | -1): Rotation {
  return ((rotation + turn * 90 + 360) % 360) as Rotation;
}

export function cropForKey(
  key: string,
  shift: boolean,
  crop: Crop,
  width: number,
  height: number,
): Crop | null {
  const step = shift ? 10 : 1;
  switch (key) {
    case "ArrowLeft":
      return moveCrop(crop, -step, 0, width, height);
    case "ArrowRight":
      return moveCrop(crop, step, 0, width, height);
    case "ArrowUp":
      return moveCrop(crop, 0, -step, width, height);
    case "ArrowDown":
      return moveCrop(crop, 0, step, width, height);
    case "+":
    case "=":
      return zoomCrop(crop, 1, width, height);
    case "-":
    case "_":
      return zoomCrop(crop, -1, width, height);
    default:
      return null;
  }
}

export function transformCrop(crop: Crop): { x: number; y: number; width: number; height: number } {
  return { x: crop.x, y: crop.y, width: crop.edge, height: crop.edge };
}
