import type { ProfileIdentityV1 } from "../../clients/profiles/profileApi";
import { BotAvatar } from "./BotAvatar";
import type { BotState } from "./botStatus";
import "./BotFace.css";

const DOT_STATES: readonly BotState[] = ["needs_you", "working", "elsewhere", "unknown"];

/** A Bot's avatar with its live state: a dot or ring for the states that want a glance, a dimmed
 * or outlined avatar for the rest, and a star on the front door. */
export function BotFace({
  profileId,
  identity,
  name,
  size,
  state,
  frontDoor,
  decorative,
}: {
  profileId: string;
  identity: ProfileIdentityV1 | null;
  name: string;
  size: number;
  state?: BotState;
  frontDoor?: boolean;
  decorative?: boolean;
}) {
  return (
    <span
      className="bot-face"
      data-state={state}
      style={{ width: size, height: size }}
      aria-hidden={decorative ? "true" : undefined}
    >
      <BotAvatar
        profileId={profileId}
        identity={
          identity ?? {
            name,
            purpose: "",
            avatar_asset_id: null,
            accent: "slate",
            shape: "rounded",
          }
        }
        name={name}
        size={size}
      />
      {frontDoor && (
        <svg
          className="bot-face-star"
          viewBox="0 0 10 10"
          width="10"
          height="10"
          aria-hidden="true"
        >
          <path d="M5 .5 6.3 3.6 9.5 3.9 7.1 6 7.8 9.2 5 7.5 2.2 9.2 2.9 6 .5 3.9 3.7 3.6Z" />
        </svg>
      )}
      {state && DOT_STATES.includes(state) && (
        <span
          className={state === "working" ? "bot-face-dot breathe" : "bot-face-dot"}
          aria-hidden="true"
        />
      )}
    </span>
  );
}
