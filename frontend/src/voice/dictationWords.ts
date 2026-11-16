const SETUP = "uv run python scripts/voice_setup.py";

const NOTICES: Record<string, string> = {
  setup_needed: `No dictation model is installed. Run ${SETUP}, then try again.`,
  model_mismatch: `The installed dictation model does not match its record. Run ${SETUP} --check, then try again.`,
  backend_missing: `The dictation engine for this computer is not installed. Run ${SETUP}, then try again.`,
  backend_mismatch: `The installed dictation engine does not match its record. Run ${SETUP} --check, then try again.`,
  engine_unavailable:
    "The dictation engine cannot start. Check the setup in Settings, then try again.",
  worker_unavailable:
    "The dictation engine cannot start. Check the setup in Settings, then try again.",
  worker_timeout: "The dictation engine took too long to start. Press the mic to try again.",
  engine_mismatch: `The dictation engine does not match the installed model. Run ${SETUP} --check, then try again.`,
  model_unavailable:
    "The chosen dictation model cannot be used. Choose another in Settings, then try again.",
  backend_unavailable:
    "The chosen dictation backend cannot be used. Choose another in Settings, then try again.",
  worker_crashed:
    "The dictation engine stopped. Words already added are kept. Press the mic to start it again.",
  protocol_error: "The dictation engine gave an unexpected answer. Press the mic to try again.",
  decode_failed: "The dictation engine could not read that speech. Press the mic to try again.",
  internal: "Dictation failed inside the app. Press the mic to try again.",
  invalid_input: "The dictation engine refused the audio. Press the mic to try again.",
  overloaded:
    "Dictation could not keep up, so it stopped. The words so far are kept. Press the mic to continue.",
  capture_cap:
    "Dictation stops at 2 minutes. The words so far are kept. Press the mic to continue.",
  final_timeout:
    "The last words took too long to arrive, so dictation stopped. Press the mic to try again.",
  busy: "Another window is dictating. Wait for it to finish, then try again.",
  refused: "The app refused the dictation connection. Reload the window, then try again.",
  timed_out:
    "No audio reached the dictation engine in time, so it stopped. Press the mic to try again.",
  connection_lost:
    "The dictation connection was lost. Words already added are kept. Press the mic to try again.",
  still_starting:
    "The dictation engine is still starting. Wait a moment, then press the mic again.",
  mic_denied: "The microphone is blocked. Allow it for this app in the browser, then try again.",
  mic_missing:
    "No microphone was found. Connect one or choose another in Settings, then try again.",
  mic_busy: "The microphone is in use by another program. Close that program, then try again.",
  mic_failed: "The microphone could not start. Check it, then try again.",
  mic_lost: "The microphone was disconnected, so dictation stopped. Words so far are kept.",
  mic_stalled: "No sound came from the microphone, so dictation stopped. Words so far are kept.",
  unavailable: "Dictation is not available now. Check the setup in Settings, then try again.",
  finish_first: "Release the mic or press Esc first. Your text is kept.",
};

export function dictationNotice(code: string): string {
  const notice = Object.hasOwn(NOTICES, code) ? NOTICES[code] : undefined;
  return notice ?? `Dictation stopped (${code}). Your text is kept. Press the mic to try again.`;
}
