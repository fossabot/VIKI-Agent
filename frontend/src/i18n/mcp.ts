import { defineCatalog } from "./defineCatalog";

export const catalog = defineCatalog({
  en: {
    "mcp.setup.title": "Add an MCP server",
    "mcp.setup.back": "Back to servers",
    "mcp.setup.inherited":
      "VIKI Agent uses the MCP servers configured in your own Claude Code. Claude Code manages their authentication and tool permissions.",
    "mcp.setup.atlassian": "Atlassian · Jira and Confluence",
    "mcp.setup.scope": "Use this server in",
    "mcp.setup.scope.local": "This folder, for me",
    "mcp.setup.scope.user": "All my folders",
    "mcp.setup.scope.project": "This project, shared through .mcp.json",
    "mcp.setup.folder":
      "Run the command in a terminal in the folder used by your chat. Use the same Claude Code installation as VIKI Agent.",
    "mcp.setup.authorize":
      "Run claude in that folder, open /mcp, and follow the server's approval and sign-in steps.",
    "mcp.setup.restart":
      "Start a new VIKI chat in the same folder and send a message. Open MCP to check the connection and available servers.",
    "mcp.setup.refresh":
      "Refresh checks the running chat; it does not load newly added configuration.",
    "mcp.setup.copy_command": "Copy Atlassian setup command",
    "mcp.setup.json": "Custom servers and JSON",
    "mcp.setup.json_help":
      "For project servers, use .mcp.json in the chat's folder with a top-level mcpServers object. Claude Code does not use VS Code's .vscode/mcp.json as its project configuration.",
    "mcp.setup.http": "Remote HTTP server",
    "mcp.setup.http_help": "Replace the example name and URL with your server's setup details.",
    "mcp.setup.stdio": "Local command server (stdio)",
    "mcp.setup.stdio_help":
      "Use an installed executable and a separate list of arguments. Claude Code starts it when loading the server.",
    "mcp.setup.copy_http": "Copy HTTP server JSON",
    "mcp.setup.copy_stdio": "Copy stdio server JSON",
    "mcp.where.title": "Where to put .mcp.json",
    "mcp.where.copy_path": "Copy folder path",
    "mcp.where.start":
      "New chats start in this folder. Claude Code reads the .mcp.json at its top level:",
    "mcp.where.start_unknown":
      "New chats start in the folder under Your vault in Settings › Workspaces and Vault. Claude Code reads the .mcp.json at its top level.",
    "mcp.where.chat_only":
      "In chat-only mode, chats start in VIKI Agent's own folder. Choose All my folders below, or set a vault in Settings › Workspaces and Vault.",
    "mcp.where.code":
      "A chat in a code folder starts in that folder, or in its worktree, and reads the .mcp.json there.",
    "mcp.where.added":
      "Code folders ticked for new chats, and a Bot's code copy, add their files, skills, commands, agents and plugin choices. Claude Code doesn't read their .mcp.json.",
    "mcp.where.new_chat":
      "A running chat keeps the servers it started with. After you add or change .mcp.json, start a new chat.",
    "mcp.where.switches":
      "The switches in a chat's MCP servers dialog turn a server on or off for that chat's folder. Claude Code saves that choice in its own ~/.claude.json; VIKI Agent writes nothing there.",
    "mcp.where.profile":
      "Servers named viki_… come from an agent profile's MCP configuration in Bots › Bot settings. VIKI Agent adds them only to that profile's chats in an Ordinary workspace.",
    "mcp.profile.help":
      "Profiles inherit Claude Code's servers. These entries add servers for this profile; their live names start with viki_. App-profile servers require an Ordinary workspace.",
    "mcp.import.title": "Import MCP JSON",
    "mcp.import.help":
      "Paste a Claude mcpServers object or a VS Code servers object. Import adds entries to this draft; save the profile after reviewing them. Up to 8 servers and 64 KiB of JSON.",
    "mcp.import.auth_help":
      "For servers needing OAuth, environment variables or authentication headers, use Claude Code setup. Keep credentials out of profile definitions.",
    "mcp.import.json": "MCP JSON",
    "mcp.import.add": "Add from JSON",
    "mcp.import.pending": "Checking JSON…",
    "mcp.import.success":
      "Added to the draft. Review the entries and save the profile to use them.",
    "mcp.import.invalid_json": "Use valid JSON with no duplicate keys.",
    "mcp.import.unsupported":
      "Import accepts command, args, type and url only. Configure other fields, credentials and variable references in Claude Code.",
    "mcp.import.duplicate":
      "A server with this name is already in the draft. No entries were added.",
    "mcp.import.count": "This would exceed 8 servers. No entries were added.",
    "mcp.import.failed": "Could not import these servers. Check the JSON and try again.",
  },
});
