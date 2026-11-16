/** Right-rail groups: Up next's routines, followed by the rail footer. */
import type { ReactElement, ReactNode } from "react";
import "./RailSections.css";

export interface UpNextSectionProps {
  routines: ReactNode;
}

export function UpNextSection({ routines }: UpNextSectionProps): ReactElement {
  return <div className="rail-up-next">{routines}</div>;
}

export interface RailFooterProps {
  tiles?: ReactNode;
  /** Count of vault changes not yet committed to git. */
  uncommitted?: number;
  skills?: ReactNode;
}

export function RailFooter({ tiles, uncommitted, skills }: RailFooterProps): ReactElement {
  return (
    <>
      {tiles}
      {uncommitted !== undefined && uncommitted > 0 ? (
        <p className="rail-uncommitted" title="Vault changes not yet committed to git">
          {uncommitted} uncommitted
        </p>
      ) : null}
      {skills}
    </>
  );
}
