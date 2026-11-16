/** The app-wide and bound-chat identity context: assistant and user names and
 * avatars, and the shared avatar object-URL cache and client every reader needs. Frozen renders
 * without a provider read `DEFAULT_IDENTITY` from the context's own default value. */
import { createContext, useContext } from "react";
import type { AvatarsApi } from "../../clients/media/avatarsApi";
import type { ProfileIdentityV1 } from "../../clients/profiles/profileApi";
import type { AvatarUrls } from "./avatarUrls";

export type Accent = ProfileIdentityV1["accent"];
export type Shape = ProfileIdentityV1["shape"];

export interface BoundProfile {
  name: string;
  avatarId: string | null;
  accent: Accent;
  shape: Shape;
  revision: number;
  /** Set only for a Bot's chat: its profile id, for the Bot's assigned or drawn hue (botHues.ts). */
  botProfileId?: string;
}

export interface Identity {
  assistantName: string;
  assistantAvatarId: string | null;
  userAvatarId: string | null;
  profile: BoundProfile | null;
  urls: AvatarUrls | null;
  avatars: AvatarsApi | null;
  /** True while the chat's binding, or its identity, is not known. An unresolved identity
   * renders no assistant name, no assistant face and no profile chips; it wins over every other
   * field, so a set `assistantName`, `assistantAvatarId` or `profile` are ignored while it is
   * true. */
  unresolved?: boolean;
}

export const DEFAULT_IDENTITY: Identity = {
  assistantName: "VIKI",
  assistantAvatarId: null,
  userAvatarId: null,
  profile: null,
  urls: null,
  avatars: null,
};

export const IdentityContext = createContext<Identity>(DEFAULT_IDENTITY);

export function useIdentity(): Identity {
  return useContext(IdentityContext);
}
