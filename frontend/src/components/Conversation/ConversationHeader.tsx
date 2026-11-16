/**
 * The 52px header over the conversation
 * (src/accept/m2b/conversation.accept.test.tsx pins the contract).
 *
 * Rules (the design's header):
 * - The session title in the JP face at 15px/500 (`.conversation-title`), truncated to one
 *   line; App already words it (rowTitle, else "Untitled session").
 * - Bound chat: with an `IdentityContext` profile bound and `profileChips` enabled, a `.chip`
 *   `<name> · r<revision>` and a `.chip` `Profile` follow the title. An unresolved identity draws
 *   no chips, whatever `profile` holds.
 */
import type { ReactElement, ReactNode } from "react";
import { useIdentity } from "../Avatar/identity";
import { RootLabel } from "../RootLabel/RootLabel";
import "./Conversation.css";

export interface ConversationHeaderProps {
  title: string;
  /** The folder the session runs in, shown beside the title; absent or null shows nothing. */
  folder?: string | null;
  /** The tab strip; when given, it takes the title's place and no folder label shows beside it. */
  tabs?: ReactNode;
  /** Whether a bound profile's name/revision and Profile chips show; defaults to true. */
  profileChips?: boolean;
}

export function ConversationHeader({
  title,
  folder,
  tabs,
  profileChips = true,
}: ConversationHeaderProps): ReactElement {
  const { profile, unresolved } = useIdentity();

  return (
    <>
      {tabs ? (
        tabs
      ) : (
        <>
          <div className="conversation-title jp">{title}</div>
          {folder ? <RootLabel text={folder} /> : null}
        </>
      )}
      {profileChips && !unresolved && profile ? (
        <>
          <span className="chip">{`${profile.name} · r${profile.revision}`}</span>
          <span className="chip">Profile</span>
        </>
      ) : null}
    </>
  );
}
