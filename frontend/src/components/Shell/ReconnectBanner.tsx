/** Connection status below the header. Recovery uses retained events or a bounded snapshot;
 * omitted content is explained in the conversation. The socket owns reconnect scheduling.
 * A protocol mismatch preserves the editor and asks the user to copy it before reloading.
 */
import type { ReactElement } from "react";
import type { SocketStatus } from "../../state/socket";
import { Spinner } from "../Loading/Loading";
import "./ReconnectBanner.css";

export interface ReconnectBannerProps {
  status: SocketStatus;
  /** True once the socket has been open at least once in this page's life. */
  everOpen: boolean;
}

export function ReconnectBanner({ status, everOpen }: ReconnectBannerProps): ReactElement | null {
  if (status === "incompatible")
    return (
      <div className="banner jp" role="alert">
        This window's version doesn't match the server's. Copy what you typed, then reload the
        window.
      </div>
    );
  if (status === "open" || status === "unauthorized") return null;
  // "connecting" also covers a cold start's very first attempt, which is not a disconnection —
  // everOpen is what tells the two apart.
  if (!everOpen) return null;
  return (
    <div className="banner jp" role="status" aria-live="polite">
      <Spinner className="banner-spin" />
      <span>Disconnected. Reconnecting…</span>
      <span className="banner-note">After reconnecting, the rest of the reply will appear.</span>
    </div>
  );
}
