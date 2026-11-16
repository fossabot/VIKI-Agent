import type { ReactElement } from "react";
import type { ProviderModelView } from "../../clients/profiles/profileLaunchApi";
import { t, tf } from "../../i18n/t";
import { Field } from "../Field/Field";
import { type ConnectionLabels, modelLabel } from "./modelLabel";

const NO_CONNECTIONS: ConnectionLabels = { status: "loading", labels: [] };

export function ProfileModelField({
  value,
  models,
  defaultModel,
  botSettings = false,
  connections = NO_CONNECTIONS,
  onChange,
  onRetry,
}: {
  value: string | null;
  models: ProviderModelView | "loading" | "failed" | null;
  defaultModel?: string | null;
  /** Only Bot settings offers saved connections; Agents › Profiles just names a saved one. */
  botSettings?: boolean;
  connections?: ConnectionLabels;
  onChange(value: string | null): void;
  onRetry(): void;
}): ReactElement {
  const catalog = typeof models === "object" && models !== null ? models : null;
  const listed = catalog?.models.some((model) => model.value === value) ?? false;
  const isConnection = value?.startsWith("viki-llm/") ?? false;
  const choices = botSettings && connections.status === "ready" ? connections.labels : [];
  const connectionListed = connections.labels.some((row) => row.model_ref === value);
  const savedUnlisted = value !== null && !listed && !(botSettings && connectionListed);
  const unavailable = savedUnlisted && !isConnection && catalog?.status === "supported";
  const label = (ref: string) =>
    modelLabel(ref, catalog, connections.labels, t("profiles.model.connected"));
  const help =
    typeof defaultModel === "string" && !defaultModel.startsWith("viki-llm/")
      ? tf("profiles.model.defaultHelp", { model: modelLabel(defaultModel, catalog) })
      : t("profiles.model.claudeDefaultHelp");

  return (
    <div className="profile-model-field">
      <Field label={t("profiles.model")} hint={help}>
        {(control) => (
          <select
            {...control}
            value={value ?? ""}
            onChange={(event) => onChange(event.target.value || null)}
          >
            <option value="">{t("profiles.model.appDefault")}</option>
            {savedUnlisted ? (
              <option value={value}>
                {label(value)}
                {unavailable ? ` (${t("profiles.model.unavailableSuffix")})` : ""}
              </option>
            ) : null}
            {catalog ? (
              <optgroup label={t("profiles.model.group")}>
                {catalog.models.map((model) => (
                  <option key={model.value} value={model.value}>
                    {model.label}
                  </option>
                ))}
              </optgroup>
            ) : null}
            {choices.length > 0 ? (
              <optgroup label={t("profiles.model.connections")}>
                {choices.map((row) => (
                  <option key={row.model_ref} value={row.model_ref}>
                    {label(row.model_ref)}
                  </option>
                ))}
              </optgroup>
            ) : null}
          </select>
        )}
      </Field>
      {isConnection && connections.status === "loading" ? (
        <p>{t("profiles.model.connectionLoading")}</p>
      ) : null}
      {(isConnection || botSettings) && connections.status === "failed" ? (
        <p>{t("profiles.model.connectionFailed")}</p>
      ) : null}
      {isConnection && connections.status === "ready" && !connectionListed ? (
        <p>{t("profiles.model.connectionRemoved")}</p>
      ) : null}
      {models === null ? <p>{t("profiles.model.notChecked")}</p> : null}
      {models === "loading" ? <p>{t("profiles.model.loading")}</p> : null}
      {models === "failed" ? (
        <p>
          {t("profiles.model.failed")}{" "}
          <button className="btn alt" type="button" onClick={onRetry}>
            {t("common.button.retry")}
          </button>
        </p>
      ) : null}
      {catalog?.status === "unverified" ? <p>{t("profiles.model.unverified")}</p> : null}
      {catalog?.status === "supported" && catalog.models.length === 0 ? (
        <p>{t("profiles.model.noneReported")}</p>
      ) : null}
    </div>
  );
}
