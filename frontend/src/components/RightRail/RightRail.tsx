import type { ReactNode } from "react";
import { useIdentity } from "../Avatar/identity";
import { Face } from "../Face/Face";
import "./RightRail.css";

export interface RightRailProps {
  /** Whether VIKI is present right now — drives the presence dot and the "here now" chip. */
  online?: boolean;
  /** Overview content kept outside the scrolling details. */
  summary?: ReactNode;
  summaryHidden?: boolean;
  /** Content pinned below the character header and above the scrolling details. */
  pinned?: ReactNode;
  /** Persistent controls below the scrolling overview summary. */
  footer?: ReactNode;
  children?: ReactNode;
}

/** The right rail frame: VIKI's character card header plus whatever labelled sections
 * (see RailSection) the caller supplies.
 *
 * Below 1280px viewport width (or at 200% zoom in a 1440px window) it is not docked but opened
 * as a drawer from the top bar's VIKI panel toggle — Shell renders it inside RailDrawer and
 * useLayoutMode decides which. A `display: none` media rule here would leave the gates,
 * routines, tiles and VIKI's own status on the page with no way to reach them, so this
 * component says nothing about the viewport. */
export function RightRail({
  online = false,
  summary,
  summaryHidden = false,
  pinned,
  footer,
  children,
}: RightRailProps) {
  const { assistantName } = useIdentity();
  return (
    <aside className="right-rail" data-region="right-rail">
      <div className="right-rail-header">
        <Face size={48} presence={online} />
        <div className="right-rail-id">
          <div className="right-rail-name">
            <span className="identity-name">{assistantName}</span>
            {online ? <span className="chip">here now</span> : null}
          </div>
        </div>
      </div>
      {pinned ? <div className="right-rail-pinned">{pinned}</div> : null}
      <div className="right-rail-details">{children}</div>
      {summary ? (
        <div className="right-rail-summary" hidden={summaryHidden}>
          {summary}
        </div>
      ) : null}
      {footer ? <div className="right-rail-footer">{footer}</div> : null}
    </aside>
  );
}
