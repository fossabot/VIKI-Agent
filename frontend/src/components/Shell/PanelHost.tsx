import {
  type PointerEvent,
  type ReactNode,
  useCallback,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import {
  defaultPanelPlacement,
  PANEL_IDS,
  type PanelId,
  type PanelSide,
  placePanel,
} from "../../state/panelPlacement";
import "./PanelHost.css";

export interface PanelHostProps {
  panels: Partial<Record<PanelId, ReactNode>>;
  hosts: Record<PanelSide, HTMLElement | null>;
}

const titles: Record<PanelId, string> = {
  sessions: "Sessions",
  agents: "Agents",
  approvals: "Approvals",
  routines: "Routines",
  skills: "Skills",
};
type Drop = { side: PanelSide; before: PanelId | null };

export function PanelHost({ panels, hosts }: PanelHostProps) {
  const instance = useId();
  const [targets] = useState(
    () =>
      new Map<PanelId, HTMLDivElement>(
        PANEL_IDS.map((id) => {
          const node = document.createElement("div");
          node.className = "panel-placement";
          node.dataset.panelId = id;
          return [id, node];
        }),
      ),
  );
  const [ends] = useState(() => ({
    left: document.createElement("div"),
    right: document.createElement("div"),
  }));
  const [placement, setPlacement] = useState(defaultPanelPlacement);
  const [preview, setPreview] = useState<{ id: PanelId; drop: Drop | null } | null>(null);
  const drag = useRef<{
    id: PanelId;
    pointer: number;
    hosts: PanelHostProps["hosts"];
    drop: Drop | null;
    release(): void;
  } | null>(null);
  const focus = useRef<HTMLElement | null>(null);
  const present = PANEL_IDS.filter((id) => panels[id] !== undefined && panels[id] !== null);
  const cancel = useCallback((update = true) => {
    const active = drag.current;
    if (!active) return;
    drag.current = null;
    active.release();
    if (update) setPreview(null);
  }, []);

  function rememberFocus() {
    const active = document.activeElement;
    focus.current =
      active instanceof HTMLElement && [...targets.values()].some((node) => node.contains(active))
        ? active
        : null;
  }

  function move(id: PanelId, side: PanelSide, before: PanelId | null = null) {
    cancel();
    rememberFocus();
    setPlacement((current) => placePanel(current, id, side, before));
  }

  useLayoutEffect(() => {
    const active = focus.current ?? document.activeElement;
    const owner = [...targets.values()].find(
      (node) => active instanceof HTMLElement && node.contains(active),
    );
    for (const side of ["left", "right"] as const) {
      let anchor: Node | null = null;
      if (preview && hosts[side]) {
        hosts[side]?.append(ends[side]);
        anchor = ends[side];
      } else ends[side].remove();
      for (const id of [...placement[side]].reverse()) {
        const node = targets.get(id);
        if (!node) continue;
        if (!present.includes(id) || !hosts[side]) {
          node.remove();
          continue;
        }
        if (node.parentNode !== hosts[side] || node.nextSibling !== anchor)
          hosts[side]?.insertBefore(node, anchor);
        anchor = node;
      }
    }
    if (owner?.isConnected && active instanceof HTMLElement) {
      const destination =
        active instanceof HTMLButtonElement && active.disabled
          ? owner.querySelector<HTMLElement>("[data-panel-grip]")
          : active;
      if (destination?.isConnected && document.activeElement !== destination)
        destination.focus({ preventScroll: true });
    }
    focus.current = null;
  });

  useLayoutEffect(() => {
    const active = drag.current;
    if (
      active &&
      (active.hosts.left !== hosts.left ||
        active.hosts.right !== hosts.right ||
        !present.includes(active.id) ||
        (active.drop?.before && !present.includes(active.drop.before)))
    )
      cancel();
  });
  useLayoutEffect(
    () => () => {
      cancel(false);
      for (const node of [...targets.values(), ...Object.values(ends)]) node.remove();
    },
    [cancel, targets, ends],
  );

  function start(event: PointerEvent<HTMLButtonElement>, id: PanelId) {
    if (event.button !== 0 || !event.isPrimary || drag.current) return;
    event.preventDefault();
    const handle = event.currentTarget;
    handle.focus({ preventScroll: true });
    handle.setPointerCapture(event.pointerId);
    const onEscape = (key: globalThis.KeyboardEvent) => {
      if (
        key.key !== "Escape" ||
        key.isComposing ||
        key.keyCode === 229 ||
        key.ctrlKey ||
        key.altKey ||
        key.metaKey ||
        key.shiftKey
      )
        return;
      key.preventDefault();
      key.stopPropagation();
      cancel();
    };
    const blur = () => cancel();
    window.addEventListener("keydown", onEscape, true);
    window.addEventListener("blur", blur);
    drag.current = {
      id,
      pointer: event.pointerId,
      hosts: { ...hosts },
      drop: null,
      release() {
        window.removeEventListener("keydown", onEscape, true);
        window.removeEventListener("blur", blur);
        if (handle.hasPointerCapture(event.pointerId))
          handle.releasePointerCapture(event.pointerId);
      },
    };
    setPreview({ id, drop: null });
  }

  function point(event: PointerEvent<HTMLButtonElement>, commit = false) {
    const active = drag.current;
    if (!active || !event.isPrimary || event.pointerId !== active.pointer) return;
    const hit = document.elementFromPoint(event.clientX, event.clientY);
    let drop: Drop | null = null;
    if (hit)
      for (const side of ["left", "right"] as const) {
        if (!hosts[side]?.contains(hit)) continue;
        if (ends[side].contains(hit)) drop = { side, before: null };
        for (const id of placement[side])
          if (
            id !== active.id &&
            present.includes(id) &&
            targets.get(id)?.querySelector(".panel-drop-target")?.contains(hit)
          )
            drop = { side, before: id };
      }
    active.drop = drop;
    if (commit) {
      cancel();
      if (drop) move(active.id, drop.side, drop.before);
    } else setPreview({ id: active.id, drop });
  }

  function interrupted(event: PointerEvent<HTMLButtonElement>) {
    if (drag.current?.pointer === event.pointerId) cancel();
  }

  return (
    <>
      {present.map((id) => {
        const side: PanelSide = placement.left.includes(id) ? "left" : "right";
        const order = placement[side].filter((item) => present.includes(item));
        const at = order.indexOf(id);
        const target = targets.get(id);
        if (!target) return null;
        return createPortal(
          <section aria-labelledby={`${instance}-${id}`}>
            <header className="panel-placement-head">
              {preview && preview.id !== id && (
                <div
                  className={`panel-drop-target${preview.drop?.side === side && preview.drop.before === id ? " is-target" : ""}`}
                >
                  Place {titles[preview.id]} before {titles[id]}
                </div>
              )}
              <h3 id={`${instance}-${id}`} aria-label={titles[id]}>
                <button
                  type="button"
                  data-panel-grip
                  aria-label={`Drag ${titles[id]}`}
                  onPointerDown={(event) => start(event, id)}
                  onPointerMove={point}
                  onPointerUp={(event) => point(event, true)}
                  onPointerCancel={interrupted}
                  onLostPointerCapture={interrupted}
                >
                  {titles[id]}
                </button>
              </h3>
              <div className="panel-placement-actions">
                {(["left", "right"] as const).map((next) => (
                  <button
                    key={next}
                    type="button"
                    aria-label={`Move ${titles[id]} ${next}`}
                    disabled={side === next || !hosts[next]}
                    onClick={() => move(id, next)}
                  >
                    Move {next}
                  </button>
                ))}
                <button
                  type="button"
                  aria-label={`Move ${titles[id]} up`}
                  disabled={at === 0}
                  onClick={() => move(id, side, order[at - 1] ?? null)}
                >
                  Move up
                </button>
                <button
                  type="button"
                  aria-label={`Move ${titles[id]} down`}
                  disabled={at === order.length - 1}
                  onClick={() => move(id, side, order[at + 2] ?? null)}
                >
                  Move down
                </button>
                <button
                  type="button"
                  aria-label={`Reset all panels from ${titles[id]}`}
                  onClick={() => {
                    cancel();
                    rememberFocus();
                    setPlacement(defaultPanelPlacement());
                  }}
                >
                  Reset all panels
                </button>
              </div>
            </header>
            {panels[id]}
          </section>,
          target,
          id,
        );
      })}
      {(["left", "right"] as const).map((side) =>
        createPortal(
          preview && (
            <div
              className={`panel-drop-target${preview.drop?.side === side && preview.drop.before === null ? " is-target" : ""}`}
            >
              Place {titles[preview.id]} at end of {side} rail
            </div>
          ),
          ends[side],
          side,
        ),
      )}
    </>
  );
}
