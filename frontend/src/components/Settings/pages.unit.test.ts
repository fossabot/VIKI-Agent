import { describe, expect, it } from "vitest";
import type { KeySpec } from "../../clients/app/shellApi";
import { settingsFixture } from "../../testShell";
import { HIDDEN_KEYS, keyDesc, keyLabel, keysOnPage, SETTINGS_PAGES, valueLabel } from "./pages";
import { searchSettings } from "./SettingsSearch";

const ROUTINE_KEYS = [
  "routines.paused",
  "notifications.enabled",
  "notifications.quiet_start",
  "notifications.quiet_end",
];

const DECISION_KEYS = [
  "decision.enabled",
  "decision.paused_until",
  "decision.focus_until",
  "decision.send_level",
  "decision.never_send",
  "decision.watch.tasks",
  "decision.watch.inbox",
  "decision.watch.vault_changes",
  "decision.watch.approvals",
  "decision.watch.chats",
  "decision.watch.routines",
  "decision.watch.improve",
  "decision.watch.kanban",
  "decision.watch.vault_health",
  "decision.watch.messaging",
  "decision.cadence",
  "decision.daily_cap_usd",
  "decision.nudges_per_hour",
  "decision.alerts",
  "decision.in_context",
  "decision.model_version",
  "decision.grant.prepare",
  "decision.grant.run",
  "decision.grant.team",
  "decision.grant.look",
  "decision.grant.second_look",
  "decision.grant.project_plan",
  "decision.grant.check",
  "decision.grant.inbox_triage",
  "decision.limit.drafts",
  "decision.limit.chats",
  "decision.limit.changes",
  "decision.plan_brake",
  "decision.limit.team",
  "decision.team.handoffs",
  "decision.limit.look",
  "decision.limit.second_look",
  "decision.limit.project_plan",
  "decision.limit.message_draft",
  "decision.limit.check",
  "decision.limit.inbox_triage",
  "decision.learning",
  "decision.shadow",
  "decision.launcher_session",
  "layout.launcher",
  "layout.launcher_panel",
];

