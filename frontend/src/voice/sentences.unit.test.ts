/**
 * Cutting reply text into Voicebox-sized sentences: sentenceChunks for a finished block,
 * createSpokenStream for the first paragraph of each text stretch while a reply still streams.
 */
import { describe, expect, test } from "vitest";
import type { Block } from "../state/view";
import { createSpokenStream, MAX_VOICEBOX_CHUNK_CHARS, sentenceChunks } from "./sentences";
import { MAX_SPOKEN_CHARS } from "./spokenAnswer";

describe("sentenceChunks", () => {
  test("merges short sentences up to the minimum, flushing what is left", () => {
    const chunks = sentenceChunks(
      "Hi. Bye. Go now. See ya later today. Another one follows after that!",
      "en",
    );
    expect(chunks).toEqual([
      "Hi. Bye. Go now. See ya later today.",
      "Another one follows after that!",
    ]);
  });

  test("keeps every chunk under 300, cutting a long sentence and forcing out a short remainder first", () => {
    const long = `${"word ".repeat(63)}end.`; // one 319-char sentence, over the 300 cap
    // Capitalised so Intl.Segmenter reads it as a new sentence, not a continuation of `long`.
    const next = `A${" a".repeat(139)} done.`; // 285 chars: fits alone, not after the cut's tail
    const chunks = sentenceChunks(`${long} ${next}`, "en");
    expect(chunks).toEqual(["word ".repeat(60).trim(), `${"word ".repeat(3)}end.`, next]);
    for (const chunk of chunks) expect(chunk.length).toBeLessThanOrEqual(MAX_VOICEBOX_CHUNK_CHARS);
  });

  test("packs Japanese sentences under the 6-character minimum with no space between them", () => {
    expect(sentenceChunks("はい。そうです。", "ja")).toEqual(["はい。そうです。"]);
  });
});

let counter = 0;
function textBlock(value: string): Block {
  return { kind: "text", id: `text-${counter++}`, text: value, done: false };
}
function toolsBlock(): Block {
  return { kind: "tools", id: `tools-${counter++}`, rows: [] };
}

describe("createSpokenStream", () => {
  test("emits a sentence only when the next one starts", () => {
    const stream = createSpokenStream();
    let chunks = stream.update([textBlock("The build finished successfully just now.")], false);
    expect(chunks).toEqual([]);

    chunks = stream.update(
      [textBlock("The build finished successfully just now. A second sentence begins here.")],
      false,
    );
    expect(chunks).toEqual([{ text: "The build finished successfully just now.", group: 0 }]);
  });

  test("emits a Japanese sentence ending in a fullwidth ？ once the next one, ending in ！, starts", () => {
    const stream = createSpokenStream();
    expect(stream.update([textBlock("ビルドは無事に終わりましたか？")], false)).toEqual([]);
    expect(
      stream.update([textBlock("ビルドは無事に終わりましたか？次の文が始まります！")], false),
    ).toEqual([{ text: "ビルドは無事に終わりましたか？", group: 0 }]);
  });

  test("closes the paragraph at a blank line, even before the stretch itself closes", () => {
    const stream = createSpokenStream();
    expect(stream.update([textBlock("The build finished successfully.")], false)).toEqual([]);

    const chunks = stream.update(
      [textBlock("The build finished successfully.\n\nDetails below.")],
      false,
    );
    expect(chunks).toEqual([{ text: "The build finished successfully.", group: 0 }]);
  });

  test("done flushes a held-back sentence that never reached the minimum", () => {
    const stream = createSpokenStream();
    expect(stream.update([textBlock("Go now.")], false)).toEqual([]);
    expect(stream.update([textBlock("Go now.")], true)).toEqual([{ text: "Go now.", group: 0 }]);
  });

  test("tools start a new group", () => {
    const stream = createSpokenStream();
    const chunks = stream.update(
      [
        textBlock("First reply sentence appears here."),
        toolsBlock(),
        textBlock("Second reply sentence appears now."),
      ],
      true,
    );
    expect(chunks).toEqual([
      { text: "First reply sentence appears here.", group: 0 },
      { text: "Second reply sentence appears now.", group: 1 },
    ]);
  });

  test("drops Markdown, a link's address, a bare URL and emoji from the spoken text", () => {
    const stream = createSpokenStream();
    const reply =
      "**Bold** and _soft_ with `code`, a [link](https://example.com/a) and\n" +
      "https://example.com/b plus 😀 emoji 👍🏽 🇯🇵 too.";
    const chunks = stream.update([textBlock(reply)], true);
    expect(chunks).toEqual([
      { text: "Bold and soft with code, a link and plus emoji too.", group: 0 },
    ]);
  });

  test("skips a leading code block to find the first paragraph", () => {
    const stream = createSpokenStream();
    const reply = "```\nconsole.log(1)\n```\n\nThe answer is forty-two today.";
    const chunks = stream.update([textBlock(reply)], true);
    expect(chunks).toEqual([{ text: "The answer is forty-two today.", group: 0 }]);
  });

  test("stops at three sentences, dropping the fourth", () => {
    const stream = createSpokenStream();
    const reply =
      "First sentence is here now. Second sentence is here now. " +
      "Third sentence is here now. Fourth sentence is here now.";
    const chunks = stream.update([textBlock(reply)], true);
    expect(chunks).toEqual([
      { text: "First sentence is here now.", group: 0 },
      { text: "Second sentence is here now.", group: 0 },
      { text: "Third sentence is here now.", group: 0 },
    ]);
  });

  test("cuts a single first sentence over the character cap, the way capped does", () => {
    const stream = createSpokenStream();
    const reply = `${"word ".repeat(100)}end.`;
    const chunks = stream.update([textBlock(reply)], true);
    // capped's own cap (350) is above the packer's Voicebox cap (300), so the cut piece may still
    // come back as more than one chunk; what matters is neither cap is ever breached.
    expect(chunks.length).toBeGreaterThan(0);
    for (const chunk of chunks) {
      expect(chunk.group).toBe(0);
      expect(chunk.text.length).toBeLessThanOrEqual(MAX_VOICEBOX_CHUNK_CHARS);
    }
    const combined = chunks.reduce((sum, chunk) => sum + chunk.text.length, 0);
    expect(combined).toBeLessThanOrEqual(MAX_SPOKEN_CHARS);
    expect(chunks.some((chunk) => chunk.text.includes("end."))).toBe(false);
    expect(chunks.at(-1)?.text.endsWith("word")).toBe(true);
  });

  test("emits nothing the second time for unchanged blocks", () => {
    const stream = createSpokenStream();
    const blocks = [textBlock("Only one short sentence appears right here.")];
    const first = stream.update(blocks, true);
    expect(first.length).toBeGreaterThan(0);
    expect(stream.update(blocks, true)).toEqual([]);
  });

  test("speaks Japanese sentences with no space between them", () => {
    const stream = createSpokenStream();
    const chunks = stream.update([textBlock("はい。そうです。")], true);
    expect(chunks).toEqual([{ text: "はい。そうです。", group: 0 }]);
  });
});
