import { describe, expect, test } from "vitest";
import {
  MAX_ATTACHMENTS,
  MAX_FILE_BYTES,
  MAX_TEXT_INSERT_BYTES,
  MAX_TURN_BYTES,
  parseAttachments,
  sortFiles,
  validateAttachmentFiles,
} from "./turnInput";

const id = "123e4567-e89b-12d3-a456-426614174000";
const row = (overrides: Record<string, unknown> = {}) => ({
  asset_id: id,
  media_type: "image/png",
  byte_size: 10,
  width: 2,
  height: 2,
  name: "a.png",
  modified_at: null,
  source: "picker",
  ...overrides,
});

test("parseAttachments accepts the bounded ordered wire and rejects duplicate IDs", () => {
  expect(
    parseAttachments([
      row(),
      { ...row(), asset_id: "123e4567-e89b-12d3-a456-426614174001", name: "b.png" },
    ]),
  ).toHaveLength(2);
  expect(parseAttachments([row(), row()])).toBeNull();
});

test("parseAttachments rejects invalid dimensions, source and total bytes", () => {
  expect(parseAttachments([row({ width: 10_000, height: 10_000 })])).toBeNull();
  expect(parseAttachments([row({ source: "remote" })])).toBeNull();
  expect(parseAttachments([row({ byte_size: MAX_TURN_BYTES + 1 })])).toBeNull();
});

test("parseAttachments enforces a 512 UTF-8 byte name limit, not a code-unit length", () => {
  expect(parseAttachments([row({ name: "あ".repeat(170) })])).toHaveLength(1);
  expect(parseAttachments([row({ name: "あ".repeat(171) })])).toBeNull();
});

describe("validateAttachmentFiles", () => {
  const file = (size = 4) => new File([new Uint8Array(size)], "a.png", { type: "image/png" });
  test("enforces count, per-file and aggregate limits", () => {
    expect(validateAttachmentFiles(Array.from({ length: MAX_ATTACHMENTS + 1 }, () => file()))).toBe(
      "attachment_count",
    );
    expect(validateAttachmentFiles([file(20 * 1024 * 1024 + 1)])).toBe("file_too_large");
    expect(validateAttachmentFiles([file(10)], 50 * 1024 * 1024 - 5)).toBe("turn_too_large");
    expect(validateAttachmentFiles([file()])).toBeNull();
  });
});

describe("sortFiles", () => {
  const image = (name: string, size = 4) =>
    new File([new Uint8Array(size)], name, { type: "image/png" });
  const text = (name: string, size = 4) => new File([new Uint8Array(size)], name, { type: "" });

  test("keeps images and refuses a non-text, non-image file by name", () => {
    const pdf = new File([new Uint8Array(4)], "doc.pdf", { type: "application/pdf" });
    const result = sortFiles([image("a.png"), pdf, image("b.png")]);
    expect(result.images.map((f) => f.name)).toEqual(["a.png", "b.png"]);
    expect(result.documents).toHaveLength(0);
    expect(result.refused).toEqual([{ name: "doc.pdf", reason: "unsupported_type" }]);
  });

  test("routes a .md/.txt/.markdown file to documents by extension, case-insensitive, whatever its MIME type", () => {
    const result = sortFiles([
      text("notes.MD"),
      text("readme.TXT"),
      text("long.markdown"),
      new File([new Uint8Array(4)], "plain.md", { type: "application/octet-stream" }),
    ]);
    expect(result.documents.map((f) => f.name)).toEqual([
      "notes.MD",
      "readme.TXT",
      "long.markdown",
      "plain.md",
    ]);
    expect(result.refused).toHaveLength(0);
  });

  test("refuses an over-100-KiB text-insert file as unsupported_type rather than a document", () => {
    const result = sortFiles([text("big.md", MAX_TEXT_INSERT_BYTES + 1)]);
    expect(result.documents).toHaveLength(0);
    expect(result.refused).toEqual([{ name: "big.md", reason: "unsupported_type" }]);
  });

  test("checks per-file size before the running count: an oversize file at the count cap is refused for its size, not the count", () => {
    const result = sortFiles([image("huge.png", MAX_FILE_BYTES + 1)], 0, MAX_ATTACHMENTS);
    expect(result.refused).toEqual([{ name: "huge.png", reason: "file_too_large" }]);
  });

  test("a count overflow refuses only the extra image, by name, in order", () => {
    const result = sortFiles(
      [image("a.png"), image("b.png"), image("c.png")],
      0,
      MAX_ATTACHMENTS - 2,
    );
    expect(result.images.map((f) => f.name)).toEqual(["a.png", "b.png"]);
    expect(result.refused).toEqual([{ name: "c.png", reason: "attachment_count" }]);
  });

  test("a turn-bytes overflow refuses only the image that pushes past the cap", () => {
    const result = sortFiles([image("a.png", 10), image("b.png", 10)], MAX_TURN_BYTES - 15, 0);
    expect(result.images.map((f) => f.name)).toEqual(["a.png"]);
    expect(result.refused).toEqual([{ name: "b.png", reason: "turn_too_large" }]);
  });

  test("applies caller limits (a capability's 5 MiB per image) to the size check", () => {
    const result = sortFiles([image("a.png", 6 * 1024 * 1024)], 0, 0, {
      maxFileBytes: 5 * 1024 * 1024,
    });
    expect(result.refused).toEqual([{ name: "a.png", reason: "file_too_large" }]);
  });
});
