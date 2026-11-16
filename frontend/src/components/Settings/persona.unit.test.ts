/**
 * `persona.ts`'s grammar: reading, rendering, validating, diffing and
 * handing off the persona block of a starter vault's `AGENTS.md`.
 */
// @ts-expect-error no @types/node in this browser-focused tsconfig; Node itself is present
// under Vitest, which runs tests through Node, not a bundled browser.
import { readFileSync } from "node:fs";
// @ts-expect-error see above
import { fileURLToPath } from "node:url";
import { describe, expect, test } from "vitest";
import {
  findPersonaHeading,
  ITEM_MAX_LENGTH,
  importsAgents,
  itemsFromText,
  MAX_ITEMS,
  NAME_MAX_LENGTH,
  PERSONA_CLOSE,
  PERSONA_OPEN,
  type Persona,
  PROACTIVITY,
  personaAddHandoff,
  personaConvertHandoff,
  personaDiff,
  personaHandoff,
  personaProblems,
  personaRepairHandoff,
  readPersona,
  renderPersona,
  TONES,
  withoutClosingHashes,
} from "./persona";

const TEMPLATE_PATH = "../../../../src/viki_agent/resources/starter/AGENTS.template.md";

/** The real starter template with its three persona tokens filled, the same shape
 * `starter_fill`/`fill` produce. Everything else in the file (the vault-id comment, the
 * sections after the persona block) is irrelevant to `readPersona`, which only looks at the text
 * between the two markers. */
function filledTemplate(): string {
  const raw: string = readFileSync(fileURLToPath(new URL(TEMPLATE_PATH, import.meta.url)), "utf-8");
  return raw
    .replace(/\{\{viki\.assistant_name\}\}/g, "VIKI")
    .replace(/\{\{viki\.address\}\}/g, "you")
    .replace(/\{\{viki\.language\}\}/g, "English");
}

/** The filled template's persona body, via `readPersona` itself: if that fixture were not "ok"
 * this throws immediately, which is clearer than a downstream assertion failing for the wrong
 * reason. */
function filledBody(): string {
  const result = readPersona(filledTemplate());
  if (result.state !== "ok") throw new Error(`template fixture is not ok: ${result.state}`);
  return result.body;
}

function validPersona(overrides: Partial<Persona> = {}): Persona {
  return {
    name: "VIKI",
    address: "you",
    tone: "calm",
    language: "English",
    proactivity: "coach",
    dos: ["Lead with the answer, then the reasoning."],
    donts: ["Pad replies with filler or hype."],
    ...overrides,
  };
}

test("the markers are exactly the template's HTML comments", () => {
  expect(PERSONA_OPEN).toBe("<!-- viki:persona -->");
  expect(PERSONA_CLOSE).toBe("<!-- /viki:persona -->");
});

test("TONES and PROACTIVITY list exactly the allowed values", () => {
  expect(TONES).toEqual(["calm", "precise", "playful"]);
  expect(PROACTIVITY).toEqual(["coach", "quiet"]);
});

describe("readPersona: ok", () => {
  test("the filled template's persona section parses, and renderPersona(parse(x)) === x", () => {
    const result = readPersona(filledTemplate());
    if (result.state !== "ok") throw new Error(`expected ok, got ${result.state}`);
    expect(result.persona).toEqual({
      name: "VIKI",
      address: "you",
      tone: "calm",
      language: "English",
      proactivity: "coach",
      dos: [
        "Lead with the answer, then the reasoning.",
        "Check the vault or the original source before answering from memory.",
      ],
      donts: ["Pad replies with filler or hype."],
    });
    expect(renderPersona(result.persona)).toBe(result.body);
  });

  test("CRLF line endings normalise to LF before parsing", () => {
    const text = filledTemplate().replace(/\n/g, "\r\n");
    const result = readPersona(text);
    if (result.state !== "ok") throw new Error(`expected ok, got ${result.state}`);
    expect(result.persona.name).toBe("VIKI");
    expect(result.body).not.toContain("\r");
  });
});

test("readPersona: no marker anywhere reads absent", () => {
  expect(readPersona("# Doc\n\nNo persona block here.\n")).toEqual({ state: "absent" });
});

describe("readPersona: broken markers read edited with no recoverable body", () => {
  test("a duplicated open marker", () => {
    const text = [PERSONA_OPEN, "## Persona", PERSONA_OPEN, "more", PERSONA_CLOSE].join("\n");
    expect(readPersona(text)).toEqual({ state: "edited", body: null });
  });

  test("a missing close marker", () => {
    const text = [PERSONA_OPEN, "## Persona", "", "stuff"].join("\n");
    expect(readPersona(text)).toEqual({ state: "edited", body: null });
  });

  test("a reordered marker pair (close before open)", () => {
    const text = [PERSONA_CLOSE, "middle", PERSONA_OPEN].join("\n");
    expect(readPersona(text)).toEqual({ state: "edited", body: null });
  });
});

