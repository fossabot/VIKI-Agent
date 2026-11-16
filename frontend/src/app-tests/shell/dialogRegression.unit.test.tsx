import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { type ReactNode, StrictMode, useRef, useState } from "react";
import { afterEach, expect, test, vi } from "vitest";
import { DecisionInbox } from "../../components/Decisions/DecisionInbox";
import { ProposalInspector } from "../../components/Decisions/ProposalInspector";
import { useModalFocus } from "../../components/Dialog/useModalFocus";
import type { DecisionsApi, ProposalDetail } from "../../state/decisionsApi";

afterEach(cleanup);

/** A dialog with a menu inside it, the shape a rail drawer takes when a session row's menu
 * is open. */
function MenuDialog({ onDismiss }: { onDismiss: () => void }) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [menu, setMenu] = useState(true);
  useModalFocus({ active: true, dialogRef, initialFocusRef: closeRef, onDismiss });
  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-label="Synthetic drawer"
      tabIndex={-1}
    >
      <button type="button" ref={closeRef}>
        Close
      </button>
      {menu ? (
        <div role="menu">
          <button type="button" role="menuitem" onClick={() => setMenu(false)}>
            Item
          </button>
        </div>
      ) : null}
    </div>
  );
}

test("Escape yields to a menu open inside the dialog, then dismisses once it is gone", () => {
  const onDismiss = vi.fn();
  const view = render(<MenuDialog onDismiss={onDismiss} />);
  try {
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onDismiss).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("menuitem"));
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onDismiss).toHaveBeenCalledTimes(1);
  } finally {
    view.unmount();
  }
});

