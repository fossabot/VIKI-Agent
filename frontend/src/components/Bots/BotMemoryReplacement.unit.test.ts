import { expect, it } from "vitest";
import { replacementRequest } from "./BotMemoryReplacement";

const ref = "bot-memory/22222222-2222-4222-8222-222222222222/MEMORY.md";
const sha256 = "a".repeat(64);

it("names the file and its hash and ends with the complete new text in one fenced block", () => {
  const text = "---\nname: tea\n---\n\n- likes tea {ref}\n";
  const request = replacementRequest(ref, sha256, text);
  expect(request).toContain(`Replace the whole file ${ref} with exactly the text`);
  expect(request).toContain(`whose SHA-256 is ${sha256}`);
  expect(request).toContain("If it differs or you cannot check it, change nothing and tell me.");
  expect(request).toContain("change no other file");
  expect(request.endsWith(`\n\`\`\`\n${text}\n\`\`\``)).toBe(true);
  // The text is data: a placeholder inside it is not filled in.
  expect(request).toContain("- likes tea {ref}\n");
});

it("fences the text with more backticks than any run inside it", () => {
  const text = "a\n```js\nx\n```\n````\nb";
  const request = replacementRequest(ref, sha256, text);
  expect(request.endsWith(`\n\`\`\`\`\`\n${text}\n\`\`\`\`\``)).toBe(true);
});
