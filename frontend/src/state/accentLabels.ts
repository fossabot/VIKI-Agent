import type { SessionColor } from "../clients/conversations/sessionMetadataApi";

export const ACCENT_LABELS: Readonly<Record<SessionColor, string>> = {
  slate: "Gray 1",
  blue: "Gray 2",
  green: "Gray 3",
  purple: "Gray 4",
  orange: "Primary",
  red: "Foreground",
};

export const SESSION_COLOR_LABELS: Readonly<Record<SessionColor, string>> = {
  blue: "Blue",
  green: "Green",
  orange: "Orange",
  purple: "Purple",
  red: "Red",
  slate: "Teal",
};
