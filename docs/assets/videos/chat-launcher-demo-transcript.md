# VIKI Agent: proactive coding, with decisions that adapt

About 2 minutes 45 seconds, narrated by a generated voice, with English captions. VIKI is
pronounced **Vicky (VIK-ee)**. It shows the real app working through a scripted
example, so the **Learning** numbers on screen are sample data, not anyone's real results.

While **Proactive VIKI** is on and not paused and an app window is open, VIKI Agent's Decision
notices supported opportunities, weighs relevance and timing, and chooses whether to stay quiet,
suggest work or start a chat for eligible work. Within the grants you set in **What VIKI may do**
and their daily limits, that work can move forward without another prompt. The film follows
**Notice → Decide → Work → Learn**, from an overview of the product through the app at work to
feedback and local adaptation. Your vault supplies durable context, and VIKI Agent shows what
Claude Code is doing and any permission it asks for.

## The connected story

| Time | On screen |
| --- | --- |
| 0:00–0:45 | Meet VIKI Agent, glimpse a skill offer from 0:03, then learn about Decision and your project's context. |
| 0:45–1:29 | See a skill offer, then choose between **Ask first** and **Allowed** for a separate due task. |
| 1:29–1:53 | Follow the chat VIKI starts for that task through Claude Code's permission prompts, an edit and a local test run. |
| 1:53–2:43 | See feedback, the Learning numbers, improvements that wait for your review, and the layers that protect your work. |

## Complete narration

**0:00**

VIKI Agent is a proactive coding agent with a decision engine that chooses when to help, what to suggest, and which work may start. It brings Claude Code and your Markdown project knowledge together, so a useful next step can reach you before you ask. With proactivity enabled and the app open, it considers your goals, context, timing, and granted authority. It can stay quiet, surface a suggestion, or start eligible work. Your vault provides durable goals, tasks, sources, and decisions. VIKI turns that context into opportunities to move your project forward.

**0:45**

That includes proposing work beyond your task list. After repeated API reviews, VIKI offers to turn that workflow into a reusable skill. The launcher explains why and links to the source chats. Make this a skill prepares an unsent message for your review. Other offers help you learn from captured sources, run a retrospective, or improve Decision questions. These proposals stay in your hands. Now follow a separate coding task from context to result. A due vault task asks for positive checkout quantities. Ask first reserves mission starts for you. Allowed lets eligible work begin within your activity limits.

**1:29**

On its next decision cycle, VIKI starts without another chat prompt. The agent reads the task and code, then requests an edit. Independent work still respects native permissions: Allow once approves the edit, and running the test command needs a separate approval. The agent responses are scripted; the app workflow, disposable edit, and local checks are real. You can follow what happened and review the evidence before deciding what comes next.

**1:53**

Your responses also shape future decisions. Opening a suggestion, acting on it, or choosing Not useful gives VIKI feedback. With enough labeled decisions, local learning adapts the scoring weights; attention estimates also adjust. Learning shows what changed, the starting and current values, and the evidence behind them. You can turn it off or reset it. This adapts VIKI's decisions, without retraining Claude. Broader improvements to skills, knowledge, and Decision questions begin as drafts for review. Sensitive instruction and system files are excluded from knowledge upkeep. The optional starter supplies deny rules; your configured Claude Code hooks can block covered operations. Discover useful work. Delegate with control. Learn from the outcome. VIKI Agent.

## How new work is suggested

This part of the film shows one kind of suggestion: turning a repeated coding workflow into a
reusable skill. After three chats that began with similar messages, the launcher offers a skill.
Its card names the source chats, and its **Why now** line gives the reason.

**Make this a skill** puts a fixed instruction in the message box without sending it. The
instruction asks for a skill draft and says not to create or change a file. When a draft comes
back, you can send it to **Approvals** with **Propose this skill**. The film stops before Send.

The other offers the film mentions help you learn from captured sources, run a retrospective or
improve Decision's questions. They only prepare a draft for you to send, or wait in
**Approvals**, and VIKI does not start a chat for them on its own, even when **Start chats to do
work** is **Allowed**.

## How decisions learn from responses

VIKI adapts how Decision scores items from how you respond, on your computer. Opening an item or
acting on it counts as positive feedback, and **Not useful** counts as negative. **Not now** sets
an item aside and counts as neither. Recent feedback also gives future Decision requests a little
context.

