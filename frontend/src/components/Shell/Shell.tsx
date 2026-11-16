import {
  type ReactNode,
  type SyntheticEvent,
  useCallback,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { clampResize } from "../../state/layout";
import {
  FILE_PANE_MIN,
  kindOf,
  minSize,
  PANE_MIN,
  type PaneGroup,
  type PaneNode,
  type Zone,
} from "../../state/panes";
import { PaneTree } from "../Panes/PaneTree";
import { type DropPayload, PaneDropOverlay, usePaneDrop } from "../Panes/paneDrop";
import { ComposerSlot } from "./ComposerSlot";
import { RailDrawer } from "./RailDrawer";
import { ResizeHandle } from "./ResizeHandle";
import type { LayoutMode } from "./useLayoutMode";
import "./Shell.css";

/** Which drawer is open, and how to close it. Supplied by App, which owns the decision — a
 * drawer closes on its own Close button, Escape or backdrop, and also on anything that takes the
 * user somewhere else (a row opened, forked or closed, a gate picked, search opened). */
export interface ShellRails {
  drawer: "left" | "right" | null;
  onClose: () => void;
}

/** Everything Shell needs to render a pane layout: the caller (which owns the actual
 * `PaneLayout` and its persistence) supplies the tree, which group is focused, whether a new
 * split may open, and the per-group content and the mutators. */
export interface ShellPanes {
  root: PaneNode;
  focused: string;
  canSplit: boolean;
  pane(groupId: string): {
    header: ReactNode;
    messages: ReactNode;
    /** Omitted uses Shell's fallback in the focused chat; null leaves the slot empty. */
    composer?: ReactNode;
    label: string;
    onActivate(event: SyntheticEvent): void;
  };
  onDrop(groupId: string, payload: DropPayload, zone: Zone): void;
  onResize(splitId: string, sizes: readonly number[]): void;
}

export interface ShellProps {
  topBar: ReactNode;
  leftRail: ReactNode;
  /** Content of the conversation header slot (52px tall). */
  header: ReactNode;
  /** Directly after the header and before the message scroller. The conversation can scroll
   * vertically in a short window; a banner that renders null adds no row. */
  banner?: ReactNode;
  /** Content of the scrolling message column (max-width 720, centred). */
  messages: ReactNode;
  /** The composer itself — it must carry its own `data-region="composer"` and sizing;
   * this slot only supplies the surrounding gutter. */
  composer: ReactNode;
  rightRail: ReactNode;
  statusBar: ReactNode;
  /** Floated over the conversation column, inside it rather than over the whole window: the
   * session-limit toast, whose `right: 24px` is measured from the column's edge.
   * A final zero-height host keeps it anchored while the conversation scrolls. */
  overlay?: ReactNode;
  /** Which rails the window is wide enough to dock. Both, when omitted: a caller that
   * does not measure the window gets the L layout, which is also what every jsdom test sees. */
  layout?: LayoutMode;
  rails?: ShellRails;
  /** A whole-window view other than the chat. Without viewSidebar its frame is the two bars
   * and one scrolling region; chat slots remain omitted. An optional viewSidebar adds the
   * left dock or drawer; a non-null rightRail adds the right dock or drawer. */
  view?: ReactNode;
  /** An optional sidebar beside a whole-window view, using the left rail's docking and drawer. */
  viewSidebar?: ReactNode;
  /** Primary content between the rails. Chat panes stay mounted but hidden while it is open. */
  center?: ReactNode;
  /** A multi-pane layout beside the single conversation. Without it, or with a one-group
   * layout, the DOM is what it is today plus `data-pane`. The caller decides whether the right
   * rail still docks (`layout`). */
  panes?: ShellPanes;
  /** Reports whether the pane tree is visible after measured fit and view/center gates. */
  onPaneModeChange?: (visible: boolean) => void;
  /** Personal positive preferences; null or omitted uses the default width. */
  railWidths?: { left: number | null; right: number | null };
  onRailWidthCommit?: (side: "left" | "right", width: number | null) => void;
}

const BOTH_DOCKED: LayoutMode = { leftDocked: true, rightDocked: true };

/** The app frame: top bar / (left rail, conversation, right rail) / status bar.
 * Ports the design's box metrics 1:1 — see e2e/layout.accept.test.ts.
 *
 * A rail the window is too narrow to dock is rendered as a modal drawer over the conversation
 * instead: the same nodes, reachable from a top-bar toggle, and absent from the DOM
 * while closed. Below 1280 that is the right rail, below 800 both. */
export function Shell({
  topBar,
  leftRail,
  header,
  banner,
  messages,
  composer,
  rightRail,
  statusBar,
  overlay,
  layout = BOTH_DOCKED,
  rails,
  view,
  viewSidebar,
  center,
  panes,
  onPaneModeChange,
  railWidths,
  onRailWidthCommit,
}: ShellProps) {
  const id = useId();
  const leftRequest = railWidths?.left ?? null;
  const rightRequest = railWidths?.right ?? null;
  const priorRequest = useRef({ left: leftRequest, right: rightRequest });
  const [wanted, setWanted] = useState({
    left: clampResize(leftRequest ?? 272, 220, 480),
    right: clampResize(rightRequest ?? 320, 240, 560),
  });
  const [collapsed, setCollapsed] = useState({ left: false, right: false });
  const [preview, setPreview] = useState<{ left: number | null; right: number | null }>({
    left: null,
    right: null,
  });
  const captured = useRef<Partial<Record<"left" | "right", { target: HTMLElement; id: number }>>>(
    {},
  );
  useLayoutEffect(() => {
    const requests = { left: leftRequest, right: rightRequest };
    for (const side of ["left", "right"] as const) {
      if (requests[side] === priorRequest.current[side]) continue;
      const drag = captured.current[side];
      if (drag?.target.hasPointerCapture(drag.id)) drag.target.releasePointerCapture(drag.id);
      setPreview((current) => ({ ...current, [side]: null }));
      setCollapsed((current) => ({ ...current, [side]: false }));
      const preference =
        side === "left"
          ? clampResize(requests.left ?? 272, 220, 480)
          : clampResize(requests.right ?? 320, 240, 560);
      setWanted((current) => ({ ...current, [side]: preference }));
    }
    priorRequest.current = requests;
  }, [leftRequest, rightRequest]);
  const [frame, setFrame] = useState<HTMLDivElement | null>(null);
  const [frameWidth, setFrameWidth] = useState<number | null>(null);
  const [frameHeight, setFrameHeight] = useState<number | null>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: docking changes discard local previews and rebind the current frame.
  useLayoutEffect(() => {
    setPreview({ left: null, right: null });
    setFrameWidth(null);
    setFrameHeight(null);
    if (!frame) return;
    let active = true;
    const measure = () => {
      if (!active) return;
      const width = frame.clientWidth;
      const height = frame.clientHeight;
      setFrameWidth((current) => (width > 0 || current !== null ? width : null));
      setFrameHeight((current) => (height > 0 || current !== null ? height : null));
    };
    measure();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(frame);
    window.addEventListener("resize", measure);
    return () => {
      active = false;
      observer?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [frame, layout.leftDocked, layout.rightDocked]);
  const hasViewSidebar =
    view !== undefined && view !== null && viewSidebar !== undefined && viewSidebar !== null;
  const hasViewRightRail = hasViewSidebar && rightRail !== undefined && rightRail !== null;
  const hasCenter =
    center !== undefined && center !== null && (view === undefined || view === null);
  const paneFloor = panes && panes.root.kind === "split" ? minSize(panes.root) : null;
  const paneReserve = (paneFloor?.width ?? 840) + 1;
  // At least 400px must remain for two usable rails. An oversized tree folds instead of
  // borrowing their entire width; a two-chat split at 1280 still leaves 220px and 219px.
  const canReservePanes = frameWidth === null || frameWidth >= paneReserve + 400;
  const reserve = hasViewRightRail ? 536 : paneFloor && canReservePanes ? paneReserve : 320;
  function allocateRails(centerReserve: number) {
    const available = frameWidth === null ? Infinity : Math.max(0, frameWidth - centerReserve);
    const widths = {
      left: layout.leftDocked && !collapsed.left ? wanted.left : 0,
      right:
        (!hasViewSidebar || hasViewRightRail) && layout.rightDocked && !collapsed.right
          ? wanted.right
          : 0,
    };
    let excess = Math.max(0, widths.left + widths.right - available);
    for (const [side, minimum] of [
      ["right", 240],
      ["left", 220],
      ["right", 0],
      ["left", 0],
    ] as const) {
      const shrink = Math.min(excess, Math.max(0, widths[side] - minimum));
      widths[side] -= shrink;
      excess -= shrink;
    }
    return { available, widths };
  }
  const allocation = allocateRails(reserve);
  const { widths } = allocation;
  function railMaximumFor(side: "left" | "right", allocated = allocation) {
    const other = side === "left" ? "right" : "left";
    return Math.max(
      0,
      Math.min(
        side === "left" ? 480 : 560,
        allocated.available - (preview[other] ?? allocated.widths[other]),
      ),
    );
  }
  function railWidthFor(side: "left" | "right", allocated = allocation) {
    if (!(side === "left" ? layout.leftDocked : layout.rightDocked)) return 0;
    return Math.min(preview[side] ?? allocated.widths[side], railMaximumFor(side, allocated));
  }
  const railMaximum = (side: "left" | "right") => railMaximumFor(side);
  // A lone pane keeps its ordinary rail sizes. Its first-split budget uses the same allocator
  // against two chat floors, so drop zones and tab actions see the width a split would receive.
  const rowAllocation =
    panes?.root.kind === "group" && canReservePanes && layout.rightDocked
      ? allocateRails(paneReserve)
      : allocation;
  const rowRoom =
    frameWidth === null
      ? null
      : frameWidth - railWidthFor("left", rowAllocation) - railWidthFor("right", rowAllocation) - 1;
  // A split root renders as panes once it fits the frame; an unmeasured frame (jsdom's default)
  // is optimistic rather than falling back to single mode on every initial render.
  const multiMode =
    paneFloor !== null &&
    (view === undefined || view === null) &&
    layout.rightDocked &&
    (rowRoom === null || (paneFloor.width <= rowRoom && paneFloor.height <= (frameHeight ?? 0)));
  useLayoutEffect(
    () => onPaneModeChange?.(multiMode && !hasCenter),
    [multiMode, hasCenter, onPaneModeChange],
  );
  const centerFocus = useCallback(() => {
    const pane = frame?.querySelector<HTMLElement>('[data-region="center-pane"]');
    const target = [...(pane?.querySelectorAll<HTMLElement>("button, [tabindex]") ?? [])].find(
      (node) => !node.matches(":disabled") && !node.closest("[hidden], [inert]"),
    );
    return target ?? pane ?? null;
  }, [frame]);
  const centerInertOwned = useRef(false);
  const drawerOpen =
    (!layout.leftDocked && rails?.drawer === "left") ||
    (!layout.rightDocked && rails?.drawer === "right");
  useLayoutEffect(() => {
    if (!frame) return;
    if (hasCenter) centerInertOwned.current = true;
    if (!centerInertOwned.current) return;
    // Drawer cleanup restores the inert values it saw at entry, before this center changed.
    // Reconcile only the pane this slot owns, after that cleanup has finished.
    for (const pane of frame.querySelectorAll('[data-region="conversation"]'))
      pane.toggleAttribute("inert", hasCenter || drawerOpen);
    if (!hasCenter) {
      if (!drawerOpen) centerInertOwned.current = false;
      return;
    }
    if (drawerOpen) return;
    const active = document.activeElement;
    if (frame.querySelector('[data-region="conversation"]')?.contains(active))
      centerFocus()?.focus({ preventScroll: true });
  }, [hasCenter, frame, centerFocus, drawerOpen]);

  function dockedRail(side: "left" | "right", content: ReactNode) {
    const left = side === "left";
    const defaultWidth = left ? 272 : 320;
    const nominalMinimum = left ? 220 : 240;
    const maximum = railMaximum(side);
    const width = Math.min(preview[side] ?? widths[side], maximum);
    const clearPreview = () => {
      delete captured.current[side];
      setPreview((current) => ({ ...current, [side]: null }));
    };
    return (
      <div
        className="shell-rail-host"
        data-resize-rail={side}
        style={{ width }}
        onPointerDownCapture={(event) => {
          const drag = captured.current[side];
          if (event.button !== 0 || !event.isPrimary || drag?.target.hasPointerCapture(drag.id))
            return;
          if (
            !(event.target instanceof HTMLElement) ||
            event.target.parentElement !== event.currentTarget ||
            event.target.getAttribute("role") !== "separator"
          )
            return;
          captured.current[side] = { target: event.target, id: event.pointerId };
        }}
        onPointerUp={(event) => {
          const drag = captured.current[side];
          if (
            drag &&
            event.isPrimary &&
            event.pointerId === drag.id &&
            !drag.target.hasPointerCapture(drag.id)
          )
            clearPreview();
        }}
      >
        <div
          className="shell-rail-content"
          id={`${id}-${side}-content`}
          hidden={width === 0}
          inert={width === 0}
        >
          {content}
        </div>
        <ResizeHandle
          label={`Resize ${side} rail`}
          controls={`${id}-${side}-content`}
          orientation="vertical"
          growDirection={left ? 1 : -1}
          value={width}
          min={Math.min(nominalMinimum, maximum)}
          max={maximum}
          defaultValue={defaultWidth}
          collapsible
          onPreview={(next) => {
            const drag = captured.current[side];
            if (drag && !drag.target.hasPointerCapture(drag.id)) clearPreview();
            else setPreview((current) => ({ ...current, [side]: next }));
          }}
          onCommit={(next, intent) => {
            clearPreview();
            setCollapsed((current) => ({ ...current, [side]: intent === "collapse" }));
            if (intent === "reset") {
              setWanted((current) => ({ ...current, [side]: defaultWidth }));
              onRailWidthCommit?.(side, null);
            } else if (intent === "resize" && next >= nominalMinimum) {
              const preference = clampResize(next, nominalMinimum, left ? 480 : 560);
              setWanted((current) => ({ ...current, [side]: preference }));
              onRailWidthCommit?.(side, preference);
            } else if (intent === "restore") {
              onRailWidthCommit?.(side, wanted[side]);
            }
          }}
        />
      </div>
    );
  }
  // Never called while `rails` is absent: without it no drawer can be open.
  const closeDrawer = rails ? rails.onClose : () => undefined;
  // Where focus goes when a drawer closes and the toggle that opened it is no longer there —
  // which is what happens when the window grows until the rail docks: TopBar drops the toggle in
  // the same commit that closes the drawer, so useModalFocus's own restore has nothing connected
  // to return to and focus would fall to document.body. The composer is the same last-resort
  // target App.restoreCloseFocus already falls back to; a center slot supplies its visible target.
  const restoreFocus = () => {
    if (hasCenter) return centerFocus();
    if (hasViewSidebar) return document.querySelector<HTMLElement>('[data-region="view"] h1');
    return (
      frame?.querySelector<HTMLElement>('.pane[data-focused] [aria-label="Message"]') ??
      frame?.querySelector<HTMLElement>(
        'main.conversation > .composer-slot [aria-label="Message"]',
      ) ??
      null
    );
  };
  // Single mode's own edge-drop zones only offer a first split: once the layout already holds
  // more than one group, a too-narrow fallback only merges a drop into the pane it is shown in.
  // The row zone reads `rowRoom`, not this box's own width: a first split allocates both docked
  // rails against the pane floors before deciding how much center width remains.
  // Unmeasured (null) is optimistic, same as multi mode's own fit check above. The split must
  // leave the pane its own floor beside the dragged kind's.
  const paneDrop = usePaneDrop({
    edges: (rect, kind) => {
      if (panes?.root.kind !== "group" || !panes.canSplit) return { row: false, column: false };
      const own = minSize(panes.root);
      const floor = kind === "file" ? FILE_PANE_MIN : PANE_MIN;
      return {
        row: rowRoom === null || rowRoom >= own.width + floor.width,
        column: rect.height >= own.height + floor.height,
      };
    },
    onDrop: (payload, zone) => {
      if (panes) panes.onDrop(panes.focused, payload, zone);
    },
  });
  if (view !== undefined && view !== null) {
    return (
      <div className="shell" data-region="shell">
        {topBar}
        {hasViewSidebar ? (
          <div className="shell-body" ref={setFrame}>
            {layout.leftDocked ? dockedRail("left", viewSidebar) : null}
            <main className="app-view app-view-with-sidebar" data-region="view">
              {view}
            </main>
            {hasViewRightRail && layout.rightDocked ? dockedRail("right", rightRail) : null}
            {layout.leftDocked ? null : (
              <RailDrawer
                side="left"
                label="Sessions and agents"
                open={rails?.drawer === "left"}
                onClose={closeDrawer}
                restoreFocus={restoreFocus}
              >
                {viewSidebar}
              </RailDrawer>
            )}
            {hasViewRightRail && !layout.rightDocked ? (
              <RailDrawer
                side="right"
                label="VIKI panel"
                open={rails?.drawer === "right"}
                onClose={closeDrawer}
                restoreFocus={restoreFocus}
              >
                {rightRail}
              </RailDrawer>
            ) : null}
          </div>
        ) : (
          <main className="app-view" data-region="view">
            {view}
          </main>
        )}
        {statusBar}
      </div>
    );
  }
  const singleComposer =
    panes?.root.kind === "group" ? panes.pane(panes.root.id).composer : undefined;
  const conversationMain =
    panes && multiMode ? (
      <main
        className="conversation conversation-panes"
        data-region="conversation"
        hidden={hasCenter}
        inert={hasCenter}
      >
        {banner}
        <PaneTree
          root={panes.root}
          renderGroup={(group) => (
            <PaneSection
              group={group}
              panes={panes}
              focused={group.id === panes.focused}
              composerFallback={composer}
            />
          )}
          onResize={panes.onResize}
        />
        <div className="conversation-overlay">{overlay}</div>
      </main>
    ) : (
      <main
        className="conversation"
        data-region="conversation"
        hidden={hasCenter}
        inert={hasCenter}
        data-pane={panes?.focused}
        // The row room a first split would leave each half, for canSplitTab to read off this
        // same box instead of the box's own (still fully docked) width; absent until measured.
        data-row-room={panes && rowRoom !== null ? rowRoom : undefined}
        onDragOver={panes ? paneDrop.onDragOver : undefined}
        onDragLeave={panes ? paneDrop.onDragLeave : undefined}
        onDrop={panes ? paneDrop.onDrop : undefined}
      >
        <div className="conversation-header" data-region="conversation-header">
          {header}
        </div>
        {banner}
        <div className="conversation-scroll">
          <div className="conversation-messages">
            <div className="message-column" data-region="message-column">
              {messages}
            </div>
          </div>
        </div>
        {panes?.root.kind === "group" && kindOf(panes.root) === "file" ? null : (
          <ComposerSlot>{singleComposer === undefined ? composer : singleComposer}</ComposerSlot>
        )}
        <div className="conversation-overlay">{overlay}</div>
        {panes ? <PaneDropOverlay zone={paneDrop.zone} /> : null}
      </main>
    );
  return (
    <div className="shell" data-region="shell">
      {topBar}
      <div className="shell-body" ref={setFrame}>
        {layout.leftDocked ? dockedRail("left", leftRail) : null}
        {conversationMain}
        {hasCenter ? (
          <main className="shell-center" data-region="center-pane" tabIndex={-1}>
            {center}
          </main>
        ) : null}
        {layout.rightDocked ? dockedRail("right", rightRail) : null}

        {/* Last in the body, so the open one is the last element in it and useModalFocus inerts
            everything before it: the conversation, the rail that is still docked, and — walking
            up — the top bar and the status bar. A closed drawer renders nothing at all. */}
        {layout.leftDocked ? null : (
          <RailDrawer
            side="left"
            label="Sessions and agents"
            open={rails?.drawer === "left"}
            onClose={closeDrawer}
            restoreFocus={restoreFocus}
          >
            {leftRail}
          </RailDrawer>
        )}
        {layout.rightDocked ? null : (
          <RailDrawer
            side="right"
            label="VIKI panel"
            open={rails?.drawer === "right"}
            onClose={closeDrawer}
            restoreFocus={restoreFocus}
          >
            {rightRail}
          </RailDrawer>
        )}
      </div>
      {statusBar}
    </div>
  );
}

/** One group in a multi-pane layout, with a persistent composer slot for each chat pane. */
function PaneSection({
  group,
  panes,
  focused,
  composerFallback,
}: {
  group: PaneGroup;
  panes: ShellPanes;
  focused: boolean;
  composerFallback: ReactNode;
}) {
  const { header, messages, composer, label, onActivate } = panes.pane(group.id);
  const drop = usePaneDrop({
    // The split must leave this pane its own floor beside the dragged kind's.
    edges: (rect, kind) => {
      const own = minSize(group);
      const floor = kind === "file" ? FILE_PANE_MIN : PANE_MIN;
      return {
        row: panes.canSplit && rect.width >= own.width + floor.width,
        column: panes.canSplit && rect.height >= own.height + floor.height,
      };
    },
    onDrop: (payload, zone) => panes.onDrop(group.id, payload, zone),
  });
  return (
    <section
      className="conversation pane"
      data-pane={group.id}
      data-focused={focused || undefined}
      aria-label={label}
      onPointerDownCapture={onActivate}
      onFocusCapture={onActivate}
      onDragOver={drop.onDragOver}
      onDragLeave={drop.onDragLeave}
      onDrop={drop.onDrop}
    >
      <div className="conversation-header">{header}</div>
      <div className="conversation-scroll">
        <div className="conversation-messages">
          <div className="message-column">{messages}</div>
        </div>
      </div>
      {kindOf(group) === "chat" ? (
        <ComposerSlot>
          {composer === undefined ? (focused ? composerFallback : null) : composer}
        </ComposerSlot>
      ) : null}
      <PaneDropOverlay zone={drop.zone} />
    </section>
  );
}
