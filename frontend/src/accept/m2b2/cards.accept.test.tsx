/**
 * Acceptance (frozen): the three cards — the permission card, the plan card and the question
 * card. A card is the only place the app takes an answer that lets claude act on the vault,
 * so what it says and how it answers are pinned here.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { PermissionCard, QuestionCard } from "../../components/Cards/Cards";
import type { PermissionRequestedData, QuestionAskedData } from "../../protocol";

afterEach(cleanup);

/* AMENDMENT (2026-09-13). `question` is new on permission.requested. When this file was
   frozen, nothing on the wire carried a per-tool sentence, so the card had to render one
   generic line for every tool. The contract was the thing that was wrong: the mapper already
   owns per-tool wording for the row grammar, so it now words the question too. The fixture
   gains the field and one assertion below pins that the card shows it. No existing assertion
   changed. */
const permission = (over: Partial<PermissionRequestedData> = {}): PermissionRequestedData => ({
  tool_use_id: "toolu_1",
  kind: "tool",
  tool: "WebFetch",
  summary: "linter-guide.docs.example.com",
  question: "May I read this web page?",
  reason: "",
  agent: "",
  plan: "",
  ...over,
});

function showPermission(data: PermissionRequestedData) {
  const onRespond = vi.fn();
  const { container } = render(
    <PermissionCard data={data} onRespond={onRespond} onOpenUrl={vi.fn()} />,
  );
  return { container, onRespond };
}

test("a tool request asks in plain words, shows the tool and offers this once only", () => {
  const { container, onRespond } = showPermission(permission());
  expect(container.querySelector(".chip")?.textContent).toBe("Waiting for approval");
  // The per-tool sentence, worded by the mapper from the tool — not a generic line.
  expect(container.querySelector(".card-question")?.textContent).toBe("May I read this web page?");
  expect(container.querySelector(".card-tool")?.textContent).toContain("WebFetch");
  expect(container.querySelector(".card-tool")?.textContent).toContain(
    "linter-guide.docs.example.com",
  );
  expect(container.textContent).toContain("Permission applies to this request only");
  fireEvent.click(screen.getByText("Allow once"));
  expect(onRespond).toHaveBeenCalledWith("toolu_1", true);
});

test("Deny answers no, and a card answers only once", () => {
  const { onRespond } = showPermission(permission());
  fireEvent.click(screen.getByText("Deny"));
  fireEvent.click(screen.getByText("Deny"));
  fireEvent.click(screen.getByText("Allow once"));
  expect(onRespond).toHaveBeenCalledTimes(1);
  expect(onRespond).toHaveBeenCalledWith("toolu_1", false);
});

test("a subagent's request says whose it is", () => {
  const { container } = showPermission(permission({ agent: "researcher" }));
  expect(container.querySelector(".card-agent")?.textContent).toContain("From researcher");
  expect(container.querySelector(".card-agent .face")?.getAttribute("data-shape")).toBe("circle");
});

test("a reason from the CLI is shown as text, whatever it contains", () => {
  const { container } = showPermission(
    permission({ reason: 'auto mode blocked <img src=x onerror="window.__pwned=1">' }),
  );
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector(".card-reason")?.textContent).toContain(
    '<img src=x onerror="window.__pwned=1">',
  );
});

test("the plan card shows the whole plan and restores the mode on approval", () => {
  const onRespond = vi.fn();
  const { container } = render(
    <PermissionCard
      data={permission({
        kind: "plan",
        tool: "ExitPlanMode",
        plan: "### Add a /recall skill\n\n- [Steps](https://example.com/x)\n",
      })}
      onRespond={onRespond}
      onOpenUrl={vi.fn()}
    />,
  );
  expect(container.querySelector(".chip")?.textContent).toBe("Plan");
  expect(container.textContent).toContain("Proceed with this plan?");
  expect(container.querySelector(".card-plan h3")?.textContent).toBe("Add a /recall skill");
  expect(container.querySelector(".card-plan a")?.getAttribute("href")).toBe(
    "https://example.com/x",
  );
  expect(container.textContent).toContain("Approving exits plan mode and continues.");
  fireEvent.click(screen.getByText("Approve"));
  expect(onRespond).toHaveBeenCalledWith("toolu_1", true);
});

