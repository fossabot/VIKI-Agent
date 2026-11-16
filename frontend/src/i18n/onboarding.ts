/**
 * The onboarding view and its shared `PathForm` (also used by the Data view's export
 * destination field). Field-level backend codes (invalid_name, the path codes,
 * unsupported_location's reasons) render through `errors.workspaces.ts`'s `field.<code>` rows,
 * not from here.
 */

import { defineCatalog } from "./defineCatalog";

export const catalog = defineCatalog({
  en: {
    "onboarding.title": "Welcome to VIKI",
    "onboarding.choose.create_desc":
      "Create a new vault folder that Claude Code and VIKI keep in sync.",
    "onboarding.choose.open_desc": "Register an existing folder as a read-only vault.",
    "onboarding.choose.later_desc": "Skip for now and keep working in the shared temporary folder.",
    "onboarding.back": "Back",

    "onboarding.guide.lead":
      "Steps 1 and 2 let you start chatting. Step 3 is needed for notes, Bots, Kanban and the persona, and everything under Optional can wait.",
    "onboarding.guide.steps": "Setup steps",
    "onboarding.guide.state.checking": "Checking…",
    "onboarding.guide.state.done": "Done",
    "onboarding.guide.state.needed": "Needed",
    "onboarding.guide.state.unchecked": "Not checked yet",
    "onboarding.guide.state.waiting": "After step 1",
    "onboarding.guide.state.restart": "Restart needed",
    "onboarding.guide.claude.title": "Install Claude Code",
    "onboarding.guide.claude.body":
      "VIKI Agent runs every chat through Claude Code, Anthropic's command-line tool, on this computer. VIKI Agent doesn't install or change it.",
    "onboarding.guide.claude.found": "Found Claude Code {version}.",
    "onboarding.guide.claude.found_unknown": "Found Claude Code.",
    "onboarding.guide.claude.older":
      "This version is older than the ones VIKI Agent was tested with ({min} to {max}). Update it, then select Check Claude Code in Settings › Providers.",
    "onboarding.guide.model.title": "Sign in to Claude, or connect a model",
    "onboarding.guide.model.body":
      "Use your Claude plan through Claude Code's own sign-in, or connect your own model: a cloud API or a local model server. An API key or cloud provider set up in Claude Code works too.",
    "onboarding.guide.model.waiting":
      "Finish step 1 first. Chats on a model connection run through Claude Code too.",
    "onboarding.guide.model.connection_version":
      "Model connections need Claude Code {min} or later, and this one isn't confirmed to be. Update Claude Code, then select Check Claude Code in Settings › Providers.",
    "onboarding.guide.model.unchecked":
      "VIKI Agent checks your sign-in when you start a chat. Not signed in to Claude Code yet? Select Sign in.",
    "onboarding.guide.model.signed_out":
      "Claude Code isn't signed in. Select Sign in, or connect a model.",
    "onboarding.guide.model.signed_in": "Signed in to Claude Code.",
    "onboarding.guide.model.connection_missing":
      "New chats are set to use a model connection that no longer exists. Choose another default model, or connect a model, in Settings › Providers.",
    "onboarding.guide.model.connection_unread":
      "New chats are set to use a model connection, but the list of connections couldn't be read here. Check it in Settings › Providers.",
    "onboarding.guide.model.connection": "New chats use your model connection.",
    "onboarding.guide.vault.title": "Create or open a vault",
    "onboarding.guide.vault.body":
      "A vault is a folder of Markdown notes that you and VIKI share. Notes, Bots, Kanban and the persona need one. Without one, chats still run in a shared temporary folder.",
    "onboarding.guide.vault.done": "Using {name}.",
    "onboarding.guide.vault.restart":
      "Your vault is registered. Restart VIKI Agent to start using it.",
    "onboarding.guide.optional.title": "Optional, any time",
    "onboarding.guide.optional.bots":
      "Bots and Kanban: a team of assistants and its tasks, under Bots and Kanban in the left rail. They need an active vault. Start with VIKI adds the team to a new vault.",
    "onboarding.guide.optional.connections":
      "Model connections: a cloud API or a local model server, alongside or instead of your Claude subscription. Settings › Providers.",
    "onboarding.guide.optional.code":
      "Code folders: let chats work in your projects. Settings › Workspaces and Vault.",
    "onboarding.guide.optional.routines":
      "Routines: prompts VIKI runs on a schedule or on demand while VIKI Agent is open. Routines in the left rail.",
    "onboarding.guide.optional.voice":
      "Voice: dictate messages and hear replies read aloud. Settings › Chat.",
    "onboarding.guide.optional.messaging":
      "Messaging: chat with VIKI from Mattermost, Slack or Discord. Messaging in the left rail.",
    "onboarding.guide.later":
      "Open this guide again from the Help menu (?) › Setup guide. Take the tour from the same menu to learn the rest of the window.",
    "onboarding.guide.menu": "Setup guide",
    "onboarding.signin.line":
      "Sign in opens Claude Code's own sign-in in a PowerShell window, and your browser shows the Claude sign-in page.",
    "onboarding.signin.line_mac":
      "Sign in opens Claude Code's own sign-in in a Terminal window, and your browser shows the Claude sign-in page.",
    "onboarding.signin.line_linux":
      "Sign in opens Claude Code's own sign-in in your desktop's terminal, and your browser shows the Claude sign-in page.",
    "onboarding.signin.opened":
      "Finish signing in in your browser. VIKI Agent checks your sign-in when you start a chat.",

    "onboarding.create.name_label": "Name",
    "onboarding.create.parent_label": "Parent folder",
    "onboarding.create.folder_label": "Folder name",
    "onboarding.create.submit": "Create",
    "onboarding.create.created_files_heading": "Files created",
    "onboarding.create.portable_metadata": "Write portable metadata",
    "onboarding.create.stalled": "A previous creation is still in progress.",
    "onboarding.create.retry_lock_reason":
      "Retry continues this setup when its starter files and setup details still match.",
    "onboarding.create.retry_cancel_reason":
      "Cancel this setup to choose again. If the starter files or setup details changed, start a new setup.",
    "onboarding.create.occupied":
      "That folder already holds something, or is already registered as a vault.",
    "onboarding.create.open_existing_button": "Open as an existing folder",

    "onboarding.open.path_label": "Folder path",
    "onboarding.open.submit": "Open",
    "onboarding.open.registered_readonly": "This folder is registered as read-only.",

    "onboarding.select.selecting": "Selecting…",
    "onboarding.select.restart_required": "Switches on the next launch.",
    "onboarding.select.failed_registered":
      "The vault is registered. To use it on the next launch, select it from the Workspaces page.",
    "onboarding.select.go_to_workspaces": "Go to the Workspaces page",

    "field.example_path": "D:\\VIKI\\Vaults\\My notes",
    "field.example_path_mac": "/Users/you/VIKI/Vaults/My notes",
    "field.example_path_linux": "/home/you/VIKI/Vaults/My notes",

    "onboarding.picker.browse": "Browse…",
    "onboarding.picker.reason.unsupported":
      "This computer can't show a folder picker, so type the path.",
    "onboarding.picker.reason.no_program":
      "Install zenity or kdialog to pick folders, or type the path.",
    "onboarding.picker.reason.no_display":
      "No display is available for a folder picker, so type the path.",
    "onboarding.picker.reason.unknown":
      "The folder picker isn't available right now, so type the path.",
  },
});
