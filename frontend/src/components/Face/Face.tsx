import "./Face.css";
import { Avatar } from "../Avatar/Avatar";
import { useAvatarUrl, variantFor } from "../Avatar/avatarUrls";
import { useIdentity } from "../Avatar/identity";
import { BotAvatar } from "../Bots/BotAvatar";

export interface FaceProps {
  /** Rendered size in pixels (the icon is always a circle: width === height). */
  size: number;
  /** Pixel size used to choose the fetched avatar variant (`variantFor`). Defaults to
   * `size`; callers can ask for a larger source image so a small avatar stays crisp. */
  imageSize?: number;
  /** Show the small breathing presence dot bottom-right (VIKI is here / active). */
  presence?: boolean;
  ringWidth?: number;
}

/** A loaded custom assistant avatar, a bound profile's decorative `Avatar`, or a neutral
 * ring while identity is unresolved. An unbound assistant without a loaded custom avatar
 * has no portrait. */
export function Face({ size, imageSize = size, presence = false, ringWidth = 1.5 }: FaceProps) {
  // Content-box dot (the ring adds outside it): 8/10/12 for 24/36/48. Offset −2, except
  // at 48 where the design pulls it in to −1.
  const presenceSize = 4 + size / 6;
  const presenceOffset = size === 48 ? -1 : -2;
  const identity = useIdentity();
  const unresolved = identity.unresolved ?? false;
  const profile = unresolved ? null : identity.profile;
  // A bound chat never shows the app assistant's avatar, so it is not fetched for one; an
  // unresolved chat fetches nothing at all.
  const { url } = useAvatarUrl(
    identity.urls,
    unresolved || profile ? null : identity.assistantAvatarId,
    variantFor(imageSize),
  );
  if (!unresolved && !profile && !url) return null;
  return (
    <span className="face" style={{ width: size, height: size }}>
      {unresolved ? (
        <span className="face-neutral" style={{ borderWidth: ringWidth }} aria-hidden="true" />
      ) : profile ? (
        // Every site that renders Face also names the bound profile in visible text nearby, so
        // its icon here is decorative: without this, Avatar's own role="img" would announce the
        // same name a second time.
        <span aria-hidden="true">
          {profile.botProfileId ? (
            <BotAvatar
              profileId={profile.botProfileId}
              identity={{
                name: profile.name,
                purpose: "",
                avatar_asset_id: profile.avatarId,
                accent: profile.accent,
                shape: profile.shape,
              }}
              name={profile.name}
              size={size}
            />
          ) : (
            <Avatar
              name={profile.name}
              avatarId={profile.avatarId}
              accent={profile.accent}
              shape={profile.shape}
              size={size}
            />
          )}
        </span>
      ) : url ? (
        <img
          className="face-img"
          src={url}
          alt=""
          width={size}
          height={size}
          style={{ borderWidth: ringWidth }}
        />
      ) : null}
      {presence ? (
        <span
          className="face-presence breathe"
          style={{
            width: presenceSize,
            height: presenceSize,
            right: presenceOffset,
            bottom: presenceOffset,
          }}
          aria-hidden="true"
        />
      ) : null}
    </span>
  );
}
