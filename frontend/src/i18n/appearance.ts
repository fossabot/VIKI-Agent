/**
 * The appearance page's own four rows. Key names follow `settings.key.<key>.label/.desc`, the
 * pattern every other settings page uses (for example `settings.key.appearance_theme.label`),
 * even though this page has its own component rather than `DocumentPage` — its rows live in a
 * different file but the same flat namespace.
 */

import { defineCatalog } from "./defineCatalog";

export const catalog = defineCatalog({
  en: {
    "settings.key.appearance_theme.label": "Theme",
    "settings.key.appearance_theme.desc": "The colour scheme this app's own screens use.",
    "settings.key.appearance_density.label": "Spacing",
    "settings.key.appearance_density.desc": "The space between rows and chat messages.",
    "settings.key.accessibility_font_scale.label": "Display size",
    "settings.key.accessibility_font_scale.desc":
      "Enlarges or shrinks every screen, including fixed panels.",
    "settings.key.accessibility_reduce_motion.label": "Animations",
    "settings.key.accessibility_reduce_motion.desc":
      "Choose whether to follow your computer's setting, always reduce animations, or allow them. " +
      "Your computer's reduced-motion setting takes priority.",

    "settings.value.appearance_theme.dark": "Dark",
    "settings.value.appearance_theme.light": "Light",
    "settings.value.appearance_theme.system": "Follow computer settings",

    "settings.value.appearance_density.comfortable": "Comfortable",
    "settings.value.appearance_density.compact": "Compact",

    // src/viki_agent/app/settings.py's `accessibility.font_scale` allowed list (0.85, 1, 1.15, 1.3, 1.5); the key
    // suffix is `String(value)` (components/Settings/pages.ts's `valueLabel` convention), so
    // "1" has no decimal point but "1.15"/"1.3"/"1.5" do.
    "settings.value.accessibility_font_scale.0.85": "85%",
    "settings.value.accessibility_font_scale.1": "100%",
    "settings.value.accessibility_font_scale.1.15": "115%",
    "settings.value.accessibility_font_scale.1.3": "130%",
    "settings.value.accessibility_font_scale.1.5": "150%",

    "settings.value.accessibility_reduce_motion.system": "Follow computer settings",
    "settings.value.accessibility_reduce_motion.reduce": "Reduce",
    "settings.value.accessibility_reduce_motion.allow": "Allow",

    "appearance.restore_defaults": "Restore defaults",
    "appearance.discarded": "Unapplied changes were discarded.",
    "appearance.reloaded": "Current settings reloaded; apply again.",
  },
});
