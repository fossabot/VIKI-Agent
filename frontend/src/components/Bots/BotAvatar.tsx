import { useContext } from "react";
import type { ProfileIdentityV1 } from "../../clients/profiles/profileApi";
import { Avatar } from "../Avatar/Avatar";
import { BotIcon } from "./BotIcon";
import { BotHuesContext } from "./botHues";

/** A Bot's avatar, in colour: its profile's uploaded image, or else its generated bot icon,
 * drawn from the profile so every Bot on that profile shares it. A stored hue (BotHuesContext)
 * overrides the icon's own design hue; with none stored, the design hue draws. */
export function BotAvatar({
  profileId,
  identity,
  name,
  size,
}: {
  profileId: string;
  identity: ProfileIdentityV1;
  name: string;
  size: number;
}) {
  const { hues, icons } = useContext(BotHuesContext);
  return (
    <Avatar
      name={name}
      avatarId={identity.avatar_asset_id}
      accent={identity.accent}
      shape={identity.shape}
      size={size}
      color
      fallback={
        <BotIcon
          seed={profileId}
          name={identity.name}
          hue={hues[profileId]}
          icon={icons?.[profileId]}
        />
      }
    />
  );
}