describe("keysOnPage", () => {
  it("lists none of the Routines view's keys on General", () => {
    const keys: Record<string, KeySpec> = {};
    for (const key of ["identity.display_name", ...ROUTINE_KEYS]) keys[key] = {} as KeySpec;
    const { known, other } = keysOnPage("general", keys);
    for (const key of ROUTINE_KEYS) {
      expect(known).not.toContain(key);
      expect(other).not.toContain(key);
    }
    expect(known).toContain("identity.display_name");
  });

  it("keeps layout.panes hidden from every settings page and out of Other", () => {
    const keys: Record<string, KeySpec> = { "layout.panes": {} as KeySpec };
    expect(HIDDEN_KEYS.has("layout.panes")).toBe(true);
    for (const page of SETTINGS_PAGES) {
      const { known, other } = keysOnPage(page, keys);
      expect([...known, ...other]).not.toContain("layout.panes");
    }
  });

  it("keeps bots.hues hidden from every settings page and out of Other", () => {
    const keys: Record<string, KeySpec> = { "bots.hues": {} as KeySpec };
    expect(HIDDEN_KEYS.has("bots.hues")).toBe(true);
    for (const page of SETTINGS_PAGES) {
      const { known, other } = keysOnPage(page, keys);
      expect([...known, ...other]).not.toContain("bots.hues");
    }
  });

  it("keeps bots.icons hidden from every settings page and out of Other", () => {
    const keys: Record<string, KeySpec> = { "bots.icons": {} as KeySpec };
    expect(HIDDEN_KEYS.has("bots.icons")).toBe(true);
    for (const page of SETTINGS_PAGES) {
      const { known, other } = keysOnPage(page, keys);
      expect([...known, ...other]).not.toContain("bots.icons");
    }
  });

  it("keeps every Decision key and the launcher position hidden from every settings page", () => {
    const keys: Record<string, KeySpec> = {};
    for (const key of DECISION_KEYS) keys[key] = {} as KeySpec;
    for (const key of DECISION_KEYS) expect(HIDDEN_KEYS.has(key)).toBe(true);
    for (const page of SETTINGS_PAGES) {
      const { known, other } = keysOnPage(page, keys);
      expect([...known, ...other]).toEqual([]);
    }
  });

  it("lists the Code view's editor keys as known rows on General", () => {
    const keys: Record<string, KeySpec> = {};
    for (const key of ["workbench.code_font_size", "workbench.line_wrap"])
      keys[key] = {} as KeySpec;
    const { known, other } = keysOnPage("general", keys);
    expect(known).toEqual(["workbench.code_font_size", "workbench.line_wrap"]);
    expect(other).toEqual([]);
  });

  it("puts the worktree cleanup key on General right after line wrap", () => {
    const keys: Record<string, KeySpec> = {};
    for (const key of [
      "workbench.code_font_size",
      "workbench.line_wrap",
      "workbench.worktree_cleanup",
    ])
      keys[key] = {} as KeySpec;
    const { known, other } = keysOnPage("general", keys);
    expect(known).toEqual([
      "workbench.code_font_size",
      "workbench.line_wrap",
      "workbench.worktree_cleanup",
    ]);
    expect(other).toEqual([]);
  });

  it("puts the sign-in switch on Chat right after the default model", () => {
    const keys: Record<string, KeySpec> = {};
    for (const key of ["chat.default_model", "chat.other_sign_in", "voice.read_aloud_rate"])
      keys[key] = {} as KeySpec;
    expect(keysOnPage("chat", keys).known).toEqual([
      "chat.default_model",
      "chat.other_sign_in",
      "voice.read_aloud_rate",
    ]);
    expect(keyLabel("chat.other_sign_in")).toBe("Let Claude Code use its other sign-in methods");
  });

  it("ends Chat with the two read-aloud keys and shows the notice flag on no page", () => {
    const keys: Record<string, KeySpec> = {};
    for (const key of [
      "chat.default_model",
      "voice.read_aloud_rate",
      "voice.read_aloud_voices",
      "voice.read_aloud_notice_seen",
    ])
      keys[key] = {} as KeySpec;
    const chat = keysOnPage("chat", keys);
    expect(chat.known.slice(-2)).toEqual(["voice.read_aloud_rate", "voice.read_aloud_voices"]);
    expect(HIDDEN_KEYS.has("voice.read_aloud_notice_seen")).toBe(true);
    for (const page of ["general", "appearance", "chat", "privacy"] as const) {
      const { known, other } = keysOnPage(page, keys);
      expect([...known, ...other]).not.toContain("voice.read_aloud_notice_seen");
    }
  });

  it("puts the three dictation keys on Chat after the read-aloud rows", () => {
    const keys: Record<string, KeySpec> = {};
    for (const key of [
      "voice.backend",
      "voice.model",
      "voice.dictation_language",
      "voice.read_aloud_voices",
    ])
      keys[key] = {} as KeySpec;
    expect(keysOnPage("chat", keys).known).toEqual([
      "voice.read_aloud_voices",
      "voice.dictation_language",
      "voice.model",
      "voice.backend",
    ]);
  });

  it("puts the pack voices key on Chat right after the speech engine key", () => {
    const keys: Record<string, KeySpec> = {};
    for (const key of ["voice.speech_engine", "voice.pack_voices", "voice.voicebox_profile"])
      keys[key] = {} as KeySpec;
    const chat = keysOnPage("chat", keys);
    expect(chat.known).toEqual([
      "voice.speech_engine",
      "voice.pack_voices",
      "voice.voicebox_profile",
    ]);
  });
});

describe("voice.pause_s", () => {
  it("lists the pause setting on Chat with its English label, description and values", () => {
    const keys: Record<string, KeySpec> = { "voice.pause_s": {} as KeySpec };
    expect(keysOnPage("chat", keys).known).toContain("voice.pause_s");
    expect(keyLabel("voice.pause_s")).toBe("Pause before voice input ends");
    expect(keyDesc("voice.pause_s")).toBe(
      "A silence this long ends voice input in dictation and hands-free. Hands-free then " +
        "sends your message. Applies the next time you speak.",
    );
    for (const [value, label] of [
      [1.2, "1.2 s"],
      [2, "2 s"],
      [3, "3 s"],
      [4, "4 s"],
      [5, "5 s"],
    ] as const) {
      expect(valueLabel("voice.pause_s", value)).toBe(label);
    }
  });
});

describe("speech engine", () => {
  it("puts voice.speech_engine before the Voicebox keys", () => {
    const keys: Record<string, KeySpec> = {};
    for (const key of [
      "voice.read_aloud_voices",
      "voice.speech_engine",
      "voice.voicebox_profile",
      "voice.voicebox_model_size",
      "voice.voicebox_url",
    ])
      keys[key] = {} as KeySpec;
    expect(keysOnPage("chat", keys).known).toEqual([
      "voice.read_aloud_voices",
      "voice.speech_engine",
      "voice.voicebox_profile",
      "voice.voicebox_model_size",
      "voice.voicebox_url",
    ]);
  });
});

