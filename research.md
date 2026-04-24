# Scribe Agent Research

A "shadow" agent running in parallel to a primary coding agent — reading the
session, cataloging ideas, distilling memory, and whispering relevant context
back into the main agent's history. Like a junior lawyer passing notes to the
lead counsel during cross-examination.

This document surveys what exists, what's theoretically sound, and how to build
one that integrates with Claude Code, opencode, and similar harnesses.

---

## 1. The core idea, restated

The primary agent (Claude Code, opencode, Cursor Composer, etc.) is operating
under two pressures that fight each other:

1. **Context window scarcity.** Every token of conversation history competes
   with every other token for the model's attention and for a fixed budget.
2. **Long-horizon coherence.** Good engineering work requires remembering
   decisions made three hours ago, patterns noticed last week, and the user's
   stated preferences from a month ago.

The primary agent can't do both well at once. It has to drop things to keep
moving, and it has no quiet moment to reflect without the user noticing a
stall.

A **scribe agent** solves this by running *out-of-band*:

- **Reads** the session transcript as it's written (or on a schedule).
- **Extracts** ideas, decisions, unresolved threads, user preferences, TODOs,
  contradictions, dead ends.
- **Catalogs** them into persistent stores (memory files, vector DBs, an
  "ideas backlog").
- **Summarizes** — producing a running digest that compresses the past without
  the primary agent having to pause.
- **Rewrites history** — replaces or augments transcript segments with tighter
  summaries, or injects just-in-time reminders pulled from its catalog, so the
  primary agent sees a more useful context on its next turn.
- **Stays invisible** to the primary agent (mostly). The primary agent may
  notice that its system prompt or tool results contain fresher notes than it
  wrote itself, but it does not address the scribe directly.

The "invisible-to-primary" framing is the interesting bit. Most prior art
treats memory agents as peers the primary agent calls explicitly. The scribe
inverts that: it's a supervisor above the primary, not a tool below it.

---

## 2. Prior art

### 2.1 MemGPT / Letta — the OS metaphor for agent memory

MemGPT treats the context window like RAM and introduces an explicit tiered
memory hierarchy with an agent that can page data between tiers:

- **Core memory** (in-context, editable by the agent)
- **Recall memory** (full conversation history, searchable)
- **Archival memory** (vector DB for long-running facts)

The agent manages its own memory via tool calls — it chooses what to page in
and out. Letta is the productized successor; "memory blocks" are the headline
abstraction.

Relevance to the scribe: MemGPT is the *opposite* design. It makes the primary
agent do all the memory work. The scribe offloads that work to a sidecar,
which is better for harnesses (like Claude Code) where you don't control the
primary agent's system prompt or tool loop.

