export interface SavedThreadMember {
  key: string;
  bot_id: string | null;
  profile_id: string | null;
  session_id: string | null;
  enabled: boolean;
}
export interface SavedThreadMessage {
  id: string;
  at: string;
  member_key: string | null;
  session_id: string;
  command_id: string;
}
export interface SavedThreadState {
  title: string;
  phase: "idle" | "running" | "paused" | "done";
  members: SavedThreadMember[];
  messages: SavedThreadMessage[];
}
export interface SavedBotThread {
  thread_id: string;
  workspace_id: string;
  revision: number;
  created_at: string;
  updated_at: string;
  state: SavedThreadState;
}
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
/** The seat words a saved member key may be; the Engineer seat is keyed by its profile UUID. */
export const SEAT_WORDS: readonly string[] = [
  "chief",
  "ops",
  "checker",
  "research",
  "writer",
  "desk",
];
const object = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const uuid = (value: unknown): value is string => typeof value === "string" && UUID.test(value);
const nullableId = (value: unknown) => value === null || uuid(value);
export function savedBotThread(value: unknown): value is SavedBotThread {
  if (
    !object(value) ||
    !uuid(value.thread_id) ||
    !uuid(value.workspace_id) ||
    !Number.isSafeInteger(value.revision) ||
    (value.revision as number) < 1 ||
    !object(value.state)
  )
    return false;
  const state = value.state;
  if (
    typeof state.title !== "string" ||
    state.title.length > 128 ||
    !["idle", "running", "paused", "done"].includes(state.phase as string) ||
    !Array.isArray(state.members) ||
    !state.members.length ||
    state.members.length > 64 ||
    !Array.isArray(state.messages) ||
    state.messages.length > 200
  )
    return false;
  const keys = new Set<string>();
  for (const member of state.members) {
    if (
      !object(member) ||
      typeof member.key !== "string" ||
      (!SEAT_WORDS.includes(member.key) && !uuid(member.key)) ||
      keys.has(member.key) ||
      !nullableId(member.bot_id) ||
      !nullableId(member.profile_id) ||
      !nullableId(member.session_id) ||
      typeof member.enabled !== "boolean"
    )
      return false;
    keys.add(member.key);
  }
  const messages = new Set<string>();
  for (const message of state.messages) {
    if (
      !object(message) ||
      !uuid(message.id) ||
      messages.has(message.id) ||
      !uuid(message.session_id) ||
      !uuid(message.command_id) ||
      typeof message.at !== "string" ||
      !Number.isFinite(Date.parse(message.at)) ||
      (message.member_key !== null && !keys.has(message.member_key as string))
    )
      return false;
    messages.add(message.id);
  }
  return [value.created_at, value.updated_at].every(
    (at) => typeof at === "string" && Number.isFinite(Date.parse(at)),
  );
}