With **Learning** on, VIKI updates its estimates every five minutes. From the first decisions it
tunes when and how loudly it speaks up. Once 20 decisions made by the Decision model carry your
label, at least three positive and three negative, it also fits the scoring weights and topic
offsets within fixed bounds. Later decisions use the learned values while Learning is on. Decision
combines the model's answers with these local weights; learning calibrates VIKI's own choices and
does not train Claude or the model behind Decision.

**Not useful** records one piece of feedback and removes the card; it sends no chat message, and
one click does not refit Decision.

The Learning screen shows numbers computed from 20 sample decisions: 17 marked useful and linked
to a goal, and three marked **Not useful** and not linked to one. For example, the **Linked to a
goal?** weight moves from 0.600000 to 1.266243.

**Learning → Show all numbers** exposes starting values, current values and supporting
observations. **Each question** shows how useful each question has been. You can turn Learning
off, or select **Reset what VIKI learned** to start its numbers over; your decisions and labels
stay. Broader improvements to question wording, skills and knowledge go through review, and the
numeric fit does not rewrite instructions or install skills.

## How the vault fits

Markdown files hold your project notes, decisions, source material and guidance, and stay usable
outside the app. Selected note excerpts can enter conversations. Decision draws on supported vault
and activity signals; that does not mean every file is scanned or every note is sent to a model.
The vault diagram in the film is an illustration, not a product screen.

Chats run through the unmodified Claude Code with its applicable instructions, skills, hooks and
permissions. Decision's optional model is TypeSafe's Decision model, a separate service with its
own saved key, a **Send level** for how much it sees (titles, excerpts or sections), **Never send**
and a fallback to VIKI's own rules. The app and your notes are on your computer, but chat replies
come from the model Claude Code uses (your Claude plan, an API key, a cloud provider or a model you
connect), and the Decision model is a separate online service, so not everything runs offline.

## Protecting valuable work

Different controls cover different actions. Configure them for the project and review the results.

| Control | What it covers |
| --- | --- |
| Grants and daily limits | Whether VIKI may start a chat for a task. **Ask first**, **Allowed** and **Off** are separate choices, and Proactive VIKI is off until you turn it on. |
| Drafts first | Offers to improve skills, notes and Decision's questions prepare an unsent message or go to **Approvals**. VIKI does not start a chat for them on its own, even when **Start chats to do work** is **Allowed**. |
| Files kept out of upkeep | Knowledge upkeep leaves out sensitive instruction files, system files and raw sources. It is not a sandbox around every Claude Code tool. |
| Starter deny rules | The optional **Start with VIKI** starter supplies Claude Code deny rules. It ships no hooks. |
| Hooks you configure | Hooks you set up in Claude Code can inspect or block the operations they cover, and VIKI Agent shows the hook notices it receives. |
| Claude Code's permission prompts and **Stop** | The film's Edit and Bash requests each need their own **Allow once**. A chat VIKI starts does not skip them. **Stop** halts a running conversation. |
| Evidence and review | A changed file and passing checks let you inspect an outcome. They do not prove that every requirement or valuable source is still correct. |

None of these layers promises to prevent every destructive change, loss of intent, outdated fact or
unsafe command, and the film describes the hooks layer without showing a hook at work. Knowledge
reviews and offers of reusable guidance support maintenance; they do not repair or expire all old
knowledge automatically.

## The autonomous coding example

This separate example starts from a dated task in `10-projects/checkout/tasks.md`. The app finds
the due task, checks the grant and routes the permission prompts. With **Start chats to do work** set to **Allowed**, VIKI starts
a chat for the task at its next check, with no prompt or per-task click from you. The film
shortens the scheduled wait and labels the cut.

VIKI starts such a chat only while **Proactive VIKI** is on and not paused, an app window is open,
the grant allows it and the daily limit is not used up; Claude Code's permission prompts still
apply. At most two chats that Decision starts run at once, and VIKI starts none of its own while
one is open, so one stays free for yours. The default grant is **Ask first**.

The chat's first message is generic; the agent reads the task note before it looks at the code.
After you approve the Edit with **Allow once**, the edit changes `checkout.py`. After you approve
the Bash request, `python -m unittest test_checkout -v` runs in that project: the positive, zero
and negative quantity cases give two failures before the edit and all pass after it.

The conversation shows the test output. Its **Manual** chip is Claude Code's permission mode; it
does not show how the chat started.

[Watch the workflow in the guide](../../index.html#chat-launcher-demo)
