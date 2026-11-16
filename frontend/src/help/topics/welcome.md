# Welcome to VIKI Agent

VIKI Agent is a local workspace that runs Claude Code on your own Claude sign-in or a model you connect.

## What VIKI Agent is

VIKI Agent is a productivity app: an AI assistant for your notes, code and tasks. Its replies come from the model you select, through your own Claude Code, and can be wrong.

VIKI Agent starts the Claude Code CLI already installed on your computer, on the sign-in it is set up with, such as your Claude plan or an API key, or on your own model. It uses the same CLAUDE.md instructions, skills, hooks and permission rules a terminal would use, except that a Bot skips your vault's CLAUDE.md and AGENTS.md and gets its own instructions instead. You see it in VIKI Agent's own window or in an Edge, Chrome or Chromium app window. Window title text is hidden; the native window controls stay available, and chat names and attention indicators appear inside the app. VIKI is the default name for the assistant; change it with **Assistant name** in **Settings** › **General**. A workspace can be a vault of Markdown notes, a code folder, or chat only, with no workspace at all.

## What it is not

VIKI Agent is independent and is not affiliated with or endorsed by Anthropic.

Use Claude Code's own sign-in, or set up your own model, a cloud API or a local model server, in **Providers**. Custom model connections send inference through VIKI's local gateway to your chosen endpoint. Claude Code acts with your account's own rights, under whatever mode and rules it runs; it is not a sandbox. Routines and messaging bridges only work while VIKI Agent is open. Your chats live in Claude Code's own transcripts on this computer; Claude Code's own settings decide how long it keeps them.

## What you need

You need Claude Code [installed](https://code.claude.com/docs/en/setup). For the subscription route, use a Claude plan that includes Claude Code and sign in: run `claude`, then `/login`. A **Sign in** button appears in the setup guide and in the chat on Windows, macOS and Linux when you still need one: it opens Claude Code's own sign-in in a PowerShell window on Windows, a Terminal window on macOS, or your desktop's terminal on Linux. On Linux without a terminal emulator VIKI Agent can use, or if the window does not open on any OS, run `claude auth login` yourself in a terminal. Custom model connections use their own API key or a local server. You also need Windows 11, macOS on Apple silicon, or desktop Linux. Saving notes needs Windows, an APFS drive on macOS, or an ext4, XFS, Btrfs or tmpfs drive on Linux.

## Your first five minutes

1. On **Welcome to VIKI**, follow the setup guide: **Install Claude Code**, select **Sign in** or connect a model, then choose **Create a vault**, **Open a folder**, or **Set up later**. Open the guide again from **Help** › **Setup guide**.
2. Creating a vault offers **Start with VIKI (recommended)**, which also adds your team's Bots, or **Blank folder**; opening a folder registers it read-only.
3. Select **New session** at the top of the left rail, or press `Alt`+`N`.
4. The new chat opens with a greeting. Type your message and press `Enter`.
5. On a **Waiting for approval** card, read the tool and its target, then choose **Allow once** or **Deny**.
6. Read the chips under the reply to see what VIKI used. Press `?` for shortcuts, or take the tour from the Help menu.

## Things to try

Not sure what to ask? Start with one of these, in your own words:

- "Give me a short tour of this vault: its main folders and what they hold."
- "Find my notes about weekly planning and summarize them."
- "Draft a note about today's meeting, and show it to me before you save it."
- In a code folder: "Explain how this project is organized, then suggest one small improvement."
- "Plan how to tidy up my notes folder, and wait for my go-ahead before changing anything."

## Common questions

### Am I talking to a person?
No. You are talking to an AI assistant. Its replies come from the model you select, through your own Claude Code, and can be wrong.

### Does VIKI Agent talk to Anthropic directly?
No. Claude Code does, the same way it would from a terminal.

### Do I need an API key?
Not from VIKI Agent. Chats use the sign-in your Claude Code is set up with: your Claude plan, or an API key or cloud provider if you set one up in Claude Code. You can also add your own model in **Providers**. Set the sign-in in Claude Code's own settings: VIKI Agent does not pass `ANTHROPIC_*` and `CLAUDE_CODE_*` variables from your shell, such as `ANTHROPIC_API_KEY` or `CLAUDE_CODE_USE_BEDROCK`, to Claude Code.

### What if I am not ready to set up a vault?
Choose **Set up later**. You can keep chatting and pick a workspace another time.

### Where are my Bots?
Bots need an active vault. **Start with VIKI (recommended)** adds the team when you create the vault. The Bots appear under **Bots** after the restart that starts using the vault.

### Is anything VIKI Agent does sandboxed?
No. Read every **Waiting for approval** card before choosing **Allow once**.

### Where do my chats go?
Into Claude Code's own transcripts on this computer; Claude Code's own settings decide how long it keeps them.
