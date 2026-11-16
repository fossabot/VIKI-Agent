# Mattermost mobile access

The optional Mattermost bridge lets one owner use a standard Mattermost mobile app to send prompts
to a dedicated VIKI Agent conversation and answer its current requests. No Mattermost plugin,
webhook, or additional package installation is required.

VIKI Agent makes outbound HTTPS and WSS connections to the configured server. Its local listener
stays on loopback; do not forward its port or expose it through a tunnel.

Open **Messaging** in the sidebar to set up and connect the bridge:

1. Select **Set up** on the Mattermost card.
2. Fill in its fields.
3. Select **Save changes**. Saved settings apply without restart.
4. Turn the bot on with its switch to connect.

What each status means:

- **Not configured:** nothing is saved yet.
- **Disabled:** it is saved and off.
- **Connecting:** it is authenticating.
- **Connected:** appears after authentication, freshness checks and the ready message.
- **Stopped:** it could not start or continue. The card says why, for example that Mattermost
  rejected the bot token, and what to do next. The reason stays until you turn the bot on again or
  save or remove its settings.

While a card is connecting or connected, it updates on its own. **Refresh status** reads state
without connecting.

A bot you leave on reconnects on its own the next time VIKI Agent starts in the workspace you turned
it on in. In any other workspace it stays off until you turn it on there.

The status shows no token, server URL or account ID. The card can show the saved Server URL and user
ID, but never the bot token again, only a placeholder. Slack and Discord setup is in
[Messaging](messaging.md).

> [!IMPORTANT]
> Prompts, assistant replies, complete operation requests and, after you send `!viki questions on`,
> questions from your other desktop chats leave the desktop host through this bridge. They are
> retained under the Mattermost server's policy. Server operators and anyone controlling the
> configured owner account can act as that owner.

Choose a server you trust, review its retention policy, and enable MFA for the owner account.

