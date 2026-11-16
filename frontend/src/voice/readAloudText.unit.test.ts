import { describe, expect, it } from "vitest";
import { parseMarkdown } from "../components/Conversation/markdownTree";
import {
  detectLanguage,
  MAX_CHUNK_CHARS,
  READ_ALOUD_LANGUAGES,
  type ReadAloudLanguage,
  speakable,
  speechChunks,
} from "./readAloudText";

function read(...blocks: string[]) {
  const result = speakable(blocks.map((block) => parseMarkdown(block)));
  if (!result) throw new Error("expected a reading, got null");
  return result;
}

describe("speakable", () => {
  it("skips a fenced block", () => {
    const { text } = read("Before.\n\n```js\nconst secret = 1;\n```\n\nAfter.");
    expect(text).toBe("Before.\nAfter.");
  });

  it("reads inline code in text but not in prose", () => {
    const { text, prose } = read("Call `fooBar()` now.");
    expect(text).toBe("Call fooBar() now.");
    expect(prose).toBe("Call now.");
  });

  it("reads link text and skips a bare or email autolink", () => {
    expect(read("See [the guide](https://example.com/guide) now.").text).toBe("See the guide now.");
    expect(read("Go to https://example.com/a now.").text).toBe("Go to now.");
    expect(read("Mail <someone@example.com> now.").text).toBe("Mail now.");
    expect(read("Mail someone@example.com now.").text).toBe("Mail now.");
  });

  it("skips an image", () => {
    expect(read("Look ![a chart of sales](https://example.com/c.png) here.").text).toBe(
      "Look here.",
    );
  });

  it("reads a table cell by cell", () => {
    const { text } = read("| Name | Role |\n| --- | --- |\n| Ann | Dev |\n| Bo | Ops |");
    expect(text).toBe("Name, Role\nAnn, Dev\nBo, Ops");
  });

  it("skips a footnote and its reference", () => {
    const { text } = read("Fact[^1] stands.\n\n[^1]: The hidden note.");
    expect(text).toBe("Fact stands.");
  });

  it("removes a URL but keeps Japanese right after it, and a 40-character token", () => {
    const token = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0";
    expect(token).toHaveLength(40);
    const { text } = read(
      `詳細は https://example.com/path?q=1です。 key ${token} end www.example.org.`,
    );
    expect(text).toBe("詳細は です。 key end");
  });

  it("is null when any block rendered plain", () => {
    const plain = { kind: "plain", reason: "depth" } as const;
    expect(speakable([parseMarkdown("fine"), plain])).toBeNull();
  });

  it("is empty for an all-code reply", () => {
    expect(read("```\ncode only\n```")).toEqual({ text: "", prose: "" });
  });

  it("joins several blocks in order", () => {
    expect(read("One.", "Two.").text).toBe("One.\nTwo.");
  });

  it("reads a soft line break as a space, one chunk, and a br as a line break", () => {
    const { text } = read("First part of\nthe second part of\nthe third part.");
    expect(text).toBe("First part of the second part of the third part.");
    expect(speechChunks(text, "en")).toHaveLength(1);
    expect(read("one  \ntwo").text).toBe("one\ntwo");
  });

  it("breaks lines around lists and headings", () => {
    expect(read("# Title\n\n- a\n- b\n\nEnd").text).toBe("Title\na\nb\nEnd");
  });
});

const SAMPLES: Record<ReadAloudLanguage, string> = {
  en: "The quick brown fox jumps over the lazy dog and the cat is not here.",
  zh: "今天天气很好，我们去公园散步吧。",
  hi: "यह एक हिंदी वाक्य है जो भाषा की पहचान के लिए है।",
  es: "El perro es del vecino y lo quiere como una familia.",
  ar: "هذا نص عربي بسيط للاختبار",
  fr: "Le chat est dans la maison et vous avez une clé pour les portes qui sont sur le mur.",
  bn: "এটি একটি বাংলা বাক্য যা ভাষা চেনার জন্য",
  pt: "Você não está com o livro, mas eles também são amigos.",
  id: "Saya tidak bisa datang karena ada urusan yang penting dan anda juga tahu ini.",
  ur: "یہ ایک اردو جملہ ہے جو ٹھیک ہے",
  ru: "Это простое русское предложение для проверки.",
  de: "Der Hund ist nicht in dem Haus und die Katze ist auch mit mir auf dem Weg.",
  ja: "これは日本語の文章です。",
  vi: "Tiếng Việt có nhiều dấu thanh và chữ đặc biệt như ơ, ư và đ.",
  te: "ఇది ఒక తెలుగు వాక్యం",
  tr: "Çocuklar bahçede oynuyor ve ışık çok güzel şimdi.",
  ta: "இது ஒரு தமிழ் வாக்கியம்",
  fa: "این یک جمله فارسی است که پیام می‌دهد",
  ko: "이것은 한국어 문장입니다.",
  am: "ይህ የአማርኛ ዓረፍተ ነገር ነው",
  th: "นี่คือประโยคภาษาไทย",
  it: "Il gatto non è nella casa e sono con un amico per questo che anche il cane.",
  gu: "આ એક ગુજરાતી વાક્ય છે",
  kn: "ಇದು ಒಂದು ಕನ್ನಡ ವಾಕ್ಯ",
};