test("readPersona: a hand-edited value inside otherwise-intact markers reads edited, body kept", () => {
  const body = filledBody().replace("- Tone: calm", "- Tone: grumpy");
  const text = `${PERSONA_OPEN}\n${body}\n${PERSONA_CLOSE}`;
  const result = readPersona(text);
  expect(result.state).toBe("edited");
  if (result.state !== "edited") throw new Error("unreachable");
  expect(result.body).toBe(body);
});

describe("readPersona: a starter identity starter_fill must never emit reads edited", () => {
  // field()'s own trim() quietly drops a trailing space from the parsed value, so without the
  // renderPersona(persona) === body round trip this would wrongly look "ok" (dropping
  // that check from readPersona must make this test fail).
  test("a trailing space on the Name line", () => {
    const body = filledBody().replace("- Name: VIKI", "- Name: VIKI ");
    const text = `${PERSONA_OPEN}\n${body}\n${PERSONA_CLOSE}`;
    expect(readPersona(text).state).toBe("edited");
  });

  // trim() strips a leading/trailing U+FEFF too (the same character starter_fill must now fold
  // into a space before its own whitespace collapse), so this is the same round-trip failure.
  test("a leading U+FEFF on the Name line", () => {
    const body = filledBody().replace("- Name: VIKI", `- Name: ${String.fromCharCode(0xfeff)}VIKI`);
    const text = `${PERSONA_OPEN}\n${body}\n${PERSONA_CLOSE}`;
    expect(readPersona(text).state).toBe("edited");
  });

  // 17 code points, but 34 UTF-16 units: over NAME_MAX_LENGTH (32), caught directly by
  // personaProblems's length bound (persona.ts's `.length` is already UTF-16 units).
  test("a 17-emoji Name value, 34 UTF-16 units over NAME_MAX_LENGTH", () => {
    const body = filledBody().replace("- Name: VIKI", `- Name: ${"\u{1F642}".repeat(17)}`);
    const text = `${PERSONA_OPEN}\n${body}\n${PERSONA_CLOSE}`;
    expect(readPersona(text).state).toBe("edited");
  });
});

test("renderPersona lays out the persona skeleton exactly", () => {
  expect(renderPersona(validPersona())).toBe(
    [
      "## Persona",
      "",
      "- Name: VIKI",
      "- Address the owner as: you",
      "- Tone: calm",
      "- Reply language: English",
      "- Proactivity: coach",
      "",
      "### Do",
      "- Lead with the answer, then the reasoning.",
      "",
      "### Don't",
      "- Pad replies with filler or hype.",
    ].join("\n"),
  );
});

describe("personaProblems: field bounds", () => {
  test("a persona within every bound has no problems", () => {
    expect(personaProblems(validPersona())).toEqual([]);
  });

  test("a name at NAME_MAX_LENGTH is fine; past it is refused", () => {
    expect(personaProblems(validPersona({ name: "x".repeat(NAME_MAX_LENGTH) }))).toEqual([]);
    expect(personaProblems(validPersona({ name: "x".repeat(NAME_MAX_LENGTH + 1) }))).not.toEqual(
      [],
    );
  });

  test("an empty name is refused", () => {
    expect(personaProblems(validPersona({ name: "" }))).not.toEqual([]);
  });

  test("a tone outside TONES is refused", () => {
    expect(personaProblems(validPersona({ tone: "grumpy" }))).not.toEqual([]);
  });

  test("a proactivity outside PROACTIVITY is refused", () => {
    expect(personaProblems(validPersona({ proactivity: "pushy" }))).not.toEqual([]);
  });

  test("exactly MAX_ITEMS Do items is fine; one more is refused", () => {
    const atLimit = Array.from({ length: MAX_ITEMS }, (_, index) => `item ${index}`);
    const overLimit = Array.from({ length: MAX_ITEMS + 1 }, (_, index) => `item ${index}`);
    expect(personaProblems(validPersona({ dos: atLimit }))).toEqual([]);
    expect(personaProblems(validPersona({ dos: overLimit }))).not.toEqual([]);
  });

  test("an item past ITEM_MAX_LENGTH is refused", () => {
    expect(personaProblems(validPersona({ donts: ["x".repeat(ITEM_MAX_LENGTH + 1)] }))).not.toEqual(
      [],
    );
  });

  test("a control character in an item is refused", () => {
    expect(personaProblems(validPersona({ dos: ["bad\u0007item"] }))).not.toEqual([]);
  });

  test("leading or trailing whitespace on a value is refused", () => {
    expect(personaProblems(validPersona({ address: " you " }))).not.toEqual([]);
  });

  test("an embedded newline on a value is refused", () => {
    expect(personaProblems(validPersona({ language: "English\nFrench" }))).not.toEqual([]);
  });
});

