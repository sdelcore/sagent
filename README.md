# sagent

A scribe agent for Claude Code sessions. It watches
`~/.claude/projects/<project>/<session>.jsonl` files as they're appended to
and writes markdown digests describing what happened, what was decided, and
what's still in flight. Runs out-of-band from Claude Code itself — invisible
to the primary agent.

- **Rule-based** `timeline.md` — tool inventory, files touched, turn-by-turn
  chronology. Zero LLM cost.
- **LLM-based** `summary.md` + `understanding.md` — running prose digest plus
  extracted decisions, open threads, ideas mentioned in passing, user
  preferences, and risks/blockers.

See `research.md` for the full design space, prior art (MemGPT, Letta, the
ClawPort "scribe" pattern, Cursor Automations), and the honest case against
building this.

## Install

### Ad-hoc via nix

```bash
nix run github:sdelcore/sagent -- digest      # digest current cwd's session
nix run github:sdelcore/sagent -- digest-all  # digest every session, one pass
nix run github:sdelcore/sagent -- list -v     # list all projects + sessions
```

### As a home-manager module (NixOS + flakes)

Add sagent as a flake input:

```nix
inputs.sagent = {
  url = "github:sdelcore/sagent";
  inputs.nixpkgs.follows = "nixpkgs";
};
```

Import the module and enable it in your home config. The module lives in
this repo under `nix/home-manager/` — reference it from the flake input, or
copy it into your own config. In the reference infra setup:

```nix
# home/<hostname>.nix
imports = [ ./modules/sagent.nix ];
services.sagent = {
  enable = true;
  apiKeyFile = "/var/lib/opnix/secrets/anthropicApiKey";  # optional
};
```

This installs the `sagent` binary and runs `sagent watch-all` as a systemd
user service. Output lands in `~/Obsidian/sagent/<hostname>/`.

### Dev shell

```bash
git clone https://github.com/sdelcore/sagent
cd sagent
nix develop
uv sync
uv run pytest
uv run sagent list -v
```

## Usage

```
sagent [options] COMMAND [args]

commands:
  digest [PATH]        digest a single session (default: current cwd's latest)
  digest-all           digest every session across every project
  watch [PATH]         watch one session/project, digest on append settle
  watch-all            watch every project, digest each session as it settles
  list [-v]            list all Claude Code projects with sessions

common flags:
  --out PATH           output root (default: $SAGENT_OUT or
                       ~/Obsidian/sagent/<hostname>/ or ./sagent-out)
  --model MODEL        model id (default: claude-sonnet-4-6)
  --no-llm             rule-based timeline only, no LLM cost
```

`PATH` can be a `.jsonl` file, a Claude Code project directory under
`~/.claude/projects/`, or a repo path (e.g. `~/src/myproj`) — sagent will
translate the repo path to the encoded project dir.

## Output layout

```
~/Obsidian/sagent/<hostname>/
  <project-name>/
    <session-id>/
      timeline.md        # rule-based: chronology + tool inventory
      summary.md         # LLM: 5–12 sentence running prose digest
      understanding.md   # LLM: decisions, open threads, ideas,
                         #      user preferences, risks & blockers
```

`<project-name>` strips the `-home-<user>-src-` prefix for readability
(e.g. `-home-sdelcore-src-droidcode` → `droidcode`).

## Auth

sagent uses the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python).
It authenticates via (in priority order):

1. `ANTHROPIC_API_KEY` — direct-API, billed per token
2. `CLAUDE_CODE_OAUTH_TOKEN` — minted by `claude setup-token`
3. Existing `~/.claude/` OAuth login — whatever `claude login` set up

**On a subscription host that already has `claude login`, no key setup is
required.** The SDK reuses the subscription auth and usage counts against
the Claude Code subscription quota.

To force the direct-API path, set `ANTHROPIC_API_KEY`. With the
home-manager module, set `services.sagent.apiKeyFile` to a file whose
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

## Commands in detail

### `sagent digest [PATH]`

One-shot digest of a single session. Writes `timeline.md`, `summary.md`,
`understanding.md` to the output dir. Most useful for spot-checking a
specific session or scripting.

### `sagent digest-all`

Sweeps every session in `~/.claude/projects/`, digesting each one. Use
`--min-bytes N` (default 5000) to skip trivial sessions. Safe to re-run —
each digest overwrites its output files.

### `sagent watch-all`

The main daemon mode. Polls every `.jsonl` under `~/.claude/projects/` on
a 2-second interval. Fires a digest after writes have been quiet for 3
seconds (avoids re-digesting on every append during an active turn).
Designed to be run as a systemd user service.

### `sagent watch [PATH]`

Single-project variant of `watch-all`. If `PATH` is a file, follows that
one file. If a directory, follows whichever session in that directory is
most recent.

### `sagent list [-v]`

Inventory of projects and sessions. `-v` shows the three most recent
sessions per project with their file sizes.

## Environment

| Variable | Purpose |
|---|---|
| `SAGENT_OUT` | Override output root |
| `ANTHROPIC_API_KEY` | Use direct-API auth (SDK backend, billed per-token) |
| `CLAUDE_CODE_OAUTH_TOKEN` | Use subscription OAuth token (from `claude setup-token`) |

If none are set, the SDK uses the existing `~/.claude/` login state that
`claude login` created. On subscription hosts, that means no setup.

## Design decisions

sagent is a **read-only observer** by design. It does not edit any files
under `~/.claude/`, does not inject context into Claude Code's prompt, and
does not run tools on your behalf. It watches and writes to its own output
directory. This is phase 1 of the scribe-pattern design in `research.md`;
phases 2–5 (memory curation, context injection, pre-compaction
augmentation, full transcript rewriting) are deliberately out of scope.

The `watch-all` daemon is tuned for low overhead: poll interval 2s,
quiet-period 3s, digest runs sequentially (no concurrent LLM calls). On
a quiet day it spends most of its time sleeping. A single digest of a
medium session takes ~30–60s and produces ~2–4KB of markdown.

## Privacy

`timeline.md` contains verbatim (but truncated) user prompts. `summary.md`
and `understanding.md` are LLM-rewritten but can quote you briefly. If
your output directory is synced to a third-party service, your prompts
land there too. The default `~/Obsidian/sagent/<hostname>/` layout is
conflict-free across machines but as public/private as the vault you put
it in.

## Development

```bash
nix develop         # or direnv allow
uv sync
uv run pytest       # 27 tests
nix build .#sagent  # build the installable package
./result/bin/sagent list -v
```

Layout:

```
sagent/
  parser.py      JSONL → normalized Event stream
  digest.py      Rule-based timeline.md
  understand.py  LLM-based summary.md + understanding.md
  watcher.py     File polling with quiet-period debounce
  cli.py         argparse CLI
```

## License

MIT. See `LICENSE` if included; otherwise treat as unlicensed personal
tooling.
