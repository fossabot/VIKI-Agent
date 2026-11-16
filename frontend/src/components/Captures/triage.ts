import type { Capture, Destination } from "../../clients/vaults/capturesApi";
import { fenceFor } from "../Decisions/handoff";

export interface TriageChoice {
  ref: string;
  revision: string | null;
  /** The capture's `modified` stamp at the moment this choice was made. Compared only while
   * `revision` is null: an unread/too-large capture has no content hash, so `null === null`
   * alone would let a stale choice survive any later change forever. */
  modified: string;
  destination: Destination;
  via: "suggestion" | "owner";
}

export function destinationLabel(destination: Destination): string {
  switch (destination.to) {
    case "knowledge":
      return "New note in 30-knowledge/";
    case "source":
      return "Source note in 40-sources/";
    case "task":
      return `Next action in 10-projects/${destination.project}/tasks.md`;
    case "area":
      return "Note in 20-areas/";
    case "person":
      return "Note in 70-people/";
    case "daily":
      return "One line in today's daily note";
    case "discard":
      return "Discard: archive it whole, never delete";
  }
}

export function captureHandoffText(choices: readonly TriageChoice[]): string {
  const body = choices
    .map(
      (choice) =>
        `${choices.indexOf(choice) + 1}. ${choice.ref} → ${destinationLabel(choice.destination)}`,
    )
    .join("\n");
  const fence = fenceFor(body);

  return [
    "File these 00-inbox captures with the /inbox flow, using my choices below. File only these captures, in this order, and keep my words verbatim. A capture marked Discard is archived whole under this vault's rules, never deleted.",
    "",
    `${fence}text`,
    body,
    `${fence}`,
    "",
    "Skip a capture that is no longer in 00-inbox/ and say so. End with the filing report.",
  ].join("\n");
}

export function pruneChoices(
  choices: readonly TriageChoice[],
  captures: readonly Capture[],
): readonly TriageChoice[] {
  const captureMap = new Map(captures.map((c) => [c.ref, c]));

  const pruned = choices.filter((choice) => {
    const capture = captureMap.get(choice.ref);
    if (!capture || capture.revision !== choice.revision) return false;
    if (capture.revision !== null) return true;
    // An unread/too-large capture has no revision hash, so `null === null` above always
    // "matches": also require its recorded `modified` stamp to still match the current one.
    return choice.modified === capture.modified;
  });

  return pruned.length === choices.length ? choices : pruned;
}
