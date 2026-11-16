/** Explains that an earlier app window has an expired launch token and must be replaced by a fresh launch. The page cannot renew that token; conversation records remain in their transcripts. */
import type { ReactElement } from "react";
import { Face } from "../Face/Face";
import "./StaleWindow.css";

export function StaleWindow(): ReactElement {
  return (
    <div className="stale" role="alert">
      <Face size={40} imageSize={128} />
      <div className="stale-title jp">This window belongs to a previous launch.</div>
      <div className="stale-line jp">
        Open VIKI Agent again. Your conversation records are kept.
      </div>
    </div>
  );
}