Sources: [Letta memory management](https://docs.letta.com/advanced/memory-management/),
[MemGPT paper](https://research.memgpt.ai/),
[Letta memory blocks](https://www.letta.com/blog/memory-blocks).

### 2.2 Claude Code — subagents, MEMORY.md, and auto-memory

Claude Code already ships several of the primitives a scribe needs:

- **Subagents** have their own context windows and can run in parallel, but by
  default they report back to the primary via tool results — they're callees,
  not observers.
- **MEMORY.md** (user-level) is loaded into every conversation; the harness
  truncates after ~200 lines, which forces curation.
- **Auto-memory** (present in this repo's `CLAUDE.md`) tells the primary agent
  to self-curate memory across turns. The scribe would *replace* this
  self-curation — the primary stops writing memories, and the scribe writes
  better ones from the outside.
- **Session transcripts** live as JSONL at
  `~/.claude/projects/<project-hash>/*.jsonl`, appended as the session runs.
  This is the scribe's primary input.
- **Hooks** (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
  `Stop`, `SessionEnd`) receive a `transcript_path` on stdin. This is the
  scribe's primary trigger and its primary injection point.

Sources: [Hooks reference](https://code.claude.com/docs/en/hooks),
[subagents docs](https://code.claude.com/docs/en/sub-agents),
[session storage](https://claude-world.com/tutorials/s16-session-storage/).

### 2.3 The "Scribe" pattern in the wild

The term is starting to appear:

- **ClawPort's "Scribe" agent** is the closest match to this idea today: a
  dedicated agent that periodically consolidates, deduplicates, and compresses
  memory across a team of specialist agents, each writing to its own
  `MEMORY.md`. "The scribe is the most important agent... coming back the next
  day to a searchable log of every decision and session is invaluable."
- **Gemini Scribe** (Allen Hutchison's project) uses a scribe-as-project-
  context pattern for Gemini, focused on scoping context to projects rather
  than observing live sessions.
- **Shadow Scribe** (yeschat) is a writing-assistant branded product; shares
  the name, not the architecture.

Source: [Taming agent sprawl — ClawPort](https://www.clawport.dev/blog/taming-agent-sprawl),
[Gemini Scribe](https://allen.hutchison.org/category/projects/gemini-scribe/).

### 2.4 Cursor Composer — Automations & Background Agents

Cursor 2.0+ has two patterns close to the scribe:

- **Background Agents** run on remote VMs in parallel with the user's local
  session. Composer's training infrastructure literally maintains a *shadow
  deployment* of the Cursor backend so training matches production — an
  interesting echo of the "shadow" framing, but for model training, not
  session observation.
- **Automations** (March 2026) are always-on agents triggered by external
  events. When "memory" is enabled, the agent gets a persistent notepad it
  writes to each run and reads from on the next — effectively a per-automation
  scribe.

Sources: [Cursor 2.0](https://cursor.com/blog/2-0),
[Composer](https://cursor.com/blog/composer),
[Cursor 3 / Automations](https://www.datacamp.com/blog/cursor-3).

### 2.5 Kimi / Composer training — context sharding

Kimi shards context across parallel sub-agents as a deliberate context-
management strategy — each sub-agent holds a slice, and a coordinator stitches
outputs. This is the "avoid overflow by parallelism" school, orthogonal to the
scribe but complementary: a scribe could maintain the shard index.

Source: [How Kimi, Cursor, and Chroma train agentic models](https://www.philschmid.de/kimi-composer-context).

### 2.6 GitHub Copilot `/fleet`, LangGraph supervisor, CrewAI

- **Copilot `/fleet`** runs multiple agents concurrently on independent
  branches. Closer to git-worktree parallelism than a scribe, but shows the
  industry is normalizing "many agents, one user."
- **Supervisor pattern** (LangGraph, CrewAI, AWS multi-agent guidance) — one
  orchestrator delegates to specialists and synthesizes results. A scribe is
  an unusual variant: the "specialist" observes and rewrites the
  orchestrator's inputs rather than executing tasks.
- **Observer / group-chat patterns** in AutoGen and Microsoft Agent Framework
  put observer agents into a shared conversation. These are closer to the
  scribe but still expect the primary agent to *know* about the observer.

Sources: [Copilot /fleet](https://github.blog/ai-and-ml/github-copilot/run-multiple-agents-at-once-with-fleet-in-copilot-cli/),
[Agent orchestration patterns — Azure](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/ai-agent-design-patterns),
[Supervisor pattern — LiveKit](https://livekit.com/blog/supervisor-pattern-voice-agents).

### 2.7 Context compaction — the production techniques

Modern coding harnesses already rewrite history; the scribe would plug into
the same seams:

- **Claude Code** runs a three-tier compaction engine. It prefers surgical
  `cache_edits` over full message rewrites to preserve prompt-cache hit rate,
  and piggybacks summarization calls on the main conversation's cache prefix.
- **Codex CLI** uses absolute token thresholds, preserves the last ~20k
  tokens of user messages, and retries summarization with backoff.
- **OpenCode** has a separate "prune" path for tool output (protects the last
  40k tokens of tool output) and maintains dual summaries — one for the UI,
  one for the model. Critically, it exposes an
  `experimental.session.compacting` hook that fires *before* compaction runs,
  letting a plugin inject domain context.
- **Amp** refuses auto-compaction in favor of manual Fork / Handoff /
  Thread References — the user does the scribe's job by hand.
- **Compaction vs summarization** (Morph's framing): compaction = deletes
  tokens, zero hallucination, fully inspectable; summarization = LLM rewrite,
  compressive but lossy and potentially wrong. A scribe can mix: keep
  original tool traces (compaction), replace prose reasoning with a
  summarized digest (summarization).

Sources:
[Compaction research gist](https://gist.github.com/badlogic/cd2ef65b0697c4dbe2d13fbecb0a0a5f),
[Claude Code compaction engine](https://barazany.dev/blog/claude-codes-compaction-engine),
[Compaction vs summarization — Morph](https://www.morphllm.com/compaction-vs-summarization),
[Factory compressing context](https://factory.ai/news/compressing-context),
[JetBrains efficient context management](https://blog.jetbrains.com/research/2025/12/efficient-context-management/).

### 2.8 Context engineering — just-in-time retrieval

Anthropic's own framing ("Effective context engineering for AI agents") and
several follow-ups argue for *just-in-time* context: keep lightweight
references in the prompt (file paths, query IDs), and fetch the body at tool
call time. A scribe naturally curates those references — it's the index the
primary agent greps.

Sources:
[Anthropic — effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents),
[Context engineering part 2 — Phil Schmid](https://www.philschmid.de/context-engineering-part-2),
[Dave Hulbert — agent-situations (ephemeral context injection)](https://github.com/dave1010/agent-situations).

---

## 3. Design space for the scribe

### 3.1 What the scribe reads

| Source | Why |
|---|---|
| Session JSONL (append-only) | Ground truth of what the primary said and did. |
| Tool results (git diffs, file reads) | What the primary *actually changed*, vs. what it claims. |
| Memory files (`MEMORY.md`, per-type memory .md) | What it already knows about the user. |
| Repo state (git log, current diff) | Ground truth of the code. |
| External telemetry (optional) | Linear tickets, Slack, calendar — richer "why." |

### 3.2 What the scribe writes

- **Digests** — running summaries, per-session and cross-session.
- **Idea catalog** — tagged list of things the user *said in passing* that
  weren't acted on, with enough context to re-raise later.
- **Decision log** — "at turn N, we chose X over Y because Z" entries.
- **Contradictions & open threads** — flags for the primary to resolve.
- **Memory files** — same format as `CLAUDE.md` auto-memory today, but
  curated by the scribe, not the primary.
- **Injection manifests** — small files the primary's next system prompt or
  hook output reads on the next turn.

### 3.3 How the scribe influences the primary

There's a spectrum from hands-off to invasive. In roughly ascending order of
how surprised the primary agent will be:

1. **Passive cataloging only.** The scribe writes files; the user decides
   when to surface them. Zero primary-side surprise.
2. **Memory-file curation.** The scribe rewrites `MEMORY.md` (and the per-
   topic memory files it references). The primary reads them on session
   start like any other user memory — transparent and benign.
3. **SessionStart / UserPromptSubmit hook injection.** The scribe's hook
   appends a "Scribe digest" block to the primary's context on each user
   turn. The primary sees fresh, relevant notes it didn't write.
4. **Pre-compaction injection.** The scribe hooks
   `experimental.session.compacting` (opencode) or `PreCompact` (Claude Code)
   and augments the compaction prompt with decisions, open threads, and
   pinned quotes from the user. The primary's summarized history is *better*
   than it would have been.
5. **Transcript rewriting.** The scribe edits the JSONL transcript directly —
   replacing long tool-result blobs with digests, dropping dead branches,
   pinning user quotes. High leverage, high blast radius. See §5.
6. **Context replacement on next turn.** A proxy MCP server sits between the
   harness and the Anthropic API, rewriting the outgoing context each call.
   Most invasive; also the most general. Works with any harness that speaks
   MCP or can be pointed at a local API shim.

Options 2–4 are the sweet spot: inexpensive, composable, reversible, and
don't require the scribe to understand the harness's cache economics.

### 3.4 When the scribe runs

- **Continuous (tail the JSONL).** `tail -f` the transcript file, react as
  lines land. Lowest latency, highest cost. The scribe becomes a long-
  running process.
- **Hook-triggered.** Run on `Stop` / `PostToolUse` / `UserPromptSubmit`.
  Pay only when something happens. Deterministic. This is what Claude
  Code's hook system is designed for.
- **Cron / scheduled.** Every N minutes, digest what's new. Cheapest,
  highest staleness. Fine for cross-session cataloging, bad for real-time
  nudges.
- **On-demand.** `/scribe digest` slash command. Most conservative; user
  opts in. A reasonable MVP.

Hybrid: hook-triggered for per-turn digesting, scheduled for cross-session
consolidation, on-demand for forced refresh.

### 3.5 What model to run the scribe on

The scribe is a *read-heavy, write-a-little* job. Summarization and
extraction are exactly what cheap/small models are good at. Run Haiku (or
a local model) for per-turn digesting, escalate to Sonnet only for
cross-session consolidation. This keeps the scribe economically defensible
even on long sessions.

---

## 4. Integration — Claude Code

### 4.1 What's available out of the box

1. **Hooks** (settings.json): `SessionStart`, `UserPromptSubmit`,
   `PreToolUse`, `PostToolUse`, `Stop`, `SessionEnd`, `PreCompact`,
   `SubagentStop`. Each receives the `transcript_path`.
2. **Transcript JSONL** is human-readable, append-only, and stable enough
   to tail.
3. **MEMORY.md** is reloaded per session — the simplest injection channel.
4. **Output from a hook** can be appended to the conversation (see the
   `additionalContext` field for `UserPromptSubmit`) — the cleanest
   per-turn injection channel.

### 4.2 Minimum viable scribe (Claude Code)

```
~/.claude/settings.json
└── hooks:
    ├── UserPromptSubmit → scribe-digest.sh   (reads transcript, emits context)
    ├── Stop              → scribe-catalog.sh (extracts ideas/decisions)
    └── SessionEnd        → scribe-consolidate.sh (cross-session memory update)
```

Each script:
1. Reads `transcript_path` from stdin JSON.
2. Sends the tail to a small model with a prompt like "extract decisions,
   open threads, user preferences; update these files."
3. Writes to `~/.claude/projects/<hash>/scribe/{digest.md,ideas.md,decisions.md}`.
4. For `UserPromptSubmit`, prints an `additionalContext` block that
   surfaces the top-3 relevant catalog entries for this prompt.

This is ~200 lines of shell + a system prompt. It's the scribe stripped
to its bones, but it delivers the core value: persistent idea catalog
and per-turn injection.

### 4.3 Stronger integration

- **Custom subagent** (`~/.claude/agents/scribe.md`) invoked by `Stop` hook.
  Gives the scribe its own context window, its own tools, and User-scope
  persistent memory at `~/.claude/agent-memory/scribe/`.
- **MCP server** (`scribe-mcp`) exposing `scribe_recall`, `scribe_log_idea`,
  `scribe_catalog` tools. Lets the primary *opt in* — contradicts the
  "invisible" framing, but useful as an escape valve.
- **PreCompact hook** that runs the scribe against pending-to-compact
  messages and writes a better summary than Claude Code's default.

### 4.4 What not to do

- Don't edit the active JSONL transcript file while the session is live.
  Claude Code reads and writes it concurrently; your edits will race and
  may invalidate the in-memory model of the session.
- Don't invalidate the prompt cache gratuitously. Prepending content
  resets the cache and roughly doubles per-turn cost. Inject via
  `additionalContext` (suffix) when possible.
- Don't let the scribe call tools that modify user code. It observes and
  records; it doesn't edit source.

---

## 5. Integration — opencode

Opencode's plugin model is, for this use case, *more* scribe-friendly than
Claude Code's hooks:

- **~52 lifecycle hooks** (oh-my-opencode demonstrates the surface area):
  per-tool, per-session, per-compaction, per-agent-handoff.
- **`experimental.session.compacting`** fires before compaction; a plugin
  can inject domain context into the compaction prompt. This is exactly
  where a scribe shines.
- **Tool execution hooks** run before tools execute — the scribe can
  intercept tool calls (e.g., to redact secrets before they hit the
  transcript).
- Plugins are plain JS/TS modules loaded from a directory or via npm.

### 5.1 Opencode scribe shape

```typescript
// ~/.config/opencode/plugin/scribe.ts
export default function scribe({ app, client }) {
  return {
    "session.message.after": async ({ message, transcript }) => {
      await updateDigest(transcript);
      await extractIdeas(message);
    },
    "experimental.session.compacting": async ({ messages, prompt }) => {
      const digest = await readDigest();
      return { prompt: `${prompt}\n\n<scribe-notes>\n${digest}\n</scribe-notes>` };
    },
    "session.start": async ({ session }) => {
      await injectMemorySnapshot(session);
    },
  };
}
```

Sources: [OpenCode plugins](https://opencode.ai/docs/plugins/),
[Does OpenCode support hooks?](https://dev.to/einarcesar/does-opencode-support-hooks-a-complete-guide-to-extensibility-k3p),
[oh-my-opencode](https://ohmyopencode.com/).

---

## 6. Integration — other harnesses

- **Cursor.** Closed surface. No public hook API equivalent. Could run the
  scribe as an Automation that reads Cursor's chat export format — but
  without a hook point, injection becomes manual (the user pastes the
  digest).
- **Codex CLI.** Has token-threshold compaction but less documented hook
  surface. Feasible as a wrapper process that tails logs.
- **Aider.** Exposes command hooks; transcripts are files. A scribe is
  straightforward here, similar to Claude Code.
- **Generic (API-level).** Run the scribe as a local HTTP proxy that the
  harness points at instead of `api.anthropic.com`. Intercept every
  request, enrich with scribe context, store every response. Universal
  but invasive — and breaks prompt caching unless you carefully mirror
  cache behavior.

---

## 7. The case against building this

Before the risks section — which lists tactical failure modes assuming you
build it — here's the strategic case for *not* building it at all. Read
this first and only proceed if none of it lands.

### 7.1 The problem you think you're solving may not be the real problem

LLM coding agents fail in a predictable rank order:

1. **Misunderstand the task** (wrong mental model of what the user wants).
2. **Misunderstand the code** (wrong mental model of the current state).
3. **Take a bad action under uncertainty** (confidently wrong tool call).
4. **Forget something said earlier in the session.**
5. **Forget something said in a prior session.**

A scribe addresses #4 and #5. Those are real but they're the *tail*. Most
bad agent turns are #1–#3 — confidence calibration and comprehension
problems, not memory problems. More memory doesn't fix a model that's
confidently wrong; it just gives it more to be confidently wrong about.

Before building a scribe, time a week of sessions. Count how often you
say "you forgot that I…" vs. "you misunderstood what I wanted." If the
second is more common, the scribe is treating the wrong disease.

### 7.2 The harness already does 70% of this

Claude Code ships:

- A three-tier compaction engine tuned for cache economics that you
  won't match in a weekend.
- `MEMORY.md` auto-memory that the primary curates itself.
- Subagents with their own context windows for domain isolation.
- Hooks that let you inject context without a separate daemon.
- `/clear`, `/compact [hint]`, and user-written slash commands.

OpenCode ships ~52 lifecycle hooks and a first-class plugin model. Cursor
has Automations with memory notepads. The platforms are actively closing
the gap you're aiming at. Anything you build now competes with their
next release, not their current state. That's a losing footrace for a
side project.

### 7.3 Two stochastic systems is worse than one

Debugging a single LLM agent that did something weird is already hard —
you're reconstructing a non-deterministic reasoning process from a log.
Add a scribe and every weird turn has two possible causes: the primary's
reasoning, or the scribe's injection poisoning the primary's context.
You can't A/B test the counterfactual because reruns are non-deterministic.
You will spend real time wondering "would it have done this without the
scribe?" and never get a clean answer.

Worse, the interaction is one-way and invisible by design. The primary
can't tell you "the scribe told me to do this" because the primary
doesn't know the scribe exists. You lose the most important tool in LLM
debugging: asking the model to explain itself.

### 7.4 Hallucination compounds; the scribe is a lossy encoder

Each summarization pass drops detail and adds paraphrase. If the scribe
writes a digest, and then a consolidator summarizes digests, and the
primary acts on the summary-of-summary, errors compound across three
generations of lossy compression. Morph's compaction-vs-summarization
analysis is blunt about this: LLM summarization "can introduce
hallucinations, paraphrase exact details, and lose technical specifics."

The mitigation ("keep raw tool outputs, only summarize prose reasoning")
is sound but means your scribe mostly doesn't save tokens — because
tool outputs are usually where the tokens are. The economic case erodes.

### 7.5 Confabulation: the primary will cite things it didn't know

If the scribe injects "the user prefers X" into the primary's context,
and the user later asks "why did you do X?", the primary will cheerfully
invent a reason — possibly the right one, possibly a plausible
fabrication. It has no way to say "a scribe told me." Provenance stamps
help but don't solve this; the primary reads the stamp and still narrates
*as if* it remembered. This degrades the user's ability to trust the
agent's self-reports, which is itself a load-bearing property of the
workflow.

### 7.6 Economics

Running a second model on every turn — even Haiku — is a multiplier on
per-session cost. For a heavy Claude Code user, that's non-trivial.
The value has to be *visible* to justify it, and most of the scribe's
value is invisible (fewer forgotten facts, better digests you never
read). Benchmarking is hard because the counterfactual is missing.

You will end up paying 1.3–2× for a feature you can't cleanly measure.

### 7.7 Cache invalidation is brutal

Anthropic's prompt cache is what makes Claude Code economically viable
on long sessions. The cache keys on a stable prefix. Any scribe
injection that modifies earlier context busts the cache for the rest of
the session. Claude Code's own compaction engine uses surgical
`cache_edits` specifically to avoid this — a hard-won engineering
investment you'd be working around.

Suffix-only injection (via `UserPromptSubmit`'s `additionalContext`) is
safe but limits the scribe to *append-only* influence. Anything stronger
trades dollars for cleverness, and the exchange rate is bad.

### 7.8 The "invisible to primary" framing is subtly incoherent

The scribe is supposed to influence the primary without the primary
knowing. But the primary reads its entire context every turn, including
scribe injections. It *will* see them. "Invisible" can only mean "not
addressed by name." The primary will notice notes it didn't write,
treat them as its own prior work, and reason from them. That's not
invisibility — that's identity confusion. The lawyer-passing-notes
metaphor breaks because the lawyer *knows* the notes aren't their own
thoughts. The LLM doesn't have that distinction.

### 7.9 Autonomy creep — users want less of it, not more

The industry is moving toward *more user control* over what agents
remember, not less. ChatGPT's memory feature has a visible panel users
can edit and wipe. Anthropic's memory is opt-in and inspectable. A
scribe that autonomously rewrites your agent's view of reality is the
opposite direction — and users get uncomfortable fast when they realize
how much implicit state is shaping outputs.

If you ship this, expect someone to ask "wait, what's actually in my
context right now?" and not have a clean answer for them.

### 7.10 Maintenance tax

Claude Code ships updates weekly. Hook names change, JSONL formats
shift, compaction internals get rewritten. OpenCode is moving even
faster. A scribe that hooks into harness internals is signing up for
perpetual breakage. This is a side project that wants to be a full-time
job.

The "harness-agnostic core CLI" mitigation (stage 5 in §8) helps, but
only for the *core*. Every harness adapter is still a moving target.

### 7.11 Prompt injection blast radius grows

If the primary reads a malicious tool result today, worst case is one
bad turn — the user sees it and course-corrects. If the *scribe* reads
that same result, extracts "user instructions" from it, and writes them
to a memory file that's loaded on every future session, the attack is
now persistent and silent. The scribe widens the blast radius of every
upstream vulnerability.

### 7.12 Simpler alternatives dominate

Before building a scribe, try:

- **Write better `CLAUDE.md` / `AGENTS.md` files by hand.** You know
  what matters; the scribe is guessing.
- **Use `/compact "keep the decision log and open threads"`** — the
  harness already accepts steering hints.
- **Keep a `NOTES.md` the primary edits itself** during the session.
  One file, one actor, no coordination problem.
- **Start fresh sessions with a prepared kickoff prompt** that
  summarizes prior work. You, the human, are the scribe. You're better
  at it than a small model.

If any of these work, the scribe is over-engineered.

### 7.13 Negative-result research exists, it just isn't loud

Letta's own "Benchmarking AI Agent Memory: Is a Filesystem All You
Need?" quietly suggests that the sophisticated memory architectures
often underperform a well-organized filesystem the agent already has
tools to read. If you squint, that's the answer: *the primary agent
plus well-organized files beats the primary agent plus a scribe plus
worse-organized files*. The scribe's value proposition depends on
doing better organization than you could do by hand — and it's a small
model doing it.

### 7.14 When the scribe *is* worth building

Not never. The honest cases:

- You're running **multi-day, multi-session** projects where
  hand-curation doesn't scale and prior-session recall genuinely
  matters.
- You're running **many agents in parallel** (Copilot `/fleet`,
  agent swarms) and need cross-agent consolidation — the original
  ClawPort scribe use case.
- You're building **a product**, not a personal tool, and the scribe
  is a differentiator worth the maintenance tax.
- You want to **research** the architecture, publish, and don't care
  whether it beats filesystem + prompts in practice.

For a solo developer iterating in Claude Code on one project, the
honest answer is probably: don't. Write better CLAUDE.md files, use
`/compact` deliberately, and call it done.

---

## 8. Risks and tradeoffs

1. **Hallucination drift.** Summaries lose detail. Every summarization
   round adds noise. Keep original tool outputs; summarize only prose
   reasoning.
2. **Prompt cache invalidation.** The scribe's injections must be suffix-
   appended, not prefix-injected, or you double the cost per turn. See
   Claude Code's preference for `cache_edits` over message rewrites.
3. **Prompt injection.** If the scribe reads web content or tool output and
   echoes it into the primary's context, a malicious tool result could
   smuggle instructions through the scribe. Sanitize and frame scribe
   output as quoted evidence, not directives.
4. **Transparency & trust.** The primary agent may act on "notes" it didn't
   write. If the user asks "why did you do X?", the agent may confabulate a
   reason. The scribe should stamp every injected block with a clear
   provenance marker (`<scribe-digest source="2026-04-14T09:00:00Z">`) so
   the primary can cite it.
5. **Recursion.** If the scribe's output enters the transcript and the
   scribe then reads its own output, it will amplify its own biases.
   Mark scribe blocks and skip them on re-read.
6. **Over-curation.** The scribe may drop the detail the primary needs.
   Keep a "full history" store separate from the curated digest, and
   let the primary query it.
7. **User consent.** The scribe is reading everything the user types.
   Make its existence visible in settings; let the user disable it;
   never ship it by default.

---

## 9. What `sagent` actually is, as built

The MVP landed as a **read-only session observer**, not an injector. See
`README.md` for usage and `nix/home-manager/sagent.nix` (via the infra
repo) for the deployment module.

**Shipped (v0.2.0):**

- JSONL parser (`sagent/parser.py`) normalizing Claude Code session
  records into an Event stream.
- Rule-based `timeline.md` (`sagent/digest.py`): chronology, tool
  inventory, files touched. Zero LLM cost.
- LLM-based `summary.md` + `understanding.md` (`sagent/understand.py`):
  running prose digest plus decisions, open threads, ideas, user
  preferences, and risks. Defaults to `claude-sonnet-4-6` via the
  Anthropic Python SDK with prompt caching; falls back to `claude -p`
  subscription when `ANTHROPIC_API_KEY` is absent.
- File-polling watcher with quiet-period debounce (`sagent/watcher.py`).
  `watch-all` follows every project in `~/.claude/projects/` at once.
- CLI (`sagent/cli.py`): `digest`, `digest-all`, `watch`, `watch-all`,
  `list`.
- Default output: `~/Obsidian/sagent/<hostname>/<project>/<session>/*.md`,
  overridable via `$SAGENT_OUT` or `--out`.
- Nix flake with `packages.default`, `apps.default`, `devShells.default`.
- Home-manager module with `services.sagent.enable`, systemd user
  service, optional `apiKeyFile` for opnix-style raw key secrets.
- 27 tests covering parser, digest, watcher, and CLI helpers.

**Not built, deliberately.** The "shadow writer" framing in §3.3 talks
about a spectrum from passive cataloging up to transcript rewriting and
API-proxy context replacement. sagent stops at passive cataloging
(level 1). None of these are implemented:

- Injection into the primary agent's context (§3.3 levels 3–6).
- Memory-file curation — no writes under `~/.claude/`.
- Pre-compaction augmentation hooks.
- Cross-session consolidation.
- Opencode / Cursor / Codex integration.

The case against building further (§7) still applies to each subsequent
stage. Revisit only if the level-1 digests turn out to be materially
useful in practice — and only then, advance one level at a time.

**Future stages, if they're ever justified:**

1. *Memory curation.* Let sagent own writes to `~/.claude/memory/`.
   Remove the primary agent's self-curation block from `CLAUDE.md`.
2. *Per-turn injection.* Add a `UserPromptSubmit` hook that reads the
   top-N catalog entries relevant to the user's prompt and emits them
   as `additionalContext`.
3. *Pre-compaction augmentation.* Hook `PreCompact` (Claude Code) or
   `experimental.session.compacting` (opencode) to inject the running
   digest into the harness's compaction prompt.
4. *Harness-agnostic core.* Extract the digest/extract/recall pipeline
   into a library so the same engine drives Claude Code hooks, opencode
   plugins, and an API-proxy mode.

---

## Sources

- [Letta memory management](https://docs.letta.com/advanced/memory-management/)
- [MemGPT paper](https://research.memgpt.ai/)
- [Letta memory blocks](https://www.letta.com/blog/memory-blocks)
- [Letta agent memory](https://www.letta.com/blog/agent-memory)
- [Claude Code hooks reference](https://code.claude.com/docs/en/hooks)
- [Claude Code subagents](https://code.claude.com/docs/en/sub-agents)
- [Claude Code session storage](https://claude-world.com/tutorials/s16-session-storage/)
- [Claude Code session hooks — auto-load context](https://claudefa.st/blog/tools/hooks/session-lifecycle-hooks)
- [Claude Code subagents memory](https://aipromptsx.com/blog/advanced-claude-code-subagents-memory)
- [Claude Code hook control flow — Steve Kinney](https://stevekinney.com/courses/ai-development/claude-code-hook-control-flow)
- [Claude Code compaction engine](https://barazany.dev/blog/claude-codes-compaction-engine)
- [OpenCode plugins](https://opencode.ai/docs/plugins/)
- [OpenCode agents](https://opencode.ai/docs/agents/)
- [Does OpenCode support hooks?](https://dev.to/einarcesar/does-opencode-support-hooks-a-complete-guide-to-extensibility-k3p)
- [oh-my-opencode](https://ohmyopencode.com/)
- [awesome-opencode](https://github.com/awesome-opencode/awesome-opencode)
- [Cursor 2.0 blog](https://cursor.com/blog/2-0)
- [Cursor Composer](https://cursor.com/blog/composer)
- [Cursor 3 / Automations](https://www.datacamp.com/blog/cursor-3)
- [GitHub Copilot /fleet](https://github.blog/ai-and-ml/github-copilot/run-multiple-agents-at-once-with-fleet-in-copilot-cli/)
- [Taming agent sprawl — ClawPort](https://www.clawport.dev/blog/taming-agent-sprawl)
- [Building an AI agent squad — Marcus Felling](https://marcusfelling.com/blog/2026/building-an-ai-agent-squad-for-your-repo)
- [Gemini Scribe](https://allen.hutchison.org/category/projects/gemini-scribe/)
- [How Kimi, Cursor, and Chroma train agentic models — Phil Schmid](https://www.philschmid.de/kimi-composer-context)
- [Code agent orchestra — Addy Osmani](https://addyosmani.com/blog/code-agent-orchestra/)
- [Context compaction research gist — badlogic](https://gist.github.com/badlogic/cd2ef65b0697c4dbe2d13fbecb0a0a5f)
- [Compaction vs summarization — Morph](https://www.morphllm.com/compaction-vs-summarization)
- [FlashCompact — Morph](https://www.morphllm.com/flashcompact)
- [Compressing context — Factory.ai](https://factory.ai/news/compressing-context)
- [JetBrains — efficient context management](https://blog.jetbrains.com/research/2025/12/efficient-context-management/)
- [Extending LLM conversations 10x with compaction — dev.to](https://dev.to/amitksingh1490/how-we-extended-llm-conversations-by-10x-with-intelligent-context-compaction-4h0a)
- [Microsoft Agent Framework — compaction](https://learn.microsoft.com/en-us/agent-framework/agents/conversations/compaction)
- [Anthropic — effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Context engineering part 2 — Phil Schmid](https://www.philschmid.de/context-engineering-part-2)
- [Context engineering guide — promptingguide.ai](https://www.promptingguide.ai/guides/context-engineering-guide)
- [Context engineering — Weaviate](https://weaviate.io/blog/context-engineering)
- [Dynamic context retrieval — Airbyte](https://airbyte.com/agentic-data/dynamic-context-retrieval)
- [Agent-situations — Dave Hulbert (ephemeral context injection)](https://github.com/dave1010/agent-situations)
- [OpenAI Agents SDK — orchestration](https://openai.github.io/openai-agents-python/multi_agent/)
- [LlamaIndex multi-agent patterns](https://developers.llamaindex.ai/python/framework/understanding/agent/multi_agent/)
- [Azure — AI agent design patterns](https://learn.microsoft.com/en-us/azure/architecture/ai-ml/guide/ai-agent-design-patterns)
- [Supervisor pattern for voice agents — LiveKit](https://livekit.com/blog/supervisor-pattern-voice-agents)
- [AWS multi-agent orchestration](https://aws.amazon.com/solutions/guidance/multi-agent-orchestration-on-aws/)
- [VoltAgent awesome-ai-agent-papers 2026](https://github.com/VoltAgent/awesome-ai-agent-papers)
- [simonw/claude-code-transcripts](https://github.com/simonw/claude-code-transcripts)
