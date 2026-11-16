/**
 * Reusing a voice turn's saved text as if typed: ArrowUp recall, Retry's
 * fill and Edit in a new chat (`App.tsx`) all put a turn's `rejoinPrompt` text back in the message
 * box. A typed Send never carries the spoken-answer request, so a turn's voice tag must not either
 * — strip it here before the text reaches the box. Text with no request, such as a typed turn's,
 * is returned as is.
 */
import { withoutSpokenAnswerRequest } from "./spokenAnswer";

export function withoutVoiceTag(text: string): string {
  return withoutSpokenAnswerRequest(text) ?? text;
}
