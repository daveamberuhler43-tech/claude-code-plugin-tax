# claude-code-plugin-tax

Every Claude Code plugin, skill folder and MCP connector you install loads text into the context window **before
you type a word**, in every session, whether you call it or not. This script reads the session logs Claude Code
already writes to disk (`~/.claude/projects/*/*.jsonl`) and prints, for each source: how many tokens it loads before
the first message, and how many times Claude actually called it.

It prints **plugin / connector names and numbers only**. No prompt text, file contents or messages are written out.

Companion to the Blast Radius video *The Best Claude Code Plugins, Ranked by What They Cost Me*.

## Run it

```bash
pip install tiktoken
python plugin_tax.py               # writes plugin-tax.json next to the script and prints a ranked table
python plugin_tax.py --selftest    # synthetic log with a known answer, plus a mutation test
python plugin_tax.py --session=<file>.jsonl   # measure one specific session's first request
```

Score each source yourself as `uses / (tokens / 1000)`. In the video: S >= 100, A >= 10, B >= 2, C >= 0.5, else F.

## What is measured and what is estimated

| Item | Status |
|---|---|
| Which skills, agents, tool names and hook text a session loaded before its first request | **measured**, from that session's log |
| Tokens per source | **estimate, a floor**: tiktoken `cl100k` is not Claude's tokenizer |
| Uses | tool calls (Skill, Agent, MCP tools) plus slash commands across all your logs, de-duplicated by tool-use id |
| Hooks | a SessionStart hook runs every session by design, so its "uses" undercount it |

To measure a real before/after, do what the video did: same app version, same folder, the same one-word prompt,
three fresh sessions with a plugin on and three with it off, and compare each session's first request
(`input_tokens + cache_creation_input_tokens + cache_read_input_tokens` in the log). Run-to-run noise was under
30 tokens on one version; an app update can move the number by thousands, so never compare across versions.

## Caveats

- One person's logs; your tier list will look different, and that's the point.
- A plugin renamed between versions splits its uses across two names; merge them yourself.
- The desktop app attaches some connectors itself; they show up as sources you never installed.

MIT licence.