test("Keep planning declines without ending plan mode", () => {
  const onRespond = vi.fn();
  render(
    <PermissionCard
      data={permission({ kind: "plan", tool: "ExitPlanMode", plan: "a plan" })}
      onRespond={onRespond}
      onOpenUrl={vi.fn()}
    />,
  );
  fireEvent.click(screen.getByText("Keep planning"));
  expect(onRespond).toHaveBeenCalledWith("toolu_1", false);
});

const asked = (over: Partial<QuestionAskedData["questions"][0]> = {}): QuestionAskedData => ({
  tool_use_id: "toolu_q",
  questions: [
    {
      header: "Destination",
      question: "Where should this go?",
      options: [
        { label: "30-knowledge", description: "evergreen knowledge notes" },
        { label: "00-inbox", description: "" },
      ],
      multi_select: false,
      ...over,
    },
  ],
});

/* AMENDMENT (2026-09-13). The two answer assertions below keyed the answers map by the
   question's `header` ("Destination"). That is what the
   card did, and it was wrong about the world: the CLI echoes whatever key it is handed straight
   into the tool result the model reads, so a header key hands the model back a chip label instead
   of the question it asked, and two questions may share a header while their texts cannot collide.
   The one recorded real exchange (tests/fixtures/claude/ask-user-question.jsonl, the client frame
   and the CLI's own tool_result) keys by the question text, and state/viki.accept.test.ts already
   pinned the wire that way — the card was the only place that disagreed. Only the key moved;
   what is picked, when it is sent and how many times are unchanged. */
/* AMENDMENT (2026-10-30). This test pinned that a single-select pick answers at once. That
   left no way to change a pick, so now a pick only selects, any pick can change, and the
   card's Submit sends. Only when it is sent moved; what is sent, its key and how many times
   are unchanged. */
test("a question card offers 44px options and answers on Submit, after any re-pick", () => {
  const onAnswer = vi.fn();
  const { container } = render(<QuestionCard data={asked()} onAnswer={onAnswer} />);
  expect(container.querySelector(".chip")?.textContent).toBe("Destination");
  expect(container.textContent).toContain("Where should this go?");
  const options = container.querySelectorAll(".card-option");
  expect(options).toHaveLength(2);
  expect((options[0] as HTMLElement).textContent).toContain("evergreen knowledge notes");
  fireEvent.click(options[1] as HTMLElement);
  fireEvent.click(options[0] as HTMLElement);
  expect(onAnswer).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("Submit"));
  expect(onAnswer).toHaveBeenCalledWith("toolu_q", { "Where should this go?": "30-knowledge" }, "");
  expect(onAnswer).toHaveBeenCalledTimes(1);
});

test("a multi-select question waits for Submit", () => {
  const onAnswer = vi.fn();
  const { container } = render(
    <QuestionCard data={asked({ multi_select: true })} onAnswer={onAnswer} />,
  );
  const options = container.querySelectorAll(".card-option");
  fireEvent.click(options[0] as HTMLElement);
  fireEvent.click(options[1] as HTMLElement);
  expect(onAnswer).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("Submit"));
  expect(onAnswer).toHaveBeenCalledWith(
    "toolu_q",
    { "Where should this go?": "30-knowledge, 00-inbox" },
    "",
  );
});

test("Other sends the owner's own words", () => {
  const onAnswer = vi.fn();
  const { container } = render(<QuestionCard data={asked()} onAnswer={onAnswer} />);
  fireEvent.click(screen.getByText("Other"));
  const input = container.querySelector(".card-other input") as HTMLInputElement;
  fireEvent.change(input, { target: { value: "Put it in 70-people" } });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(onAnswer).toHaveBeenCalledWith("toolu_q", {}, "Put it in 70-people");
});

test("an option label is text, never markup", () => {
  const { container } = render(
    <QuestionCard
      data={asked({ options: [{ label: "<script>window.__pwned=1</script>", description: "" }] })}
      onAnswer={vi.fn()}
    />,
  );
  expect(container.querySelector("script")).toBeNull();
  expect(container.textContent).toContain("<script>window.__pwned=1</script>");
});