- **Server retention:** Mattermost's
  [retention documentation](https://docs.mattermost.com/administration-guide/comply/data-retention-policy)
  describes default soft deletion. Deleting a message in the UI does not establish database
  erasure.
- **Mobile clients:** they can also store messages while signed in; see Mattermost's
  [mobile FAQ](https://docs.mattermost.com/deployment-guide/mobile/mobile-faq).
- **Launch token:** VIKI Agent's local launch token is not sent to Mattermost.

## Set up the bot and owner

Use a Mattermost server with a certificate that the desktop client verifies.

- **Reachability:** both the desktop host and phone need to reach its HTTPS service. A private
  server can be reached through your VPN; VPN reachability does not replace certificate
  verification.
- **Base URL:** configure the server's base URL, including a deployment prefix if it has one,
  without `/api/v4`.
- **Not supported:** redirects, environment proxies, cookie authentication, and custom
  authentication headers are not supported by the bridge.

Set up the bot:

1. Ask a Mattermost system admin to enable bot creation under **System Console >
   Integrations > Bot Accounts**.
2. In the Product menu, open **Integrations > Bot Accounts**.
3. Select **Add Bot Account**, and create a bot with the **Member** role. Leave the extra
   all-channel posting permissions off. The bridge refuses a System Admin bot.
4. Copy the generated **access token** before closing the success screen. Use the token value,
   rather than its Token ID. See Mattermost's
   [bot account setup](https://docs.mattermost.com/developers/integrate/reference/bot-accounts).
5. Add the bot to the owner's team through the team menu's **Invite People > Invite
   Member** action, as described in the bot account setup guide.

Set up the owner:

1. Select the active human account that will use the bridge.
2. Under **System Console > User Management > Users**, open that user's configuration and copy its
   user ID. Use this human ID, rather than the bot ID, username, email, or display name. The bridge
   accepts a 26-character ID containing lowercase letters and digits. See
   [user ID lookup](https://docs.mattermost.com/administration-guide/configure/user-management-configuration-settings#identify-a-users-id).
3. Enable MFA for that human account through **Profile > Security > Multi-factor
   Authentication** if the admin has enabled Mattermost MFA. For SSO, use your identity provider's
   MFA. See Mattermost's
   [MFA guide](https://docs.mattermost.com/administration-guide/onboard/multi-factor-authentication).

The bridge requires an active bot, a different active non-bot owner, and their direct message.

## Configure and enable

Enter the server, owner and bot details on the Mattermost card, then turn the bot on.

1. Open **Messaging** and select **Set up** on the Mattermost card.
2. Fill in **Server URL**, **Your user ID** and **Bot token**.
3. Select **Save changes**. Saved settings apply without restart.
4. Turn the bot on with its switch.

### Field rules

- **Server URL:** for a server under a deployment prefix, include that prefix in the Server URL.
  The URL must use HTTPS and a simple ASCII path, with no username, password, query, fragment,
  percent encoding, dot segments, spaces, or backslashes.
- **First save:** all three fields are required the first time. An incomplete or invalid save is
  refused, and nothing is stored or stopped.
- **Later saves:** leaving **Bot token** empty keeps the token already saved for that same Server
  URL. Changing the Server URL requires entering the bot token again, so a saved token is never
  sent to a server it was not entered for.
- **New bot token:** a new bot token must be 26 lowercase letters and digits, the form of a
  Mattermost access token. Anything else is refused with a hint and is not stored.
- **Token saved earlier:** a token saved earlier is not checked again. If Mattermost rejects it,
  the card reads **Stopped** and says so.
- **Token storage:** the bot token is sealed in the local app database and is never shown again,
  only a placeholder.

### Desktop host and workspace

Keep the desktop host awake and VIKI Agent running:

- **Windows and Linux:** keep an app window open.
- **macOS:** in a browser window, keep that browser running; in VIKI Agent's own window,
  keep a window open.

The bridge works in the workspace and folder selected when VIKI Agent started. A code-folder startup
workspace must be persistent and ordinary. Changing the desktop's selected workspace does not move
the bridge conversation.

If the startup folder moves, or its registration or policy changes, the bridge refuses to start a
conversation there. Fix the problem, then restart VIKI Agent.

### Permission mode

Each fresh bridge conversation starts in Claude Code's `auto` permission mode on the sign-in
Claude Code is set up with, with your instructions, hooks and Claude Code's own permissions.

- **`auto` mode:** Claude Code's classifier reviews actions instead of asking you. A request it
  still makes, such as one matching an ask rule or a protected path, arrives in the DM as a
  permission card.
- **Manual mode:** where `auto` mode is unavailable, Claude Code starts the conversation in Manual
  mode instead, so every action arrives in the DM as a permission card.

## Use the mobile direct message

Talk to the bridge's dedicated conversation in a direct message with the bot.

1. In the standard
   [Mattermost iOS or Android app](https://docs.mattermost.com/end-user-guide/access/access-your-workspace),
   sign in as the configured owner on that server.
2. Open a direct message with the bot.
3. Wait for its ready message before sending. Earlier messages are not picked up.

Send ordinary text to start the dedicated conversation. A prompt is limited to 8 KiB of UTF-8 text.

- **While Claude is working:** a message you send is added to the running turn, and Claude reads it
  after its current step. The bot replies "Added to the running turn."
- **Conversation busy:** if the turn ends before the message is added, or after `!viki stop` or
  `!viki new` until the stopped turn has ended, the bot replies that the conversation is busy. Send
  the message again after the turn ends.
- **Threads:** replies go to the initiating message's thread in the bound direct message.

### Bridge commands

| Command | Effect |
|---|---|
| `!viki help` | Show the commands and request limits. |
| `!viki status` | While a turn runs, show its status line (the one described below); otherwise report that the bridge conversation is ready or busy. |
| `!viki new` | Deny its pending requests and close its conversation. The next ordinary message creates a fresh conversation. |
| `!viki stop` | Deny its pending requests and request a stop of its running turn. |
| `!viki questions on` | Also send questions from your other desktop chats here until you turn this off or the bot stops. |
| `!viki questions off` | Stop sending desktop chat questions here. |

These commands affect the bridge's own conversation. They do not open, resume, or control another
desktop conversation. Claude Code's `/` commands and other `!` commands are refused; there are no
mobile mode, model, or folder controls.

### What the bridge sends

The bridge sends the final reply of every turn of its own conversation while the conversation is
active, plus current permission and question requests. That includes a turn Claude Code starts by
itself, such as a background agent's report.

- **After `!viki stop` or `!viki new`:** a turn Claude Code starts by itself is not posted, and its
  requests are denied until you send a new message.
- **Not sent:** thinking, general tool output, the lists of tools and servers Claude Code has
  loaded, or other desktop conversations, except their questions once you turn on
  `!viki questions on`.

### Progress display

While Claude works, the bot shows progress:

- **Accepted message:** it marks the message that starts a turn 👀 as soon as it accepts it.
- **Typing:** it shows "typing" in the thread every 5 seconds, not while a request waits for you, at
  most 360 times a turn.
- **Status reply:** after 20 seconds it posts one status reply in the thread. It edits that reply at
  most every 20 seconds when it changes, for example "Working · 2m 10s · 7 steps · editing files".
  A second line shows "Claude: " followed by Claude's latest short note (at most 280 characters).
- **Activity phrase:** the activity is a fixed phrase such as reading files, running a command or
  thinking, and reads "Waiting for you" while a request waits. The activity phrase names no tool,
  file, path or command; the note is Claude's own words and can mention them, as a final reply can.
- **After the final reply:** the status reply becomes "Done", "Stopped" or "Error" with the time and
  step count. 👀 is replaced by ✅ (done), ⚠️ (error) or ⏹ (stopped, including `!viki stop` and
  `!viki new`).

Progress uses its own connection. If one of its calls fails, progress ends for that turn and the bot
keeps running. A rate limit pauses progress.

### Long replies and formatting

- **Large replies:** large replies are marked as shortened and point to the desktop. A final reply
  uses at most four posts of 12 KiB each, with a 64 KiB source-text cap.
- **Readable formatting:** chat replies and routine notices keep paragraphs, headings, lists,
  emphasis, and inline or fenced code readable.
- **Neutralized markup:** markup that could create mentions, HTML, images, or links is neutralized.
  URL-like tokens appear as inert inline code.
- **Operation details:** sent as complete literal JSON before choice instructions become active.
- **Question details:** a question's detail is sent as a readable post listing its questions and
  options instead.
- **Bot result notices:** fit within 8 KiB after chat formatting.
- **Not supported:** Mattermost file uploads, attachments, edited posts, history, and search.

Editing or deleting a message right after sending it, before the bot has read it back from
Mattermost, can stop the bot, which then says why. Send a new message instead of editing.

How a post looks depends on your Mattermost server and your mobile app. VIKI Agent hasn't been
checked against every version, and reactions, typing and the edited status reply are untested
on a real Mattermost server.

## Bot routine notices

Named [Bots](bots.md) can send routine results to the same verified owner/bot direct message. This
is a separate opt-in for each Bot:

1. Complete the setup above and wait for **Messaging** to show **Connected**.
2. Select the Bot in **Bots**. It must use the Messaging bridge's startup workspace.
3. Choose **Enable Mattermost delivery for this launch** before starting a routine.

Delivery starts off at each launch. A run started while delivery was off is not sent when you enable
it later. Routine notices are Mattermost-only; Slack and Discord do not receive them.

### What a notice contains

- **Succeeded run:** can send its final assistant response, shortened when it reaches the notice
  limit.
- **Failed, blocked or expired run:** can send a fixed status directing you to the desktop, without
  partial reply text.
- **Cancelled or unknown run outcome:** not sent.
- **Requests:** Bot notices carry no permission or question commands. Open the run in VIKI Agent to
  review and answer a request.
- **Replies to a notice:** replies go to the dedicated Messaging conversation. They do not continue
  that Bot's regular chat or its routine run. Use **Open conversation** or **Open latest run chat**
  in VIKI Agent for those conversations.

### Delivery status

Bots shows **Routine outcome** separately from **Mattermost delivery**. A local success stays
succeeded if its notice fails or cannot be confirmed. **Mattermost delivery** shows:

- **Sent:** the server confirmed the post.
- **Sending:** still awaiting a result.
- **Held:** Decision's rules held it.
- **Off:** delivery was off or the run was not eligible.
- **Unavailable:** it could not use the bridge.
- **Failed:** reports a definite delivery failure.
- **Unknown:** delivery could not be confirmed.

There is no automatic retry or replay. Check the direct message before repeating work after an
unknown result.

These all cancel notices still waiting to be sent:

- Choosing **Disable Mattermost delivery**.
- Turning the Mattermost bot off in Messaging.
- A bridge failure.
- Removing or resetting the Bot.
- Closing the last VIKI Agent window.

Enabling or connecting Mattermost again requires a fresh Bot delivery opt-in, even for the same
direct message. Restarting leaves Bot delivery off.

A post that has already been sent cannot be retracted. Notices follow the server and mobile
client's retention policy described above.

## Answer a current request

A request in the bridge's own conversation waits until you answer it, send `!viki stop` or
`!viki new`, or the bot stops; it has no expiry.

### Plain-text answers

When a request of that conversation is the only request waiting as your message arrives, answer it
in plain text:

- **Permission request:** `allow` or `deny`, in any letter case, allows it once or denies it. Any
  other text gets a hint and is not sent to Claude.
- **Question:** option numbers, one per question in order, with commas where a question allows
  several, like `2` or `1,3 2` for two questions, pick those options. Any other text is sent as
  your own answer.

Plain text answers nothing in two other cases:

- **Beside another request:** a request of this conversation waits beside another request, such as
  a desktop question relayed with `!viki questions on`. Nothing is sent to Claude; use the command
  shown under the request you mean.
- **Request gone:** the request went away after your message arrived. The bot replies that your
  message answered nothing and was not sent to Claude.

The bot replies "Allowed once.", "Denied.", "Picked: " followed by the option labels, or
"Answered in your own words.", then the choice receipt described below.

### Thumbs reactions

On a permission request, you can also react with a thumbs up (`+1`) to allow it once or a thumbs
down (`-1`) to deny it. React on the request's detail post or its instructions post in the bot's
direct message.

- **Several requests:** this works when several requests wait.
- **Checked first:** VIKI Agent checks the reaction through Mattermost's REST API before it counts.
- **Still being posted:** a reaction on a request still being posted is ignored.
- **Permission requests only:** questions and desktop chat questions do not take reactions.
- **Once:** each request is answered once.

### Commands with a nonce

A permission request shows the complete tool input or plan, then commands containing a random
single-use nonce. The commands work in place of the plain answer, and desktop chat questions need
them. Copy the nonce from that current request:

```text
!viki allow <nonce>
!viki deny <nonce>
```

Allow means **Allow once**. If the full input is unavailable or too long to display, the mobile
request offers only Deny and directs you to the desktop for approval. The bridge does not approve a
shortened request, whether you answer in plain text, with a reaction or with a command.

A fully displayed question lists its questions with numbered options and accepts free-text
**Other**, and a pick when its instructions include a Pick line:

```text
!viki pick <nonce> <picks>
!viki answer <nonce> <free-text>
```

- **`!viki pick`:** takes one option number per question, in order, with numbers joined by commas
  where a question allows more than one, like `1,3`.
- **`!viki answer`:** sends your own words as your reply instead of a pick.
- **No swapping:** permission and question commands cannot be interchanged.
- **Background agents:** current requests from a background agent in this same conversation remain
  answerable after its parent turn completes.
- **Nonce lifetime:** a nonce belongs to one request that is still waiting and has no time limit.
  An old, cancelled, duplicate, or mismatched nonce cannot answer another request.

### Choice receipts

The reply reports the normal local choice receipt, such as `delivered_locally`, `not_delivered`, or
`unknown`. Check the desktop when delivery is uncertain.

> [!IMPORTANT]
> The bridge makes no second delivery attempt, and a failure does not undo an Allow already
> delivered locally.

## Answer desktop chat questions

Send `!viki questions on` to also receive questions from your other desktop chats here, and
`!viki questions off` to stop.

- **At every connection:** relaying desktop chat questions starts off.
- **Questions already waiting:** `!viki questions on` also posts any question already waiting, from
  every desktop chat except any messaging bridge's own conversation.
- **Permission cards:** permission cards from those chats stay on the desktop.
- **Answering:** pick or answer a desktop question the same way as the bridge's own questions
  above. Its nonce works for as long as the question waits.
- **Limits:** at most 8 are held at once. A question too long to show posts a fixed notice with no
  nonce.
- **When this bot stops:** turning this bot off or stopping it leaves those chats' questions open on
  the desktop; it never denies them.

## When mobile access stops

Requests do not expire. These events stop the bridge:

- **Windows and Linux:** closing the last VIKI Agent window stops the bridge straight away.
- **Failures and limits:** a connection or authentication failure, a question too long to show, a
  failure to send a reply, a failure in the bridge's own Claude Code conversation, or a capacity
  limit also stops it.

> [!IMPORTANT]
> On macOS, closing VIKI Agent's last browser window can leave the browser and bridge running.
> Quit that browser with ⌘Q to end the launch and stop the bridge. In VIKI Agent's own window,
> closing the last window starts the server's grace period before it exits.

When the bridge stops:

- **Its own conversation:** pending requests in its own conversation are denied, including requests
  raised while it shuts down, and that conversation is closed.
- **Other desktop conversations:** they remain separate. A question relayed from one of them is
  never denied when this bridge stops, and stays open on the desktop.

When a bot that had posted its ready message stops, it posts one line in the direct message saying
why, best effort within 3 seconds:

- It reached a safety limit.
- Its Claude Code conversation reported an error or was closed.
- A request changed after it was shown or could not be shown completely.
- An unexpected error.
- Its connection to the chat service was lost.
- It was turned off or its settings changed in Messaging.
- The VIKI Agent window was closed or VIKI Agent shut down.
- VIKI Agent needs a restart.

**Messaging** shows the reason on the card, including these reasons: a safety limit, the
conversation reported an error or was closed, a request changed or could not be shown completely,
and an unexpected error.

To connect again:

- **Same launch:** turn the bot on again after a stop, or after turning it off, to create a fresh
  bridge in the same launch. Opening a window or refreshing status does not connect it.
- **Restart requested:** if Messaging asks you to restart VIKI Agent, because the startup workspace
  changed or the bridge could not shut down cleanly, fix the problem and restart.
- **No automatic recovery:** there is no automatic reconnect within a launch or turn retry, and
  offline messages are not backfilled.
- **Fresh prompt:** wait for the new ready message, then send a fresh prompt.
- **Uncertain sends:** a send may have reached Mattermost before the bot was turned off or a
  response was lost. Check the DM before repeating uncertain work.

Session and choice records use the app's normal retention and reset controls. The bridge adds no
separate transcript store.

## Disable and revoke

You can turn the bot off, remove its saved settings, or revoke its token in Mattermost.

- **Turn the bot off:** in **Messaging**, turn the Mattermost bot off to invalidate current mobile
  requests and close that bridge's dedicated conversation. Other desktop chats and messaging
  providers remain available. Turn it on again to connect a fresh bridge in this launch.
- **Remove saved settings:** select **Remove saved settings** to delete the saved Server URL, user
  ID and bot token from this computer and turn the bot off.

To revoke the token in Mattermost (see
[token revocation](https://docs.mattermost.com/developers/integrate/reference/personal-access-token#revoke-a-personal-access-token)):

1. Open **System Console > User Management > Users**.
2. Find the bot.
3. Select **Manage Tokens**.
4. Delete its token.

> [!IMPORTANT]
> Turning the bot off does not revoke the token in Mattermost or delete the saved settings, and
> Remove does not revoke it in Mattermost. Revoking the token does not erase content already held
> by Mattermost or mobile clients.

See [Security and privacy](security.md#optional-messaging-bridges) for the trust and
permission boundary.
