import { type ReactElement, useContext, useState } from "react";
import type { ProfileIdentityV1 } from "../../clients/profiles/profileApi";
import { t, tf } from "../../i18n/t";
import { AvatarEditor } from "../Avatar/AvatarEditor";
import { BotAvatar } from "../Bots/BotAvatar";
import { BotHuesContext } from "../Bots/botHues";
import { designFor } from "../Bots/botIconDesign";
import { BotIconDialog } from "./BotIconDialog";
import "./BotLookField.css";

export interface BotLookFieldProps {
  profileId: string;
  name: string;
  design: ProfileIdentityV1;
  avatar: string | null;
  imageRemovalPending: boolean;
  draftDisabled?: boolean;
  compact?: boolean;
  onUpload: (assetId: string) => void;
  onReset: () => void;
}

export function BotLookField({
  profileId,
  name,
  design,
  avatar,
  imageRemovalPending,
  draftDisabled = false,
  compact = false,
  onUpload,
  onReset,
}: BotLookFieldProps): ReactElement {
  const { hues, icons } = useContext(BotHuesContext);
  const [iconProfileId, setIconProfileId] = useState<string | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [notice, setNotice] = useState<{ profileId: string; text: string } | null>(null);
  const identity = { ...design, name, avatar_asset_id: avatar };
  const dialogProfileId = iconProfileId;
  const currentIcon = dialogProfileId === null ? null : (icons[dialogProfileId] ?? null);
  const currentHue =
    dialogProfileId === null
      ? designFor(profileId, name).hue
      : (hues[dialogProfileId] ?? designFor(dialogProfileId, name).hue);

  function openIcon(): void {
    setNotice(null);
    setIconProfileId(profileId);
  }

  function reset(): void {
    setNotice(null);
    onReset();
  }

  return (
    <section
      className={`bot-look${compact ? " bot-look-compact" : ""}`}
      aria-label={t("bots.look.legend")}
    >
      {!compact ? (
        <BotAvatar profileId={profileId} identity={identity} name={name} size={64} />
      ) : null}
      <div className="bot-look-actions">
        <button type="button" className="btn alt sm" onClick={openIcon}>
          {t("bots.look.chooseIcon")}
        </button>
        <button
          type="button"
          className="btn alt sm"
          disabled={draftDisabled}
          onClick={() => setUploadOpen(true)}
        >
          {t("bots.look.uploadImage")}
        </button>
        <button type="button" className="btn txt sm" disabled={draftDisabled} onClick={reset}>
          {t("bots.look.reset")}
        </button>
        <p>{t("bots.look.uploadPriority")}</p>
        {imageRemovalPending ? <p>{t("bots.look.saveRevision")}</p> : null}
      </div>
      <div className="bot-look-status" role="status" aria-live="polite">
        {notice?.profileId === profileId ? notice.text : ""}
      </div>
      {dialogProfileId !== null ? (
        <BotIconDialog
          profileId={dialogProfileId}
          name={name}
          hue={currentHue}
          icon={currentIcon}
          onClose={() =>
            setIconProfileId((current) => (current === dialogProfileId ? null : current))
          }
          onSaved={(icon) => {
            setNotice({
              profileId: dialogProfileId,
              text: tf("bots.icon.saved", {
                name: icon === null ? t("bots.icon.automatic") : t(`bots.icon.${icon}`),
              }),
            });
          }}
        />
      ) : null}
      {uploadOpen ? (
        <AvatarEditor
          onSaved={(assetId) => {
            onUpload(assetId);
            setUploadOpen(false);
          }}
          onCancel={() => setUploadOpen(false)}
        />
      ) : null}
    </section>
  );
}
