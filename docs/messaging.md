# Slack and Discord messaging

The optional Slack and Discord bridges let one configured owner send text and answer current
requests in a private direct message. Each uses a separate conversation, pinned to the workspace
selected when VIKI Agent launched. Desktop chats and the other providers remain separate.

- **`auto` mode:** each conversation uses Claude Code's `auto` permission mode. Claude Code's
  classifier reviews its actions, and a request it still makes arrives as a permission card.
- **Manual mode:** where `auto` mode is unavailable, Claude Code starts the conversation in Manual
  mode, and every action arrives as a card.

Set up and turn on each bridge from **Messaging**. Saved settings apply without restart.

1. Select **Set up** on its card, or select the provider's name to open the card.
2. Fill in its fields.
3. Select **Save changes**.
4. Turn the bot on with the switch beside its status.

A bot you leave on reconnects on its own the next time VIKI Agent starts in the workspace you
turned it on in. In any other workspace it stays off until you turn it on there. Mattermost setup
is in [Mattermost mobile access](mattermost.md).

Cards start closed. An open card also shows whether anything is saved: **Needs setup**,
**Saved settings**, or **Settings unreadable**. The status, the switch and any stop reason, error
or cooldown stay visible while a card is closed.

| Status | What to do |
|---|---|
| **Not configured** | Select **Set up**, fill in the fields and select **Save changes**. |
| **Disabled** | Turn the bot on with the switch when you want to connect. |
| **Connecting** | Wait for authentication and the bot's ready message. |
| **Connected** | Send fresh text in the configured owner's DM. |
| **Stopped** | Follow the card's reason: fix what it names, then turn the bot on again after cleanup and any displayed cooldown. |
| The card says "Restart VIKI Agent to use this bot again with the current workspace." | Restart VIKI Agent. This happens when the startup workspace changed or the bridge could not shut down cleanly. Until then its switch stays visible but does nothing. |

- **Refresh status:** reads state without connecting.
- **Turning a bot off:** stops that bridge, invalidates its current requests and closes its
  dedicated conversation. You can turn it on again in the same launch.
- **Tokens and other providers:** these controls do not revoke provider tokens or stop another
  provider.
- **New connections:** each new connection waits for fresh messages. It does not pick up offline
  messages or retry a previous turn.

A stopped card says why. For example:

- Slack rejected the bot or app token, or Discord rejected the bot token.
- The service could not be reached. A network refusal during Slack's Socket Mode setup or Discord's
  Gateway lookup appears as an unreachable service.
- The account check failed.
- The bot reached a safety limit.
- Its conversation reported an error or was closed.
- A request changed or could not be shown completely.
- An unexpected error occurred.
- The connection ended after it was connected.

A bot that had posted its ready message also posts one line saying why in the direct message, best
effort within 3 seconds. The reason stays until you turn the bot on again or save or remove the
settings. While a card is connecting or connected, it updates on its own.

## Before connecting

Keep the desktop host awake and VIKI Agent running:

- **Windows and Linux:** keep an app window open.
- **macOS:** in a browser window, keep that browser running; in VIKI Agent's own window,
  keep a window open.

A code startup workspace must be persistent and ordinary. Selecting another workspace in the app
does not move a bridge: it keeps working in the workspace VIKI Agent started with. VIKI Agent checks
that workspace again before the bridge starts a conversation.

> [!IMPORTANT]
> Prompts, final replies, complete operation requests and, after you send `!viki questions on`,
> questions from your other desktop chats leave this computer through a bridge. Slack or Discord
> and signed-in clients can retain them under their own policies. Local Reset and exports do not
> erase remote copies.

Service operators and anyone controlling the configured owner account are part of this trust
boundary. Use an account you control, enable MFA, and review the service's retention policy.

How tokens and network access are handled:

- **Tokens:** bot and app tokens are sealed in the local app database and are never shown again,
  only a placeholder.
- **Status and settings card:** Messaging status contains no tokens, account IDs, server URLs or
  message text. The settings card can show the saved non-secret values (such as a member or user
  ID), but never a token.
- **Network:** the local listener stays on loopback; these bridges make outbound HTTPS and WSS
  connections. Do not expose the local port to reach the bot.

## Slack setup

Use an internal Slack app in the configured owner's workspace. The owner and bot must belong to the
same workspace, with a private two-person DM; shared and multi-person conversations are refused.

In Slack:

1. Follow Slack's [Socket Mode setup](https://docs.slack.dev/apis/events-api/using-socket-mode/)
   to create the app, enable Socket Mode and generate an app-level token.