test("itemsFromText trims, drops blanks, and strips a leading dash", () => {
  expect(itemsFromText("- one\n  two  \n\n- three\n")).toEqual(["one", "two", "three"]);
});

describe("personaDiff", () => {
  test("marks unchanged, added and removed lines (LCS)", () => {
    expect(personaDiff("a\nb\nc", "a\nx\nc")).toEqual([
      { kind: "same", text: "a" },
      { kind: "del", text: "b" },
      { kind: "add", text: "x" },
      { kind: "same", text: "c" },
    ]);
  });

  test("an unchanged body diffs as all same", () => {
    const body = filledBody();
    expect(personaDiff(body, body).every((entry) => entry.kind === "same")).toBe(true);
  });
});

describe("personaHandoff", () => {
  test("names both markers and instructs keeping them, with the body fenced as markdown", () => {
    const handoff = personaHandoff("- Name: VIKI");
    expect(handoff).toContain("Please update the persona in this vault's AGENTS.md.");
    expect(handoff).toContain(
      "Replace the lines between `<!-- viki:persona -->` and `<!-- /viki:persona -->` with exactly the block below. Keep both marker lines and change nothing else in the file.",
    );
    expect(handoff).toContain("```markdown\n- Name: VIKI\n```");
  });

  test("a body containing a triple-backtick run gets a four-backtick fence", () => {
    const body = "Some code:\n```\nfenced\n```\nDone.";
    const handoff = personaHandoff(body);
    expect(handoff).toContain(`\`\`\`\`markdown\n${body}\n\`\`\`\``);
  });
});

test.each([
  "## Persona",
  "## Personas:",
  "## **Persona (VIKI)**",
  "## \u30da\u30eb\u30bd\u30ca\u8a2d\u5b9a",
])("finds Persona heading", (heading) => {
  const text =
    "before\r\n" + heading + "\r\nbody\r\n### nested\r\nstill nested\r\n## Next\r\nafter";
  expect(findPersonaHeading(text)).toEqual({
    heading,
    section: heading + "\r\nbody\r\n### nested\r\nstill nested\r\n",
  });
  expect(readPersona(text)).toEqual({ state: "absent" });
});

