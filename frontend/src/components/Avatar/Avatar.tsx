import type { ReactNode } from "react";
import "./Avatar.css";
import { useAvatarUrl, variantFor } from "./avatarUrls";
import { type Accent, type Shape, useIdentity } from "./identity";

/** The user-given name is the accessible name; `alt=""` keeps image metadata from replacing it. */
export function Avatar({
  name,
  avatarId,
  accent,
  shape,
  size,
  color = false,
  fallback,
}: {
  name: string;
  avatarId: string | null;
  accent: Accent;
  shape: Shape;
  size: number;
  /** Shows an uploaded image in colour instead of the app's grayscale. */
  color?: boolean;
  /** Drawn in place of the initial while no image shows. */
  fallback?: ReactNode;
}) {
  const identity = useIdentity();
  const { url } = useAvatarUrl(identity.urls, avatarId, variantFor(size));
  const trimmed = name.trim();
  const initial = Array.from(trimmed)[0]?.toUpperCase() ?? "?";
  // No name to announce: an empty accessible label reads as "unlabelled image" to a screen
  // reader, so an empty name is decorative instead of a role="img" with nothing to say.
  const namedProps = trimmed === "" ? { "aria-hidden": true } : { role: "img", "aria-label": name };
  return (
    <span
      className="avatar"
      data-accent={accent}
      data-shape={shape}
      data-color={color ? "true" : undefined}
      {...namedProps}
      style={{ width: size, height: size }}
    >
      {url ? (
        <img alt="" src={url} />
      ) : (
        (fallback ?? (
          <span className="avatar-initials" aria-hidden="true">
            {initial}
          </span>
        ))
      )}
    </span>
  );
}
