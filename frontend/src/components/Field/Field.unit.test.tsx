import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Field } from "./Field";

afterEach(cleanup);

describe("Field", () => {
  it("ties the label to the control by id and omits absent description and invalid", () => {
    render(<Field label="Name">{(c) => <input {...c} />}</Field>);
    const input = screen.getByLabelText("Name");
    expect(screen.getByText("Name").getAttribute("for")).toBe(input.id);
    expect(input.hasAttribute("aria-describedby")).toBe(false);
    expect(input.hasAttribute("aria-invalid")).toBe(false);
  });

  it("describes the control by the hint and the error ids that exist", () => {
    const { rerender } = render(
      <Field label="Name" hint="Shown in the list">
        {(c) => <input {...c} />}
      </Field>,
    );
    const hint = screen.getByText("Shown in the list");
    expect(screen.getByLabelText("Name").getAttribute("aria-describedby")).toBe(hint.id);
    rerender(
      <Field label="Name" hint="Shown in the list" error="Too short">
        {(c) => <input {...c} />}
      </Field>,
    );
    const error = screen.getByRole("alert");
    expect(error.textContent).toBe("Too short");
    const input = screen.getByLabelText("Name");
    expect(input.getAttribute("aria-describedby")).toBe(`${hint.id} ${error.id}`);
    expect(input.getAttribute("aria-invalid")).toBe("true");
  });

  it("describes by the error alone when there is no hint", () => {
    render(
      <Field label="Name" error="Bad">
        {(c) => <input {...c} />}
      </Field>,
    );
    expect(screen.getByLabelText("Name").getAttribute("aria-describedby")).toBe(
      screen.getByRole("alert").id,
    );
  });

  it("names a native group from visible text and references its hint and error", () => {
    const { container } = render(
      <Field label="Vaults" hint="Choose vaults" error="Choose one" group>
        {(c) => (
          <fieldset {...c}>
            <input aria-label="Main" type="checkbox" />
          </fieldset>
        )}
      </Field>,
    );
    const group = screen.getByRole("group", { name: "Vaults" });
    const title = screen.getByText("Vaults");
    const hint = screen.getByText("Choose vaults");
    const error = screen.getByRole("alert");
    expect(title.tagName).not.toBe("LABEL");
    expect(group.getAttribute("aria-labelledby")).toBe(title.id);
    expect(group.getAttribute("aria-describedby")).toBe(`${hint.id} ${error.id}`);
    expect(group.getAttribute("aria-invalid")).toBe("true");
    expect(container.querySelectorAll("label[for]")).toHaveLength(0);
    const ids = [...container.querySelectorAll("[id]")].map((element) => element.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("renders the more details closed", () => {
    const { container } = render(
      <Field label="Name" more={{ summary: "Why", text: "Because" }}>
        {(c) => <input {...c} />}
      </Field>,
    );
    const details = container.querySelector("details");
    expect(details?.hasAttribute("open")).toBe(false);
    expect(details?.querySelector("summary")?.textContent).toBe("Why");
    expect(details?.textContent).toContain("Because");
  });
});
