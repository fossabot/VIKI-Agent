/** One avatar picker: the current avatar, a Set/Replace button that opens the avatar editor, and
 * a Reset button that clears it. The editor decides its own save and cancel copy; this field only
 * turns its outcome into `onChange`. */
import { type ReactNode, useId, useState } from "react";
import "./AvatarField.css";
import { Avatar } from "./Avatar";
import { AvatarEditor } from "./AvatarEditor";
import type { Accent, Shape } from "./identity";

export interface AvatarFieldProps {
  label: string;
  /** The id of a label element the caller already renders for this row; when absent, the field
   * renders its own label span. Either way the buttons are described by it, so a row never shows
   * the label twice and its buttons are never announced without one. */
  labelId?: string;
  name: string;
  accent: Accent;
  shape: Shape;
  avatarId: string | null;
  disabled: boolean;
  /** The read-only notice's id when the document is read-only, else null (or omitted). The
   * buttons then stay focusable with `aria-disabled` and name the notice, the same rule
   * `SettingRow`'s controls and the onboarding row follow, rather than leaving the tab order. */
  readOnlyNoticeId?: string | null;
  /** Replaces the initial when no uploaded image is selected. */
  fallback?: ReactNode;
  /** A small caption shown below the preview only when no image is selected. */
  emptyText?: string;
  onChange: (avatarId: string | null) => void;
}

export function AvatarField({
  label,
  labelId,
  name,
  accent,
  shape,
  avatarId,
  disabled,
  readOnlyNoticeId = null,
  fallback,
  emptyText,
  onChange,
}: AvatarFieldProps) {
  const [editing, setEditing] = useState(false);
  const ownLabelId = useId();
  const effectiveLabelId = labelId ?? ownLabelId;
  const readOnly = readOnlyNoticeId !== null;
  const describedBy = readOnly ? `${effectiveLabelId} ${readOnlyNoticeId}` : effectiveLabelId;

  function saved(id: string): void {
    onChange(id);
    setEditing(false);
  }

  function open(): void {
    if (readOnly) return;
    setEditing(true);
  }

  function reset(): void {
    if (readOnly) return;
    onChange(null);
  }

  return (
    <div className="avatar-field">
      {labelId === undefined && <span id={ownLabelId}>{label}</span>}
      <div className="avatar-field-preview">
        <Avatar
          name={avatarId === null && fallback === false ? "" : name}
          avatarId={avatarId}
          accent={accent}
          shape={shape}
          size={64}
          fallback={fallback}
        />
        {avatarId === null && emptyText ? (
          <span className="avatar-field-empty">{emptyText}</span>
        ) : null}
      </div>
      <button
        type="button"
        className="btn alt sm"
        disabled={!readOnly && disabled}
        aria-disabled={readOnly || undefined}
        aria-describedby={describedBy}
        onClick={open}
      >
        {avatarId ? "Replace" : "Set"}
      </button>
      <button
        type="button"
        className="btn alt sm"
        disabled={!readOnly && (disabled || avatarId === null)}
        aria-disabled={readOnly || undefined}
        aria-describedby={describedBy}
        onClick={reset}
      >
        Reset
      </button>
      {editing ? <AvatarEditor onSaved={saved} onCancel={() => setEditing(false)} /> : null}
    </div>
  );
}
