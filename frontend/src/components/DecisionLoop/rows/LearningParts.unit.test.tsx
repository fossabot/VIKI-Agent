import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";
import { decisionLoopExamples } from "../../../clients/decisionLoop/fake";
import type {
  LearningCalibration,
  LearningQuestionRate,
  LearningShadowStats,
  LearningView,
} from "../../../clients/decisionLoop/types";
import {
  AllNumbers,
  CalibrationPart,
  QuestionsPart,
  ShadowPart,
  WhatChanged,
} from "./LearningParts";

afterEach(cleanup);

function baseLearning(overrides: Partial<LearningView> = {}): LearningView {
  return { ...decisionLoopExamples().page.learning, ...overrides };
}

/** `baseLearning` with every now set back to its start, so nothing has moved. */
function flatLearning(overrides: Partial<LearningView> = {}): LearningView {
  const base = baseLearning();
  return {
    ...base,
    bias: { start: base.bias.start, now: base.bias.start },
    weights: base.weights.map((weight) => ({ ...weight, now: weight.start })),
    topics: base.topics.map((topic) => ({ ...topic, now: topic.start })),
    reach: base.reach.map((reach) => ({ ...reach, now: reach.start })),
    costs: base.costs.map((cost) => ({ ...cost, now: cost.start })),
    fitted_at: null,
    ...overrides,
  };
}

/** The cell's row, for asserting the rest of that row's cells with `within`. */
function rowFor(text: string): HTMLElement {
  const row = screen.getByText(text).closest("tr");
  if (row === null) throw new Error(`no table row for "${text}"`);
  return row as HTMLElement;
}

// fitted_at + 5 minutes, so WhatChanged's own `updatedAt` reads "5 minutes ago" deterministically.
const NOW_MS = Date.parse("2026-10-20T03:00:00.000Z") + 5 * 60_000;

// --- WhatChanged ---

describe("WhatChanged", () => {
  test("shows at most 5 movers of at least 0.05, largest first", () => {
    render(<WhatChanged learning={baseLearning()} nowMs={NOW_MS} />);
    const lines = screen.getAllByRole("listitem").map((item) => item.textContent);
    expect(lines).toEqual([
      "About Tasks due — higher (0.00 → 0.46)",
      "About Uncommitted changes — lower (0.00 → -0.31)",
      "About Chats VIKI started — higher (0.00 → 0.18)",
      "About Approvals — higher (0.00 → 0.12)",
      "Linked to a goal? — higher (0.60 → 0.71)",
    ]);
    // The 6th-largest mover (Interruption cost at Alert, |delta| 0.09) is past the cap of 5.
    expect(screen.queryByText("Interruption cost at Alert — lower (1.00 → 0.91)")).toBeNull();
  });

  test("explains that no large changes appear when a value moved less than the threshold", () => {
    render(
      <WhatChanged learning={flatLearning({ bias: { start: 0, now: 0.01 } })} nowMs={NOW_MS} />,
    );
    expect(screen.getByText("No large changes to VIKI's learned values yet.")).toBeTruthy();
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
  });

  test("includes a mover exactly 0.05 from its start", () => {
    render(
      <WhatChanged learning={flatLearning({ bias: { start: 0, now: 0.05 } })} nowMs={NOW_MS} />,
    );
    expect(screen.getByText("Baseline — higher (0.00 → 0.05)")).toBeTruthy();
  });

  test('shows "Updated <N> minutes ago" once VIKI has fitted, within the hour', () => {
    render(<WhatChanged learning={baseLearning()} nowMs={NOW_MS} />);
    expect(screen.getByText("Updated 5 minutes ago")).toBeTruthy();
  });

  test('shows "Updated <N> hours ago" within a day of fitting', () => {
    const threeHoursLater = Date.parse("2026-10-20T03:00:00.000Z") + 3 * 60 * 60_000;
    render(<WhatChanged learning={baseLearning()} nowMs={threeHoursLater} />);
    expect(screen.getByText("Updated 3 hours ago")).toBeTruthy();
  });

  test('shows the same calendar-date style as "Learning since …" once a day or more has passed', () => {
    const overADayLater = Date.parse("2026-10-20T03:00:00.000Z") + 25 * 60 * 60_000;
    render(<WhatChanged learning={baseLearning()} nowMs={overADayLater} />);
    expect(screen.getByText("Updated Oct 20, 2026")).toBeTruthy();
  });

  test("omits the Updated line while unfitted, even with movers to show", () => {
    render(<WhatChanged learning={baseLearning({ fitted_at: null })} nowMs={NOW_MS} />);
    expect(screen.getByText("About Tasks due — higher (0.00 → 0.46)")).toBeTruthy();
    expect(screen.queryByText(/^Updated /)).toBeNull();
  });
});

// --- AllNumbers ---

