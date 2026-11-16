/**
 * Static help content: the 17 help topics, each grouped (Start here / Using VIKI Agent /
 * Reference), titled and led from its page's first `# ` line and the first line after it, in
 * the Help center's display order; free-text search over them; the curated public "What's new"
 * page; the getting-started tour's steps; and the draft text for
 * "Ask VIKI".
 */
import type { HelpTopicId } from "../components/Help/helpStore";
import agentsMd from "./topics/agents.md?raw";
import approvalsMd from "./topics/approvals.md?raw";
import chatsMd from "./topics/chats.md?raw";
import codeMd from "./topics/code.md?raw";
import dataMd from "./topics/data.md?raw";
import decisionMd from "./topics/decision.md?raw";
import gettingStartedMd from "./topics/getting-started.md?raw";
import glossaryMd from "./topics/glossary.md?raw";
import keyboardShortcutsMd from "./topics/keyboard-shortcuts.md?raw";
import messagingMd from "./topics/messaging.md?raw";
import routinesMd from "./topics/routines.md?raw";
import settingsMd from "./topics/settings.md?raw";
import sourcesMd from "./topics/sources.md?raw";
import troubleshootingMd from "./topics/troubleshooting.md?raw";
import vaultMd from "./topics/vault.md?raw";
import voiceMd from "./topics/voice.md?raw";
import welcomeMd from "./topics/welcome.md?raw";

export type HelpGroup = "Start here" | "Using VIKI Agent" | "Reference";

export const HELP_GROUPS: readonly HelpGroup[] = ["Start here", "Using VIKI Agent", "Reference"];

export interface HelpTopic {
  id: HelpTopicId;
  title: string;
  lead: string;
  body: string;
  group: HelpGroup;
  related: readonly HelpTopicId[];
  action?: "tour" | "diagnostics";
}

/** The first line starting with `# ` (not `## `), with the marker stripped. */
function titleOf(md: string): string {
  const match = /^# (.+)$/m.exec(md);
  return match ? (match[1] as string).trim() : "";
}

