/**
 * `withoutVoiceTag`: the pure strip App.tsx's voice-turn reuse sites
 * (ArrowUp recall, Retry's fill, Edit in a new chat) apply to `rejoinPrompt`'s text before it
 * reaches the message box, so a typed Send never carries the spoken-answer request.
 */
import { describe, expect, test } from "vitest";
import { withSpokenAnswerRequest } from "./spokenAnswer";
import { withoutVoiceTag } from "./voiceRecall";

describe("withoutVoiceTag", () => {
  test("strips a voice turn's request", () => {
    expect(withoutVoiceTag(withSpokenAnswerRequest("buy milk"))).toBe("buy milk");
  });

  test("leaves a typed turn's text unchanged", () => {
    expect(withoutVoiceTag("buy milk")).toBe("buy milk");
  });
});
