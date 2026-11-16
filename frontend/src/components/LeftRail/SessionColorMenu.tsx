import * as Dropdown from "@radix-ui/react-dropdown-menu";
import { useRef, useState } from "react";
import { SESSION_COLORS, type SessionColor } from "../../clients/conversations/sessionMetadataApi";
import { SESSION_COLOR_LABELS } from "../../state/accentLabels";
import { Icon } from "../Icon/Icon";
import "./SessionColorMenu.css";

export interface SessionColorMenuProps {
  color: SessionColor | null;
  pending: boolean;
  onColor: (color: SessionColor | null) => void;
  portalContainer?: HTMLElement | null;
}

export function SessionColorMenu({
  color,
  pending,
  onColor,
  portalContainer,
}: SessionColorMenuProps) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLDivElement>(null);
  return (
    <Dropdown.Sub open={open} onOpenChange={setOpen}>
      <Dropdown.SubTrigger ref={triggerRef} className="mi session-color-trigger" disabled={pending}>
        <Icon name="color" size={14} />
        Color label
      </Dropdown.SubTrigger>
      <Dropdown.Portal
        container={portalContainer ?? triggerRef.current?.closest<HTMLElement>(".rail-drawer")}
      >
        <Dropdown.SubContent
          className="session-color-menu menu-panel"
          sideOffset={6}
          collisionPadding={8}
          onPointerDown={(event) => event.stopPropagation()}
          onClick={(event) => event.stopPropagation()}
          onKeyDown={(event) => event.stopPropagation()}
          onFocusOutside={(event) => {
            // Leaving a parent item can focus its menu before the pointer reaches this submenu.
            if (event.target === triggerRef.current?.closest('[role="menu"]')) {
              event.preventDefault();
            }
          }}
        >
          <Dropdown.RadioGroup
            value={color ?? "none"}
            onValueChange={(value) => onColor(value === "none" ? null : (value as SessionColor))}
          >
            <Dropdown.RadioItem
              className="mi session-color-choice"
              value="none"
              disabled={pending}
              aria-label="No color label"
            >
              <span className="session-color-swatch" aria-hidden="true" />
              <span className="session-color-name">No color</span>
              <Dropdown.ItemIndicator className="session-color-selected" aria-hidden="true">
                Selected
              </Dropdown.ItemIndicator>
            </Dropdown.RadioItem>
            {SESSION_COLORS.map((value) => {
              const name = SESSION_COLOR_LABELS[value];
              return (
                <Dropdown.RadioItem
                  key={value}
                  className="mi session-color-choice"
                  value={value}
                  disabled={pending}
                  aria-label={`${name} color label`}
                >
                  <span
                    className="session-color-swatch"
                    data-color={value}
                    style={{ backgroundColor: `var(--label-${value})` }}
                    aria-hidden="true"
                  />
                  <span className="session-color-name">{name}</span>
                  <Dropdown.ItemIndicator className="session-color-selected" aria-hidden="true">
                    Selected
                  </Dropdown.ItemIndicator>
                </Dropdown.RadioItem>
              );
            })}
          </Dropdown.RadioGroup>
        </Dropdown.SubContent>
      </Dropdown.Portal>
    </Dropdown.Sub>
  );
}
