import { type ReactElement, useEffect, useRef, useState } from "react";
import { KnowledgeError, type VaultKnowledgeApi } from "../../state/vaultKnowledgeApi";
import type { UploadedAttachment } from "../../turnInput";
import "./KeepInVault.css";

export interface KeepInVaultContext {
  api: Pick<VaultKnowledgeApi, "keepAttachment">;
  workspaceId: string;
  policyRevision: number;
  rootPhysicalId: string;
}

const GENERIC = "Couldn't save to vault.";
const REFUSALS: Record<string, string> = {
  vault_protected: "This vault is protected, so it can't be saved to.",
  vault_read_only: "This vault is read-only, so it can't be saved to.",
  asset_unavailable: "This attachment is no longer available, so it can't be saved.",
  invalid_name: "This name can't be used. Choose a different name.",
  conflict: "The vault's contents changed. Reopen it and try again.",
  vault_locked: "Another save is in progress. Wait a moment and try again.",
  overloaded: "It's busy right now. Wait a moment and try again.",
  too_large: "This is too large to save.",
  unsupported_filesystem: "This environment doesn't support saving to the vault.",
};

function refusalText(code: string): string {
  return Object.hasOwn(REFUSALS, code) ? (REFUSALS[code] ?? GENERIC) : GENERIC;
}

type Outcome = { ref: string; reused: boolean };

/** One attachment's keep control. It writes nothing until the server answers, and it shows the
 * returned ref for the user to copy; it never puts the ref into a note. */
export function KeepInVault({
  attachment,
  vault,
  onDone,
}: {
  attachment: UploadedAttachment;
  vault: KeepInVaultContext;
  onDone?: (ref: string) => void;
}): ReactElement {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState(attachment.name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const live = useRef(true);
  const opener = useRef<HTMLButtonElement>(null);
  const closed = useRef(false);
  useEffect(() => {
    if (!open && closed.current) {
      closed.current = false;
      opener.current?.focus();
    }
  }, [open]);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  async function keep(): Promise<void> {
    setBusy(true);
    setError("");
    try {
      const kept = await vault.api.keepAttachment(
        vault.workspaceId,
        {
          policyRevision: vault.policyRevision,
          rootPhysicalId: vault.rootPhysicalId,
          assetId: attachment.asset_id,
          name,
        },
        crypto.randomUUID(),
      );
      if (!live.current) return;
      setOutcome({ ref: kept.ref, reused: kept.reused });
      onDone?.(kept.ref);
    } catch (failure) {
      if (live.current)
        setError(failure instanceof KnowledgeError ? refusalText(failure.code) : GENERIC);
    } finally {
      if (live.current) setBusy(false);
    }
  }

  function close(): void {
    closed.current = true;
    setOutcome(null);
    setError("");
    setOpen(false);
  }

  if (!open)
    return (
      <button
        type="button"
        ref={opener}
        className="attachment-item-action"
        onClick={() => setOpen(true)}
      >
        Save to vault
      </button>
    );
  return (
    <fieldset className="keep-in-vault">
      <legend>Save to vault</legend>
      {outcome ? (
        <>
          <p role="status">
            {outcome.reused
              ? "A file with this content already exists. Using the existing copy."
              : "Saved to vault."}{" "}
            <code className="mono">{outcome.ref}</code>
          </p>
          <button type="button" className="attachment-item-action" onClick={close}>
            Close
          </button>
        </>
      ) : (
        <>
          <label>
            Name
            <input
              value={name}
              placeholder="Image"
              maxLength={255}
              disabled={busy}
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <p>
            The file extension comes from the file's own type. A file with the same content is
            reused.
          </p>
          <button
            type="button"
            className="attachment-item-action"
            disabled={busy}
            onClick={() => void keep()}
          >
            {busy ? "Saving…" : "Save"}
          </button>{" "}
          <button type="button" className="attachment-item-action" disabled={busy} onClick={close}>
            Close
          </button>
          {error ? <p role="alert">{error}</p> : null}
        </>
      )}
    </fieldset>
  );
}