/** The first non-empty line after the `# ` title line, trimmed. */
function leadOf(md: string): string {
  const lines = md.split("\n");
  const titleIndex = lines.findIndex((line) => /^# /.test(line));
  for (let i = titleIndex + 1; i < lines.length; i++) {
    const line = lines[i];
    if (line !== undefined && line.trim() !== "") return line.trim();
  }
  return "";
}

export const HELP_TOPICS: HelpTopic[] = [
  {
    id: "welcome",
    title: titleOf(welcomeMd),
    lead: leadOf(welcomeMd),
    body: welcomeMd,
    group: "Start here",
    related: ["getting-started", "chats", "approvals", "troubleshooting"],
    action: "tour",
  },
  {
    id: "getting-started",
    title: titleOf(gettingStartedMd),
    lead: leadOf(gettingStartedMd),
    body: gettingStartedMd,
    group: "Start here",
    related: ["welcome", "chats", "keyboard-shortcuts"],
    action: "tour",
  },
  {
    id: "chats",
    title: titleOf(chatsMd),
    lead: leadOf(chatsMd),
    body: chatsMd,
    group: "Using VIKI Agent",
    related: ["approvals", "sources", "keyboard-shortcuts"],
  },
  {
    id: "approvals",
    title: titleOf(approvalsMd),
    lead: leadOf(approvalsMd),
    body: approvalsMd,
    group: "Using VIKI Agent",
    related: ["chats", "agents", "settings"],
  },
  {
    id: "sources",
    title: titleOf(sourcesMd),
    lead: leadOf(sourcesMd),
    body: sourcesMd,
    group: "Using VIKI Agent",
    related: ["chats", "vault"],
  },
  {
    id: "vault",
    title: titleOf(vaultMd),
    lead: leadOf(vaultMd),
    body: vaultMd,
    group: "Using VIKI Agent",
    related: ["sources", "data"],
  },
  {
    id: "code",
    title: titleOf(codeMd),
    lead: leadOf(codeMd),
    body: codeMd,
    group: "Using VIKI Agent",
    related: ["approvals", "settings"],
  },
  {
    id: "agents",
    title: titleOf(agentsMd),
    lead: leadOf(agentsMd),
    body: agentsMd,
    group: "Using VIKI Agent",
    related: ["approvals", "routines", "code"],
  },
  {
    id: "decision",
    title: titleOf(decisionMd),
    lead: leadOf(decisionMd),
    body: decisionMd,
    group: "Using VIKI Agent",
    related: ["agents", "approvals", "settings"],
  },
  {
    id: "routines",
    title: titleOf(routinesMd),
    lead: leadOf(routinesMd),
    body: routinesMd,
    group: "Using VIKI Agent",
    related: ["agents", "messaging"],
  },
  {
    id: "messaging",
    title: titleOf(messagingMd),
    lead: leadOf(messagingMd),
    body: messagingMd,
    group: "Using VIKI Agent",
    related: ["agents", "routines", "data"],
  },
  {
    id: "voice",
    title: titleOf(voiceMd),
    lead: leadOf(voiceMd),
    body: voiceMd,
    group: "Using VIKI Agent",
    related: ["settings", "chats"],
  },
  {
    id: "settings",
    title: titleOf(settingsMd),
    lead: leadOf(settingsMd),
    body: settingsMd,
    group: "Using VIKI Agent",
    related: ["voice", "data", "keyboard-shortcuts"],
  },
  {
    id: "data",
    title: titleOf(dataMd),
    lead: leadOf(dataMd),
    body: dataMd,
    group: "Using VIKI Agent",
    related: ["chats", "troubleshooting"],
  },
  {
    id: "keyboard-shortcuts",
    title: titleOf(keyboardShortcutsMd),
    lead: leadOf(keyboardShortcutsMd),
    body: keyboardShortcutsMd,
    group: "Reference",
    related: ["chats", "getting-started"],
  },
  {
    id: "troubleshooting",
    title: titleOf(troubleshootingMd),
    lead: leadOf(troubleshootingMd),
    body: troubleshootingMd,
    group: "Reference",
    related: ["data", "settings"],
    action: "diagnostics",
  },
  {
    id: "glossary",
    title: titleOf(glossaryMd),
    lead: leadOf(glossaryMd),
    body: glossaryMd,
    group: "Reference",
    related: ["welcome", "getting-started"],
  },
];

export interface HelpSearchHit {
  id: HelpTopicId;
  title: string;
  snippet: string;
}

const SNIPPET_RADIUS = 60;
const MAX_HITS = 20;

/** `±60` characters of `source` around `index`, with "…" where the slice was cut. */
function snippetAround(source: string, index: number, needleLength: number): string {
  const start = Math.max(0, index - SNIPPET_RADIUS);
  const end = Math.min(source.length, index + needleLength + SNIPPET_RADIUS);
  const prefix = start > 0 ? "…" : "";
  const suffix = end < source.length ? "…" : "";
  return prefix + source.slice(start, end) + suffix;
}

/**
 * Searches `HELP_TOPICS` for `q`: trimmed and lowercased, matched against each topic's title
 * first and its body second, at most 20 hits in topic order. Empty (after trim) matches nothing.
 */
export function searchHelp(q: string): HelpSearchHit[] {
  const needle = q.trim().toLowerCase();
  if (!needle) return [];
  const hits: HelpSearchHit[] = [];
  for (const topic of HELP_TOPICS) {
    const titleIndex = topic.title.toLowerCase().indexOf(needle);
    if (titleIndex !== -1) {
      hits.push({
        id: topic.id,
        title: topic.title,
        snippet: snippetAround(topic.title, titleIndex, needle.length),
      });
      continue;
    }
    const bodyIndex = topic.body.toLowerCase().indexOf(needle);
    if (bodyIndex !== -1) {
      hits.push({
        id: topic.id,
        title: topic.title,
        snippet: snippetAround(topic.body, bodyIndex, needle.length),
      });
    }
  }
  return hits.slice(0, MAX_HITS);
}

export const TOUR_REGIONS = [
  "left-rail",
  "new-session",
  "composer",
  "conversation",
  "right-rail",
  "topbar",
  "statusbar",
] as const;

export interface TourStep {
  title: string;
  body: string;
}

/** Splits `md` into its `## ` sections, each a step's title and trimmed body. */
export function tourSteps(md: string): TourStep[] {
  const steps: TourStep[] = [];
  let current: { title: string; body: string[] } | null = null;
  for (const line of md.split("\n")) {
    const heading = /^## (.+)$/.exec(line);
    if (heading) {
      if (current) steps.push({ title: current.title, body: current.body.join("\n").trim() });
      current = { title: (heading[1] as string).trim(), body: [] };
    } else if (current) {
      current.body.push(line);
    }
  }
  if (current) steps.push({ title: current.title, body: current.body.join("\n").trim() });
  return steps;
}

/** A backtick fence longer than any backtick run already in `text`, minimum three. */
function fenceFor(text: string): string {
  const runs = text.match(/`+/g);
  const longest = runs ? Math.max(...runs.map((run) => run.length)) : 0;
  return "`".repeat(Math.max(3, longest + 1));
}

/** The prefilled "Ask VIKI" draft text for a help page's excerpt; the caller sends it, not this. */
export function askText({
  title,
  excerpt,
  question,
}: {
  title: string;
  excerpt: string;
  question: string;
}): string {
  const cut = Array.from(excerpt).slice(0, 4000).join("");
  const fence = fenceFor(cut);
  return [
    "I have a question about VIKI Agent.",
    `Help page: ${title}`,
    `${fence}\n${cut}\n${fence}`,
    `My question: ${question}`,
  ].join("\n\n");
}

/** Loads only the public release notes shipped in Help. */
export function loadWhatsNew(): Promise<{ default: string }> {
  return import("./whats-new.md?raw");
}
