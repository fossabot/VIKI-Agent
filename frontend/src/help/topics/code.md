# Code

Open a code project in VIKI Agent, read it from a chat, or edit it.

## Add a code folder
Select **Add folders…** on **Settings** › **Workspaces and Vault** to register one or more: pick them in your system's folder picker, holding `Ctrl` (⌘ on macOS) or `Shift` to pick several, or choose **Type a path instead** to type one.
VIKI then shows each as **Trusted** or **Not trusted**.
Until you trust a folder, VIKI starts no session there and runs no Git command on it; you can still browse its files.
To trust one, select **Trust…** on its row in **Workspaces and Vault**, then **Trust this folder**. Trust a folder only if you're willing to run its commands yourself.
Tick a trusted folder's row to let each new chat from **New session** read and edit it too, up to 7 at a time, with no restart; a chat already open keeps the folders it started with. Chats started from the Code workbench, Bot conversations and routine runs don't get ticked folders. A chat with extra folders names them in a chip on its header.
Each row's menu offers **Details and trust**, **Open in Code** and, except on a protected folder or the folder chats start in, **Remove from list…**, which asks you to confirm **Remove** and leaves nothing on disk changed.
**Browse…** beside a folder field, such as a new vault's parent folder, opens the same system picker.

## Look at code from a chat
In an open chat, the right panel's **Overview** and **Code** tabs switch between your usual chat tools and this chat's code.
Pick a **Code folder**, then open an item from **Files** or **Changes**.
The file opens read-only, beside the chat when there's room, or in the middle pane otherwise.
Select some lines and choose **Quote in chat** to add the exact reference to your draft, without sending it.
Close with **Close code preview**, or select **Back to chat**.
Select **Open workbench** for the full Code view.

## The workbench
There, select **Edit** to change a file in a trusted, ordinary folder.
Select **Save** or **Revert**. An unsaved edit stays in the window even if you browse to another file.
A save that didn't finish shows under **Stopped saves**.

## Good to know
Checks and previews are not sandboxes, and VIKI Agent does not require them to pass before you ship.
Tip: The Code sidebar shows whichever folder you choose — it doesn't automatically switch to a chat's own worktree.
Idle worktrees VIKI made are archived and removed automatically. Restore one from the **Worktrees** tab.

## Bot copies
A Bot you gave a code folder works in a copy of it, and the **Worktrees** tab labels each copy **Bot copy** or **Bot thread copy**. A copy a Bot chat still uses is kept. When its chat, thread or Bot is deleted, VIKI removes the copy when its work is already merged, otherwise archives it with its branch kept, even with cleanup off; **Restore** brings archived changes back from **Archived changes**. A copy VIKI can't clean up safely, such as one with a submodule, a switched branch or a merge in progress, stays in the **Worktrees** tab marked **Released** until you remove it with **Remove**. Removing a copy discards whatever is only in it. If Git refuses, clean the copy up in a terminal or run `git worktree remove --force <copy path>` in the code folder, then select **Remove** again.

Once a folder has had a Bot copy, any change to its Git settings, hooks or attributes makes VIKI's own Git actions there refuse until you undo the change or select **Confirm the current settings** in the **Worktrees** tab. This is expected after `git push -u`, an editor's publish action, switching to a branch that sets upstream tracking, adding or changing a remote, or `git config` for your name or e-mail, whether you or Claude Code runs it. The Code view's Git actions and a new Bot chat refuse, and the status bar's Git chip shows an unknown status. Confirming means you have looked at the current settings and accept them as the new baseline. The check goes on after the last copy is removed, until you remove the folder from the list.
