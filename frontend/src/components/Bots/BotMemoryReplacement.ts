import { t, tf } from "../../i18n/t";

/**
 * The unsent message a memory-file edit prepares. VIKI Agent writes nothing in `bot-memory/`: the
 * Bot applies this request through its own permission card. The fence is longer than any backtick
 * run in `text`, so the text cannot end the block early. `text` is LF without a BOM; with
 * `keepEndings` the request asks the Bot to keep the file's own line endings and BOM.
 */
export function replacementRequest(
  ref: string,
  sha256: string,
  text: string,
  keepEndings = false,
): string {
  const longestRun = (text.match(/`+/g) ?? []).reduce((most, run) => Math.max(most, run.length), 0);
  const fence = "`".repeat(Math.max(3, longestRun + 1));
  const endings = keepEndings ? ` ${t("bots.memory.replaceRequest.endings")}` : "";
  return tf("bots.memory.replaceRequest", { ref, sha256, fence, text, endings });
}
