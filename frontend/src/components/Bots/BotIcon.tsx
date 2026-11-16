import { useId, useMemo } from "react";
import "./BotIcon.css";
import { buildGeometry, designFor, type Ellipse, HEAD, type Hue, TILE } from "./botIconDesign";

// tokens.css only, through style: dark-theme values, since the svg carries data-theme="dark".
const GHOST = "var(--or)";
const INK = "var(--on-or)";

function Highlight({ at, fill }: { at: Ellipse; fill: string }) {
  return (
    <ellipse
      cx={at.cx}
      cy={at.cy}
      rx={at.rx}
      ry={at.ry}
      transform={`rotate(-28 ${at.cx} ${at.cy})`}
      style={{ fill }}
    />
  );
}

/** A Bot's generated icon (botIconDesign.ts). Decorative: the avatar around it carries the name.
 * `hue`, when given, overrides the design's own (BotAvatar, for a profile's stored hue). */
export function BotIcon({
  seed,
  name,
  hue,
  icon,
}: {
  seed: string;
  name: string;
  hue?: Hue | undefined;
  icon?: string | undefined;
}) {
  // useId's punctuation varies by React version; url(#...) references want a plain id.
  const id = `bot-icon${useId().replace(/[^A-Za-z0-9_-]/g, "")}`;
  const geometry = useMemo(() => {
    const design = designFor(seed, name, icon);
    return buildGeometry(hue === undefined ? design : { ...design, hue });
  }, [seed, name, hue, icon]);
  const { design, led, gear } = geometry;
  const url = (part: string) => `url(#${id}-${part})`;
  return (
    <svg
      className="bot-icon"
      data-theme="dark"
      // The drawing spans 100x100; this trims its empty margin so small avatars show more bot.
      viewBox="4 4 92 92"
      shapeRendering="geometricPrecision"
      aria-hidden="true"
      focusable="false"
      data-seat={design.seat ?? undefined}
      data-head={design.head}
      data-visor={design.visor}
      data-gear={design.gear}
      data-eyes={design.eyes}
      data-emblem={design.emblem}
      data-icon={design.emblem}
      data-hue={design.hue}
    >
      <defs>
        <clipPath id={`${id}-head`}>
          <path d={geometry.head} />
        </clipPath>
        <clipPath id={`${id}-glass`}>
          {geometry.visor.map((d) => (
            <path key={d} d={d} />
          ))}
        </clipPath>
        <radialGradient id={`${id}-depth`} cx="0.38" cy="0.34" r="0.75">
          <stop offset="0.45" style={{ stopColor: INK, stopOpacity: 0 }} />
          <stop offset="1" style={{ stopColor: INK, stopOpacity: 0.7 }} />
        </radialGradient>
        <radialGradient id={`${id}-shine`}>
          <stop offset="0" style={{ stopColor: GHOST, stopOpacity: 0.32 }} />
          <stop offset="1" style={{ stopColor: GHOST, stopOpacity: 0 }} />
        </radialGradient>
        <radialGradient id={`${id}-glint`}>
          <stop offset="0" style={{ stopColor: GHOST, stopOpacity: 0.9 }} />
          <stop offset="1" style={{ stopColor: GHOST, stopOpacity: 0 }} />
        </radialGradient>
        <radialGradient id={`${id}-bounce`}>
          <stop offset="0" style={{ stopColor: GHOST, stopOpacity: 0.16 }} />
          <stop offset="1" style={{ stopColor: GHOST, stopOpacity: 0 }} />
        </radialGradient>
        <linearGradient id={`${id}-rim`} x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" style={{ stopColor: GHOST, stopOpacity: 0.95 }} />
          <stop offset="0.6" style={{ stopColor: GHOST, stopOpacity: 0.55 }} />
          <stop offset="0.95" style={{ stopColor: GHOST, stopOpacity: 0 }} />
        </linearGradient>
        <linearGradient id={`${id}-frame`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style={{ stopColor: "var(--b-alt)" }} />
          <stop offset="1" style={{ stopColor: HEAD }} />
        </linearGradient>
        <linearGradient id={`${id}-edge`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style={{ stopColor: GHOST, stopOpacity: 0.6 }} />
          <stop offset="0.45" style={{ stopColor: GHOST, stopOpacity: 0 }} />
        </linearGradient>
        <linearGradient id={`${id}-pane`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style={{ stopColor: "var(--fill-hi)" }} />
          <stop offset="1" style={{ stopColor: INK }} />
        </linearGradient>
        <filter id={`${id}-bloom`} x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="2.2" />
        </filter>
        <filter id={`${id}-soft`} x="-20%" y="-20%" width="140%" height="140%">
          <feGaussianBlur stdDeviation="1.1" />
        </filter>
      </defs>

      {gear.stems ? (
        <>
          <path
            d={gear.stems}
            style={{ fill: "none", stroke: HEAD, strokeWidth: 2.6, strokeLinecap: "round" }}
          />
          <path
            d={gear.stems}
            style={{ fill: "none", stroke: "var(--t3)", strokeWidth: 1.3, strokeLinecap: "round" }}
          />
        </>
      ) : null}

      <path d={geometry.head} style={{ fill: HEAD }} />
      <g clipPath={url("head")}>
        {Object.entries(geometry.tiles).map(([opacity, d]) => (
          <path key={opacity} d={d} style={{ fill: TILE, fillOpacity: Number(opacity) }} />
        ))}
        {Object.entries(geometry.glints).map(([opacity, d]) => (
          <path
            key={opacity}
            d={d}
            style={{
              fill: "none",
              stroke: GHOST,
              strokeOpacity: Number(opacity),
              strokeWidth: 0.4,
              strokeLinecap: "round",
            }}
          />
        ))}
        <path d={geometry.head} style={{ fill: url("depth") }} />
        <Highlight at={geometry.shine} fill={url("shine")} />
        <Highlight at={geometry.glint} fill={url("glint")} />
        <Highlight at={geometry.bounce} fill={url("bounce")} />
      </g>
      <path
        d={geometry.head}
        filter={url("soft")}
        style={{ fill: "none", stroke: url("rim"), strokeWidth: 2.2, strokeOpacity: 0.35 }}
      />
      <path d={geometry.head} style={{ fill: "none", stroke: url("rim"), strokeWidth: 1.2 }} />

      {gear.ear ? (
        <>
          <path
            d={gear.ear}
            style={{ fill: HEAD, stroke: GHOST, strokeOpacity: 0.85, strokeWidth: 1.4 }}
          />
          <path
            d={gear.earLight}
            style={{ fill: "none", stroke: GHOST, strokeOpacity: 0.6, strokeWidth: 0.7 }}
          />
        </>
      ) : null}

      {Object.entries(geometry.loose).map(([shade, d]) => (
        <path key={shade} d={d} style={{ fill: shade }} />
      ))}
      <path d={geometry.looseLight} style={{ fill: GHOST, fillOpacity: 0.28 }} />

      <g clipPath={design.visor === "band" ? url("head") : undefined}>
        {geometry.frame.map((d) => (
          <path
            key={d}
            d={d}
            style={{ fill: url("frame"), stroke: url("edge"), strokeWidth: 0.8 }}
          />
        ))}
        {geometry.visor.map((d) => (
          <path key={d} d={d} style={{ fill: url("pane") }} />
        ))}
        <g clipPath={url("glass")}>
          {geometry.visor.map((d) => (
            <path
              key={d}
              d={d}
              filter={url("soft")}
              style={{ fill: "none", stroke: INK, strokeWidth: 3.2, strokeOpacity: 0.85 }}
            />
          ))}
          <path d={geometry.streak} style={{ fill: GHOST, fillOpacity: 0.08 }} />
        </g>
      </g>
      <path
        d={geometry.gloss}
        style={{
          fill: "none",
          stroke: GHOST,
          strokeOpacity: 0.35,
          strokeWidth: 1,
          strokeLinecap: "round",
        }}
      />

      <path d={geometry.eyesDim} style={{ fill: led, fillOpacity: 0.12 }} />
      <g filter={url("bloom")} style={{ fill: led, opacity: 0.85 }}>
        <path d={geometry.eyesLit} />
        <path d={geometry.emblem} />
        {gear.balls ? <path d={gear.balls} /> : null}
      </g>
      <g style={{ fill: led }}>
        <path d={geometry.eyesLit} />
        <path d={geometry.emblem} />
        {gear.balls ? <path d={gear.balls} /> : null}
      </g>
      <path d={geometry.emblemLight} style={{ fill: GHOST, fillOpacity: 0.42 }} />
      <path d={geometry.emblemShade} style={{ fill: INK, fillOpacity: 0.32 }} />
      <path d={geometry.eyesHot + gear.ballsHot} style={{ fill: GHOST, fillOpacity: 0.62 }} />
      {geometry.sparks.map((spark, index) => (
        // Sparks are a fixed list per icon, so their order is their identity.
        // biome-ignore lint/suspicious/noArrayIndexKey: the list never reorders
        <path key={index} d={spark.d} style={{ fill: led, fillOpacity: spark.opacity }} />
      ))}
    </svg>
  );
}
