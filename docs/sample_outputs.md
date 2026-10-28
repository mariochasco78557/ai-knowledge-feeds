# Sample outputs (captured 2026-10-28, sanitized)

## 1. Per-prompt router hook (dry run)
`ai-kb-recall.py --dry-run "How do I configure a SessionStart hook in Claude Code?"`
```
Flowara router (paths under <vault>/wiki/; open ONLY what a line names; kb ids → ai-kb kb_get):
[ROUTER] R02 Claude Code hooks → tools/claude-code/claude-directory.md | kb docs:anthropic-claude-code-docs:en/hooks.md docs:anthropic-claude-code-docs:en/hooks-guide.md | can
--- path=router routes=['R02'] chars=319 tokens≈80 ms=1 top=[('R02', 3.62)]
```

## 2. Status line
```
[AI KB status] sync 2026-10-28 12:47 UTC ok (0 err) · pending videos 204 · Flowara candidates 42 unpromoted (week to 2026-10-25, branch merged) · canon updated 2026-10-28 (0 d)
```

## 3. CLI search (canonical docs only)
`kb search "prompt caching" --authority canonical --limit 3`
```
1. [canonical/doc] 2026-10-27 Model configuration @ Model configuration > Environment variables > Prompt caching | docs:anthropic-claude-code-docs:en/model-config.md chunk=66
   Prompt caching configuration Claude Code automatically uses [prompt caching](
2. [canonical/doc] 2026-10-26 How Claude Code uses prompt caching @ How Claude Code uses prompt caching > Disable prompt caching | docs:anthropic-claude-code-docs:en/prompt-caching.md chunk=30
   Disable prompt caching Disabling caching is occasionally useful when debugging caching behavior with a specific model or provider…
3. [canonical/doc] 2026-10-28 Prompt caching @ Caching strategies and considerations > What invalidates the | docs:anthropic-platform-docs:en/build-with-claude/prompt-caching.md chunk=23
   …As described in [Structuring your prompt](
```

## 4. ROUTER.md (first 15 lines, truncated; two internal comment lines omitted)
```
# ROUTER — topic → destination (curated; drift: `ops/router_check.py`)
<!-- Hook injects the 1-3 best lines as [ROUTER]; open ONLY what they name. kb ids → `kb_get`; "text" → `kb_search` (src=S).
Cols: id | topic | keywords (!strong, x* prefix, phrases; ES/EN stemmed; more in ops/router_synonyms.json) | vault | kb | council | auth -->

R01 | Claude Code skills | !skill,SKILL.md,habilidad,slash command,comando slash,custom command | [[claude-directory]] [[skill-writing-and-evolution]] | cc:en/skills.md | - | can
R02 | Claude Code hooks | !hook,UserPromptSubmit,SessionStart,PreToolUse,PostToolUse,statusline | [[claude-directory]] | cc:en/hooks.md cc:en/hooks-guide.md | - | can
R03 | Subagents, agent teams | !subagent,agent teams,equipo de agentes,delegar,orquestar,en paralelo,plumber | [[sequential-subagent-delegation]] | cc:en/sub-agents.md cc:en/agent-teams.md | - | can
R04 | MCP servers | !mcp,!conector,connector,servidor mcp,mcp server | [[claude-directory]] | cc:en/mcp.md | - | can
R05 | Memory, CLAUDE.md | !claude.md,!agents.md,memory,!memoria,instrucciones,regla,memorizar | [[memory]] [[claude-md-strategy]] | cc:en/memory.md | - | can
R06 | Plugins, reusable modules | !plugin,!marketplace,modulo,reutilizable,reutilizar,libreria | [[flowara-reusable-modules-index]] | cc:en/plugins/overview.md | - | can
R07 | Headless, Agent SDK, scheduled | !headless,claude -p,agent sdk,sdk,cron,!routines,tarea programada,timer,desatendido | [[agentic-workflow-ladder]] | cc:en/headless.md cc:en/scheduled-tasks.md | - | can
R08 | Permissions, settings, sandbox | !permisos,!permission,sandbox,settings.json,allowlist,bypass | [[permissions]] | cc:en/permissions.md cc:en/settings.md | - | can
R09 | Sessions, context, token economy | sesion,!compact,ventana de contexto,context window,tokens,handoff,contexto,limite de uso | [[session-discipline]] | cc:en/costs.md | - | can
```