test.each([
  "## Personality",
  "## PersonasX",
  "## How to read the Persona section",
  "#Persona",
  "---\n## Persona\n---",
  "```md\n## Persona\n```",
  "~~~md\n## Persona\n~~~",
  "Persona\n=======",
  "<!-- ## Persona -->",
  "<!--\n## Persona\n-->\n## Other",
  "---\nmetadata\n## Persona",
])("rejects unsupported headings", (text) => expect(findPersonaHeading(text)).toBeNull());
describe("withoutClosingHashes", () => {
  test.each([
    ["Persona ##", "Persona"],
    ["Persona \t## \t", "Persona"],
    ["Persona #", "Persona"],
    ["a # #", "a #"],
    ["  ##", ""],
    ["Persona##", "Persona##"],
    ["##", "##"],
    ["Persona", "Persona"],
    ["Persona   ", "Persona   "],
    ["C# notes", "C# notes"],
    ["", ""],
  ])("%j reads as %j, as the closing sequence regex would", (text, expected) => {
    expect(withoutClosingHashes(text)).toBe(expected);
    expect(text.replace(/[ \t]+#+[ \t]*$/, "")).toBe(expected);
  });

  test("reads each character of a long run of blanks a bounded number of times", () => {
    let reads = 0;
    class Counted extends String {
      override charCodeAt(index: number): number {
        reads += 1;
        return super.charCodeAt(index);
      }
    }
    for (const text of [
      " ".repeat(20_000),
      `x${" ".repeat(20_000)}#x`,
      `x${" ".repeat(20_000)}#`,
    ]) {
      reads = 0;
      withoutClosingHashes(new Counted(text) as unknown as string);
      expect(reads).toBeGreaterThan(0);
      expect(reads).toBeLessThanOrEqual(2 * text.length + 2);
    }
  });

  test("a Persona heading with a closing sequence is still found", () => {
    expect(findPersonaHeading(`## Persona ##\nbody`)?.heading).toBe("## Persona ##");
  });
});

test("findPersonaHeading preserves BOM, CRLF, and first match", () => {
  expect(findPersonaHeading("\uFEFF## Persona\r\none\r\n## Persona\r\ntwo\r\n# End")).toEqual({
    heading: "\uFEFF## Persona",
    section: "\uFEFF## Persona\r\none\r\n",
  });
  expect(findPersonaHeading("## Persona\ntext\n### nested\ninside\n## End")).toEqual({
    heading: "## Persona",
    section: "## Persona\ntext\n### nested\ninside\n",
  });
  expect(findPersonaHeading("---\r\nmetadata\r\n...\r\n## Persona\r\nbody")).toEqual({
    heading: "## Persona",
    section: "## Persona\r\nbody",
  });
});
test("HTML-comment heading does not end a Persona section", () => {
  expect(findPersonaHeading("## Persona\nbody\n<!--\n## End\n-->\nstill body")).toEqual({
    heading: "## Persona",
    section: "## Persona\nbody\n<!--\n## End\n-->\nstill body",
  });
});
test("comment-prefixed ATX text is not a Persona candidate", () => {
  expect(findPersonaHeading("<!-- hidden -->## Persona\nbody")).toBeNull();
});
test("comment-prefixed ATX text does not terminate a Persona section", () => {
  const text = "## Persona\nbody\n<!-- hidden -->## End\nstill body";
  expect(findPersonaHeading(text)).toEqual({ heading: "## Persona", section: text });
});
test("starter explanatory heading stays hidden after marker deletion", () => {
  const text = filledTemplate().replace(
    /<!-- viki:persona -->[\s\S]*?<!-- \/viki:persona -->\r?\n?/,
    "",
  );
  expect(readPersona(text)).toEqual({ state: "absent" });
  expect(findPersonaHeading(text)).toBeNull();
});
test.each(["@AGENTS.md", " @AGENTS.md ", "@./AGENTS.md", "  @./AGENTS.md  "])(
  "imports standalone variants",
  (line) => expect(importsAgents("prose\n" + line + "\nafter")).toBe(true),
);
test.each([
  "See @AGENTS.md here",
  ["<!--", "@AGENTS.md", "-->"].join("\n"),
  "`@AGENTS.md`",
  "`code` @AGENTS.md",
  "@AGENTS.md `code`",
  "@AGENTS.md <!-- trailing -->",
  "<!-- leading --> @AGENTS.md",
  ["`open", "@AGENTS.md", "close`"].join("\n"),
  "``open\n@./AGENTS.md\nclose``",
  ["````md", "@AGENTS.md", "````"].join("\n"),
  "@AGENTS.md.bak",
])("rejects import lookalikes", (text) => expect(importsAgents(text)).toBe(false));
test("handoffs distinguish conversion, repair and add", () => {
  const source = "## Persona\nContains ```\n";
  const converted = personaConvertHandoff("## Persona", source);
  expect(converted).toContain(
    `Add exactly one pair of markers: ${PERSONA_OPEN} and ${PERSONA_CLOSE}`,
  );
  expect(converted).toContain("around the quoted existing section");
  expect(converted).toContain("````markdown\n" + source + "\n````");
  expect(converted).toContain("10");
  expect(converted).toContain("200");
  expect(converted).toContain("\n\n");
  const repaired = personaRepairHandoff("edited body");
  expect(repaired).toContain(
    `Repair the existing marker structure and body using ${PERSONA_OPEN} and ${PERSONA_CLOSE}`,
  );
  expect(repaired).toContain("exactly one correctly ordered pair");
  expect(repaired).toContain("Do not add a second pair");
  const unrecoverable = personaRepairHandoff(null);
  expect(unrecoverable).toContain(
    `Repair the existing marker structure and body using ${PERSONA_OPEN} and ${PERSONA_CLOSE}`,
  );
  expect(unrecoverable).toContain("exactly one correctly ordered pair");
  expect(unrecoverable).toContain("Do not add a second pair");
  expect(unrecoverable).toContain("Do not guess");
  expect(unrecoverable).not.toContain("Current recoverable body");
  const body = renderPersona(validPersona());
  const added = personaAddHandoff(body);
  expect(added).toContain(PERSONA_OPEN);
  expect(added).toContain(PERSONA_CLOSE);
  expect(added).toContain("Preserve all other text");
  expect(added).toContain("10");
  expect(added).toContain("200");
  expect(added).toContain("\n\n");
  expect(repaired).toContain("10");
  expect(repaired).toContain("200");
  expect(repaired).toContain("\n\n");
  const quotedBlock = /^(`{3,})markdown\n([\s\S]*?)\n\1$/.exec(
    added.split("New block:\n")[1] ?? "",
  );
  expect(quotedBlock).not.toBeNull();
  expect(readPersona(quotedBlock?.[2] ?? "")).toEqual({
    state: "ok",
    persona: validPersona(),
    body,
  });
  expect(personaRepairHandoff("```")).toContain("````markdown");
  expect(personaAddHandoff("```")).toContain("````markdown");
});