describe("AllNumbers", () => {
  test("lists every number's start, now and support, plus the Interruption bar", () => {
    render(<AllNumbers learning={baseLearning()} />);
    expect(screen.getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual([
      "Number",
      "Start",
      "Now",
      "Based on",
    ]);

    const bias = rowFor("Baseline");
    expect(within(bias).getByText("-0.5")).toBeTruthy();
    expect(within(bias).getByText("-0.42")).toBeTruthy();
    expect(within(bias).getByText("64")).toBeTruthy();

    const topic = rowFor("About Approvals");
    expect(within(topic).getByText("0.12")).toBeTruthy();
    expect(within(topic).getByText("7")).toBeTruthy();

    const reach = rowFor("Seen in time at Listed");
    expect(within(reach).getByText("0.3")).toBeTruthy();
    expect(within(reach).getByText("0.28")).toBeTruthy();
    expect(within(reach).getByText("30")).toBeTruthy();

    const cost = rowFor("Interruption cost at Alert");
    expect(within(cost).getByText("0.91")).toBeTruthy();
    expect(within(cost).getByText("4")).toBeTruthy();

    const bar = rowFor("Interruption bar");
    expect(within(bar).getByText("0.42 of 3")).toBeTruthy();
    expect(within(bar).getAllByText("–")).toHaveLength(2);
  });

  test("reads past the Japanese floor an earlier server sent", () => {
    const { unmount } = render(<AllNumbers learning={baseLearning()} />);
    const rows = screen.getAllByRole("row").map((row) => row.textContent);
    unmount();
    const older = { ...baseLearning(), ja_floor: { start: 0.1, now: 0.1, support: 10 } };
    render(<AllNumbers learning={older} />);
    expect(screen.getAllByRole("row").map((row) => row.textContent)).toEqual(rows);
    expect(screen.queryByText(/Japanese/)).toBeNull();
  });
});

// --- CalibrationPart ---

function baseCalibration(overrides: Partial<LearningCalibration> = {}): LearningCalibration {
  return { ...decisionLoopExamples().page.learning.calibration, ...overrides };
}

describe("CalibrationPart", () => {
  test("shows the error score and each non-empty bin", () => {
    render(<CalibrationPart calibration={baseCalibration()} />);
    expect(
      screen.getByText(
        "Prediction error 0.18 across 64 decisions with feedback. Lower is better; 0.25 is a coin-flip prediction.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Predicted usefulness 0.2–0.4: 31% counted useful (13 decisions with feedback)",
      ),
    ).toBeTruthy();
  });

  test("shows No feedback results yet. before the first label, with no bins", () => {
    const empty = baseCalibration({
      n: 0,
      brier: null,
      bins: baseCalibration().bins.map((bin) => ({
        ...bin,
        n: 0,
        mean_p: null,
        rate: null,
      })),
    });
    render(<CalibrationPart calibration={empty} />);
    expect(screen.getByText("No feedback results yet.")).toBeTruthy();
    expect(screen.queryByText(/^Prediction error/)).toBeNull();
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
  });

  test("dashes an error score that has no value", () => {
    render(<CalibrationPart calibration={baseCalibration({ brier: null })} />);
    expect(
      screen.getByText(
        "Prediction error – across 64 decisions with feedback. Lower is better; 0.25 is a coin-flip prediction.",
      ),
    ).toBeTruthy();
  });

  test("reads past the per-language scores an earlier server sent", () => {
    const older = { ...baseCalibration(), en: { n: 50, brier: 0.17 }, ja: { n: 14, brier: 0.21 } };
    render(<CalibrationPart calibration={older} />);
    expect(
      screen.getByText(
        "Prediction error 0.18 across 64 decisions with feedback. Lower is better; 0.25 is a coin-flip prediction.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/English|Japanese/)).toBeNull();
  });
});

// --- QuestionsPart ---

describe("QuestionsPart", () => {
  test("shows each question's yes and no useful rates", () => {
    const questions = decisionLoopExamples().page.learning.questions;
    render(<QuestionsPart questions={questions} />);
    expect(
      screen.getByText(
        "Linked to a goal? When the model answered Yes: 68% counted useful (41 decisions with feedback) · When the model answered No: 21% counted useful (19 decisions with feedback)",
      ),
    ).toBeTruthy();
    expect(screen.getAllByRole("listitem")).toHaveLength(questions.length);
  });

  test("dashes a side with no rate yet", () => {
    const question: LearningQuestionRate = {
      name: "test_q",
      label: "Test question?",
      yes_n: 0,
      yes_rate: null,
      no_n: 5,
      no_rate: 0.4,
    };
    render(<QuestionsPart questions={[question]} />);
    expect(
      screen.getByText(
        "Test question? When the model answered Yes: – counted useful (0 decisions with feedback) · When the model answered No: 40% counted useful (5 decisions with feedback)",
      ),
    ).toBeTruthy();
  });
});

// --- ShadowPart ---

function baseShadow(overrides: Partial<LearningShadowStats> = {}): LearningShadowStats {
  return { ...decisionLoopExamples().page.learning.shadow, ...overrides };
}

describe("ShadowPart", () => {
  test("renders nothing while shadowing is off", () => {
    const { container } = render(<ShadowPart shadow={baseShadow({ setting: "off" })} />);
    expect(container.firstChild).toBeNull();
  });

  test("shows the waiting sentence before the first shadow decision", () => {
    render(
      <ShadowPart
        shadow={baseShadow({
          setting: "latest",
          decisions: 0,
          agree: 0,
          kind_agree: 0,
          labels: 0,
          brier: null,
          live_brier: null,
        })}
      />,
    );
    expect(screen.getByText("No comparison decisions yet.")).toBeTruthy();
  });

  test("shows agreement and, once some decisions are labelled, the error comparison", () => {
    render(<ShadowPart shadow={baseShadow()} />);
    expect(
      screen.getByText(
        "Same attention level as the version in use on 41 of 50 decisions; same item type on 45.",
      ),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Across 12 decisions with feedback: prediction error 0.17 for the comparison version, 0.19 for the version in use.",
      ),
    ).toBeTruthy();
  });

  test("omits the error comparison while none of the shadow decisions are labelled", () => {
    render(<ShadowPart shadow={baseShadow({ labels: 0, brier: null, live_brier: null })} />);
    expect(
      screen.getByText(
        "Same attention level as the version in use on 41 of 50 decisions; same item type on 45.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/^Across \d+ decisions with feedback/)).toBeNull();
  });
});
