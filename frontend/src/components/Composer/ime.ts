/**
 * The one IME predicate the composer and the `/` menu both need:
 * either signal alone means a Japanese (or other IME) candidate window is still open, and the
 * Enter/Escape/ArrowUp that closes it must never be read as the user's own keystroke.
 *
 * `isComposing` is the standards field; `keyCode === 229` is what some IME/browser pairs send
 * instead (Composer.tsx's own docstring: "Enter sends only when the event is not composing:
 * `!event.nativeEvent.isComposing && event.keyCode !== 229`"). Shared here rather than written
 * twice — Composer reads it off `event.nativeEvent` (a React synthetic event), SlashMenu off the
 * native `KeyboardEvent` its own `window` listener receives; both shapes satisfy this parameter.
 */
export function isComposingKeydown(event: { isComposing: boolean; keyCode: number }): boolean {
  return event.isComposing || event.keyCode === 229;
}
