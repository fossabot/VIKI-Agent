/**
 * Read-aloud strings: the reply controls and notes, and the two Settings rows. The OS steps
 * (`readaloud.how.*`) are shared by the reply note and the Settings field.
 */

import { defineCatalog } from "./defineCatalog";

export const catalog = defineCatalog({
  en: {
    "readaloud.read": "Read aloud",
    "readaloud.read.label": "Read this reply aloud",
    "readaloud.pause": "Pause",
    "readaloud.pause.label": "Pause reading aloud",
    "readaloud.resume": "Resume",
    "readaloud.resume.label": "Resume reading aloud",
    "readaloud.stop": "Stop",
    "readaloud.stop.label": "Stop reading aloud",
    "readaloud.notice":
      "Read aloud uses only the voices this computer's speech engine reports as installed, and " +
      "costs nothing. The browser's online voices are not used: the text goes only to that engine.",
    "readaloud.notice.linux":
      "On Linux that engine is speech-dispatcher, with the voices and modules you set up for it.",
    "readaloud.notice.confirm": "Read aloud",
    "readaloud.nothing": "This reply has no text to read aloud.",
    "readaloud.unreadable": "This reply cannot be read aloud.",
    "readaloud.failed": "Read-aloud stopped because the voice reported an error.",
    "readaloud.unsupported": "This browser cannot read aloud.",
    "readaloud.no_voice": "No voice for {language} is installed on this computer.",
    "readaloud.no_voices": "No voice for reading aloud is installed on this computer.",
    "readaloud.how.windows":
      "To add one, open Windows Settings › Time & language › Speech › Manage voices › Add " +
      "voices, then reopen VIKI Agent.",
    "readaloud.how.mac":
      "To add one, open System Settings › Accessibility › Read & Speak (Spoken Content on " +
      "earlier macOS) › System voice, then reopen VIKI Agent.",
    "readaloud.how.linux":
      "Voices come from speech-dispatcher: install it and a voice for it, such as espeak-ng, " +
      "with your distribution's package manager, then reopen VIKI Agent.",
    "readaloud.settings.loading": "Looking for installed voices…",
    "readaloud.settings.automatic": "Automatic ({voice})",
    "readaloud.settings.not_installed": "{voice} (not installed)",
    "readaloud.settings.missing": "No installed voice: {languages}",

    "settings.key.voice_read_aloud_rate.label": "Read-aloud speed",
    "settings.key.voice_read_aloud_rate.desc":
      "How fast replies are read aloud. A change applies from the next reply you play.",
    "settings.key.voice_read_aloud_voices.label": "Read-aloud voices",
    "settings.key.voice_read_aloud_voices.desc":
      "The voice for each language. Only voices this computer's speech engine reports as " +
      "installed are listed; the browser's online voices are not used.",

    "settings.value.voice_read_aloud_rate.0.75": "0.75×",
    "settings.value.voice_read_aloud_rate.1": "1×",
    "settings.value.voice_read_aloud_rate.1.25": "1.25×",
    "settings.value.voice_read_aloud_rate.1.5": "1.5×",
    "settings.value.voice_read_aloud_rate.2": "2×",
  },
});