describe("detectLanguage", () => {
  it("lists the 24 languages", () => {
    expect(READ_ALOUD_LANGUAGES).toHaveLength(24);
    expect(Object.keys(SAMPLES).sort()).toEqual([...READ_ALOUD_LANGUAGES].sort());
  });

  it.each(READ_ALOUD_LANGUAGES)("recognises %s from a sample", (code) => {
    expect(detectLanguage(SAMPLES[code])).toBe(code);
  });

  it("calls Japanese prose with English identifiers ja", () => {
    expect(detectLanguage("このfooBarとbazQuxの関数は設定を読み込みます。")).toBe("ja");
  });

  it("calls English with one Japanese word en", () => {
    expect(
      detectLanguage("The quick brown fox jumps over the lazy dog near the 日本 border today."),
    ).toBe("en");
  });

  it("calls other Latin text en", () => {
    expect(detectLanguage("Kowalski przyszedł wczoraj.")).toBe("en");
    expect(detectLanguage("Xyzzy plugh.")).toBe("en");
  });

  it("tells a short Spanish and a short Portuguese sentence apart", () => {
    expect(detectLanguage("El gato es del vecino.")).toBe("es");
    expect(detectLanguage("O gato não é do vizinho, você sabe.")).toBe("pt");
  });

  it("is null without a letter", () => {
    expect(detectLanguage("12345 67 8")).toBeNull();
    expect(detectLanguage("")).toBeNull();
  });
});

describe("speechChunks", () => {
  function nonSpace(parts: string[]): string {
    return parts.join("").replace(/\s/g, "");
  }

  it("makes a chunk of each short line", () => {
    expect(speechChunks("One.\n\n  Two.  \nThree.", "en")).toEqual(["One.", "Two.", "Three."]);
  });

  it("splits a 2,500-character line by sentence within the cap", () => {
    const line = Array.from({ length: 100 }, (_, i) => `Sentence number ${i} is here.`).join(" ");
    expect(line.length).toBeGreaterThan(2500);
    const chunks = speechChunks(line, "en");
    expect(chunks.length).toBeGreaterThan(2);
    for (const chunk of chunks) {
      expect(chunk.length).toBeLessThanOrEqual(MAX_CHUNK_CHARS);
      expect(chunk).toBe(chunk.trim());
      expect(chunk.endsWith(".")).toBe(true);
    }
    expect(nonSpace(chunks)).toBe(nonSpace([line]));
  });

  it("cuts a CJK line without spaces and a sentence over the cap, keeping every character", () => {
    const cjk = "あいうえお".repeat(700);
    const chunks = speechChunks(cjk, "ja", 100);
    expect(chunks.every((chunk) => chunk.length <= 100)).toBe(true);
    expect(nonSpace(chunks)).toBe(cjk);

    const clauses = "いろは、にほへと、ちりぬるを、".repeat(60);
    const cut = speechChunks(clauses, "ja", 50);
    expect(cut.every((chunk) => chunk.length <= 50)).toBe(true);
    expect(cut.every((chunk) => chunk.endsWith("、"))).toBe(true);
    expect(nonSpace(cut)).toBe(clauses);
  });

  it("cuts at the last space when a sentence is over the cap", () => {
    const words = "word ".repeat(60).trim();
    const chunks = speechChunks(words, "en", 50);
    expect(chunks.every((chunk) => chunk.length <= 50 && !chunk.startsWith(" "))).toBe(true);
    expect(chunks.every((chunk) => chunk.split(" ").every((word) => word === "word"))).toBe(true);
    expect(nonSpace(chunks)).toBe(nonSpace([words]));
  });

  it("never splits a surrogate pair", () => {
    const chunks = speechChunks("😀".repeat(30), "en", 11);
    expect(chunks.join("")).toBe("😀".repeat(30));
    const lone = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;
    for (const chunk of chunks) expect(lone.test(chunk)).toBe(false);
  });
});
