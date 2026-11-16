/**
 * TeamStrip: the right panel's Bots strip, below Needs you and a Bot chat's Bot activity, above
 * the native agents. Its current tile is the open chat's Bot. It lists the vault's Bots on mount,
 * window focus and client or vault changes, then shows the team seats and other Bots. A
 * tile only opens something: a started Bot's chat, a prepared chat, or the Bots page. It never
 * creates, resets or changes a Bot, and it never starts a turn.
 */
import { type ReactNode, useContext, useEffect, useId, useRef, useState } from "react";
import type { BotRow, BotsApi } from "../../clients/messaging/botsApi";
import type { PreparedProfilePreview } from "../../clients/profiles/profileLaunchApi";
import { BotFace } from "../Bots/BotFace";
import { BotHuesContext, botHue } from "../Bots/botHues";
import {
  type BotStatus,
  botStatus,
  countText,
  NOT_STARTED,
  statusCounts,
  type TeamLive,
} from "../Bots/botStatus";
import { botName, seatFace, type TeamSeat, teamRoster } from "../Bots/team";
import { READINESS_TEXT, type TeamReadiness } from "../Bots/teamReadiness";
import "./TeamStrip.css";

/** One tile: a Bot (filling a seat or not), or a seat no Bot fills yet. */
type Member = {
  key: string;
  name: string;
  status: BotStatus;
} & ({ bot: BotRow; seat: TeamSeat | null } | { bot: null; seat: TeamSeat });

/** `bots` is null when the read failed. */
interface Read {
  api: BotsApi;
  bots: BotRow[] | null;
}

export function TeamStrip({
  api,
  activeWorkspaceId,
  readiness,
  live,
  selectedId,
  currentBotId,
  onOpenSession,
  onOpenPrepared,
  onShowBot,
  onShowSeat,
  onShowAll,
}: {
  api: BotsApi;
  activeWorkspaceId: string | null | undefined;
  readiness: TeamReadiness;
  live: TeamLive;
  selectedId: string | null;
  currentBotId?: string | null;
  onOpenSession: (sessionId: string) => void;
  onOpenPrepared: (preview: PreparedProfilePreview, bot: BotRow) => void;
  onShowBot: (botId: string) => void;
  onShowSeat: (seat: TeamSeat) => void;
  onShowAll: () => void;
}) {
  const [read, setRead] = useState<Read | null>(null);
  const { hues } = useContext(BotHuesContext);
  // Chat reads live for one (api, vault); a focus refresh only replaces the list read.
  const scope = useRef<AbortController | null>(null);
  const pending = useRef<AbortSignal | null>(null);
  const latest = useRef({ selectedId, onOpenPrepared, onShowBot });
  const statusBase = useId();

  useEffect(() => {
    latest.current = { selectedId, onOpenPrepared, onShowBot };
  });

  // biome-ignore lint/correctness/useExhaustiveDependencies: a vault change re-reads the list
  useEffect(() => {
    const controller = new AbortController();
    scope.current = controller;
    let listing: AbortController | undefined;
    function refresh() {
      listing?.abort();
      const request = new AbortController();
      listing = request;
      api.list(request.signal).then(
        (list) => {
          if (!request.signal.aborted) setRead({ api, bots: list.bots });
        },
        () => {
          if (request.signal.aborted) return;
          setRead((was) => (was?.api === api && was.bots ? was : { api, bots: null }));
        },
      );
    }
    refresh();
    window.addEventListener("focus", refresh);
    return () => {
      controller.abort();
      listing?.abort();
      window.removeEventListener("focus", refresh);
    };
  }, [api, activeWorkspaceId]);

  const answer = read?.api === api ? read : null;
  // No seat is drawn before the Bots are known: an unfilled seat means "no Bot yet".
  const roster = answer?.bots ? teamRoster(answer.bots, activeWorkspaceId ?? "") : null;
  const members: Member[] = [];
  for (const { seat, bot } of roster?.seats ?? []) {
    if (bot)
      members.push({
        key: bot.bot_id,
        name: botName(bot),
        seat,
        bot,
        status: botStatus(bot, live, activeWorkspaceId),
      });
    else if (typeof activeWorkspaceId === "string" && readiness !== "no_vault")
      members.push({ key: seat.seat, name: seat.name, seat, bot: null, status: NOT_STARTED });
  }
  for (const bot of roster?.others ?? [])
    members.push({
      key: bot.bot_id,
      name: botName(bot),
      seat: null,
      bot,
      status: botStatus(bot, live, activeWorkspaceId),
    });

  const count = countText(statusCounts(members.map((m) => m.status)));

  function openChat(bot: BotRow): void {
    const signal = scope.current?.signal;
    if (!signal || pending.current === signal) return;
    pending.current = signal;
    const clickedFrom = selectedId;
    api
      .chat(bot, signal)
      .then(
        (result) => result.preview,
        () => null,
      )
      .then((preview) => {
        if (signal.aborted || latest.current.selectedId !== clickedFrom) return;
        if (preview && "session_id" in preview) latest.current.onOpenPrepared(preview, bot);
        else latest.current.onShowBot(bot.bot_id);
      })
      .finally(() => {
        if (pending.current === signal) pending.current = null;
      });
  }

  function open(member: Member): void {
    const { bot } = member;
    if (!bot) onShowSeat(member.seat);
    else if (member.status.state === "unavailable") onShowBot(bot.bot_id);
    else if (live.sessions[bot.chat_session_id]) onOpenSession(bot.chat_session_id);
    else openChat(bot);
  }

  let body: ReactNode;
  if (!answer || (readiness === "loading" && members.length === 0)) {
    body = <ul className="rail-team-members" aria-busy="true" />;
  } else if (!answer.bots && members.length === 0) {
    body = <p className="rail-team-note">Could not load your Bots.</p>;
  } else if (members.length === 0) {
    body = (
      <p className="rail-team-note">
        {readiness === "no_vault" ? READINESS_TEXT.no_vault : "No Bots in this vault yet."}
      </p>
    );
  } else {
    body = (
      <ul className="rail-team-members">
        {members.map((member, index) => {
          const statusId = `${statusBase}-${index}`;
          const isCurrent =
            member.bot !== null && currentBotId != null && member.bot.bot_id === currentBotId;
          const face = member.bot
            ? { profileId: member.bot.profile_id, identity: member.bot.profile?.identity ?? null }
            : seatFace(member.seat.seat);
          return (
            <li key={member.key}>
              <button
                type="button"
                className="rail-team-member"
                title={member.status.text}
                aria-describedby={statusId}
                aria-current={isCurrent ? "true" : undefined}
                data-bot-hue={
                  isCurrent && member.bot
                    ? botHue(member.bot.profile_id, member.name, hues)
                    : undefined
                }
                onClick={() => open(member)}
              >
                <BotFace
                  profileId={face.profileId}
                  identity={face.identity}
                  name={member.name}
                  size={56}
                  state={member.status.state}
                  frontDoor={member.seat?.seat === "chief"}
                  decorative
                />
                <span className="rail-team-name">{member.name}</span>
              </button>
              <span id={statusId} className="sr-only">
                {member.status.text}
              </span>
            </li>
          );
        })}
      </ul>
    );
  }

  return (
    <section className="rail-team" aria-label="Bots">
      <div className="rail-team-head">
        <span className="lbl">Bots</span>
        {count ? <span className="rail-team-count">{count}</span> : null}
      </div>
      {body}
      {members.length > 0 ? (
        <button type="button" className="btn txt sm" onClick={onShowAll}>
          All Bots
        </button>
      ) : null}
    </section>
  );
}
