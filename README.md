# AI Knowledge Feeds: a self-updating AI knowledge base + MCP server for Claude Code

Pulls official Anthropic docs, Claude Code / Codex changelogs and (optionally) creator videos into a local,
searchable knowledge base. Claude writes distilled notes, SQLite FTS indexes everything, and an MCP server
exposes it to Claude Code. Official sources are marked `canonical`, creator content `practice`, so tools can
prefer docs on conflicts and flag stale practice advice.

Why: a coding agent works better with the right two pointers than with a dumped context. This repo keeps the
sources fresh and cheap to query; the companion repo
[`claude-code-toolkit`](https://github.com/mariochasco78557/claude-code-toolkit) holds the per-prompt router hook.

## Architecture

```
sources.yaml
 (docs, changelogs, GitHub, YouTube, local .md)
        |
        v
  daily sync (systemd timer, throttled, retry backoff)
        |
        v
  transcripts + notes distilled by Claude (headless `claude -p`)
        |
        v
  data/ (Markdown, git-friendly) --> index/ (SQLite FTS + generated pages)
        |
        v
  MCP server `kb-mcp`
   kb_search / kb_get / kb_whats_new / kb_status / kb_topics
        |
        v
  per-prompt router hook (UserPromptSubmit) --> claude-code-toolkit
        |
        v
  weekly promotion --> Markdown vault (git branch, you merge)
```

## Features

- Authority model: `canonical` (official docs, changelogs) beats `practice` (creators); results carry both.
- Fetch retry backoff for failed items: 1 h / 6 h / 24 h / 48 h, state saved per item, so interrupted runs resume.
- Proxy health check before YouTube fetches (`KB_PROXY` env var or `proxy:` per source); a down proxy skips the
  run without burning attempts.
- `kb doctor`: one-screen health check (proxy, pending/backoff queue, failures, systemd timers).
- Throttling and capped systemd units (Nice, CPU/memory limits) for shared machines.
- Fragment retrieval: search returns chunks, `kb_get` returns only the section or timestamp you ask for.
- Weekly promotion: `kb vault-candidates` lists new items against the page names of a Markdown vault, and
  `ops/kb_promote.py` proposes additions as a git branch (never `main`); you review and merge.
- Local Markdown import (`local_md`, `generic_md` importer) for private notes that stay out of git.

## Quick start

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), `git`. LLM note generation needs the `claude`
CLI on `PATH`; set `KB_NO_LLM=1` to skip it. GitHub sources use `gh`.

```bash
git clone https://github.com/mariochasco78557/ai-knowledge-feeds
cd ai-knowledge-feeds
uv sync
cp sources.example.yaml sources.yaml
uv run kb sync
uv run kb search "prompt caching"
uv run kb status
uv run kb doctor
```

Data lives in `./data`, `./index`, `./state` (all git-ignored). Set `AI_KB_ROOT=/some/dir` to move them
(a `sources.yaml` inside that directory is picked up too).

Run the tests with `uv run pytest -q` (offline, no data needed).

### MCP server for Claude Code

Add to your project's `.mcp.json` (or run
`claude mcp add --scope user ai-kb -- uv run --directory /path/to/ai-knowledge-feeds kb-mcp`):

```json
{
  "mcpServers": {
    "ai-kb": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/ai-knowledge-feeds", "kb-mcp"],
      "env": { "AI_KB_ROOT": "/path/to/ai-knowledge-feeds" }
    }
  }
}
```

Tools: `kb_search`, `kb_get`, `kb_whats_new`, `kb_status`, `kb_topics`. Protocol: search first, then fetch
only the fragment you need.

## Scheduling (systemd user timers)

```bash
bash ops/install.sh        # review it first; it writes units to ~/.config/systemd/user
```

| Unit | When | Does |
|---|---|---|
| `kb-sync.timer` | daily | `kb sync` (docs, GitHub, new videos) |
| `kb-trickle-weekday/weekend.timer` | frequent, daytime | processes a few queued videos (`--pending-only`) |
| `kb-promote.timer` | weekly (opt-in) | `ops/kb_promote.py`, needs `KB_VAULT` (a git repo of your vault) |

Units assume `uv` in `~/.local/bin` and are capped (Nice 19, CPUQuota 50%, MemoryMax 1400M). Pass environment
(`KB_PROXY`, `KB_VAULT`, `KB_GIT_NAME`, ...) with `systemctl --user edit <unit>`.

## Numbers (from the author's own deployment)

- 31 sources: official docs, changelogs, GitHub releases and creator channels.
- Per-prompt hook: about 0.25 s and at most 1200 characters injected.
- Router precision on a fixed test set: 0.70 first hit, 0.80 within the top 2, no false positives on short prompts.

## What broke and how it was fixed

Details in [`docs/AI_Knowledge_Base_case_study.pdf`](docs/AI_Knowledge_Base_case_study.pdf).

- YouTube blocked the server IP: added a proxy health check, retry backoff (1 h / 6 h / 24 h / 48 h) and `kb doctor`.
- Low router precision: added synonyms, ES/EN stemming, IDF weights and top-2 results, measured against a fixed test set.
- A Mac with Python 3.9 could not run the hook: made the hook code compatible with older Python so one set of hooks runs on both machines.

## Screenshots

Real outputs (sanitized); more in [`docs/sample_outputs.md`](docs/sample_outputs.md).

![Router hook dry run](docs/screenshot_1.png)
![Status line](docs/screenshot_2.png)
![CLI search](docs/screenshot_3.png)

## Configuration reference

| Variable | Purpose |
|---|---|
| `AI_KB_ROOT` | Where `data/`, `index/`, `state/` live (default: repo root) |
| `KB_PROXY` | SOCKS5/HTTP proxy for YouTube requests |
| `KB_NO_LLM` | Skip Claude note generation |
| `KB_CLAUDE_BIN` | Path of the `claude` CLI |
| `KB_THROTTLE_*` | Override `throttle:` settings from `sources.yaml` |
| `KB_VAULT`, `KB_VAULT_WIKI` | Vault repo and wiki directory for the weekly promotion |

## License

MIT, see [LICENSE](LICENSE).

Author: Mario Chasco, [flowaraai.com](https://flowaraai.com), [youtube.com/@mariochasco_AI](https://youtube.com/@mariochasco_AI)
