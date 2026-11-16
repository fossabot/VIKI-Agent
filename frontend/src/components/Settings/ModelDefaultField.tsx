import { useContext, useEffect, useState } from "react";
import {
  type ModelConnection,
  ModelConnectionsContext,
} from "../../clients/app/modelConnectionsApi";
import { t } from "../../i18n/t";
import "./ModelConnections.css";

export function ModelDefaultField({
  value,
  labelId,
  disabled,
  onChange,
}: {
  value: unknown;
  labelId: string;
  disabled: boolean;
  onChange: (value: string | null) => void;
}) {
  const api = useContext(ModelConnectionsContext);
  const [connections, setConnections] = useState<ModelConnection[]>([]);
  const external = typeof value === "string" && value.startsWith("viki-llm/");
  const committed = typeof value === "string" && !external ? value : "";
  const [field, setField] = useState({ seed: value, draft: committed });
  if (field.seed !== value) {
    const wasExternal = typeof field.seed === "string" && field.seed.startsWith("viki-llm/");
    setField({
      seed: value,
      draft:
        external || wasExternal || field.draft === (field.seed ?? "") ? committed : field.draft,
    });
  }
  useEffect(() => {
    const controller = new AbortController();
    if (api)
      void api
        .list(controller.signal)
        .then((listing) => {
          if (!controller.signal.aborted) setConnections(listing.connections);
        })
        .catch(() => undefined);
    return () => controller.abort();
  }, [api]);
  return (
    <div className="llm-default">
      <select
        className="input"
        aria-labelledby={labelId}
        disabled={disabled}
        value={external ? (value as string) : "native"}
        onChange={(event) => onChange(event.target.value === "native" ? null : event.target.value)}
      >
        <option value="native">{t("llm.native")}</option>
        {external && !connections.some((row) => row.model_ref === value) && (
          <option value={value as string}>{t("llm.current_connection")}</option>
        )}
        {connections.map((row) => (
          <option key={row.id} value={row.model_ref}>
            {row.name} · {row.model}
          </option>
        ))}
      </select>
      {!external && (
        <input
          className="input"
          aria-label={t("llm.model")}
          disabled={disabled}
          value={field.draft}
          maxLength={1024}
          onChange={(event) => setField({ seed: value, draft: event.target.value })}
          onBlur={() => {
            if (field.draft !== committed) onChange(field.draft || null);
          }}
        />
      )}
      <p className="settings-row-desc">{t("llm.configure")}</p>
    </div>
  );
}
