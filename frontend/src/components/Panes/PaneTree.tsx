import { type CSSProperties, type ReactNode, useLayoutEffect, useState } from "react";
import { minSize, type PaneGroup, type PaneNode, type PaneSplit } from "../../state/panes";
import { ResizeHandle } from "../Shell/ResizeHandle";
import "./PaneTree.css";

export interface PaneTreeProps {
  root: PaneNode;
  renderGroup(group: PaneGroup): ReactNode;
  onResize(splitId: string, sizes: readonly number[]): void;
}

/**
 * Renders a pane layout: a group is the caller's own content (`renderGroup`); a split lays its
 * children out along `dir`, with a draggable sash between each adjacent pair. A sash's bounds
 * are computed in pixels, so it appears only once the split's own box has been measured.
 */
export function PaneTree({ root, renderGroup, onResize }: PaneTreeProps): ReactNode {
  if (root.kind === "group") return renderGroup(root);
  return <SplitView split={root} renderGroup={renderGroup} onResize={onResize} />;
}

type Preview = { index: number; px: number } | null;

function SplitView({
  split,
  renderGroup,
  onResize,
}: Omit<PaneTreeProps, "root"> & { split: PaneSplit }) {
  const axis: "width" | "height" = split.dir === "row" ? "width" : "height";
  const [node, setNode] = useState<HTMLDivElement | null>(null);
  const [totalPx, setTotalPx] = useState<number | null>(null);
  const [preview, setPreview] = useState<Preview>(null);

  useLayoutEffect(() => {
    setTotalPx(null);
    if (!node) return;
    let active = true;
    const measure = () => {
      if (!active) return;
      const px = axis === "width" ? node.clientWidth : node.clientHeight;
      setTotalPx((current) => (px > 0 || current !== null ? px : null));
    };
    measure();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(node);
    return () => {
      active = false;
      observer?.disconnect();
    };
  }, [node, axis]);

  const floors = split.children.map((child) => minSize(child)[axis]);
  const basePx = totalPx === null ? null : split.sizes.map((share) => share * totalPx);

  /** A commit or reset re-divides the dragged pair's combined share by the new px split;
   * every other size is passed through unchanged. */
  function commit(index: number, px: number) {
    setPreview(null);
    if (basePx === null) return;
    const pairPx = (basePx[index] ?? 0) + (basePx[index + 1] ?? 0);
    const pairShare = (split.sizes[index] ?? 0) + (split.sizes[index + 1] ?? 0);
    const before = pairPx > 0 ? (px / pairPx) * pairShare : pairShare / 2;
    const sizes = split.sizes.slice();
    sizes[index] = before;
    sizes[index + 1] = pairShare - before;
    onResize(split.id, sizes);
  }

  const cells: ReactNode[] = [];
  split.children.forEach((child, index) => {
    const dragged = preview !== null && (preview.index === index || preview.index === index - 1);
    const style: CSSProperties = {};
    if (dragged && preview !== null && basePx !== null) {
      const pairPx = (basePx[preview.index] ?? 0) + (basePx[preview.index + 1] ?? 0);
      style.flex = `0 0 ${preview.index === index ? preview.px : pairPx - preview.px}px`;
    } else {
      style.flex = `${split.sizes[index] ?? 0} 1 0px`;
    }
    if (axis === "width") style.minWidth = floors[index];
    else style.minHeight = floors[index];
    cells.push(
      <div key={child.id} className="pane-cell" id={child.id} style={style}>
        <PaneTree root={child} renderGroup={renderGroup} onResize={onResize} />
      </div>,
    );

    const sibling = split.children[index + 1];
    if (sibling && basePx !== null) {
      const precedingPx =
        preview !== null && preview.index === index ? preview.px : (basePx[index] ?? 0);
      const pairPx = (basePx[index] ?? 0) + (basePx[index + 1] ?? 0);
      cells.push(
        <div key={`${child.id}:${sibling.id}`} className="pane-sash" data-dir={split.dir}>
          <ResizeHandle
            label="Resize panes"
            controls={child.id}
            orientation={split.dir === "row" ? "vertical" : "horizontal"}
            growDirection={1}
            value={precedingPx}
            min={floors[index] ?? 0}
            max={pairPx - (floors[index + 1] ?? 0)}
            defaultValue={pairPx / 2}
            onPreview={(px) => setPreview({ index, px })}
            onCommit={(px) => commit(index, px)}
          />
        </div>,
      );
    }
  });

  return (
    <div className="pane-split" data-dir={split.dir} ref={setNode}>
      {cells}
    </div>
  );
}
