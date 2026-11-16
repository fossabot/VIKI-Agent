import { defineCatalog } from "./defineCatalog";

export const catalog = defineCatalog({
  en: {
    "magpie.title": "Magpie routing and providers",
    "magpie.help": "Manage providers, routing groups and usage in the Magpie dashboard.",
    "magpie.authority":
      "VIKI starts Magpie with its subscription accounts turned off, so Magpie doesn't read the sign-ins of Claude Code, Codex or other agents, or use subscription plans. It can change agent settings and run plugins you install. Its routing groups can retry requests or switch upstream models. Claude Code handles tools and permissions for VIKI chats.",
    "magpie.storage":
      "Provider keys entered in Magpie are saved in Magpie's own files, separate from VIKI's encrypted connection settings.",
    "magpie.version": "Magpie {version}",
    "magpie.desktop": "Dashboard follows VIKI's theme and navigation",
    "magpie.browser": "Dashboard opens in your browser",
    "magpie.open": "Open Magpie",
    "magpie.opening": "Opening Magpie…",
    "magpie.connect": "Choose a model or group",
    "magpie.connect_help":
      "Add providers in Magpie. Choose a model or routing group here, add it to VIKI, then select Use for new chats to make it the default. Magpie starts when that model is used.",
    "magpie.pick_model": "Magpie model or routing group",
    "magpie.add_model": "Add model to VIKI",
    "magpie.gateway_key": "Gateway access key",
    "magpie.gateway_key_help":
      "Optional access key for Magpie's local gateway. Manage provider API keys in Magpie.",
    "magpie.empty_models":
      "Magpie returned no models or routing groups. Add a provider in Magpie, then list models again.",
    "magpie.saved_model": "This model or group is already added to VIKI.",
    "magpie.update_key": "Update gateway key",
    "magpie.new_key": "New gateway access key",
    "magpie.save_key": "Save gateway key",
    "magpie.key_updated": "Gateway access key updated.",
    "magpie.missing":
      "Magpie is missing from this build. Installed copies of VIKI Agent include it.",
    "magpie.missing_source":
      "A copy run from source prepares it once with this command, run in the repository folder. Then open this page again.",
    "magpie.failed":
      "Couldn't open Magpie. Check that its local ports are free, then try Open Magpie again.",
    "llm.title": "Chat models",
    "llm.current_default": "Current default",
    "llm.missing_default": "Unavailable saved connection",
    "llm.native_title": "Claude Code sign-in",
    "llm.native_help":
      "Use the sign-in your Claude Code is set up with: your Claude plan, an API key or a cloud provider. Settings › Chat can limit chats to your Claude subscription. Claude Code handles VIKI chat tools and permissions.",
    "llm.advanced": "Advanced — Direct connections",
    "llm.direct_label": "Direct connection",
    "llm.direct_help":
      "Connect VIKI directly to an API endpoint or local model server. Manage these connections and keys here.",
    "llm.capacity":
      "VIKI can save up to 8 Magpie models and direct connections in total. Remove one before adding another.",
    "llm.model_review_required":
      "Review the model before sending. Your message has not been sent. Reopen this chat to check the current model.",
    "llm.reopen": "Reopen chat",
    "llm.setup": "Choose a model provider",
    "llm.help":
      "Use your Claude Code sign-in, or choose a model or routing group from Magpie. Direct connections are available in Advanced. Choose a model that supports tool calling.",
    "llm.add": "Add connection",
    "llm.name": "Connection name",
    "llm.url": "API base URL",
    "llm.mode": "API format",
    "llm.model": "Model ID",
    "llm.key": "API key (optional for local servers)",
    "llm.update_key": "Update API key",
    "llm.new_key": "New API key",
    "llm.save_key": "Save API key",
    "llm.cancel_key": "Cancel",
    "llm.key_updated": "API key updated.",
    "llm.key_scope":
      "Open chats keep their current key until you close and reopen them. Leave the key empty for a local server that needs no key.",
    "llm.local": "Local server",
    "llm.cloud": "Cloud or custom API",
    "llm.local_help":
      "Start your model server first. Ollama uses port 11434; LM Studio uses port 1234. Use your provider or server's API base URL, including its API prefix, such as /v1. Choose the API format supported by that provider or server. Use HTTPS for remote servers.",
    "llm.test": "Test connection",
    "llm.models": "List models",
    "llm.discovering": "Listing models…",
    "llm.test_help":
      "Testing sends a short prompt to this endpoint and does not save the connection. Provider charges may apply. Save the connection to keep it, then choose Use for new chats to make it the default.",
    "llm.testing": "Testing connection…",
    "llm.tested": "The endpoint returned a complete response.",
    "llm.save": "Save connection",
    "llm.saving": "Saving…",
    "llm.use": "Use for new chats",
    "llm.default": "Default for new chats",
    "llm.remove": "Remove connection",
    "llm.subscription": "Use Claude Code sign-in for new chats",
    "llm.scope":
      "Connections are saved on this computer. The default applies to new chats, including chats started by automation. Agents › Profiles chats, routine runs and inbox suggestions use Claude Code models; a Bot can use a connection its profile pins in Bot settings. Choose a connection before a chat starts; a running chat keeps its connection. Removing a connection keeps its history but prevents those chats from continuing after their process closes.",
    "llm.keys_help":
      "Keys saved in this connection form are encrypted on this computer and are not included in settings exports.",
    "llm.failed": "Couldn't complete this request. Check the connection and try again.",
    "llm.stale": "Settings changed in another window. Reload the connections before saving again.",
    "llm.reload": "Reload connections",
    "llm.loading": "Loading model connections…",
    "llm.current_connection": "Saved connection",
    "llm.native": "Claude Code model",
    "llm.configure": "Manage connections in Settings → Providers.",
  },
});
