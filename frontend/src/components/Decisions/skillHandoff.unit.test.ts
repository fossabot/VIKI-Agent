/**
 * Hand-off text generators for skill proposals: apply (protected policy) and trial runs.
 */
import { describe, expect, it } from "vitest";
import type { SkillProposalDetail } from "../../state/skillProposalsApi";

import { applyHandoffText, trialHandoffText } from "./skillHandoff";

describe("skillHandoff", () => {
  const baseDetail: Omit<SkillProposalDetail, "revision" | "base_revision" | "text" | "diff"> = {
    kind: "skill_proposal",
    id: "550e8400-e29b-41d4-a716-446655440000",
    origin: { workspace_id: "ws1", session_id: null },
    relative_id: "skills/test-skill/SKILL.md",
    attention: false,
    title: "Test Skill",
    state: "open",
    name: "test-skill",
    trigger: "button",
    sessions: ["550e8400-e29b-41d4-a716-446655440001"],
    text_bytes: 100,
    approved_at: null,
    rejected_at: null,
    reason: null,
    created_at: "2026-10-09T12:00:00.000Z",
    updated_at: "2026-10-09T12:00:00.000Z",
    description: "A test skill",
    unsupported_fields: [],
    shadowed: null,
    policy: "protected",
    current_text: null,
    diff_reason: null,
  };

  describe("applyHandoffText", () => {
    it("generates protected policy text for a new skill", () => {
      const detail = {
        ...baseDetail,
        policy: "protected" as const,
        revision: "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        base_revision: null,
        text: "---\nname: test-skill\ndescription: A test skill\n---\n\nBody text.",
        current_text: null,
        diff: "",
      } as SkillProposalDetail;

      const text = applyHandoffText(detail);

      const expected =
        "Apply an approved skill proposal through your /retro flow. I approved it in VIKI Agent's Approvals.\n" +
        "Target: .claude/skills/test-skill/SKILL.md, a new skill. Its sha256 should start with abcdef123456 afterwards.\n" +
        "Evidence: You asked for it; chats 550e8400-e29b-41d4-a716-446655440001.\n" +
        "Record it as approved in today's retro report, write the file with exactly the content below, and commit it on its own (viki(retro): …).\n" +
        "\n" +
        "```SKILL.md\n" +
        "---\nname: test-skill\ndescription: A test skill\n---\n\nBody text.\n" +
        "```";

      expect(text).toBe(expected);
    });

    it("generates protected policy text for updating a skill", () => {
      const detail = {
        ...baseDetail,
        policy: "protected" as const,
        revision: "123456789abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        base_revision: "0000000000000000000000000000000000000000000000000000000000000000",
        text: "---\nname: test-skill\n---\nUpdated text.",
        current_text: "---\nname: test-skill\n---\nOld text.",
        diff: "--- a/SKILL.md\n+++ b/SKILL.md\n-Old text.\n+Updated text.",
      } as SkillProposalDetail;

      const text = applyHandoffText(detail);

      const expected =
        "Apply an approved skill proposal through your /retro flow. I approved it in VIKI Agent's Approvals.\n" +
        "Target: .claude/skills/test-skill/SKILL.md, replacing the version whose sha256 starts with 000000000000. Its sha256 should start with 123456789abc afterwards.\n" +
        "Evidence: You asked for it; chats 550e8400-e29b-41d4-a716-446655440001.\n" +
        "Record it as approved in today's retro report, write the file with exactly the content below, and commit it on its own (viki(retro): …).\n" +
        "\n" +
        "```SKILL.md\n" +
        "---\nname: test-skill\n---\nUpdated text.\n" +
        "```";

      expect(text).toBe(expected);
    });

    it("uses other-policy text for new skill with ordinary policy", () => {
      const detail = {
        ...baseDetail,
        policy: "ordinary" as const,
        revision: "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        base_revision: null,
        text: "---\nname: test-skill\n---\nSkill content.",
        current_text: null,
        diff: "",
      } as SkillProposalDetail;

      const text = applyHandoffText(detail);

      const expected =
        "Create .claude/skills/test-skill/SKILL.md in this vault with exactly the content below. Its sha256 should start with abcdef123456 afterwards. Change nothing else.\n" +
        "I approved this skill proposal in VIKI Agent's Approvals. Evidence: You asked for it; chats 550e8400-e29b-41d4-a716-446655440001.\n" +
        "\n" +
        "```SKILL.md\n" +
        "---\nname: test-skill\n---\nSkill content.\n" +
        "```";

      expect(text).toBe(expected);
    });

    it("uses other-policy text for changed skill with ordinary policy", () => {
      const detail = {
        ...baseDetail,
        policy: "ordinary" as const,
        revision: "123456789abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        base_revision: "0000000000000000000000000000000000000000000000000000000000000000",
        text: "---\nname: test-skill\n---\nUpdated content.",
        current_text: "---\nname: test-skill\n---\nOld content.",
        diff: "--- a/SKILL.md\n+++ b/SKILL.md\n-Old content.\n+Updated content.",
      } as SkillProposalDetail;

      const text = applyHandoffText(detail);

      const expected =
        "Create .claude/skills/test-skill/SKILL.md in this vault with exactly the content below, replacing the version whose sha256 starts with 000000000000. Its sha256 should start with 123456789abc afterwards. Change nothing else.\n" +
        "I approved this skill proposal in VIKI Agent's Approvals. Evidence: You asked for it; chats 550e8400-e29b-41d4-a716-446655440001.\n" +
        "\n" +
        "```SKILL.md\n" +
        "---\nname: test-skill\n---\nUpdated content.\n" +
        "```";

      expect(text).toBe(expected);
    });

    it("pins fences longer than the text with protected policy", () => {
      const detail = {
        ...baseDetail,
        policy: "protected" as const,
        revision: "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        base_revision: null,
        text: "---\nname: test\n---\n\nCode with ``` backticks.",
        current_text: null,
        diff: "",
      } as SkillProposalDetail;

      const text = applyHandoffText(detail);

      // Should have a fence longer than 3 backticks
      expect(text).toMatch(/````SKILL\.md/);
      expect(text).toMatch(/````$/m);
    });
  });

  describe("trialHandoffText", () => {
    it("generates trial run text", () => {
      const detail = {
        ...baseDetail,
        revision: "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        base_revision: null,
        text: "---\nname: test-skill\n---\nTrial skill text.",
        current_text: null,
        diff: "",
      } as SkillProposalDetail;

      const text = trialHandoffText(detail);

      const expected =
        "Dry run of a proposed skill that is not installed: test-skill. Follow the SKILL.md below as if it were installed, on the task after it. Do not create or change any file.\n" +
        "\n" +
        "```SKILL.md\n" +
        "---\nname: test-skill\n---\nTrial skill text.\n" +
        "```\n" +
        "\n" +
        "Task: ";

      expect(text).toBe(expected);
    });

    it("pins fences longer than the text in trial run", () => {
      const detail = {
        ...baseDetail,
        revision: "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        base_revision: null,
        text: "```code block```",
        current_text: null,
        diff: "",
      } as SkillProposalDetail;

      const text = trialHandoffText(detail);

      const expected =
        "Dry run of a proposed skill that is not installed: test-skill. Follow the SKILL.md below as if it were installed, on the task after it. Do not create or change any file.\n" +
        "\n" +
        "````SKILL.md\n" +
        "```code block```\n" +
        "````\n" +
        "\n" +
        "Task: ";

      expect(text).toBe(expected);
    });
  });
});
