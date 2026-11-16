# Routines

Routines are prompts VIKI runs for you, on a schedule or on demand, in a workspace you choose.

Each attempt starts a fresh chat with the current profile instructions and the routine
prompt. For a profile that has a Bot in the routine's workspace, the instructions and VIKI Agent's memory rule and Bot rules are the chat's system prompt, the prompt is its first message, and the chat skips the vault's own instruction files. A Bot lists routines using its profile and vault, but their results do not append to
its regular conversation. Use **Open session** on a run to read its result or answer a request.

## Find your routines
Open **Routines** in the left rail — its badge flags routines that need you.

## Create and test
Select **New routine** and write its prompt. Then pick a profile, a workspace, and a schedule — **Manual only**, **Daily** or **Weekly**.
A routine moves through states: **Draft**, **Tested**, **Enabled**, **Paused**, **Archived**.
You can enable it only once it's **Tested** or **Paused**. Editing a **Tested** routine's prompt or schedule sends it back to **Draft**, so it needs another **Test run**.
Select **Test run** to try it, or, once enabled, **Run now** to run it right away.

## Wait for a card, or stop at a draft
A **Daily** or **Weekly** routine can wait for work: under **Scheduled runs**, choose **Only when a card is waiting** and enter its **Seat**. Before each scheduled time, VIKI Agent checks `kanban/` in the routine's vault for a note with `status: todo` and the tag `owner/<seat>`; with none, that time shows as **Skipped** and no chat starts. **Test run** and **Run now** run without checking Kanban.
Or choose **Only when a note changed** and enter a **Folder** of the routine's vault, such as `00-inbox`: a scheduled time runs only when a note there was added or edited after the start of the routine's last run at this version that succeeded or stopped at a draft; otherwise it shows as **Skipped** and no chat starts. A folder with no notes, or a misspelled one, never counts as changed. **Test run** and **Run now** run without checking the folder.
Under **When a confirmation is needed**, **Stop at a draft** denies a permission or plan request at once, so Claude finishes with a draft and reports what needs your decision; a run that ends this way shows as **Stopped for your decision**. Questions still wait, and it applies to **Test run** too. **Wait until the time limit** is the default.
With **Start Bots' work** in **Decision**, VIKI can also start a Bot's tested routine set to **Only when a card is waiting** when one of your cards waits for its seat; that run shows as **Started by VIKI**.

A routine run of a Bot whose profile pins a model connection uses the native default model with your Claude Code sign-in; the preview says so.

A schedule is independent of Decision and its optional model. It starts an enabled
routine at the saved time while the app is open and its conditions allow the run. Decision
can start a card-waiting routine between those times: **Ask first** offers **Start**, and
**Allowed** can start it after the local routine and grant checks. Creating a Bot or card
does not create, test or enable a routine.

## Your routine list
Routines are grouped as **Needs you**, **Scheduled** or **Not scheduled**.
At most two routines run at once. Starting a third shows **Only 2 routines can run at the same time.** Try it again once one of them finishes.

## Runs only while open
Routines run only while VIKI Agent is open. Each run uses your Claude Code sign-in and its usage limits or billing, like a chat you start. Closing the last window denies any waiting confirmation at once.
A running routine then stops within 5 minutes.
A schedule that comes due while VIKI Agent is closed does not run. It shows as **Missed** the next time you open VIKI Agent, unless it's more than 7 days old.

## History and notifications
Each routine keeps a history of its past runs and their outcomes.
Use **Turn on desktop notices** in **Needs you** to enable the same notification setting used for routine notices. VIKI Agent offers desktop notices only while it is in the background. Browser notification permission and quiet hours still apply.
Tip: After an **Unknown** result, check the routine's run history (and its chat) before running it again.
