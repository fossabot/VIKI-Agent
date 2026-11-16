import { createContext } from "react";
import type { ConversationSearchApi } from "../clients/conversations/conversationSearchApi";

export const ArtifactContext = createContext<{
  api: ConversationSearchApi;
  sessionId: string;
} | null>(null);
