/**
 * A folder's name beside a session (a rail row, a tab, the conversation header). The text is
 * a text node and the full text is the tooltip; CSS keeps it to one ellipsised line.
 */
import type { ReactElement } from "react";
import "./RootLabel.css";

export function RootLabel({ text }: { text: string }): ReactElement {
  return (
    <span className="root-label" title={text}>
      {text}
    </span>
  );
}
