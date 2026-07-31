# sagent

A scribe for coding-agent sessions. It reads what your agents already
wrote to disk and produces markdown digests in your Obsidian vault (or
any directory you point it at):

- a **per-session** file capturing what happened (Summary + Understanding),
  plus a byte-exact record of every command run
- a **per-project** rolling digest (`project.md`) that accumulates
  decisions, open threads, preferences, and risks — and lets them
  resolve, supersede, or go stale rather than only pile up
- a **per-host index** (`INDEX.md`) listing every project at a glance

Two harnesses are supported, and they share one `project.md` per
directory — the same work through a different tool is still the same work:

| Harness | Source | Notes |
|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` | one file per session |
| opencode | `~/.local/share/opencode/opencode.db` | one SQLite database for everything; needs the `opencode` binary for content export |

Every output starts with **YAML front matter** so a downstream agent
can triage many projects without loading full bodies.

It runs out-of-band from the agents themselves — invisible to them. See
`research.md` for the full design space, prior art (MemGPT, Letta, the
ClawPort "scribe" pattern, Cursor Automations), and the honest case
against building this.

## Install

### Ad-hoc via nix

```bash
nix run github:sdelcore/sagent -- list -v       # inventory
nix run github:sdelcore/sagent -- digest        # current cwd's latest session
nix run github:sdelcore/sagent -- digest-all    # everything in one pass
```

### As a home-manager module

The module ships with the flake, so there is nothing to write:

```nix
# flake.nix
inputs.sagent.url = "github:sdelcore/sagent";

# home/<hostname>.nix
imports = [ inputs.sagent.homeModules.default ];
services.sagent = {
  enable = true;
  maxPerHour = 7;      # cap LLM calls per host — see Rate limiting
};
```

Rebuild, and sagent runs as a user systemd service writing to
`~/Obsidian/sagent/<hostname>/`. The unit already carries its runtime
dependencies, including `git` — rebrand detection shells out to it and fails
silently without it.

Options are declared in [`nix/hm-module.nix`](nix/hm-module.nix); read that
rather than a copy here, which is how the previous version of this section
came to document a bug. The ones worth knowing: `apiKeyFile` (bill an
Anthropic key instead of your Claude subscription), `maxPerHour`, `outDir`,
`model`, and `extraArgs` for anything the CLI takes but the module does not
name.


### Dev shell

```bash
git clone https://github.com/sdelcore/sagent
cd sagent
nix develop
uv sync
uv run pytest          # 464 tests
uv run sagent list -v
```

## Usage

```
sagent [options] COMMAND [args]
```

| Command | What it does |
|---|---|
| `digest [TARGET]` | digest one session (default: current cwd's latest) |
| `digest-all` | digest every session across every project |
| `watch [TARGET]` | watch one session/project, digest when it settles |
| `watch-all` | watch everything, both harnesses — this is what the service runs |
| `rollup [PROJECT]` | re-run the project roll-up against existing digests |
| `prune` / `purge-self` | drop thin output dirs / clean up legacy self-digests |
| `list [-v]` | inventory projects with sessions, per harness |

`TARGET` is a `.jsonl` file, a Claude Code project directory, a repo path
(`~/src/myproj` — translated to the encoded project dir), or an opencode
session id with `--harness opencode`.

Run `sagent --help` or `sagent COMMAND --help` for the full flag list; it is
generated from the parser and cannot drift the way a copy here would. The
flags worth knowing up front:

| Flag | Why |
|---|---|
| `--no-llm` | rule-based output only, no API cost — good for a first look |
| `--harness {claude-code,opencode,all}` | restrict to one source (default: all) |
| `--min-prompts N` | drop sessions thinner than N user prompts |
| `--max-per-hour N` | cap LLM calls per rolling hour (`watch-all`) |
| `--idle-seconds N` | how long a session must sit still before digesting |

## Output layout

```
~/Obsidian/sagent/<hostname>/
  INDEX.md                              # fleet-wide overview, auto-regenerated
  <project>/
    project.md                          # cumulative rolling digest (real projects)
    sessions/
      <YYYY-MM-DD>-<id8>.md             # per-session combined digest
  <scratchpad-project>/
    recent.md                           # date-grouped one-liners (no LLM cost)
    sessions/
      <YYYY-MM-DD>-<id8>.md
