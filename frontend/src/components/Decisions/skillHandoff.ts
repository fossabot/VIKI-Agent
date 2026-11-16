/**
 * Hand-off text generators for skill proposals: apply (protected policy) and trial runs.
 * These builders generate the exact messages sent to the model when a skill proposal
 * is approved (protected policy) or trialed (dry run).
 */
import type { SkillProposalDetail } from "../../state/skillProposalsApi";
import { SKILL_TRIGGER_WORDS } from "../../state/skillProposalsApi";
import { fenceFor } from "./handoff";

/**
 * Generate hand-off text for applying an approved skill proposal through the /retro flow.
 * Branches on detail.policy: "protected" uses the protected policy text,
 * other policies use the create/replace text.
 */
export function applyHandoffText(detail: SkillProposalDetail): string {
  const r12 = detail.revision.slice(0, 12);
  const b12 = (detail.base_revision ?? "").slice(0, 12);
  const isNew = detail.base_revision === null;
  const body = detail.text;
  const bodyTrimmed = body.endsWith("\n") ? body.slice(0, -1) : body;
  const fence = fenceFor(body);
  const sessions = detail.sessions.join(", ");
  const triggerWords = SKILL_TRIGGER_WORDS[detail.trigger];
  const block = `${fence}SKILL.md\n${bodyTrimmed}\n${fence}`;

  if (detail.policy === "protected") {
    const targetLine = isNew
      ? `Target: .claude/skills/${detail.name}/SKILL.md, a new skill. Its sha256 should start with ${r12} afterwards.`
      : `Target: .claude/skills/${detail.name}/SKILL.md, replacing the version whose sha256 starts with ${b12}. Its sha256 should start with ${r12} afterwards.`;

    return [
      "Apply an approved skill proposal through your /retro flow. I approved it in VIKI Agent's Approvals.",
      targetLine,
      `Evidence: ${triggerWords}; chats ${sessions}.`,
      "Record it as approved in today's retro report, write the file with exactly the content below, and commit it on its own (viki(retro): …).",
      "",
      block,
    ].join("\n");
  } else {
    const createLine = isNew
      ? `Create .claude/skills/${detail.name}/SKILL.md in this vault with exactly the content below. Its sha256 should start with ${r12} afterwards. Change nothing else.`
      : `Create .claude/skills/${detail.name}/SKILL.md in this vault with exactly the content below, replacing the version whose sha256 starts with ${b12}. Its sha256 should start with ${r12} afterwards. Change nothing else.`;

    return [
      createLine,
      `I approved this skill proposal in VIKI Agent's Approvals. Evidence: ${triggerWords}; chats ${sessions}.`,
      "",
      block,
    ].join("\n");
  }
}

/**
 * Generate hand-off text for a trial run of a proposed skill.
 * Trial runs are dry runs that follow the skill's instructions without creating or changing files.
 */
export function trialHandoffText(detail: SkillProposalDetail): string {
  const body = detail.text;
  const bodyTrimmed = body.endsWith("\n") ? body.slice(0, -1) : body;
  const fence = fenceFor(body);
  const block = `${fence}SKILL.md\n${bodyTrimmed}\n${fence}`;

  return [
    `Dry run of a proposed skill that is not installed: ${detail.name}. Follow the SKILL.md below as if it were installed, on the task after it. Do not create or change any file.`,
    "",
    block,
    "",
    "Task: ",
  ].join("\n");
}
