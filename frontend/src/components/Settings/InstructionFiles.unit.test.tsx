import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { t, tf } from "../../i18n/t";
import { InstructionFiles } from "./InstructionFiles";

afterEach(cleanup);
beforeEach(() => {
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
  });
});

const files = [
  {
    name: "AGENTS.md",
    path: "C:/sample/Vault/AGENTS.md",
    text: "# Persona\r\n\r\nhello\r\n",
    pill: "Marked",
    note: "Persona section found.",
  },
  {
    name: "CLAUDE.md",
    path: "C:/sample/Vault/CLAUDE.md",
    text: "@AGENTS.md",
    pill: "Linked",
    note: "An AGENTS reference was found.",
  },
] as const;

test("shows both exact paths, translated accessible names, and open full text", () => {
  const { container } = render(<InstructionFiles files={files} />);

  expect(screen.getByText(t("settings.persona.files.title"))).toBeTruthy();
  for (const file of files) {
    expect(screen.getByText(file.path).closest("dd")?.classList.contains("mono")).toBe(true);
    const copy = screen.getByLabelText(
      tf("settings.persona.files.copy_path_label", { name: file.name }),
    );
    expect(copy.className).toBe("btn alt sm");
    expect(
      screen.getByLabelText(tf("settings.persona.files.full_text_label", { name: file.name }))
        .textContent,
    ).toBe(file.text);
  }
  expect(container.querySelector("details")).toBeNull();
});

test("an editor replaces that file's text, line count and nothing else", () => {
  const withEditor = [{ ...files[0], editor: <textarea aria-label="Edit AGENTS.md" /> }, files[1]];
  render(<InstructionFiles files={withEditor} />);

  expect(screen.getByLabelText("Edit AGENTS.md")).toBeTruthy();
  expect(
    screen.queryByLabelText(tf("settings.persona.files.full_text_label", { name: "AGENTS.md" })),
  ).toBeNull();
  expect(
    screen.getByLabelText(tf("settings.persona.files.full_text_label", { name: "CLAUDE.md" })),
  ).toBeTruthy();
  expect(screen.getByText("C:/sample/Vault/AGENTS.md")).toBeTruthy();
  expect(screen.getByText("Marked")).toBeTruthy();
});

test("copies the supplied path at click time", () => {
  const { rerender } = render(<InstructionFiles files={files} />);
  const updated = files.map((file) =>
    file.name === "AGENTS.md" ? { ...file, path: "C:/sample/Changed/AGENTS.md" } : file,
  );
  rerender(<InstructionFiles files={updated} />);

  fireEvent.click(
    screen.getByLabelText(tf("settings.persona.files.copy_path_label", { name: "AGENTS.md" })),
  );
  expect(navigator.clipboard.writeText).toHaveBeenCalledWith("C:/sample/Changed/AGENTS.md");
});

test.each([
  { text: "", count: 0 },
  { text: "one", count: 1 },
  { text: "one\ntwo\nthree", count: 3 },
  { text: "one\r\ntwo\r\n", count: 2 },
])("counts physical lines", ({ text, count }) => {
  render(<InstructionFiles files={[{ ...files[0], text }]} />);
  expect(
    screen.getByText(
      tf(count === 1 ? "settings.persona.files.line_one" : "settings.persona.files.line_other", {
        count,
      }),
    ),
  ).toBeTruthy();
});

test("full text is keyboard focusable and markup-shaped text stays text", () => {
  const source = "<img src=x>";
  const { container } = render(<InstructionFiles files={[{ ...files[0], text: source }]} />);
  const pre = screen.getByLabelText(
    tf("settings.persona.files.full_text_label", { name: "AGENTS.md" }),
  );

  expect(pre.getAttribute("tabindex")).toBe("0");
  pre.focus();
  expect(document.activeElement).toBe(pre);
  expect(pre.textContent).toBe(source);
  expect(container.querySelector("img")).toBeNull();
});

test("missing or unreadable files show quiet rows without text disclosure or alert", () => {
  const missing = { ...files[1], text: null, pill: "Missing", note: "No CLAUDE.md in this vault." };
  const unreadable = {
    ...files[0],
    text: null,
    pill: "Unreadable",
    note: "Couldn't read AGENTS.md (invalid_encoding).",
    tone: "error" as const,
  };
  const { container } = render(<InstructionFiles files={[missing, unreadable]} />);

  expect(screen.getByText(missing.note)).toBeTruthy();
  expect(screen.getByText(unreadable.note)).toBeTruthy();
  expect(container.querySelector("details")).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.queryByRole("status")).toBeNull();
});