```

`<project>` strips the `-home-<user>-src-` prefix for readability
(`-home-sdelcore-src-droidcode` → `src-droidcode`).

### Project type: project vs scratchpad

Auto-detected from the encoded project dir name:

- **scratchpad**: `-<user>` (cwd was `$HOME`), `-tmp`, `-var-tmp`. These are
  one-off questions, not coherent projects. Output is `recent.md` only — a
  date-grouped list of one-line gists. **No LLM call** for the rollup;
  pure text concatenation.
- **project**: everything else. Output is `project.md` — a cumulative
  digest produced by an incremental LLM rollup that folds each new
  session into the existing document.

### Front matter

Every output file leads with a YAML front matter block so a downstream
agent can `head` it for triage without loading the body.

**`project.md`:**

```yaml
---
type: "project"
source: "claude-code"
project: "src-droidcode"
description: "Vite+Tauri 2 desktop app for Claude Code; migrated from React Native/Expo."
tagline: "Phase 1+2 done; Phase 3 (UI routes) pending"
remote_url: "git@github.com:acme/droidcode.git"
last_updated: "2026-04-22T13:00:00Z"
session_count: 17
sessions_last_7d: 3
decisions: 12
open_threads: 4
preferences: 5
risks: 6
---
```

`description` is stable (what the project IS, capped at 280 chars).
`tagline` is volatile (current state, regenerated each rollup). The LLM
emits both as `DESCRIPTION:` / `TAGLINE:` leading lines; sagent parses
them, caps `description` at the last word boundary with `…`, and lifts
both into front matter.

`remote_url` is the project's `git remote get-url origin`, captured from
the live repo at roll-up time. Because sagent keys projects by filesystem
path, a renamed or transferred repo would otherwise split into two
unrelated digests. When two project keys share a remote, both digests gain
a `rebrand_detected: "<old-key> → <new-key>"` line (newest by
`last_updated` is treated as canonical) so the rename can be reconciled.
It flags only — digests are never merged automatically.

**Per-session `<date>-<id8>.md`:**

```yaml
---
type: "session"
source: "claude-code"
session_id: "15955874-4045-4512-a569-2d6dc27fb8da"
short_id: "15955874"
date: "2026-04-22"
started_at: "2026-04-22T13:37:23Z"
project: "src-droidcode"
cwd: "/home/sdelcore/src/droidcode"
branch: "main"
events: 2542
prompts: 71
tools: 1117
gist: "Phase 1+2 migration committed; smoke test passing"
source_jsonl: "/home/sdelcore/.claude/projects/-home-sdelcore-src-droidcode/15955874-...jsonl"
---

# Session 15955874 — 2026-04-22
## Summary ...
## Understanding ...
```

Body is **Summary + Understanding only**. There is no embedded
chronological timeline — agents wanting forensic detail follow
`source_jsonl` to the raw Claude Code session file (always available,
authoritative, complete).

**`recent.md`** (scratchpads):

```yaml
---
type: "scratchpad"
source: "claude-code"
project: "home-sdelcore"
last_updated: "2026-04-25T11:45:17Z"
session_count_30d: 200
window_days: 30
---
```

**`INDEX.md`** is regenerated after every rollup. Reads only the front
matter from each project file (cheap, no LLM call) and produces a
single-page list of every project on the host with description,
tagline, and counts.

## Auth

sagent uses the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python).
The SDK authenticates via, in priority order:

1. `ANTHROPIC_API_KEY` — direct-API, billed per token
2. `CLAUDE_CODE_OAUTH_TOKEN` — minted by `claude setup-token`
3. Existing `~/.claude/` OAuth login — whatever `claude login` set up

**On a subscription host that already has `claude login`, no key setup
is required.** Usage counts against the Claude Code subscription quota.

To force the direct-API path, set `ANTHROPIC_API_KEY`. With the
home-manager module, point `services.sagent.apiKeyFile` at a file whose
contents are the raw key. Example with opnix:

```nix
# nix/modules/secrets/opnix.nix
secrets."anthropicApiKey" = {
  reference = "op://Infrastructure/anthropic/credential";
  mode = "0444";
};