describe("voicebox settings", () => {
  it("puts the four voicebox keys on Chat right after the read-aloud voice map", () => {
    const keys: Record<string, KeySpec> = {};
    for (const key of [
      "voice.read_aloud_voices",
      "voice.voicebox_profile",
      "voice.voicebox_model_size",
      "voice.voicebox_url",
      "voice.speak_while_writing",
      "voice.dictation_language",
    ])
      keys[key] = {} as KeySpec;
    expect(keysOnPage("chat", keys).known).toEqual([
      "voice.read_aloud_voices",
      "voice.voicebox_profile",
      "voice.voicebox_model_size",
      "voice.voicebox_url",
      "voice.speak_while_writing",
      "voice.dictation_language",
    ]);
  });

  it("names the four voicebox keys in English", () => {
    expect(keyLabel("voice.voicebox_profile")).toBe("Voicebox voice");
    expect(keyDesc("voice.voicebox_profile")).toBe(
      "Read replies aloud with a voice from the Voicebox app on this computer, in the languages " +
        "Voicebox speaks. VIKI Agent sends the text to read to Voicebox. Off uses this computer's " +
        "own voices.",
    );
    expect(keyLabel("voice.voicebox_model_size")).toBe("Voicebox model size");
    expect(keyDesc("voice.voicebox_model_size")).toBe(
      "The model size Voicebox uses: Qwen voices take 0.6B or 1.7B, TADA voices 1B or 3B, other " +
        "voices ignore it. 0.6B suits a GPU with 8 GB.",
    );
    expect(keyLabel("voice.voicebox_url")).toBe("Voicebox address");
    expect(keyDesc("voice.voicebox_url")).toBe(
      "Where Voicebox answers on this computer: http with 127.0.0.1, localhost or [::1] and a " +
        "port. The Voicebox app uses http://127.0.0.1:17493.",
    );
    expect(keyLabel("voice.speak_while_writing")).toBe("Speak while VIKI writes");
    expect(keyDesc("voice.speak_while_writing")).toBe(
      "In hands-free, start playing the spoken answer while VIKI is still writing the reply. " +
        "Needs a Voicebox voice.",
    );
  });
});

describe("valueLabel", () => {
  it("names a dictation language code in English and takes auto from the catalog", () => {
    expect(valueLabel("voice.dictation_language", "ja")).toBe("Japanese");
    expect(valueLabel("voice.dictation_language", "auto")).toBe("Auto");
    expect(valueLabel("voice.backend", "cuda")).toBe("NVIDIA GPU (CUDA)");
    expect(valueLabel("voice.model", "small-q8_0")).toBeNull();
  });
});

describe("saved sidebar widths", () => {
  const keys: Record<string, KeySpec> = { ...settingsFixture().keys };
  keys["workbench.code_font_size"] = {
    scope: "app",
    applies: "now",
    kind: "number",
    personal: false,
    default: 13,
    allowed: Array.from({ length: 10 }, (_, index) => index + 11),
  };
  keys["workbench.line_wrap"] = {
    scope: "app",
    applies: "now",
    kind: "bool",
    personal: false,
    default: false,
    allowed: null,
  };
  for (const [key, minimum, maximum, defaultValue] of [
    ["layout.left_rail_width", 220, 480, 272],
    ["layout.right_rail_width", 240, 560, 320],
  ] as const) {
    keys[key] = {
      scope: "app",
      applies: "now",
      kind: "number",
      personal: false,
      default: defaultValue,
      allowed: [minimum, maximum],
    };
  }
  it("hides width rows and search hits while retaining the Code rows", () => {
    for (const page of SETTINGS_PAGES) {
      const { known, other } = keysOnPage(page, keys);
      for (const key of ["layout.left_rail_width", "layout.right_rail_width"])
        expect([...known, ...other]).not.toContain(key);
    }
    for (const query of ["width", "sidebar", "layout"])
      expect(searchSettings(keys, query)).toEqual([]);
    expect(keysOnPage("general", keys).known).toContain("workbench.code_font_size");
  });
  it("names portable width keys", () => {
    expect(keyLabel("layout.left_rail_width")).toBe("Left sidebar width");
    expect(keyLabel("layout.right_rail_width")).toBe("Right sidebar width");
    expect(keyDesc("layout.left_rail_width")).toBe(
      "Preferred width in pixels, from 220 to 480. Reset uses 272.",
    );
    expect(keyDesc("layout.right_rail_width")).toBe(
      "Preferred width in pixels, from 240 to 560. Reset uses 320.",
    );
  });
});
