/** The "How VIKI works" strip: four numbered steps, each with a button that jumps to the
 * matching row, then Learns and Your data lines. Open as a section while proactive VIKI is off;
 * a closed `details` while it is on. Props only; the page owns what a jump does. */

import { t } from "../../i18n/t";
import "./HowItWorks.css";

export type JumpRow = "watch" | "model" | "reach" | "do" | "how" | "privacy";

export interface HowItWorksProps {
  enabled: boolean;
  onJump: (row: JumpRow) => void;
}

export function HowItWorks({ enabled, onJump }: HowItWorksProps) {
  const jump = (row: JumpRow, label: string) => (
    <button type="button" className="btn txt sm" onClick={() => onJump(row)}>
      {label}
    </button>
  );
  const steps = [
    {
      row: "watch",
      title: t("decision_page.how.notices.title"),
      text: t("decision_page.how.notices.text"),
      model: null,
      jump: t("decision_page.how.notices.jump"),
    },
    {
      row: "model",
      title: t("decision_page.how.decides.title"),
      text: t("decision_page.how.decides.text"),
      model: t("decision_page.how.decides.model"),
      jump: t("decision_page.how.decides.jump"),
    },
    {
      row: "reach",
      title: t("decision_page.how.tells.title"),
      text: t("decision_page.how.tells.text"),
      model: null,
      jump: t("decision_page.how.tells.jump"),
    },
    {
      row: "do",
      title: t("decision_page.how.acts.title"),
      text: t("decision_page.how.acts.text"),
      model: null,
      jump: t("decision_page.how.acts.jump"),
    },
  ] as const;
  const body = (
    <>
      <ol className="dloop-how-steps">
        {steps.map((step, i) => (
          <li key={step.row} className="dloop-how-step">
            <span className="dloop-how-no" aria-hidden="true">
              {i + 1}
            </span>
            <div className="dloop-how-text">
              <strong className="dloop-how-title">{step.title}</strong>
              <p>
                {step.text}
                {step.model === null ? "" : ` ${step.model}`}
              </p>
              {jump(step.row, step.jump)}
            </div>
          </li>
        ))}
      </ol>
      <p className="dloop-how-foot">
        {t("decision_page.how.learns")} {jump("how", t("decision_page.how.learns.jump"))}
      </p>
      <p className="dloop-how-foot">
        {t("decision_page.how.data")} {jump("privacy", t("decision_page.how.data.jump"))}
      </p>
    </>
  );
  if (enabled) {
    return (
      <details className="dloop-how dloop-details">
        <summary>{t("decision_page.how.title")}</summary>
        {body}
      </details>
    );
  }
  return (
    <section className="dloop-how" aria-labelledby="how-works-heading">
      <h2 id="how-works-heading" className="dloop-how-heading">
        {t("decision_page.how.title")}
      </h2>
      {body}
    </section>
  );
}