# home/<hostname>.nix
services.sagent.apiKeyFile = "/var/lib/opnix/secrets/anthropicApiKey";
```

The launcher reads the file at start-up and exports
`ANTHROPIC_API_KEY` before exec-ing sagent.

## State and idempotence

sagent persists state at `$SAGENT_STATE` or
`$XDG_STATE_HOME/sagent/state.json` (default
`~/.local/state/sagent/state.json`). Per-session it tracks
`last_digested_size`, `last_event_index`, `last_digested_at`, and
`digest_count`. Per-project it tracks `last_rolled_up_session_id`,
`last_rolled_up_at`, and `rollup_count`.

Three knobs use this state:

- **Skip if unchanged.** Re-running a digest on a session whose source
  size hasn't changed is a no-op.
- **Incremental summarization.** When a session has grown since the
  last digest, sagent reads the prior `summary.md` + `understanding.md`
  from disk and sends only the new events (since
  `last_event_index`) to the LLM, plus the prior digest. The LLM
  returns the updated full digest. Saves tokens and produces stable
  documents.
- **Periodic drift reset.** Every Nth digest of a session
  (`--full-rebuild-every`, default 10) does a cold rebuild from full
  transcript to counter paraphrase drift across many incremental
  rounds. Set to `0` to disable.

`--no-state` runs cold every time. `--force-full` does a cold rebuild
once. Delete the state file to start over.

## Rate limiting

Two layers:

1. **Reactive.** Detects rate-limit error signatures from the Agent SDK
   (`rate limit`, `usage limit`, `5-hour`, `weekly`, `429`, `throttle`,
   …). On detection, raises `SagentRateLimitError`; the watcher catches
   it, sleeps `--rate-limit-cooldown` (default 1800s), and **does not
   mark the session as digested** so it retries on the next pass.
2. **Proactive.** `--max-per-hour N` caps LLM calls per rolling hour
   via a sliding window. Exceeding the budget sleeps until the oldest
   call ages out. `0` (the default) disables the cap.

Each session digest counts as **one** LLM call; each project rollup
counts as **one more**. So `--max-per-hour 7` ≈ 3–4 sessions/hour.

**Cross-host caveat:** if you run sagent on multiple machines, they
share your subscription quota. Configure each host's `maxPerHour` so
the sum doesn't exceed your tier's 5h limit:

| Tier | 5h limit | Suggested per-host (2 hosts) |
|---|---|---|
| Pro | 45 messages | 4–5 |
| Max-5x | 225 messages | 15–20 |
| Max-20x | 900 messages | 60+ |

## Filtering

sagent drops noise both before and after parsing the JSONL:

- **`< 5KB` source files** — Claude Code writes ~100-byte stub JSONLs
  on every invocation; never digested. Tunable via `--min-bytes`.
- **`< 1` user prompt** — sessions where the agent had no real prompt
  are dropped after parse. Tunable via `--min-prompts`.
- **Self-generated sessions** — JSONL files whose first user prompt
  matches sagent's own prompt headers are skipped (legacy cleanup;
  v0.7+ uses `--no-session-persistence` to prevent these in the first
  place). Run `sagent purge-self` once to delete the leftovers from
  pre-v0.7.
- **For LLM digestion**, the transcript sent to the LLM omits:
  - assistant_thinking blocks (internal reasoning, not what
    "happened")
  - successful tool_result content (file dumps, command stdout)
  - Claude Code's auto-injected wrappers
    (`<local-command-caveat>`, `<system-reminder>`, `<bash-stdout>`,
    `<bash-stderr>`, `<command-stdout>`, `<command-stderr>`)
  - tool_use inputs collapsed to a brief signature
    (`(tool: Edit /x/y.py)`, `(tool: Bash: git status)`)

Errors from tool calls are kept — they shape the narrative.

## Secrets policy in digests

Both digest prompts (per-session and project rollup) lead with a
`SECRETS POLICY` block stating that any credentials in the source
material have been rotated and instructing the LLM to:

- Acknowledge an exposure occurred (so you can verify rotation
  actually happened)
- **Not** echo literal credential values — no tokens, passwords,
  hashes, or `op://...` paths that resolve to secret material
- Hostnames, IPs, file paths, and `op://...` reference paths *without*
  secret content are fine to mention

This avoids the LLM refusing to summarize sessions that contain
credentials.

## Commands in detail

### `sagent digest [PATH]`

One-shot digest of a single session. Writes the per-session file under
`<out>/<project>/sessions/<date>-<id8>.md` and (unless `--skip-rollup`)
runs the project rollup. Most useful for spot-checking.

### `sagent digest-all`

Sweeps every session from both harnesses, digesting each one. Real projects
are processed before scratchpads, so the high-value cumulative digests are
not starved behind thousands of one-off sessions. Honors `--min-bytes`,
`--min-delta`, `--min-prompts`, `--max-per-hour`. Stops on
`SagentRateLimitError`.

### `sagent watch-all`

The main daemon mode, and what the systemd service runs. Sweeps both
harnesses: Claude Code's `.jsonl` files on a 2s filesystem scan, and the
opencode database on a slower 30s clock (`--db-poll-seconds`), since a
handful of SQLite queries is not free and the settle window is minutes wide
anyway.

Fires a digest after a session has been quiet for `--idle-seconds` (default
**300** = 5 min) — long enough not to summarize mid-turn. Hydrates from state
on startup so a service restart doesn't re-digest the corpus.

Liveness for opencode comes from the rows, never the database file's mtime:
under WAL, with a long-lived opencode server holding the connection, that
mtime was measured **8 minutes** behind the newest session data.

### `sagent watch [PATH]`

Single-project variant. Same idle threshold, same state.

### `sagent rollup [PROJECT]`

Re-run the project-level rollup against existing per-session digests
without redoing them. Useful after migration or to refresh stale
`project.md`.

