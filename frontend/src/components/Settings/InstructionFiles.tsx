import type { ReactElement, ReactNode } from "react";
import { t, tf } from "../../i18n/t";
import { CopyButton } from "../Conversation/Copy";
import "./InstructionFiles.css";
import { visibleText, withoutBom } from "./instructionText";

export interface InstructionFile {
  name: "AGENTS.md" | "CLAUDE.md";
  path: string;
  text: string | null;
  pill: string;
  note: string;
  tone?: "ready" | "error";
  /** Shown in place of the read-only text. */
  editor?: ReactNode;
}

export interface InstructionFilesProps {
  files: readonly InstructionFile[];
}

function physicalLineCount(text: string): number {
  if (text === "") return 0;
  const lines = text.split(/\r\n|\r|\n/);
  return lines.length - (/(?:\r\n|\r|\n)$/.test(text) ? 1 : 0);
}

/**
 * Instruction text for reading. A character that draws nothing is written as its code, with a
 * note, so no line reads differently here from what Claude Code receives.
 */
export function InstructionText({
  source,
  label,
}: {
  source: string;
  label: string;
}): ReactElement {
  // A BOM only marks the encoding: it says nothing inside the text, so it is not flagged.
  const shown = visibleText(withoutBom(source));
  return (
    <>
      {shown.hidden ? <p className="settings-error">{t("settings.persona.files.hidden")}</p> : null}
      {/* biome-ignore lint/a11y/useSemanticElements: preserve preformatted text semantics */}
      <pre
        className="instruction-files-text"
        role="group"
        // The read-only text needs a tab stop for keyboard navigation and selection.
        // biome-ignore lint/a11y/noNoninteractiveTabindex: see above
        tabIndex={0}
        aria-label={label}
      >
        {shown.text}
      </pre>
    </>
  );
}

export function InstructionFiles({ files }: InstructionFilesProps): ReactElement {
  return (
    <section className="settings-card instruction-files" aria-labelledby="instruction-files-title">
      <div className="settings-card-head">
        <h3 id="instruction-files-title" className="settings-card-title">
          {t("settings.persona.files.title")}
        </h3>
      </div>
      {files.map((file) => (
        <div className="settings-row instruction-files-row" key={file.name}>
          <div className="settings-row-label">
            <span>{file.name}</span>
            <p className="settings-row-desc">{file.note}</p>
          </div>
          <div className="settings-row-control">
            <div className="settings-row-status">
              <span className="settings-pill" data-tone={file.tone ?? "ready"}>
                {file.pill}
              </span>
            </div>
            <dl className="settings-facts instruction-files-facts">
              <div>
                <dt>{t("settings.persona.files.path")}</dt>
                <dd className="mono instruction-files-path">{file.path}</dd>
              </div>
            </dl>
            <CopyButton
              getText={() => file.path}
              label={tf("settings.persona.files.copy_path_label", { name: file.name })}
              className="btn alt sm"
            />
            {file.editor !== undefined ? (
              file.editor
            ) : file.text !== null ? (
              <div className="instruction-files-body">
                <p className="settings-note">
                  {tf(
                    physicalLineCount(file.text) === 1
                      ? "settings.persona.files.line_one"
                      : "settings.persona.files.line_other",
                    { count: physicalLineCount(file.text) },
                  )}
                </p>
                <InstructionText
                  source={file.text}
                  label={tf("settings.persona.files.full_text_label", { name: file.name })}
                />
              </div>
            ) : null}
          </div>
        </div>
      ))}
    </section>
  );
}
