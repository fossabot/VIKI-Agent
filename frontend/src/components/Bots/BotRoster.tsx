import { useId, useState } from "react";
import type { BotRow } from "../../clients/messaging/botsApi";
import type { ProfileIdentityV1 } from "../../clients/profiles/profileApi";
import { t } from "../../i18n/t";
import { timeLabel } from "../../state/rows";
import { Icon } from "../Icon/Icon";
import { BotFace } from "./BotFace";
import { type BotStatus, botStatus, type TeamLive } from "./botStatus";
import { botName, type SeatId, seatFace, type TeamRoster } from "./team";
import { READINESS_TEXT, type TeamReadiness } from "./teamReadiness";
import "./BotRoster.css";

interface RowProps {
  key: string;
  name: string;
  profileId: string;
  identity: ProfileIdentityV1 | null;
  role?: string;
  frontDoor?: boolean;
  purpose: string;
  status: BotStatus | null;
  time: string;
  selected: boolean;
  onSelect: () => void;
}

function Row({ id, row }: { id: string; row: RowProps }) {
  const statusId = row.status ? `${id}-status` : undefined;
  return (
    <li className={row.selected ? "bots-row bots-row-selected" : "bots-row"}>
      <div className="bots-identity">
        <BotFace
          profileId={row.profileId}
          identity={row.identity}
          name={row.name}
          size={40}
          {...(row.status ? { state: row.status.state } : {})}
          {...(row.frontDoor ? { frontDoor: true } : {})}
        />
        <div className="bots-row-text">
          <button
            type="button"
            className="btn txt"
            aria-current={row.selected ? "true" : undefined}
            aria-describedby={statusId}
            onClick={row.onSelect}
          >
            {row.name}
          </button>
          {row.role ? <span className="bots-role">{row.role}</span> : null}
          {row.time ? <span className="bots-time">{row.time}</span> : null}
        </div>
      </div>
      {row.status ? (
        <p className="bots-status" id={statusId}>
          {row.status.text}
        </p>
      ) : null}
      {row.purpose ? <p className="bots-purpose">{row.purpose}</p> : null}
    </li>
  );
}

function Group({
  title,
  note,
  action,
  children,
}: {
  title: string;
  note?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  const id = useId();
  return (
    <div className="bots-group">
      <h2 id={id}>{title}</h2>
      {note ? <p className="bots-note">{note}</p> : null}
      {action ? <div className="bots-group-action">{action}</div> : null}
      <ul aria-labelledby={id}>{children}</ul>
    </div>
  );
}

export function BotRoster({
  roster,
  team,
  seatFallback,
  activeWorkspaceId,
  selectedId,
  selectedSeat,
  onSelectBot,
  onSelectSeat,
  onAddMissing,
  adding = false,
}: {
  roster: TeamRoster;
  team: { live: TeamLive; readiness: TeamReadiness } | null;
  seatFallback: BotStatus;
  activeWorkspaceId: string;
  selectedId: string | null;
  selectedSeat: SeatId | null;
  onSelectBot: (id: string) => void;
  onSelectSeat: (seat: SeatId) => void;
  onAddMissing?: () => void;
  adding?: boolean;
}) {
  const uid = useId();
  const [query, setQuery] = useState("");
  const live = team?.live ?? null;
  const search = query.trim().toLocaleLowerCase();
  const matches = (row: RowProps) =>
    [row.name, row.purpose, row.role ?? ""].some((value) =>
      value.toLocaleLowerCase().includes(search),
    );

  const botRow = (bot: BotRow, extra: Partial<RowProps> = {}): RowProps => ({
    key: bot.bot_id,
    name: botName(bot),
    profileId: bot.profile_id,
    identity: bot.profile?.identity ?? null,
    purpose: bot.profile?.identity.purpose ?? "",
    status: live ? botStatus(bot, live, activeWorkspaceId) : null,
    time: live
      ? timeLabel(
          live.sessionRows.find((r) => r.session_id === bot.chat_session_id)?.modified ?? "",
          live.nowMs,
        )
      : "",
    selected: selectedSeat === null && bot.bot_id === selectedId,
    onSelect: () => onSelectBot(bot.bot_id),
    ...extra,
  });

  const seatRows = (): RowProps[] =>
    roster.seats.map(({ seat, bot }) => {
      const role = { role: seat.role, frontDoor: seat.seat === "chief" };
      if (bot) return botRow(bot, role);
      const face = seatFace(seat.seat);
      return {
        ...role,
        key: seat.seat,
        name: seat.name,
        profileId: face.profileId,
        identity: face.identity,
        purpose: face.identity.purpose,
        status: seatFallback,
        time: "",
        selected: selectedSeat === seat.seat,
        onSelect: () => onSelectSeat(seat.seat),
      };
    });

  const list = (rows: RowProps[]) =>
    rows.filter(matches).map((row) => <Row key={row.key} id={`${uid}-${row.key}`} row={row} />);
  const seats = team && team.readiness !== "no_vault" ? seatRows() : [];

  return (
    // biome-ignore lint/a11y/useSemanticElements: the roster holds lists and headings, which a fieldset would not
    <div className="bots-roster" role="group" aria-label="Bot roster">
      <label className="bots-search">
        {t("bots.search")}
        <input
          type="search"
          value={query}
          maxLength={128}
          onChange={(event) => setQuery(event.target.value)}
        />
      </label>
      {search &&
      ![
        ...seats,
        ...roster.others.map((bot) => botRow(bot)),
        ...roster.away.map((bot) => botRow(bot)),
      ].some(matches) ? (
        <p role="status">{t("bots.search.empty")}</p>
      ) : null}
      {team ? (
        <Group
          title="Core team"
          {...(team.readiness !== "ready" ? { note: READINESS_TEXT[team.readiness] } : {})}
          {...(onAddMissing
            ? {
                action: (
                  <button
                    type="button"
                    className="btn alt sm"
                    disabled={adding}
                    onClick={onAddMissing}
                  >
                    <Icon name="plus" />
                    {t("bots.team.addMissing")}
                  </button>
                ),
              }
            : {})}
        >
          {list(seats)}
        </Group>
      ) : null}
      {roster.others.length > 0 ? (
        <Group title="Your Bots">{list(roster.others.map((b) => botRow(b)))}</Group>
      ) : null}
      {roster.away.length > 0 ? (
        <Group title="In other vaults">{list(roster.away.map((b) => botRow(b)))}</Group>
      ) : null}
    </div>
  );
}
