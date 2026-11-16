/**
 * The three vault tiles at the bottom of the right rail
 * (src/accept/m3b/gates.accept.test.tsx pins the contract).
 *
 * Rules:
 * - A `.tiles` row (flex, 8px gap, 20px above) of three `.tile`s in this order, each with
 *   `data-tile` "knowledge" | "sources" | "inbox": a mono 18px number (`.tile-n`) over an
 *   11.5px dim label (`.tile-l`) reading "knowledge", "sources", "inbox".
 * - The inbox tile carries `tile-hot` when inbox > 0: orange-tinted border
 *   (rgba(215,119,87,.45)), background rgba(215,119,87,.06) and number colour #E8906F. The
 *   other tiles: 1px #262626 border, radius 6px, 8px 10px padding, #111111 background.
 * - Each tile is a button; with `onOpen` it passes its VIKI folder name, without it the tile is
 *   disabled.
 */
import type { ReactElement } from "react";
import type { VaultCounts } from "../../protocol";
import { RollingNumber } from "../Loading/RollingNumber";

export interface VaultTilesProps {
  counts: VaultCounts;
  onOpen?: (folder: string) => void;
}

const TILES: ReadonlyArray<{ key: keyof VaultCounts; label: string; folder: string }> = [
  { key: "knowledge", label: "knowledge", folder: "30-knowledge" },
  { key: "sources", label: "sources", folder: "40-sources" },
  { key: "inbox", label: "inbox", folder: "00-inbox" },
];

export function VaultTiles({ counts, onOpen }: VaultTilesProps): ReactElement {
  return (
    <div className="tiles">
      {TILES.map(({ key, label, folder }) => {
        const hot = key === "inbox" && counts.inbox > 0;
        return (
          <button
            key={key}
            type="button"
            className={hot ? "tile tile-hot" : "tile"}
            data-tile={key}
            disabled={!onOpen}
            onClick={() => onOpen?.(folder)}
          >
            <div className="tile-n mono roll-host">
              <RollingNumber value={counts[key]} />
            </div>
            <div className="tile-l">{label}</div>
          </button>
        );
      })}
    </div>
  );
}
