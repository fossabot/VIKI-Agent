import type { ReactElement } from "react";

/** The one inline icon map: a bare stroke path inside an `.ico`-styled svg (fill/stroke come
 * from `.ico`). Add an icon here, not as a local path. */
const PIN_PATH = "M6 2h4 M7 2v4L5 9h6L9 6V2 M8 9v5";

export const ICONS = {
  newWindow: "M2 3h12v10H2z M2 6h12",
  terminal: "M2 3h12v10H2z M5 7l2 1.5L5 10 M8.5 10h2.5",
  rename: "M10.5 2.5l3 3L6 13H3v-3z M9 4l3 3",
  pin: PIN_PATH,
  unpin: `${PIN_PATH} M2 2l12 12`,
  markRead: "M3 8.5l3 3 7-7",
  check: "M3 8.5l3 3 7-7",
  markUnread: "M8 5a3 3 0 1 1 0 6a3 3 0 1 1 0-6z",
  color: "M2 2h6l6 6-6 6-6-6z M5.5 5.5h.01",
  copy: "M5 5h8v9H5z M3 11V2h8",
  fork: "M5 2v12 M5 10c0-3 6-2 6-6 M11 2v2",
  transcript: "M9 2h5v5 M14 2L7 9 M12 9v5H2V4h5",
  export: "M8 2v8 M5 7l3 3 3-3 M3 13h10",
  newChat: "M2 3h5l2 2h5v9H2z M8 7v4 M6 9h4",
  archive: "M2.5 4.5h11v2h-11zM3.5 6.5v6h9v-6M6.5 9h3",
  unarchive: "M2.5 4.5h11v2h-11zM3.5 6.5v6h9v-6 M8 12V8.5 M6.5 10L8 8.5 9.5 10",
  close: "M4 4l8 8 M12 4l-8 8",
  chat: "M2 2h12v9H7l-4 3v-3H2z",
  capabilities: "M2 2h5v5H2z M9 2h5v5H9z M2 9h5v5H2z M9 9h5v5H9z",
  messaging: "M2 2h12v9H7l-4 3v-3H2z M5 5h6 M5 8h4",
  bots: "M3 4h10v9H3z M8 1v3 M6 7v1 M10 7v1 M6 10h4 M1 7v3 M15 7v3",
  kanban: "M1 2h14v12H1z M6 2v12 M11 2v12 M3 5h1 M8 5h1 M13 5h0 M3 8h1 M8 8h1",
  threads: "M1 2h10v8H5l-3 3v-3H1z M11 5h4v8h-2v2l-3-2H7v-3",
  people:
    "M5.5 7a2 2 0 1 0 0-4 2 2 0 0 0 0 4 M10.5 7a2 2 0 1 0 0-4 2 2 0 0 0 0 4 M1.5 13v-1a4 4 0 0 1 8 0v1z M9 9a3.5 3.5 0 0 1 5.5 2.8v1.2h-4",
  agents:
    "M8 2a2 2 0 1 1 0 4 2 2 0 0 1 0-4 M4 14v-2a4 4 0 0 1 8 0v2 M3 4a2 2 0 0 0 0 4 M1 13v-2a3 3 0 0 1 2-2 M13 4a2 2 0 0 1 0 4 M15 13v-2a3 3 0 0 0-2-2",
  routines: "M2 5a6 6 0 1 1 0 6 M2 1v4h4 M8 4v4l3 2",
  code: "m6 4-4 4 4 4 M10 4l4 4-4 4",
  decision: "M8 2a4 4 0 0 1 2.5 7.1V11h-5V9.1A4 4 0 0 1 8 2 M6 13h4 M7 15h2",
  folder: "M2 3h5l2 2h5v9H2z",
  plus: "M8 2v12 M2 8h12",
  search: "M11.5 7a4.5 4.5 0 1 1-9 0a4.5 4.5 0 1 1 9 0 M10.5 10.5 14 14",
  trash: "M3 4.5h10 M6.5 4.5V3h3v1.5 M4.5 4.5l.7 9h5.6l.7-9 M7 7v4.5 M9 7v4.5",
  statusTodo: "M8 2.5a5.5 5.5 0 1 1 0 11a5.5 5.5 0 1 1 0-11z",
  statusWorking: "M5.5 3.5v9l7-4.5z",
  statusBlocked: "M8 2.5a5.5 5.5 0 1 1 0 11a5.5 5.5 0 1 1 0-11z M4.1 11.9l7.8-7.8",
  statusReview:
    "M1.5 8s2.5-4.5 6.5-4.5 6.5 4.5 6.5 4.5-2.5 4.5-6.5 4.5S1.5 8 1.5 8z M8 6.5a1.5 1.5 0 1 1 0 3a1.5 1.5 0 1 1 0-3z",
  statusDone: "M8 2.5a5.5 5.5 0 1 1 0 11a5.5 5.5 0 1 1 0-11z M5.5 8.2l1.8 1.8 3.2-3.6",
  statusOther: "M8 2.5a5.5 5.5 0 1 1 0 11a5.5 5.5 0 1 1 0-11z M5.5 8h5",
  statusUnread: "M8 2l6.5 11.5h-13z M8 6.5v3 M8 11.5h.01",
  shield:
    "M8 1.5 13.5 4v3.5c0 3.5-2.3 5.7-5.5 7-3.2-1.3-5.5-3.5-5.5-7V4z M5.5 7.5 7.3 9.3 10.8 5.8",
} as const;

export type IconName = keyof typeof ICONS;

export interface IconProps {
  name: IconName;
  size?: 14 | 16 | 20;
}

export function Icon({ name, size = 16 }: IconProps): ReactElement {
  return (
    <svg
      className="ico"
      width={size}
      height={size}
      viewBox="0 0 16 16"
      aria-hidden="true"
      data-icon={name}
    >
      <path d={ICONS[name]} />
    </svg>
  );
}
