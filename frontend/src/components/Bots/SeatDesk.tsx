import { type ReactNode, useId } from "react";
import type { ProfileSummary } from "../../clients/profiles/profileApi";
import { BotFace } from "./BotFace";
import type { BotStatus, TeamLive } from "./botStatus";
import { seatFace, type TeamSeat } from "./team";
import "./SeatDesk.css";

export const ROUTINES_NEED_ORDINARY =
  "Scheduled routines need a vault whose policy is Ordinary in Settings → Workspaces; this conversation still works here.";

export function DeskHead({
  face,
  name,
  role,
  status,
  headingRef,
}: {
  face: ReactNode;
  name: string;
  role?: string;
  status?: string;
  headingRef?: ((element: HTMLHeadingElement | null) => void) | undefined;
}) {
  return (
    <div className="bots-desk-head">
      <span aria-hidden="true">{face}</span>
      <h2 tabIndex={-1} ref={headingRef}>
        {name}
      </h2>
      {role && <span className="bots-role">{role}</span>}
      <p className="bots-status" aria-live="polite">
        {status}
      </p>
    </div>
  );
}

function reuseLine(seat: TeamSeat, profile: ProfileSummary | null | undefined): string {
  if (profile === null)
    return `Creates a profile named ${seat.name} from the seat's job description.`;
  if (profile === undefined)
    return `Uses your profile named ${seat.name} if you have one; otherwise creates it from the seat's job description.`;
  const name = profile.identity.name;
  return profile.profile_id === seat.profileId
    ? `Uses the seat's profile, ${name}.`
    : `Uses your profile ${name}.`;
}

export function SeatDesk({
  seat,
  status,
  vaultName,
  policy,
  profile,
  canOpen,
  blockedReason,
  busy,
  onOpen,
  headingRef,
}: {
  seat: TeamSeat;
  status: BotStatus;
  live: TeamLive;
  vaultName: string;
  policy: string;
  profile: ProfileSummary | null | undefined;
  canOpen: boolean;
  blockedReason: string | null;
  busy: boolean;
  onOpen: () => void;
  headingRef?: ((element: HTMLHeadingElement | null) => void) | undefined;
}) {
  const reasonId = useId();
  const face = seatFace(seat.seat);
  return (
    <section className="bots-detail" aria-label="Selected Bot">
      <DeskHead
        face={
          <BotFace
            profileId={face.profileId}
            identity={face.identity}
            name={seat.name}
            size={64}
            state={status.state}
            frontDoor={seat.seat === "chief"}
            decorative
          />
        }
        name={seat.name}
        role={seat.role}
        status={status.text}
        headingRef={headingRef}
      />
      <p>{seat.starter?.purpose}</p>
      <h3>Opening this seat</h3>
      <ul>
        <li>{reuseLine(seat, profile)}</li>
        <li>{`Adds ${seat.name} to ${vaultName}.`}</li>
        <li>
          Opens its conversation with an empty message box: its profile's instructions and VIKI
          Agent's memory rule and Bot rules go to Claude Code as its system prompt. Nothing is sent
          until you send it.
        </li>
      </ul>
      <p className="bots-note">
        {`${seat.name} uses the same profile in every vault, so editing it changes ${seat.name}'s next conversations everywhere.`}
      </p>
      {policy !== "ordinary" && <p className="bots-note">{ROUTINES_NEED_ORDINARY}</p>}
      <details>
        <summary>Read the job</summary>
        <pre className="bots-job">{seat.starter?.instructions}</pre>
      </details>
      <button
        type="button"
        className="btn go"
        disabled={busy || !canOpen}
        aria-describedby={blockedReason ? reasonId : undefined}
        onClick={onOpen}
      >
        Open conversation
      </button>
      {blockedReason && (
        <p id={reasonId} className="bots-note">
          {blockedReason}
        </p>
      )}
    </section>
  );
}