function FocusDialog({
  label,
  children,
  onDismiss = () => {},
}: {
  label: string;
  children?: ReactNode;
  onDismiss?: () => void;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  useModalFocus({ active: true, dialogRef, initialFocusRef: closeRef, onDismiss });
  return (
    <div ref={dialogRef} role="dialog" aria-label={label} aria-modal="true">
      <button type="button" ref={closeRef}>
        {label} close
      </button>
      <button type="button">{label} last</button>
      {children}
    </div>
  );
}

function ModalPair({
  outer,
  inner = false,
  added = false,
  onOuter,
  onInner,
}: {
  outer: boolean;
  inner?: boolean;
  added?: boolean;
  onOuter?: () => void;
  onInner?: () => void;
}) {
  return (
    <div>
      <button type="button" data-testid="modal-background">
        Open outer
      </button>
      {added ? <aside data-testid="added-background">New background</aside> : null}
      {outer ? (
        <FocusDialog label="Outer" {...(onOuter ? { onDismiss: onOuter } : {})}>
          {inner ? (
            <FocusDialog label="Inner" {...(onInner ? { onDismiss: onInner } : {})} />
          ) : null}
        </FocusDialog>
      ) : null}
    </div>
  );
}

test.each([null, "", "owner-value"])(
  "StrictMode nested teardown restores original inert %s and leaves aria unchanged",
  (original) => {
    const { rerender } = render(
      <StrictMode>
        <ModalPair outer={false} />
      </StrictMode>,
    );
    const background = screen.getByTestId("modal-background");
    if (original !== null) background.setAttribute("inert", original);
    background.setAttribute("aria-hidden", "false");
    rerender(
      <StrictMode>
        <ModalPair outer />
      </StrictMode>,
    );
    rerender(
      <StrictMode>
        <ModalPair outer inner />
      </StrictMode>,
    );
    expect(screen.getByRole("dialog", { name: "Inner" }).closest("[inert]")).toBeNull();

    rerender(
      <StrictMode>
        <ModalPair outer={false} />
      </StrictMode>,
    );

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(background.getAttribute("inert")).toBe(original);
    expect(background.getAttribute("aria-hidden")).toBe("false");
    rerender(
      <StrictMode>
        <ModalPair outer />
      </StrictMode>,
    );
    rerender(
      <StrictMode>
        <ModalPair outer={false} />
      </StrictMode>,
    );
    expect(background.getAttribute("inert")).toBe(original);
  },
);

test("observer-added background loses inert when both nested dialogs close", async () => {
  const { rerender } = render(<ModalPair outer />);
  rerender(<ModalPair outer inner />);
  await act(async () => {
    rerender(<ModalPair outer inner added />);
  });
  const added = screen.getByTestId("added-background");
  expect(added.hasAttribute("inert")).toBe(true);

  rerender(<ModalPair outer={false} added />);

  expect(added.hasAttribute("inert")).toBe(false);
});

test("normal nested close order preserves topmost keys, containment and opener focus", () => {
  const onOuter = vi.fn();
  const onInner = vi.fn();
  const callbacks = { onOuter, onInner };
  const { rerender } = render(<ModalPair outer={false} {...callbacks} />);
  const background = screen.getByTestId("modal-background");
  background.focus();
  rerender(<ModalPair outer {...callbacks} />);
  const outerClose = screen.getByRole("button", { name: "Outer close" });
  expect(document.activeElement).toBe(outerClose);
  rerender(<ModalPair outer inner {...callbacks} />);
  const innerClose = screen.getByRole("button", { name: "Inner close" });
  expect(document.activeElement).toBe(innerClose);
  background.focus();
  expect(document.activeElement).toBe(innerClose);
  fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Inner last" }));
  fireEvent.keyDown(window, { key: "Escape" });
  expect(onInner).toHaveBeenCalledOnce();
  expect(onOuter).not.toHaveBeenCalled();

  rerender(<ModalPair outer {...callbacks} />);
  expect(document.activeElement).toBe(outerClose);
  expect(background.hasAttribute("inert")).toBe(true);
  rerender(<ModalPair outer={false} {...callbacks} />);
  expect(document.activeElement).toBe(background);
  expect(background.hasAttribute("inert")).toBe(false);
});

test("Open in the editor closes the actual inbox and inspector without leaving the app inert", async () => {
  const detail: ProposalDetail = {
    kind: "vault_proposal",
    id: "proposal",
    origin: { workspace_id: "workspace", session_id: null },
    revision: "b".repeat(64),
    state: "open",
    attention: false,
    title: "notes/example.md",
    ref: "notes/example.md",
    origin_kind: "note_edit",
    base_revision: "a".repeat(64),
    text_bytes: 8,
    handed_at: null,
    created_at: "2026-10-01T00:00:00Z",
    current_revision: "a".repeat(64),
    proposed_text: "example",
    diff: "-old\n+new",
    diff_reason: null,
  };
  const api: DecisionsApi = {
    decisions: vi.fn(),
    proposalDetail: vi.fn(async () => detail),
    proposalAct: vi.fn(),
    createProposal: vi.fn(),
  };
  const openEditor = vi.fn();
  function InboxAndInspector() {
    const [open, setOpen] = useState(true);
    const [inspecting, setInspecting] = useState(false);
    return (
      <div>
        <main data-testid="app-background" aria-label="Editor">
          <button type="button">Editor action</button>
        </main>
        {open ? (
          <DecisionInbox
            items={[]}
            sessionLabel={() => ""}
            workspaceName="Synthetic"
            renderAnswer={() => null}
            onOpenConversation={vi.fn()}
            onClose={() => setOpen(false)}
          >
            <button type="button" onClick={() => setInspecting(true)}>
              Inspect proposal
            </button>
            {inspecting ? (
              <ProposalInspector
                api={api}
                item={detail}
                workspaceName="Synthetic"
                activeWorkspaceId="workspace"
                onClose={() => setInspecting(false)}
                onChanged={vi.fn()}
                onHandoff={vi.fn()}
                onOpenEditor={() => {
                  openEditor();
                  // App.showDecisions(false) clears the inbox and its inspected proposal together.
                  setInspecting(false);
                  setOpen(false);
                }}
              />
            ) : null}
          </DecisionInbox>
        ) : null}
      </div>
    );
  }
  const view = render(<InboxAndInspector />);
  try {
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Inspect proposal" }));
    });
    expect(screen.getAllByRole("dialog")).toHaveLength(2);
    const background = screen.getByTestId("app-background");
    expect(background.hasAttribute("inert")).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Open in the editor" }));

    expect(openEditor).toHaveBeenCalledOnce();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(background.hasAttribute("inert")).toBe(false);
    expect(background.getAttribute("aria-label")).toBe("Editor");
  } finally {
    view.unmount();
  }
});