### `sagent prune`

Walks the output tree and removes per-session digests whose source
JSONL has fewer than `--min-prompts` user prompts (default 1). Also
`--prune-orphans` to drop outputs whose source JSONL no longer exists.
`--dry-run` to preview.

### `sagent purge-self`

One-shot cleanup: scans `~/.claude/projects/` and **deletes** JSONL
files whose first user prompt matches sagent's own prompt headers
(`Session \``, `Project: \``, `PRIOR SUMMARY:`, `PRIOR PROJECT.md:`).
These are leftovers from before v0.7 added `--no-session-persistence`
to the Agent SDK call. Use `--dry-run -v` to preview.

### `sagent list [-v]`

Inventory of Claude Code projects with session counts and project
type (project vs scratchpad).

## Environment

| Variable | Purpose |
|---|---|
| `SAGENT_OUT` | Override output root |
| `SAGENT_STATE` | Override state file path |
| `XDG_STATE_HOME` | Default state base if `SAGENT_STATE` unset |
| `ANTHROPIC_API_KEY` | Direct-API auth (per-token billing) |
| `CLAUDE_CODE_OAUTH_TOKEN` | Subscription OAuth token (`claude setup-token`) |

If none of the auth vars are set, the SDK uses the existing
`~/.claude/` login state.

## Privacy

Per-session files contain LLM-rewritten paraphrases that may quote
your prompts briefly. The `source_jsonl` field in front matter points
at the verbatim Claude Code session. Front matter `cwd`, `branch`, and
`gist` are visible to anything that reads the directory.

If your output dir is synced to a third-party service (Obsidian Sync,
iCloud, Dropbox, Syncthing-to-cloud), your digests propagate there.
The default `~/Obsidian/sagent/<hostname>/` layout is conflict-free
across machines (per-host subdir) but as public/private as the vault
you put it in.

## Design

sagent is a **read-only observer**. It does not edit anything under
`~/.claude/`, does not inject context into Claude Code's prompt, and
does not run tools on your behalf. It watches and writes to its own
output directory. This is phase 1 of the scribe-pattern design in
`research.md`; phases 2–5 (memory curation, context injection,
pre-compaction augmentation, full transcript rewriting) are
deliberately out of scope.

The `watch-all` daemon is tuned for low overhead: 2s poll, 5-min
idle threshold, sequential digests, no concurrent LLM calls. On a
quiet day it spends most of its time sleeping.

`source: "claude-code"` is in every front matter — forward-compatible
with multi-source plugins (OpenCode, others) coming later.

## Development

```bash
nix develop                # or direnv allow
uv sync
uv run pytest              # 464 tests
nix build .#sagent         # build the installable package
./result/bin/sagent list -v
```

Layout:

```
sagent/
  parser.py        JSONL → normalized Event stream
  digest.py        per-session markdown composition + front matter
  understand.py    LLM-driven Summary + Understanding generator
  rollup.py        project.md + recent.md + INDEX.md generators
  watcher.py       file polling with idle-settle + rate-limit handling
  rate.py          sliding-window limiter + error-text detection
  state.py         persistent JSON state for sessions and projects
  frontmatter.py   tiny YAML emitter/splitter (no PyYAML dep)
  cli.py           argparse entry-point
```

## Releases

- **1.0** — first stable. Incorporates 0.11's project source context
  reading: rollups now ground their description in the actual project
  directory (README, manifests, CLAUDE.md, top-level listing) rather
  than transcript paraphrase alone. Verified running on nightman and
  dayman.
- **0.11** — `project_context.py` reads top-level anchor files from
  the project's cwd and feeds them to the rollup LLM as
  `PROJECT SOURCE CONTEXT`.
- **0.10** — YAML front matter on every output, INDEX.md per host,
  Timeline section dropped from session files (use `source_jsonl`).
- **0.9** — transcript filtering before LLM call (drop thinking,
  successful tool_result, noise-tag stripping).
- **0.8** — SECRETS POLICY in digest prompts.
- **0.7** — `--no-session-persistence` on SDK calls + `purge-self`
  command + skip self-generated sessions.
- **0.6** — rate limiting (`--max-per-hour`,
  `--rate-limit-cooldown`) + project priority over scratchpads.
- **0.5** — project rollup, scratchpad recent.md, single-file per
  session, project-type detection.
- **0.4** — persistent state, incremental summarization, 5-min idle
  default.
- **0.3** — Claude Agent SDK backend (subscription auth without API
  key).
- **0.2** — Sonnet default + README + research.md.
- **0.1** — initial: parser, rule-based timeline, LLM digest, watcher.

## License

MIT. See `LICENSE` if included; otherwise treat as unlicensed personal
tooling.
