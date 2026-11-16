/** Renders grouped session rows with selection, live state, listing status and optional archive and menu actions. The parent owns session actions; this component reports row and control interactions. */
import {
  type KeyboardEvent,
  type ReactElement,
  type ReactNode,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type { SessionColor } from "../../clients/conversations/sessionMetadataApi";
import type { SessionRow } from "../../protocol";
import { SESSION_COLOR_LABELS } from "../../state/accentLabels";
import type { SessionGroup } from "../../state/rows";
import { rowTitle, timeLabel } from "../../state/rows";
import { CHAT_DRAG_TYPE } from "../Panes/paneDrop";
import { RootLabel } from "../RootLabel/RootLabel";
import { OrganizedSessionMenu } from "./OrganizedSessionMenu";
import { RowMenu } from "./RowMenu";
import { type RowContext, rowMetaParts, rowState } from "./rowState";
import "./rowState.css";

export interface SessionRowMenu {
  onFork: (sessionId: string) => void;
  onOpenTranscript: (sessionId: string) => void;
  onCloseSession: (sessionId: string) => void;
}

export interface SessionArchive {
  isArchived: (sessionId: string) => boolean;
  onToggle: (sessionId: string) => void;
  pending: ReadonlySet<string>;
}

export interface SessionOrganization {
  isPinned: (sessionId: string) => boolean;
  pending: ReadonlySet<string>;
  onRename: (sessionId: string) => void;
  onPin: (sessionId: string) => void;
  unreadOf?: (sessionId: string) => boolean;
  colorOf?: (sessionId: string) => SessionColor | null;
  onUnread?: (sessionId: string) => void;
  onColor?: (sessionId: string, color: SessionColor | null) => void;
  onNewWindow?: (sessionId: string) => void;
  newWindowPending?: ReadonlySet<string>;
  newWindowErrorOf?: (sessionId: string) => string | null;
  onExport?: (sessionId: string) => void;
  onOpenTerminal?: (sessionId: string) => void;
  canOpenTerminal?: (sessionId: string) => boolean;
  onNewChatInWorkspace?: (sessionId: string) => void;
}

export interface SessionListProps {
  groups: readonly SessionGroup[];
  other: readonly SessionRow[];
  selectedId: string | null;
  liveIds: ReadonlySet<string>;
  nowMs: number;
  onOpen: (sessionId: string) => void;
  /** Row status details. Without it, rows use the liveIds-only wording. */
  rowContext?: RowContext;
  /** Without it no row shows a `⋯`, and RowMenu never mounts. */
  menu?: SessionRowMenu;
  /** The listing note (state/catalog.ts listingNote), or null when the rows speak for
   * themselves. Without it the rail renders no note. */
  listing?: string | null;
  /** The folder a session runs in, shown after its meta; null or absent shows nothing. */
  folderOf?: (sessionId: string) => string | null;
  /** True shows a "Started by VIKI" chip after the row's title. Without it (or when it returns
   * false) the row shows no such chip. */
  startedByViki?: (sessionId: string) => boolean;
  /** Archive toggle state. Without it no row carries `sess-archivable` and no `.sess-archive`
   * button renders. */
  archive?: SessionArchive;
  /** App-owned metadata actions on listed rows, including transcripts this window does not hold. */
  organization?: SessionOrganization;
  /** Each row is a drag source carrying its chat id as `CHAT_DRAG_TYPE`, for a pane or tab
   * strip to drop. */
  dragRows?: boolean;
}

export function SessionList({
  groups,
  other,
  selectedId,
  liveIds,
  nowMs,
  onOpen,
  rowContext,
  menu,
  listing,
  folderOf,
  startedByViki,
  archive,
  organization,
  dragRows,
}: SessionListProps): ReactElement {
  const [showOther, setShowOther] = useState(false);
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const pinFocus = useRef<string | null>(null);

  // Pinning moves a row between groups, which replaces its menu trigger in the DOM.
  useLayoutEffect(() => {
    const id = pinFocus.current;
    if (id === null || organization?.pending.has(id)) return;
    pinFocus.current = null;
    const active = document.activeElement;
    if (
      active instanceof HTMLElement &&
      active !== document.body &&
      active.closest<HTMLElement>(".sess")?.dataset.sessionId !== id
    )
      return;
    const row = [...(listRef.current?.querySelectorAll<HTMLElement>(".sess") ?? [])].find(
      (element) => element.dataset.sessionId === id,
    );
    (row?.querySelector<HTMLElement>(".sess-menu") ?? listRef.current)?.focus({
      preventScroll: true,
    });
  });

  // Escape closes the menu from wherever focus is, and belongs to the window for the same
  // reason SearchPalette's does. The IME guard is the same one: a keydown that closes a
  // candidate window is the IME's Escape, not the user's.
  useEffect(() => {
    if (menuFor === null) return;
    function onWindowKeyDown(event: globalThis.KeyboardEvent): void {
      if (event.isComposing || event.keyCode === 229) return;
      if (event.key === "Escape") setMenuFor(null);
    }
    window.addEventListener("keydown", onWindowKeyDown);
    return () => window.removeEventListener("keydown", onWindowKeyDown);
  }, [menuFor]);

  function act(sessionId: string, run: (id: string) => void): void {
    setMenuFor(null);
    run(sessionId);
  }

  /** Archiving removes this row — and, when it came from the menu, the menu with it — which
   * would otherwise drop focus to <body>. Move it to the row taking this one's place first,
   * while this row is still here to find its neighbours in `.sessions`. */
  function focusAfterArchive(sessionId: string): void {
    const list = listRef.current;
    if (!list) return;
    const rows = [...list.querySelectorAll<HTMLElement>(".sess")];
    const index = rows.findIndex((el) => el.dataset.sessionId === sessionId);
    if (index === -1) return;
    (rows[index + 1] ?? rows[index - 1] ?? list).focus();
  }

  function actArchive(sessionId: string, toggle: (id: string) => void): void {
    setMenuFor(null);
    focusAfterArchive(sessionId);
    toggle(sessionId);
  }

  /** A meta line as `.m`'s class plus its children, kept apart so the `⋯` can be appended
   * inside the same flex box (see rowState.css on why it cannot live outside it). Null when
   * the row has no meta at all. */
  type Meta = { className: string; children: ReactNode };

  /** Metadata for a row whose state rowContext knows. Null for "plain": the plain row's rule
   * decides whether a plain row shows its time, and this must not second-guess it. */
  function stateMeta(session: SessionRow, context: RowContext): Meta | null {
    const state = rowState(session, context);
    if (state === "plain") return null;
    const parts = rowMetaParts(session, state, context, nowMs);
    const dotClass = parts.dot === "ring" ? "sess-dot sess-dot-live" : "sess-dot breathe";
    return {
      className: state === "writing" ? "m sess-meta-writing" : "m",
      children: [
        parts.chip ? (
          <span className={parts.chipDim ? "chip dim" : "chip"} key="chip">
            {parts.chip}
          </span>
        ) : null,
        parts.dot === "none" ? null : <span className={dotClass} aria-hidden="true" key="dot" />,
        parts.text,
      ],
    };
  }

  /** The row metadata: the live row's breathing dot and "· live", the Today row's time,
   * nothing anywhere else. */
  function plainMeta(session: SessionRow, live: boolean, showTime: boolean): Meta | null {
    if (live)
      return {
        className: "m",
        children: [
          <span className="sess-dot breathe" aria-hidden="true" key="dot" />,
          `${timeLabel(session.modified, nowMs)} · live`,
        ],
      };
    if (showTime) return { className: "m", children: timeLabel(session.modified, nowMs) };
    return null;
  }

  function row(session: SessionRow, showTime: boolean): ReactElement {
    const id = session.session_id;
    const live = liveIds.has(id);
    const state = rowContext ? rowState(session, rowContext) : null;
    const view = rowContext?.app.sessions[id];
    const latestTurn = view?.turns.at(-1);
    // Idle and replayed history are not evidence that this window observed a successful turn.
    const completed =
      state === "live" &&
      view &&
      !view.closed &&
      !view.blocked &&
      !view.error &&
      !view.rateLimit &&
      !view.compacting &&
      latestTurn?.status === "success" &&
      latestTurn.history === false;
    const activity =
      state === "writing"
        ? "running"
        : state === "waiting"
          ? "needs-you"
          : completed
            ? "done"
            : null;
    const classes = ["sess"];
    if (id === selectedId) classes.push("sess-selected");
    if (live) classes.push("sess-live");
    if (archive) classes.push("sess-archivable");
    if (activity) classes.push("sess-has-activity");
    const unread = organization?.unreadOf?.(id) ?? false;
    const color = organization?.colorOf?.(id) ?? null;
    if (unread) classes.push("sess-unread");
    const archived = archive?.isArchived(id) ?? false;
    const started = startedByViki?.(id) ?? false;
    // `liveIds` is what App knows the app itself holds, which is exactly when Close and Fork
    // mean something; a transcript row we are not holding gets no `⋯` (the design draws it on
    // the live row alone).
    const organized = organization && archive;
    const hasMenu = !organized && menu !== undefined && live;
    const folder = folderOf?.(id) ?? null;
    const meta =
      (rowContext ? stateMeta(session, rowContext) : null) ??
      plainMeta(session, live, showTime) ??
      (folder || organization ? { className: "m", children: null } : null);
    return (
      // A real <button> would fire its own click on Enter/Space in addition to the handler
      // below, double-calling onOpen: the custom-widget div+role pattern avoids that.
      // biome-ignore lint/a11y/useSemanticElements: see above
      <div
        key={id}
        className={classes.join(" ")}
        role="button"
        tabIndex={0}
        data-session-id={id}
        data-unread={unread || undefined}
        draggable={dragRows || undefined}
        onDragStart={
          dragRows ? (event) => event.dataTransfer.setData(CHAT_DRAG_TYPE, id) : undefined
        }
        onClick={() => onOpen(id)}
        onKeyDown={(event: KeyboardEvent<HTMLDivElement>) => {
          if (
            event.target === event.currentTarget &&
            (event.key === "Enter" || event.key === " ")
          ) {
            event.preventDefault();
            onOpen(id);
          }
        }}
      >
        {activity ? (
          <span className="sess-activity" data-activity={activity} aria-hidden="true" />
        ) : null}
        {color ? (
          <span
            className="sess-color-label"
            data-color={color}
            style={{ backgroundColor: `var(--label-${color})` }}
            role="img"
            aria-label={`${SESSION_COLOR_LABELS[color]} color label`}
          />
        ) : null}
        {started ? (
          <span className="t jp sess-title-chip">
            <span className="t-label">{rowTitle(session)}</span>
            <span className="chip dim">Started by VIKI</span>
          </span>
        ) : (
          <span className="t jp">{rowTitle(session)}</span>
        )}
        {meta ? (
          <span className={meta.className}>
            {meta.children}
            {unread ? <span className="sess-unread-label">Unread</span> : null}
            {folder ? <RootLabel text={folder} /> : null}
            {organization && archive ? (
              <OrganizedSessionMenu
                sessionId={id}
                pinned={organization.isPinned(id)}
                archived={archived}
                pending={organization.pending.has(id)}
                onRename={() => organization.onRename(id)}
                onPin={() => {
                  pinFocus.current = id;
                  organization.onPin(id);
                }}
                onArchive={() => actArchive(id, archive.onToggle)}
                unread={unread}
                color={color}
                newWindowPending={organization.newWindowPending?.has(id) ?? false}
                newWindowError={organization.newWindowErrorOf?.(id) ?? null}
                {...(organization.onUnread ? { onUnread: () => organization.onUnread?.(id) } : {})}
                {...(organization.onColor
                  ? { onColor: (value: SessionColor | null) => organization.onColor?.(id, value) }
                  : {})}
                {...(organization.onNewWindow
                  ? { onNewWindow: () => organization.onNewWindow?.(id) }
                  : {})}
                {...(organization.onExport ? { onExport: () => organization.onExport?.(id) } : {})}
                {...(organization.onOpenTerminal && organization.canOpenTerminal?.(id)
                  ? { onOpenTerminal: () => organization.onOpenTerminal?.(id) }
                  : {})}
                {...(organization.onNewChatInWorkspace
                  ? { onNewChatInWorkspace: () => organization.onNewChatInWorkspace?.(id) }
                  : {})}
                {...(live && menu
                  ? {
                      onFork: () => menu.onFork(id),
                      onOpenTranscript: () => menu.onOpenTranscript(id),
                      onClose: () => menu.onCloseSession(id),
                    }
                  : {})}
              />
            ) : null}
            {hasMenu ? (
              <button
                type="button"
                className="sess-menu"
                aria-label="Session menu"
                aria-expanded={menuFor === id}
                onClick={(event) => {
                  // Without this the row's own onClick would also fire and open the session.
                  event.stopPropagation();
                  setMenuFor((open) => (open === id ? null : id));
                }}
              >
                ⋯
              </button>
            ) : null}
          </span>
        ) : null}
        {archive ? (
          <button
            type="button"
            className="sess-archive"
            aria-label={`${archived ? "Unarchive" : "Archive"} ${rowTitle(session)}`}
            disabled={archive.pending.has(id)}
            onClick={(event) => {
              // Without this the row's own onClick would also fire and open the session.
              event.stopPropagation();
              focusAfterArchive(id);
              archive.onToggle(id);
            }}
          >
            <svg className="ico" width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
              <path d="M2.5 4.5h11v2h-11zM3.5 6.5v6h9v-6M6.5 9h3" />
            </svg>
          </button>
        ) : null}
        {hasMenu && menuFor === id && menu ? (
          // The menu is absolutely positioned inside `.sess`, which is already `position:
          // relative`; a click inside it must not reach the row either.
          // biome-ignore lint/a11y/noStaticElementInteractions: a click shield, not a control
          // biome-ignore lint/a11y/useKeyWithClickEvents: keyboard reaches the items themselves
          <div onClick={(event) => event.stopPropagation()}>
            <RowMenu
              onFork={() => act(id, menu.onFork)}
              onOpenTranscript={() => act(id, menu.onOpenTranscript)}
              onClose={() => act(id, menu.onCloseSession)}
              archived={archived}
              // exactOptionalPropertyTypes: omit the key entirely rather than pass `undefined`.
              {...(archive ? { onArchive: () => actArchive(id, archive.onToggle) } : {})}
            />
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <div className="sessions" ref={listRef} tabIndex={-1}>
      {/* Its own class, never `.lbl` or `.sess`: the note is neither a group nor a row, and
          counting either must not change because the list could not be read. */}
      {listing ? <div className="sess-note">{listing}</div> : null}
      {groups.map((group) => (
        <div className="sess-group" key={group.label}>
          <div className="lbl">{group.label}</div>
          {group.rows.map((session) => row(session, group.label === "Today"))}
        </div>
      ))}
      {other.length > 0 ? (
        <div className="sess-group">
          <button
            type="button"
            className="lbl sess-other"
            onClick={() => setShowOther((shown) => !shown)}
          >
            {`Other (${other.length})`}
          </button>
          {showOther ? other.map((session) => row(session, false)) : null}
        </div>
      ) : null}
    </div>
  );
}
