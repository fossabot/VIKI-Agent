/** The parts a shared Bot thread draws from its members: a name tag (BotTag), a roster-at-a-
 * glance strip (BotThreadStrip) and a native Bot picker (BotRecipient). Each one colours itself
 * by its Bot's assigned hue (botHues.ts), read from BotHuesContext. */
import { useContext } from "react";
import type { ThreadMember } from "../../state/botThreads";
import { BotFace } from "./BotFace";
import { type BotHues, BotHuesContext, botHue } from "./botHues";
import "./BotThreadParts.css";

/** What these parts need from a thread member: enough to draw a face and a name, nothing about
 * its live session, seat or bound Bot row. */
export type ThreadBot = Pick<ThreadMember, "key" | "name" | "profileId" | "identity">;

function hueOf(bot: ThreadBot, hues: BotHues) {
  return botHue(bot.profileId, bot.identity?.name ?? bot.name, hues);
}

/** One Bot's name tag: an 18px decorative face beside its name, both tinted by its hue. */
export function BotTag({ bot }: { bot: ThreadBot }) {
  const { hues } = useContext(BotHuesContext);
  return (
    <span className="bot-tag" data-bot-hue={hueOf(bot, hues)}>
      <BotFace
        profileId={bot.profileId}
        identity={bot.identity}
        name={bot.name}
        size={18}
        decorative
      />
      <b className="bot-tag-name">{bot.name}</b>
    </span>
  );
}

/** A compact participant row for thread buttons: three decorative faces, overflow count, and
 * screen-reader text naming every participant. */
export function BotThreadListFaces({ bots }: { bots: readonly ThreadBot[] }) {
  const { hues } = useContext(BotHuesContext);
  return (
    <span className="bot-thread-list-faces">
      {bots.slice(0, 3).map((bot) => (
        <span key={bot.key} className="bot-thread-list-face" data-bot-hue={hueOf(bot, hues)}>
          <BotFace
            profileId={bot.profileId}
            identity={bot.identity}
            name={bot.name}
            size={18}
            decorative
          />
        </span>
      ))}
      {bots.length > 3 && (
        <span className="bot-thread-list-more" aria-hidden="true">
          +{bots.length - 3}
        </span>
      )}
      <span className="sr-only">{bots.map((bot) => bot.name).join(", ")}</span>
    </span>
  );
}

/** A thread's Bots at a glance: one 28px decorative face per Bot, the working one (`lit`, its
 * key; null while no Bot works) lit and the rest dimmed, with a needs-you dot for any Bot whose
 * key is in `needsYou`. The strip is purely decorative — the thread's own text already carries
 * this for assistive technology. */
export function BotThreadStrip({
  bots,
  lit,
  needsYou,
}: {
  bots: readonly ThreadBot[];
  lit: string | null;
  needsYou: ReadonlySet<string>;
}) {
  const { hues } = useContext(BotHuesContext);
  return (
    <span className="bot-thread-strip" aria-hidden="true">
      {bots.map((bot) => (
        <span
          key={bot.key}
          className="bot-thread-strip-face"
          data-bot-hue={hueOf(bot, hues)}
          data-lit={bot.key === lit ? "true" : "false"}
        >
          <BotFace
            profileId={bot.profileId}
            identity={bot.identity}
            name={bot.name}
            size={28}
            decorative
            {...(needsYou.has(bot.key) ? { state: "needs_you" as const } : {})}
          />
        </span>
      ))}
    </span>
  );
}

/** A native Bot picker: the chosen Bot's 18px face and hue beside a decorative copy of `label` —
 * the select itself carries the real accessible name, so the two never announce twice. Renders
 * nothing when there is no Bot to choose from. */
export function BotRecipient({
  label,
  bots,
  value,
  disabled,
  onChange,
}: {
  label: string;
  bots: readonly ThreadBot[];
  value: string;
  disabled?: boolean;
  onChange: (key: string) => void;
}) {
  const { hues } = useContext(BotHuesContext);
  if (bots.length === 0) return null;
  const chosen = bots.find((bot) => bot.key === value) ?? (bots[0] as ThreadBot);
  return (
    <label className="bot-recipient" data-bot-hue={hueOf(chosen, hues)}>
      <BotFace
        profileId={chosen.profileId}
        identity={chosen.identity}
        name={chosen.name}
        size={18}
        decorative
      />
      <span aria-hidden="true">{label}</span>
      <select
        aria-label={label}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      >
        {bots.map((bot) => (
          <option key={bot.key} value={bot.key}>
            {bot.name}
          </option>
        ))}
      </select>
    </label>
  );
}
