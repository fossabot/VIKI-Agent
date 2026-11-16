/** Render one session's turns in order, grouping adjacent tool and notice blocks so they stay in context. Auto-follow applies near the bottom; targeted block navigation stays scoped to its turn. Provider content uses the safe Markdown and text renderers. */

import * as Dropdown from "@radix-ui/react-dropdown-menu";
import {
  memo,
  type ReactElement,
  type ReactNode,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type { TurnStatus } from "../../protocol";
import type { ExcerptApi } from "../../state/excerptApi";
import { timeLabel } from "../../state/rows";
import type { Block, NoticeBlock, SessionView, TextBlock, ToolBlock, Turn } from "../../state/view";
import { NoticeRow, RoutingRow, ThoughtRow, ToolBlockRows } from "./Blocks";
import "./Conversation.css";
import { t } from "../../i18n/t";
import { withoutSpokenAnswerRequest } from "../../voice/spokenAnswer";
import { Avatar } from "../Avatar/Avatar";
import { useIdentity } from "../Avatar/identity";
import { Face } from "../Face/Face";
import { SourcesStrip } from "../Sources/SourcesStrip";
import { AttachmentImages } from "./AttachmentImages";
import { CopyButton } from "./Copy";
import { ExcerptText } from "./ExcerptText";
import { Markdown } from "./Markdown";
import { ReadAloudContext, type ReadAloudSettings, useReadAloudReply } from "./ReadAloud";

/** What the name row over a sent message says when no display name is configured. */
const DEFAULT_USER_LABEL = "You";

export interface UserTurnProps {
  user: NonNullable<Turn["user"]>;
  nowMs: number;
  /** The user's `identity.display_name` setting; "You" when they have not set one. The
   * explicit `undefined` is what lets Conversation forward its own optional prop unchanged
   * under `exactOptionalPropertyTypes`. */
  userLabel?: string | undefined;
  /** With the active workspace's id, shows the vault excerpts in the text as citations
   * (ExcerptText.tsx); without either, the text is rendered as it always was. */
  excerptApi?: ExcerptApi | undefined;
  workspaceId?: string | undefined;
  onOpenVault?: ((ref: string, start: number) => void) | undefined;
}

export function UserTurn({
  user,
  nowMs,
  userLabel = DEFAULT_USER_LABEL,
  excerptApi,
  workspaceId,
  onOpenVault,
}: UserTurnProps): ReactElement {
  const identity = useIdentity();
  const heard = withoutSpokenAnswerRequest(user.text);
  const text = heard ?? user.text;
  return (
    <div className="turn-user">
      <div className="user-turn jp">
        {user.command ? <span className="cmd-chip mono">{user.command}</span> : null}
        {heard !== null ? (
          <span className="voice-chip">
            <span aria-hidden="true">{t("handsfree.voice.tag")}</span>
            <span className="sr-only">{t("handsfree.voice.label")}</span>
          </span>
        ) : null}
        {excerptApi && workspaceId ? (
          <ExcerptText
            text={text}
            api={excerptApi}
            workspaceId={workspaceId}
            onOpenVault={onOpenVault}
          />
        ) : (
          text
        )}
        {user.attachments?.length || user.unavailable_attachments?.length ? (
          <AttachmentImages
            attachments={user.attachments ?? []}
            unavailable={user.unavailable_attachments ?? []}
          />
        ) : null}
      </div>
      <div className="user-at">{`${userLabel} · ${timeLabel(user.at, nowMs)}`}</div>
      {identity.userAvatarId ? (
        <Avatar
          name={userLabel}
          avatarId={identity.userAvatarId}
          accent="slate"
          shape="circle"
          size={20}
        />
      ) : null}
    </div>
  );
}

/** Everything a tool/notice panel does not swallow into itself: rendered one at a time, in
 * order, by `renderBlock`. */
type PlainBlock = Exclude<Block, ToolBlock | NoticeBlock>;

function isPanelBlock(block: Block): block is ToolBlock | NoticeBlock {
  return block.kind === "tools" || block.kind === "notice";
}

interface PanelItem {
  kind: "panel";
  /** The run's first block id: stable across re-renders as rows are appended to it. */
  id: string;
  blocks: (ToolBlock | NoticeBlock)[];
}

interface PlainItem {
  kind: "plain";
  block: PlainBlock;
  /** Position in `turn.blocks`, so `renderBlock` can still tell the last block of the turn from
   * an interior one — grouping otherwise loses that once several blocks fold into one item. */
  index: number;
}

/** Folds every maximal run of ADJACENT "tools"/"notice" blocks into one `PanelItem`, in arrival
 * order — see this file's "Grouped tool/notice panels" contract above. */
function groupBlocks(blocks: readonly Block[]): (PanelItem | PlainItem)[] {
  const items: (PanelItem | PlainItem)[] = [];
  blocks.forEach((block, index) => {
    if (!isPanelBlock(block)) {
      items.push({ kind: "plain", block, index });
      return;
    }
    const last = items.at(-1);
    if (last?.kind === "panel") {
      last.blocks.push(block);
    } else {
      items.push({ kind: "panel", id: block.id, blocks: [block] });
    }
  });
  return items;
}

/** Parsing itself now runs in a worker under a deadline (Markdown.tsx), never on this thread, so
 * this memo is not what keeps a `text.delta` off the frame budget. What it still buys: a resumed
 * transcript arrives as a 20-turn page, ~250 blocks, and without a memo every delta would still
 * reconcile every one of them, not just re-render the block that actually changed.
 *
 * The wrapper sits at the call site rather than in Markdown.tsx because this is the only place
 * that renders hundreds of them at once (Cards renders exactly one plan). `text` and `streaming`
 * are plain values and `onOpenUrl` is App's `viki.actions.openUrl` off an object built once, so
 * a block that has stopped changing bails out for the rest of the session — including the
 * finished blocks of the turn that is still streaming, which `Reply`'s own memo cannot save. */
const MemoizedMarkdown = memo(Markdown);

function renderBlock(
  block: PlainBlock,
  isLast: boolean,
  turnRunning: boolean,
  onOpenUrl: (url: string) => void,
  targetBlockId: string | undefined,
): ReactElement {
  switch (block.kind) {
    case "text":
      return (
        <MemoizedMarkdown
          key={block.id}
          text={block.text}
          streaming={!block.done && turnRunning && isLast}
          onOpenUrl={onOpenUrl}
        />
      );
    case "thinking":
      return <ThoughtRow key={block.id} block={block} />;
    case "routing":
      return (
        <RoutingRow
          key={block.id}
          block={block}
          background={block.status === "running" && !turnRunning}
          target={block.id === targetBlockId}
        />
      );
  }
}

/** One shared `.tools mono` panel for a run of adjacent tools/notice blocks — a "tools" block
 * contributes its rows via `ToolBlockRows`, a "notice" block words itself via `NoticeRow`;
 * neither carries a panel of its own (Blocks.tsx). */
function renderPanel(item: PanelItem): ReactElement {
  return (
    <div className="tools mono" key={item.id}>
      {item.blocks.map((block) =>
        block.kind === "tools" ? (
          <ToolBlockRows key={block.id} block={block} />
        ) : (
          <NoticeRow key={block.id} block={block} />
        ),
      )}
    </div>
  );
}

/** What the reply's own CopyButton puts on the clipboard: every text block's own words, in
 * arrival order, joined by a blank line — never a tool row, a thought, or "VIKI" itself. Read off
 * `turn` at click time (a plain field read, not a DOM query, so there is nothing to memoize): by
 * the time the user clicks, `turn` is whatever Reply's latest render received. */
function replyText(turn: Turn): string {
  return turn.blocks
    .filter((block): block is TextBlock => block.kind === "text")
    .map((block) => block.text)
    .join("\n\n");
}

export interface ReplyProps {
  turn: Turn;
  onOpenUrl: (url: string) => void;
  /** Set only on the turn `jumpTo.turnId` names (Conversation's render loop below) — passed to
   * `renderBlock` so the one routing block it names, if any, renders with `target={true}`. */
  targetBlockId?: string | undefined;
}

/** Memoized on the turn, which is what stops a delta from re-rendering the whole transcript: the
 * reducer replaces only the turn a frame touched (`replaceTurn` slices and writes one index), so
 * every other turn on screen comes back as the same object and bails out here — its markdown,
 * its tool rows and its thoughts are never reconciled again. `onOpenUrl` is stable (see
 * MemoizedMarkdown above), so nothing else defeats it.
 *
 * Deliberately NOT on the `.turn` wrapper below: that one also renders `UserTurn`, which takes
 * `nowMs` — `Date.now()` at every App render — so a memo there would never bail out. */
export const Reply = memo(function Reply({
  turn,
  onOpenUrl,
  targetBlockId,
}: ReplyProps): ReactElement {
  const identity = useIdentity();
  const unresolved = identity.unresolved ?? false;
  const profile = unresolved ? null : identity.profile;
  const running = turn.status === "running";
  const lastIndex = turn.blocks.length - 1;
  const items = groupBlocks(turn.blocks);
  const hasWords = turn.blocks.some((block) => block.kind === "text");
  const readAloudUi = useReadAloudReply(turn);

  return (
    <div className={running ? "reply reply-has-activity" : "reply"}>
      <Face size={36} presence={running} />
      <div className="reply-body">
        <div className="reply-name-row">
          {unresolved ? null : <span className="reply-name">{identity.assistantName}</span>}
          {profile && !profile.botProfileId ? (
            <>
              <span className="chip">{`r${profile.revision}`}</span>
              <span className="chip">Profile</span>
            </>
          ) : null}
          {hasWords ? <CopyButton label="Copy reply" getText={() => replyText(turn)} /> : null}
          {readAloudUi.controls}
        </div>
        {readAloudUi.note}
        {items.map((item) =>
          item.kind === "panel"
            ? renderPanel(item)
            : renderBlock(item.block, item.index === lastIndex, running, onOpenUrl, targetBlockId),
        )}
        {turn.status === "cancelled" ? <div className="reply-cancelled">Stopped</div> : null}
      </div>
    </div>
  );
});

/** Auto-follow keeps the live edge in view until the user deliberately scrolls away from it. */
const FOLLOW_THRESHOLD_PX = 80;

/** The element that actually scrolls: Shell wraps this column in `.conversation-scroll`
 * (`.conversation-list` itself is a `flex:1` child of the auto-height `.message-column`,
 * which never becomes a scroll box). Falls back to the column when no such ancestor exists,
 * which is how the acceptance test renders it — standalone, driving `.conversation-list`'s
 * own scroll metrics directly. */
/** Within this of the top, the user is asking for what came before. */
const OLDER_THRESHOLD_PX = 120;

function scrollHostOf(list: HTMLDivElement): HTMLElement {
  // `[data-scroll-host]` is the launcher panel's own marker (LauncherChat.tsx's
  // `.dloop-chat-messages`): the panel has no `.conversation-scroll` three-layer wrapper
  // (Shell.css, Shell.tsx), so it names itself the scroll host directly rather than taking on
  // that class's own CSS and Composer.tsx's `.composer-slot`-keyed sizing, neither of which the
  // panel's layout uses.
  return list.closest<HTMLElement>(".conversation-scroll, [data-scroll-host]") ?? list;
}

/** Where the user was reading. `top` alone goes wrong when the content above re-lays out (a
 * remount shows its Markdown plain until the worker answers), so the first visible turn and its
 * offset from the scroll box's top are kept too, and win when that turn is rendered. */
export interface SavedScroll {
  top: number;
  following: boolean;
  turnId?: string;
  offset?: number;
}

function turnNode(list: HTMLDivElement, turnId: string): HTMLElement | undefined {
  return Array.from(list.querySelectorAll<HTMLElement>("[data-turn-id]")).find(
    (element) => element.dataset.turnId === turnId,
  );
}

/** The one `[data-routing-id]` element inside `turn` naming `blockId` — a block-level jump's own
 * target, scoped to the turn `turnNode` already found, so a stale id from another turn has
 * nothing there to match. */
function routingNode(turn: HTMLElement, blockId: string): HTMLElement | undefined {
  return Array.from(turn.querySelectorAll<HTMLElement>("[data-routing-id]")).find(
    (element) => element.dataset.routingId === blockId,
  );
}

function firstVisibleTurn(
  list: HTMLDivElement,
  host: HTMLElement,
): { turnId: string; offset: number } | undefined {
  // Reads every turn above the first visible one on each scroll; switch to a binary search over
  // the turns' tops if a very long transcript ever makes that show in a profile.
  const hostTop = host.getBoundingClientRect().top;
  for (const element of list.querySelectorAll<HTMLElement>("[data-turn-id]")) {
    const rect = element.getBoundingClientRect();
    const turnId = element.dataset.turnId;
    if (turnId !== undefined && rect.bottom > hostTop) {
      return { turnId, offset: rect.top - hostTop };
    }
  }
  return undefined;
}

function savedScroll(list: HTMLDivElement, host: HTMLElement, following: boolean): SavedScroll {
  return { top: host.scrollTop, following, ...firstVisibleTurn(list, host) };
}

/** What the aria-live region says when a turn it watched finish lands on each status. "cancelled"
 * uses the exact sentence `.reply-cancelled` already shows (above) — one event, one wording,
 * rather than the visual and the spoken channel describing the same thing two different ways. */
const TURN_DONE_ANNOUNCEMENT: Readonly<Record<TurnStatus, string>> = {
  success: "Reply finished",
  cancelled: "Stopped",
  error: "Reply failed",
};

/** How long a completion sentence stays in the region before it goes quiet again — long enough
 * to be read, short enough that a screen-reader user browsing the transcript later never finds a
 * stale "just finished" sitting at the top, hours after it stopped being true. */
const ANNOUNCEMENT_SETTLE_MS = 4000;

/** A failed or cancelled live turn can be retried, once the caller wires `onRetryTurn` in — a
 * history turn never qualifies (the reducer forces it to "success" anyway, since a transcript
 * page keeps no live status), and neither does a turn with no user message to put back. */
function canRetry(turn: Turn, onRetryTurn: ((turn: Turn) => void) | undefined): boolean {
  return (
    Boolean(onRetryTurn) &&
    !turn.history &&
    (turn.status === "error" || turn.status === "cancelled") &&
    Boolean(turn.user)
  );
}

interface TurnMenuProps {
  turn: Turn;
  selected: boolean;
  // The explicit `undefined`s are what let Conversation forward its own optional props unchanged
  // under `exactOptionalPropertyTypes` (see UserTurnProps's `userLabel` above for the same rule).
  onSelectTurn?: ((turnId: string, selected: boolean) => void) | undefined;
  onQuoteTurn?: ((turn: Turn) => void) | undefined;
  onBranchTurn?: ((turn: Turn) => void) | undefined;
  onReusePrompt?: ((turn: Turn) => void) | undefined;
}

/** The "More actions" (⋯) menu for a finished turn: Quote into draft, New chat with this
 * turn, Edit in a new chat (only once the turn carries a user message) and the Select-for-export
 * toggle — each item only once its own callback is wired, same as the separate buttons and
 * checkbox this replaces. Controlled and click-guarded the way `LeftRail/OrganizedSessionMenu.tsx`'s
 * own "⋯" trigger is: Radix opens a trigger on pointerdown, which a plain `click` (jsdom's
 * `fireEvent.click` included) never synthesizes, so the trigger's own `onClick` bridges that for
 * anything that only ever fires a `click`. */
function TurnMenu({
  turn,
  selected,
  onSelectTurn,
  onQuoteTurn,
  onBranchTurn,
  onReusePrompt,
}: TurnMenuProps): ReactElement {
  const [open, setOpen] = useState(false);
  const pointerClick = useRef(false);

  return (
    <Dropdown.Root open={open} onOpenChange={setOpen} modal={false}>
      <Dropdown.Trigger
        aria-label="More actions"
        title="More actions"
        onPointerDown={(event) => {
          pointerClick.current = true;
          event.stopPropagation();
        }}
        onKeyDown={(event) => {
          pointerClick.current = false;
          event.stopPropagation();
        }}
        onClick={(event) => {
          event.stopPropagation();
          // Radix already toggled on pointerdown; its trailing click must not reopen it.
          if (!pointerClick.current && !open) setOpen(true);
          pointerClick.current = false;
        }}
      >
        ⋯
      </Dropdown.Trigger>
      <Dropdown.Portal>
        <Dropdown.Content className="turn-menu menu-panel" align="end" sideOffset={6}>
          {onQuoteTurn ? (
            <Dropdown.Item className="mi" onSelect={() => onQuoteTurn(turn)}>
              Quote into draft
            </Dropdown.Item>
          ) : null}
          {onBranchTurn ? (
            <Dropdown.Item className="mi" onSelect={() => onBranchTurn(turn)}>
              New chat with this turn
            </Dropdown.Item>
          ) : null}
          {turn.user && onReusePrompt ? (
            <Dropdown.Item className="mi" onSelect={() => onReusePrompt(turn)}>
              Edit in a new chat
            </Dropdown.Item>
          ) : null}
          {onSelectTurn ? (
            <Dropdown.CheckboxItem
              className="mi"
              checked={selected}
              onCheckedChange={(checked) => onSelectTurn(turn.id, checked === true)}
            >
              Select for export
              <Dropdown.ItemIndicator className="turn-menu-check" aria-hidden="true">
                ✓
              </Dropdown.ItemIndicator>
            </Dropdown.CheckboxItem>
          ) : null}
        </Dropdown.Content>
      </Dropdown.Portal>
    </Dropdown.Root>
  );
}

export interface ConversationProps {
  /** Scrolls to and focuses one turn; `request` bumps to repeat an identical jump. With
   * `blockId` naming a routing block inside that turn, the jump moves to that one row instead
   * of the turn — see this file's "Block-level jump" header note and the layout effect below. */
  jumpTo?: { turnId: string; request: number; blockId?: string } | undefined;
  historyGap?: boolean;
  selectedTurns?: ReadonlySet<string>;
  onSelectTurn?: (turnId: string, selected: boolean) => void;
  onQuoteTurn?: (turn: Turn) => void;
  onBranchTurn?: (turn: Turn) => void;
  onReusePrompt?: (turn: Turn) => void;
  /** Opens the retry review (RetryReview.tsx) for a failed or cancelled live turn. Optional; App.tsx
   * wires this to Retry… in the turn's own `.turn-actions` row, beside the turn's "More actions"
   * menu (TurnMenu, above). */
  onRetryTurn?: (turn: Turn) => void;
  /** Extra actions for a finished turn, drawn in its `.turn-actions` row just before the turn's
   * "More actions" menu (TurnMenu, above). */
  renderTurnActions?: (turn: Turn) => ReactNode;
  initialScroll?: SavedScroll | undefined;
  onRememberScroll?: (value: SavedScroll) => void;
  tabPanel?: boolean;
  view: SessionView;
  nowMs: number;
  onOpenUrl: (url: string) => void;
  /** Asks for the page before the oldest turn on screen. Called when the user scrolls to the
   * top and the view says there is one — and after every commit that leaves the column too
   * short to scroll at all, since then they never could; viki.ts refuses a second request while
   * one is in flight, so this needs no throttle of its own. */
  onLoadOlder?: () => void;
  /** Passed straight to every UserTurn; "You" when the shell has no display name. */
  userLabel?: string;
  /** Passed to every UserTurn so a sent excerpt shows as a citation; see UserTurnProps. */
  excerptApi?: ExcerptApi;
  workspaceId?: string;
  onOpenVault?: (ref: string, start: number) => void;
  /** Read-aloud settings for every reply (ReadAloud.tsx); absent, the replies keep the context
   * they inherit. */
  readAloud?: ReadAloudSettings;
}

export function Conversation({
  jumpTo,
  historyGap = false,
  selectedTurns,
  onSelectTurn,
  onQuoteTurn,
  onBranchTurn,
  onReusePrompt,
  onRetryTurn,
  renderTurnActions,
  initialScroll,
  onRememberScroll,
  tabPanel,
  view,
  nowMs,
  onOpenUrl,
  onLoadOlder,
  userLabel,
  excerptApi,
  workspaceId,
  onOpenVault,
  readAloud,
}: ConversationProps): ReactElement {
  const inheritedReadAloud = useContext(ReadAloudContext);
  const listRef = useRef<HTMLDivElement>(null);
  const followingRef = useRef(initialScroll?.following ?? true);
  // The pill needs a render when following changes; the ref alone cannot cause one. Both exist
  // because the layout effect below must read the value synchronously, before paint, and state
  // read there would be a render behind.
  const [following, setFollowing] = useState(initialScroll?.following ?? true);
  const initialScrollRef = useRef(initialScroll);
  const rememberScrollRef = useRef(onRememberScroll);
  rememberScrollRef.current = onRememberScroll;

  // Turn-completion aria-live (see this file's header comment). Seeded empty — never from
  // `view.turns` at mount — so a turn already over on this component's very first render (a
  // reload, a resumed transcript) can never read as "was running a moment ago".
  const previousStatusesRef = useRef<Map<string, Turn["status"]>>(new Map());
  // `n` gives every announcement its own DOM identity (the `key` below), so the second turn in a
  // session to finish the same way as the first — success-after-success is the ordinary case —
  // still replaces the live region's content instead of writing the byte-identical string over
  // itself: React's setState bails out on an unchanged primitive (see App.tsx's `draft` effect
  // for the same rule), and even without that, an unchanged text node is not a mutation a screen
  // reader has anything to notice.
  const [announcement, setAnnouncement] = useState({ text: "", n: 0 });
  const announcementTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  // Unmounting mid-timer (the session is closed, or another is opened) must not fire a setState
  // on a gone component.
  useEffect(() => () => clearTimeout(announcementTimerRef.current), []);

  // No dependency array, like the auto-follow layout effect below: it must compare every
  // render's statuses against the last render it saw, including the render `setAnnouncement`
  // itself causes — which is also why it converges rather than looping (that second pass finds
  // every status already equal to what it just recorded, so `next` comes back empty and nothing
  // schedules another update).
  useEffect(() => {
    let next = "";
    for (const turn of view.turns) {
      const was = previousStatusesRef.current.get(turn.id);
      // A steered turn flips running -> success without ever finishing on its own (it folded
      // into the next one instead, sessionReducer.ts's user.message case), so that flip is not
      // news — the turn it opened gets the real announcement when that one finishes.
      if (was === "running" && turn.status !== "running" && !turn.steered) {
        next = TURN_DONE_ANNOUNCEMENT[turn.status];
      }
      previousStatusesRef.current.set(turn.id, turn.status);
    }
    if (next) {
      clearTimeout(announcementTimerRef.current);
      setAnnouncement((prev) => ({ text: next, n: prev.n + 1 }));
      // Cleared a beat later so the sentence never sits permanently at the top of the
      // conversation's accessibility tree, minutes after it stopped being true.
      announcementTimerRef.current = setTimeout(() => {
        setAnnouncement((prev) => ({ ...prev, text: "" }));
      }, ANNOUNCEMENT_SETTLE_MS);
    }
  });

  // How far the top of the content is from the bottom of the scroll box, kept current on every
  // scroll. A history page arrives at the TOP, so without restoring against it the user would
  // be thrown forward by exactly the height of what they just asked for.
  const anchorRef = useRef(0);
  // The last scroll position everyone agreed on: what the follow effect below wrote, or what a
  // `scroll` event reported. The effect compares against it to tell the two things that can move
  // the column apart — content arriving under a followed column leaves scrollTop exactly where it
  // was, while the user's own gesture does not. See the effect for why that matters.
  const followedTopRef = useRef(0);
  const firstTurnId = view.turns[0]?.id;
  const previousFirstRef = useRef(firstTurnId);

  useLayoutEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const host = scrollHostOf(list);
    const saved = initialScrollRef.current;
    if (saved) {
      host.scrollTop = saved.top;
      const turn = saved.turnId === undefined ? undefined : turnNode(list, saved.turnId);
      if (turn && saved.offset !== undefined) {
        host.scrollTop +=
          turn.getBoundingClientRect().top - host.getBoundingClientRect().top - saved.offset;
      }
    }
    followedTopRef.current = host.scrollTop;
    anchorRef.current = host.scrollHeight - host.scrollTop;
    return () => {
      rememberScrollRef.current?.(savedScroll(list, host, followingRef.current));
    };
  }, []);

  function jumpToLatest(): void {
    const list = listRef.current;
    if (!list) return;
    const host = scrollHostOf(list);
    host.scrollTop = host.scrollHeight - host.clientHeight;
    followedTopRef.current = host.scrollTop;
    followingRef.current = true;
    setFollowing(true);
  }

  // Runs after every render (a new turn, a delta, a status change): jumps to the bottom only
  // while the user was already there, so a streaming reply never fights a manual scroll-up.
  useLayoutEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const host = scrollHostOf(list);
    const prepended = firstTurnId !== previousFirstRef.current;
    previousFirstRef.current = firstTurnId;
    // A page (the first one included) that does not fill the box leaves nothing to scroll, so
    // the scroll listener below would never ask for what came before it. viki.ts refuses a
    // second in-flight request, so this only has to hold off while one is known to be out.
    if (
      typeof view.historyCursor === "string" &&
      !view.historyLoading &&
      host.scrollHeight <= host.clientHeight
    )
      askForOlder.current?.();
    if (followingRef.current) {
      // The user's scroll moves scrollTop the instant the input is processed, but the `scroll`
      // event that would clear `followingRef` is only dispatched at the next rendering step —
      // and a delta commits in between (one per `text.delta`, nothing coalesces them), so
      // without this check the effect wins the race, yanks the column back under their eyes, and
      // the queued handler then measures the position WE just wrote and re-affirms following.
      // Position is the honest signal and the only one every input device produces: content
      // arriving below never moves scrollTop, so a scrollTop that has gone UP the page since the
      // last agreed value is the user and nothing else. The edge test is the same one
      // `handleScroll` uses, so a nudge inside the threshold does not flicker the pill on and
      // then straight back off.
      const scrolledUp = host.scrollTop < followedTopRef.current - 1;
      const awayFromEdge =
        host.scrollHeight - host.scrollTop - host.clientHeight > FOLLOW_THRESHOLD_PX;
      if (scrolledUp && awayFromEdge) {
        followingRef.current = false;
        setFollowing(false);
        return;
      }
      host.scrollTop = host.scrollHeight - host.clientHeight;
      // Read back rather than reused: the browser clamps the write to the real scroll range.
      followedTopRef.current = host.scrollTop;
      return;
    }
    // An older page landed above what the user is reading: put the same content back under
    // their eyes rather than leaving them wherever the new height happens to put them.
    if (prepended) host.scrollTop = host.scrollHeight - anchorRef.current;
    // Content streaming in below a column the user has scrolled away from grows scrollHeight
    // without a scroll event, so the anchor the listener keeps goes stale: a page landing after
    // that would put them further down by exactly what streamed in. Re-measured here, after every
    // commit, from the position the user actually holds.
    anchorRef.current = host.scrollHeight - host.scrollTop;
  });

  // Markdown formats late (a worker answer lands after its own commit) and says so with a bubbling
  // `markdownformat` event: the block re-lays out with no delta and no scroll event to re-anchor
  // it, exactly like the streamed content the layout effect above re-measures after every commit.
  // Only that event re-pins, never a plain size change: a Thought row, a tool preview or
  // `+N more` the user opens at the live edge must stay where they pressed it. Growth above a
  // reader who scrolled away is left to the browser's own scroll anchoring.
  useEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const host = scrollHostOf(list);
    function handleFormat(): void {
      if (followingRef.current) {
        host.scrollTop = host.scrollHeight - host.clientHeight;
        followedTopRef.current = host.scrollTop;
      }
      anchorRef.current = host.scrollHeight - host.scrollTop;
    }
    list.addEventListener("markdownformat", handleFormat);
    return () => list.removeEventListener("markdownformat", handleFormat);
  }, []);

  // `scroll` does not bubble, and the scrolling element is an ancestor outside this component's
  // own JSX, so it needs its own listener rather than React's onScroll (which only ever sees
  // scrolls on the node it is attached to). The host is a stable ancestor for the component's
  // lifetime, so one listener covers every render.
  useEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const host = scrollHostOf(list);
    // An arrow, not a declaration: a hoisted function would lose the narrowing of `list`.
    const handleScroll = (): void => {
      const atEdge = host.scrollHeight - host.scrollTop - host.clientHeight <= FOLLOW_THRESHOLD_PX;
      followingRef.current = atEdge;
      followedTopRef.current = host.scrollTop;
      anchorRef.current = host.scrollHeight - host.scrollTop;
      setFollowing(atEdge);
      rememberScrollRef.current?.(savedScroll(list, host, atEdge));
      if (host.scrollTop <= OLDER_THRESHOLD_PX) askForOlder.current?.();
    };
    // A wheel toward the top is dispatched BEFORE the scroll it causes, which makes it the one
    // signal that beats a delta's layout effect to the punch even on the first frame of a
    // gesture — the frame where a compositor-driven scroll may not have reached the main thread's
    // scrollTop yet, and so the only frame the position check above can still lose. Only the ref
    // is cleared: `handleScroll` follows a moment later and decides the pill from the real
    // position, so a 3px brush at the live edge never flashes it.
    function handleWheel(event: WheelEvent): void {
      if (event.deltaY < 0) followingRef.current = false;
    }
    host.addEventListener("scroll", handleScroll);
    host.addEventListener("wheel", handleWheel, { passive: true });
    return () => {
      host.removeEventListener("scroll", handleScroll);
      host.removeEventListener("wheel", handleWheel);
    };
  }, []);

  // The listener above is installed once, so the callback it calls has to be reachable through a
  // ref rather than captured: the prop changes identity on every render.
  const askForOlder = useRef<(() => void) | undefined>(undefined);
  askForOlder.current = onLoadOlder;

  // Scrolls to and focuses jumpTo's turn, or, with a blockId naming a routing block inside it,
  // that one row instead — see this file's header comment, "Block-level jump".
  useLayoutEffect(() => {
    if (!jumpTo || !listRef.current) return;
    const turn = turnNode(listRef.current, jumpTo.turnId);
    if (!turn) return;
    const target =
      (jumpTo.blockId === undefined ? undefined : routingNode(turn, jumpTo.blockId)) ?? turn;
    const host = scrollHostOf(listRef.current);
    followingRef.current = false;
    setFollowing(false);
    host.scrollTop += target.getBoundingClientRect().top - host.getBoundingClientRect().top - 12;
    followedTopRef.current = host.scrollTop;
    anchorRef.current = host.scrollHeight - host.scrollTop;
    target.focus({ preventScroll: true });
  }, [jumpTo]);

  return (
    <div
      className="conversation-list"
      ref={listRef}
      {...(tabPanel
        ? {
            role: "tabpanel",
            id: `conversation-panel-${view.sessionId}`,
            "aria-labelledby": `session-tab-${view.sessionId}`,
          }
        : {})}
    >
      {/* Screen-reader-only (base.css's .sr-only clips it; it must stay in the accessibility
          tree, so never display:none) — see this file's header comment for what it says and
          when. */}
      <div className="sr-only" role="status" aria-live="polite">
        <span key={announcement.n}>{announcement.text}</span>
      </div>
      {view.turns.map((turn, index) => {
        const retryable = canRetry(turn, onRetryTurn);
        // Whether the turn's "More actions" (⋯) menu has anything to hold — also what decides,
        // together with `retryable` and `renderTurnActions`, whether `.turn-actions` itself
        // renders at all.
        const menuOffered = Boolean(
          onSelectTurn || onQuoteTurn || onBranchTurn || (turn.user && onReusePrompt),
        );
        const isJumpTarget = jumpTo?.turnId === turn.id;
        const targetBlockId = isJumpTarget ? jumpTo?.blockId : undefined;
        // A routing block in this turn matches targetBlockId only once RoutingRow's own
        // data-routing-id lookup (the layout effect below) would also find it, so this mirrors
        // that resolution without needing the DOM: the turn keeps its own search highlight
        // unless a row inside it is about to carry the highlight instead.
        const hasRoutingTarget = turn.blocks.some(
          (block) => block.kind === "routing" && block.id === targetBlockId,
        );
        return (
          <div
            className={`turn${isJumpTarget && !hasRoutingTarget ? " turn-search-target" : ""}`}
            key={turn.id}
            data-turn-id={turn.id}
            tabIndex={-1}
          >
            {historyGap && !turn.history && view.turns[index - 1]?.history ? (
              <p role="note">
                Some saved turns between this search result and the current conversation are
                omitted.
              </p>
            ) : null}
            {turn.user ? (
              <UserTurn
                user={turn.user}
                nowMs={nowMs}
                userLabel={userLabel}
                excerptApi={excerptApi}
                workspaceId={workspaceId}
                onOpenVault={onOpenVault}
              />
            ) : null}
            {turn.blocks.length > 0 ? (
              <ReadAloudContext.Provider value={readAloud ?? inheritedReadAloud}>
                <Reply turn={turn} onOpenUrl={onOpenUrl} targetBlockId={targetBlockId} />
              </ReadAloudContext.Provider>
            ) : null}
            <SourcesStrip turn={turn} sessionId={view.sessionId} />
            {turn.status !== "running" && (menuOffered || retryable || renderTurnActions) ? (
              <div className="turn-actions">
                {selectedTurns?.has(turn.id) ? (
                  <span className="turn-selected chip">Selected for export</span>
                ) : null}
                {retryable ? (
                  <button type="button" onClick={() => onRetryTurn?.(turn)}>
                    Retry…
                  </button>
                ) : null}
                {renderTurnActions?.(turn)}
                {menuOffered ? (
                  <TurnMenu
                    turn={turn}
                    selected={selectedTurns?.has(turn.id) ?? false}
                    onSelectTurn={onSelectTurn}
                    onQuoteTurn={onQuoteTurn}
                    onBranchTurn={onBranchTurn}
                    onReusePrompt={onReusePrompt}
                  />
                ) : null}
              </div>
            ) : null}
          </div>
        );
      })}
      {/* A message sent while Claude Code is still on its current step, shown here until its
          steer.ended arrives (sessionReducer.ts's steer.pending/steer.ended cases). App.tsx
          renders the card column as this component's own sibling, right after it, so DOM order
          alone keeps these rows above the cards with no card knowledge needed here. */}
      {(view.steers ?? []).map((steer) => (
        <div className="steer-waiting" key={steer.steer_id}>
          <div className="steer-waiting-text">{steer.text}</div>
          {steer.images > 0 ? (
            <div className="steer-waiting-images">{`+${steer.images} images`}</div>
          ) : null}
          <div className="steer-waiting-caption">
            Waiting — Claude reads this after its current step.
          </div>
        </div>
      ))}
      {/* The jump-to-latest pill. Sticky rather than absolutely placed over the composer: the
          state that decides it (are we at the live edge) lives here, with the scroll listener,
          and lifting it out to App would need a second channel back down to scroll. Negative
          margin so it floats over the last rows instead of adding a gap under them — it is only
          ever on screen when the user has scrolled away from the bottom, which is when floating
          is exactly what it should do. */}
      {!following && view.turns.length > 0 ? (
        <button type="button" className="jump-pill jp" onClick={jumpToLatest}>
          ↓ Jump to latest
        </button>
      ) : null}
    </div>
  );
}
