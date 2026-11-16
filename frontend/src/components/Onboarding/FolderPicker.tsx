/**
 * ADR-050's OS folder picker. `FolderPickerProvider` reads `ShellApi.folderPickerStatus()` once
 * per `api` instance and hands the result down through context, so every `PathForm` that opts
 * into a Browse… button shares one status read instead of one each; unavailable with a null
 * reason until that read settles, or if it fails. `useFolderPicker` is the hook a field reads:
 * `null` outside a provider (the field then stays input-only), otherwise the live status plus
 * `pick`, a thin call-through to `ShellApi.pickFolders` with `(title, multiple)` swapped to put
 * the commonly-passed title first. `pickerReasonText` turns the status's `reason` into the
 * catalog sentence explaining why the picker is not available.
 */
import {
  createContext,
  type ReactElement,
  type ReactNode,
  useContext,
  useEffect,
  useState,
} from "react";
import type { FolderPickerStatus, PickerReason, ShellApi } from "../../clients/app/shellApi";
import { t } from "../../i18n/t";

export interface FolderPicker {
  status: FolderPickerStatus;
  pick(title: string, multiple: boolean): Promise<string[]>;
}

const UNAVAILABLE: FolderPickerStatus = { available: false, multiple: false, reason: null };

export const FolderPickerContext = createContext<FolderPicker | null>(null);

export function FolderPickerProvider({
  api,
  children,
}: {
  api: ShellApi;
  children: ReactNode;
}): ReactElement {
  const [status, setStatus] = useState<FolderPickerStatus>(UNAVAILABLE);

  useEffect(() => {
    setStatus(UNAVAILABLE);
    let live = true;
    api.folderPickerStatus().then(
      (value) => {
        if (live) setStatus(value);
      },
      () => {
        if (live) setStatus(UNAVAILABLE);
      },
    );
    return () => {
      live = false;
    };
  }, [api]);

  const value: FolderPicker = {
    status,
    pick: (title, multiple) => api.pickFolders(multiple, title),
  };
  return <FolderPickerContext.Provider value={value}>{children}</FolderPickerContext.Provider>;
}

export function useFolderPicker(): FolderPicker | null {
  return useContext(FolderPickerContext);
}

export function pickerReasonText(reason: PickerReason | null): string {
  if (reason === "unsupported") return t("onboarding.picker.reason.unsupported");
  if (reason === "no_program") return t("onboarding.picker.reason.no_program");
  if (reason === "no_display") return t("onboarding.picker.reason.no_display");
  return t("onboarding.picker.reason.unknown");
}
