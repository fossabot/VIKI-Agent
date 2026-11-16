/**
 * Hands-free conversation strings: the toggle, its first-use notice (the read-aloud notice plus
 * one sentence about the mic), the status strip's five phases plus the voice-preparing label
 * shown in place of Thinking, the card announcement, why the mode ended or did not start
 * (`no_voice` names the gate's check for a local English voice — Enter only ever picks
 * `HANDS_FREE_LANGUAGE`, "en"), the notice's own Dismiss, what the loop last recovered from or
 * skipped (the strip's note line), and the tag on a message hands-free sent.
 */

import { defineCatalog } from "./defineCatalog";

export const catalog = defineCatalog({
  en: {
    "handsfree.toggle": "Hands-free",
    "handsfree.notice.mic":
      "Hands-free also opens the microphone to listen for your reply, and sends it on its own " +
      "once you stop talking.",
    "handsfree.notice.confirm": "Start hands-free",
    "handsfree.phase.listening": "Listening",
    "handsfree.phase.sending": "Sending",
    "handsfree.phase.thinking": "Thinking",
    "handsfree.phase.waiting": "Waiting for you",
    "handsfree.phase.speaking": "Speaking",
    "handsfree.phase.rearming": "Listening",
    "handsfree.phase.preparing": "Getting the voice ready",
    "handsfree.hint.interrupt": "Press the mic to interrupt",
    "handsfree.end": "End",
    "handsfree.announce.card": "Approval needed on screen",
    "handsfree.notice.dictation_unavailable":
      "Dictation is not ready, so hands-free did not start.",
    "handsfree.notice.no_voice":
      "No local English voice is installed, so hands-free did not start.",
    "handsfree.notice.dismiss": "Dismiss",
    "handsfree.notice.nothing_heard": "Hands-free heard nothing, so it stopped.",
    "handsfree.notice.send_blocked": "The message could not be sent, so hands-free stopped.",
    "handsfree.notice.turn_failed": "The reply failed, so hands-free stopped.",
    "handsfree.notice.turn_stopped": "The reply was stopped, so hands-free stopped.",
    "handsfree.notice.speech_failed": "Reading the reply aloud failed, so hands-free stopped.",
    "handsfree.notice.window_hidden": "Hands-free stopped because this window was covered.",
    "handsfree.notice.session_switched": "Hands-free stopped because you switched chats.",
    "handsfree.note.voice_fallback":
      "The chosen voice did not answer, so the system voice read this reply.",
    "handsfree.note.voice_slow":
      "The chosen voice is still starting, so the system voice read this reply.",
    "handsfree.note.nothing_read": "This reply had nothing to read aloud.",
    "handsfree.note.no_voice_for":
      "No installed voice speaks this reply's language, so it was not read aloud.",
    "handsfree.voice.tag": "voice",
    "handsfree.voice.label": "Sent by voice",
  },
});