2. Give the bot these scopes: `users:read`, `im:write`, `im:read`, `im:history` and `chat:write`.
3. Subscribe only to the bot event `message.im`.
4. Install or reinstall the app in that workspace to apply its permissions.
5. Give the app-level token `connections:write`.

In the Slack card in **Messaging**:

1. Use the installed bot's `xoxb-` token for **Bot token**.
2. Use the app-level token for **App token**.
3. Copy the active human owner's member ID, not the bot ID, display name or email, into
   **Your member ID**. Slack's
   [member ID instructions](https://slack.com/help/articles/360003827751-Create-a-link-to-a-members-profile)
   describe **More → Copy member ID** in a profile.
4. Select **Save changes**. Save refuses a **Bot token** that does not start with `xoxb-` or an
   **App token** that does not start with `xapp-`, and says which.
5. Turn the Slack bot on.
6. Wait for the ready message in the owner's DM with the bot.

Send new top-level text there.

- **Refused:** Slack thread replies, edited messages, files and attachments.
- **Replies:** replies from VIKI Agent stay top-level and use Slack mrkdwn for readable paragraphs,
  headings, emphasis and code.
- **Links:** URL-like text is kept inert; Slack link and media unfurls are disabled.
- **New connection requests:** Socket Mode periodically requests a new connection. VIKI Agent stops
  the Slack bridge then; turn the Slack bot on again manually.
- **Posting pace:** posting is paced at one new post per second. A rate-limit response stops the
  bridge until its cooldown expires.
- **Message history:** the bridge uses
  [conversations.history](https://docs.slack.dev/reference/methods/conversations.history/) only to
  confirm the one live message it received.
- **App distribution:** Slack's limits depend on app distribution. This setup is for an internal
  app, not a commercially distributed Marketplace integration.

## Discord setup

Set up a Discord application with a bot, then connect it from the Discord card in **Messaging**.
This bridge does not provide slash commands or an interaction endpoint.

1. Create an application with a bot using Discord's
   [application and bot setup](https://docs.discord.com/developers/quick-start/getting-started).
2. Arrange a DM between that bot and the configured human owner. Installation and DM availability
   depend on Discord's app and account settings.
3. In the Discord card's **Bot token** field, use the bot token from the application's **Bot** page.
   Do not use a human account token, OAuth access token, application public key or webhook token.
4. Copy the human owner's numeric user ID into **Your user ID**, using Discord's
   [Developer Mode ID tools](https://support.discord.com/hc/en-us/articles/206346498-Where-can-I-find-my-User-Server-Message-ID).
   Use a different non-bot account from the authenticated bot.
5. Fill in **Client URL**. It must be a public HTTPS app or support page without embedded
   credentials, whitespace or control characters.
6. Select **Save changes**.
7. Turn the Discord bot on, and wait for the bot's ready DM.

The **Client URL** is included only in the required HTTP header,
`DiscordBot (<client-url>, <installed-version>)`; VIKI Agent does not fetch it. See Discord's
[User-Agent requirements](https://docs.discord.com/developers/reference#user-agent).

Send plain new top-level text.

- **Refused:** guild messages, group DMs, webhooks and self-bot authentication. Edited messages,
  references/replies, forwarded messages, files, embeds, components and polls are refused too.
- **Replies:** replies are top-level posts with readable paragraphs, lists, emphasis and code.
- **Inert text and size:** URL-like text stays inert, mentions are suppressed, and posts are capped
  at 2000 UTF-16 units each.
- **Connection:** the connection uses Discord REST and Gateway v10 with `DIRECT_MESSAGES` intent
  only.
- **Stops:** missing heartbeat acknowledgment, reconnect/invalid-session requests or socket loss
  stop the bridge. Turn the Discord bot on again manually after cleanup and any displayed cooldown.
- **Discord's limits:** Discord's REST pacing, session-start quota and five-second Identify window
  remain in force when you disable and reconnect. There is no automatic Resume or reconnect within
  a launch.

## Text and current approvals

Ordinary text starts the dedicated conversation. The bot posts the final reply of every turn of its
conversation while it is active, including a turn Claude Code starts by itself, such as a
background agent's report.

- **While Claude is working:** a message you send is added to the running turn, and the bot replies
  "Added to the running turn."
- **Conversation busy:** if the turn ends first, or after `!viki stop` or `!viki new` until the
  stopped turn has ended, the bot replies that the conversation is busy. Send the message again
  after the turn ends.
- **Bridge commands:** `!viki help`, `!viki status`, `!viki new` and `!viki stop` affect this
  bridge's conversation. While a turn runs, `!viki status` shows its status line; otherwise it says
  the conversation is ready or busy.
- **Refused:** Claude Code's slash commands and other `!` commands.
- **Mode, model and workspace:** Messaging does not change the conversation's mode, model or
  workspace.

### Answer a request

Current permission requests offer **Allow once** and **Deny** through `!viki allow <nonce>` and
`!viki deny <nonce>`. A fully displayed question lists its questions with numbered options.

- **Pick:** when a fully displayed question's instructions include a Pick line, it accepts a pick
  through `!viki pick <nonce> <picks>`: one option number per question, in order, with numbers
  joined by commas where a question allows more than one, like `1,3`.
- **Your own words:** a fully displayed question also accepts free-text **Other** through
  `!viki answer <nonce> <free-text>`, which sends your own words as your reply instead of a pick.
- **Nonce:** copy the nonce from the current request. It is single-use and has no time limit.
- **Waiting:** a request waits until you answer it, send `!viki stop` or `!viki new`, or the bot
  stops.
- **Incomplete details:** the bridge requires complete operation details before offering approval;
  missing or oversized permission details offer Deny only.

When a request of the bot's own conversation is the only request waiting as your message arrives,
you can answer in plain text instead:

- **Permission:** `allow` or `deny`.
- **Question:** option numbers (one per question, commas where a question allows several) or your
  own words.

While a request of the bot's own conversation waits beside another request (a relayed desktop
question counts), nothing is sent to Claude. Use the command shown under the request you mean.

### Questions from desktop chats

Send `!viki questions on` to also receive questions from your other desktop chats here, and
`!viki questions off` to stop.

- **At every connection:** relaying desktop chat questions starts off.
- **Questions already waiting:** `!viki questions on` also posts any question already waiting, from
  every desktop chat except any messaging bridge's own conversation.
- **Permission cards:** permission cards from those chats stay on the desktop.
- **Nonces:** a desktop question's nonce works for as long as the question waits.
- **Limits:** at most 8 are held at once. A question too long to show posts a fixed notice with no
  nonce.
- **When this bot stops:** turning this bot off or stopping it leaves those chats' questions open on
  the desktop; it never denies them.

### Formatting and delivery

Chat replies and status or instruction messages keep their supported formatting across providers.

- **Slack:** maps common headings and emphasis to mrkdwn.
- **Mattermost and Discord:** use their native Markdown.
- **Neutralized syntax:** URL-like text is rendered as inert code, and mention, HTML, image, and
  unsafe control syntax is neutralized.
- **Operation and plan details:** remain complete literal JSON.
- **Question details:** a question's detail is a readable post listing its questions and options
  instead.
- **Choice instructions:** sent only after the complete request has reached the DM.
- **Split posts:** provider posts may split with continuation labels.
- **Shortened replies:** shortened assistant replies direct you to the desktop.
- **Not sent:** thinking, general tool output and other desktop conversations, except a desktop
  chat's questions once you turn on `!viki questions on`.

If sending stops partway through a request, it does not grant a mobile approval. A post may have
reached the service before the bot was turned off or a response was lost. Check the DM and desktop
when delivery is uncertain; there is no automatic resend or choice retry.

### Mattermost-only features

- **Reactions and progress:** thumbs reactions on permission requests and the progress display
  (👀, typing and the status reply) are Mattermost-only; see
  [Mattermost mobile access](mattermost.md).
- **Bot routine notices:** named Bot routine notices are Mattermost-only, with their separate
  launch consent. Slack and Discord do not receive them. See
  [Bot routine notices](mattermost.md#bot-routine-notices).

## Remove configuration and roll back

Select **Remove saved settings** in a provider's card to delete its settings from this computer.
This also turns that bot off.

- **Tokens:** **Remove saved settings** does not revoke the provider's tokens. They keep working
  until you revoke or rotate them in that provider's app settings, before or after Remove.
  Revoking or rotating a token does not erase messages the service or its clients already keep.
- **Rolling back:** for rolling back to an older build, see the rollback steps in
  [Security and privacy](security.md#rolling-back-after-an-upgrade).
- **Records:** session and approval records use the app's normal retention and reset controls.

How a post looks depends on your Slack workspace or Discord server and the app you read it in.
Installing the Discord bot depends on Discord's own settings. VIKI Agent hasn't been checked
against every version.
