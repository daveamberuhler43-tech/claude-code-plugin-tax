#!/usr/bin/env python3
"""
plugin_tax.py - what each installed Claude Code plugin / connector loads before the first message, and how often it
was actually used. Numbers only; reads the local session logs (~/.claude/projects/*/*.jsonl).

    python plugin_tax.py               # writes plugin-tax.json and prints a ranked table
    python plugin_tax.py --selftest    # synthetic log with a known answer

COST (per source, from the newest session's attachments recorded before its first request):
  skills    lines of the skill list whose name is "<plugin>:<skill>"            (unprefixed -> "(user skills)")
  agents    lines of the agent list whose type is "<plugin>:<agent>"            (unprefixed -> "(user agents)")
  tools     deferred tool-name lines "mcp__<server>__<tool>"; plugin servers are "plugin_<plugin>_<server>"
  hooks     SessionStart hook output, attributed by the hook's command (plugin cache path or known name)
  tokens    tiktoken cl100k of that text: a RULER, not Claude's tokenizer, so treat it as a floor
USE (all logs since the first session, sub-agents included, de-duplicated by tool_use id):
  Skill tool calls by prefix, Agent tool calls by subagent_type prefix, MCP tool calls by server, slash commands.
Hooks have no "use" count: they run on every session by design.
"""
import collections
import glob
import json
import os
import re
import sys

import tiktoken

ENC = tiktoken.get_encoding("cl100k_base")
HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
TOP = os.path.join(HOME, ".claude", "projects", "*", "*.jsonl")
SUB = os.path.join(HOME, ".claude", "projects", "*", "*", "subagents", "*.jsonl")
tok = lambda s: len(ENC.encode(s, disallowed_special=()))


def plugin_of_name(name, default):
    return name.split(":", 1)[0] if ":" in name else default


def server_of_tool(name):
    parts = name.split("__")
    if len(parts) < 3 or parts[0] != "mcp":
        return None
    srv = parts[1]
    m = re.match(r"plugin_([^_]+(?:-[^_]+)*)_", srv)
    return ("plugin:" + m.group(1)) if m else ("server:" + srv)


def hook_owner(a):
    cmd = str(a.get("command", ""))
    m = re.search(r"plugins[\\/]+cache[\\/]+[^\\/]+[\\/]+([^\\/]+)", cmd)
    if m:
        return m.group(1)
    low = cmd.lower()
    for key in ("caveman", "ponytail", "superpowers", "claude-mem", "impeccable"):
        if key in low or key in str(a.get("stdout", "")).lower()[:200]:
            return key
    return "(other hook)"


def json_strings(x):
    """Every string inside a parsed JSON value (decoded, so newlines are real newlines)."""
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from json_strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from json_strings(v)


def first_request_attachments(lines):
    out = []
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") == "assistant":
            break
        a = o.get("attachment")
        if o.get("type") == "attachment" and isinstance(a, dict):
            out.append(a)
    return out


def cost(atts):
    """-> {source: {"items": n, "tokens": t, "kind": {...}}}"""
    c = collections.defaultdict(lambda: {"items": 0, "tokens": 0, "skills": 0, "agents": 0, "tools": 0, "hooks": 0})
    for a in atts:
        t = a.get("type")
        if t == "skill_listing":
            names = set(a.get("names") or [])
            cur = None
            buf = collections.defaultdict(list)
            for line in (a.get("content") or "").split("\n"):
                key = line[2:].split(": ", 1)[0].split(" - ", 1)[0] if line.startswith("- ") else None
                if key in names:
                    cur = plugin_of_name(key, "(user skills)")
                    c[cur]["items"] += 1
                    c[cur]["skills"] += 1
                if cur:
                    buf[cur].append(line)
            for k, v in buf.items():
                c[k]["tokens"] += tok("\n".join(v))
        elif t == "agent_listing_delta":
            for line in a.get("addedLines") or []:
                name = line[2:].split(": ", 1)[0] if line.startswith("- ") else ""
                src = plugin_of_name(name, "(user agents)")
                c[src]["items"] += 1
                c[src]["agents"] += 1
                c[src]["tokens"] += tok(line)
        elif t == "deferred_tools_delta":
            for line in a.get("addedLines") or []:
                src = server_of_tool(line.strip()) or "(built-in tools)"
                c[src]["items"] += 1
                c[src]["tools"] += 1
                c[src]["tokens"] += tok(line)
    # Hooks. What the model sees: plain-text stdout of a SessionStart hook is shown as-is; a JSON stdout is NOT
    # shown raw, its additionalContext / systemMessage arrives as a hook_additional_context / hook_system_message
    # attachment. Counting the JSON stdout too would count the same text twice (caught 16 Oct).
    json_hooks = []
    for a in atts:
        if a.get("type") == "hook_success" and str(a.get("hookEvent")) == "SessionStart":
            text = str(a.get("stdout") or "").strip()
            if not text:
                continue
            try:
                json.loads(text)
                json_hooks.append(a)
            except ValueError:
                src = hook_owner(a)
                c[src]["hooks"] += 1
                c[src]["tokens"] += tok(text)
    for a in atts:
        if a.get("type") in ("hook_additional_context", "hook_system_message") and str(a.get("hookEvent")) == "SessionStart":
            parts = a.get("content")
            for part in (parts if isinstance(parts, list) else [parts]):
                part = str(part or "")
                if not part.strip():
                    continue
                probe = part.strip()[:60]
                owner = next((hook_owner(h) for h in json_hooks
                              if any(probe in v for v in json_strings(json.loads(h["stdout"])))), "(other hook)")
                c[owner]["hooks"] += 1
                c[owner]["tokens"] += tok(part)
    return c


def usage(lines, seen):
    u = collections.Counter()
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        m = o.get("message")
        if o.get("type") == "assistant" and isinstance(m, dict) and isinstance(m.get("content"), list):
            for b in m["content"]:
                if not (isinstance(b, dict) and b.get("type") == "tool_use") or b.get("id") in seen:
                    continue
                seen.add(b.get("id"))
                name, inp = b.get("name", ""), b.get("input") or {}
                if name == "Skill":
                    u[plugin_of_name(str(inp.get("skill") or inp.get("name") or ""), "(user skills)")] += 1
                elif name in ("Agent", "Task"):
                    u[plugin_of_name(str(inp.get("subagent_type") or "general-purpose"), "(user agents)")] += 1
                elif name.startswith("mcp__"):
                    u[server_of_tool(name)] += 1
        if o.get("type") == "user" and isinstance(m, dict) and isinstance(m.get("content"), str):
            for cmd in re.findall(r"<command-name>/?([^<]+)</command-name>", m["content"]):
                if ":" in cmd:
                    u[cmd.split(":", 1)[0]] += 1
    return u


def selftest():
    def att(**k):
        return {"type": "attachment", "attachment": k}
    lines = [json.dumps(x) for x in [
        att(type="skill_listing", names=["pk:a", "pk:b", "mine"], content="- pk:a: first\n- pk:b: second\n- mine: third"),
        att(type="agent_listing_delta", addedLines=["- pk:agent1: x", "- loose-agent: y"]),
        att(type="deferred_tools_delta", addedLines=["mcp__plugin_pk_srv__tool1", "mcp__abc-123__t2", "Read"]),
        att(type="hook_success", hookEvent="SessionStart", command="Loading caveman mode...", stdout="CAVEMAN MODE ACTIVE talk short"),
        att(type="hook_success", hookEvent="SessionStart", command="x/plugins/cache/sp-dev/superpowers/1/hook.cmd",
            stdout=json.dumps({"additionalContext": "YOU HAVE SUPERPOWERS long text"})),
        att(type="hook_additional_context", hookEvent="SessionStart", content=["YOU HAVE SUPERPOWERS long text"]),
        {"type": "assistant", "message": {"content": [], "usage": {"input_tokens": 1}}},
    ]]
    c = cost(first_request_attachments(lines))
    ok = (c["pk"]["skills"] == 2 and c["(user skills)"]["skills"] == 1 and c["pk"]["agents"] == 1
          and c["(user agents)"]["agents"] == 1 and c["plugin:pk"]["tools"] == 1 and c["server:abc-123"]["tools"] == 1
          and c["caveman"]["hooks"] == 1 and c["superpowers"]["hooks"] == 1
          and c["superpowers"]["tokens"] == tok("YOU HAVE SUPERPOWERS long text"))
    print("cost attribution (skills/agents/tools/hooks, JSON hook counted once):", "PASS" if ok else f"FAIL {dict(c)}")
    use_lines = [json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": "pk:a"}},
        {"type": "tool_use", "id": "t2", "name": "mcp__abc-123__t2", "input": {}}]}})] * 2   # duplicated line
    u = usage(use_lines, set())
    ok2 = u["pk"] == 1 and u["server:abc-123"] == 1
    print("usage de-duplicated by tool_use id:", "PASS" if ok2 else f"FAIL {u}")
    # mutation: if the JSON hook were counted from stdout as well, superpowers would carry more tokens
    doubled = tok(json.dumps({"additionalContext": "YOU HAVE SUPERPOWERS long text"})) + tok("YOU HAVE SUPERPOWERS long text")
    ok3 = c["superpowers"]["tokens"] < doubled
    print("mutation (double-counted hook would read", doubled, "we read", c["superpowers"]["tokens"], "):", "PASS" if ok3 else "FAIL")
    return 0 if ok and ok2 and ok3 else 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    tops = sorted(glob.glob(TOP))
    pinned = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--session=")), None)
    # newest session whose pre-request attachments include a skill list (or the one pinned with --session=<file>)
    best, best_ts = None, ""
    for f in tops:
        if pinned and os.path.basename(f) != pinned:
            continue
        with open(f, encoding="utf-8", errors="ignore") as fh:
            head = fh.readline()
            try:
                ts = json.loads(head).get("timestamp", "")
            except ValueError:
                ts = ""
        if ts >= best_ts:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                atts = first_request_attachments(fh)
            if any(a.get("type") == "skill_listing" for a in atts):
                best, best_ts = atts, ts
    c = cost(best)
    seen, u = set(), collections.Counter()
    for f in tops + sorted(glob.glob(SUB)):
        with open(f, encoding="utf-8", errors="ignore") as fh:
            u.update(usage(fh, seen))
    rows = []
    for src in set(c) | {k for k in u if k}:
        r = dict(c.get(src, {"items": 0, "tokens": 0, "skills": 0, "agents": 0, "tools": 0, "hooks": 0}))
        r["source"], r["uses"] = src, u.get(src, 0)
        rows.append(r)
    rows.sort(key=lambda r: -r["tokens"])
    json.dump({"session_start": best_ts[:10], "rows": rows}, open(os.path.join(HERE, "plugin-tax.json"), "w"), indent=1)
    print(f"cost from session starting {best_ts[:10]}; uses from all logs")
    print(f"{'source':42s} {'tokens':>7s} {'items':>6s} {'sk':>5s} {'ag':>4s} {'tl':>4s} {'hk':>3s} {'uses':>5s}")
    for r in rows[:40]:
        print(f"{r['source'][:42]:42s} {r['tokens']:7d} {r['items']:6d} {r['skills']:5d} {r['agents']:4d} {r['tools']:4d} {r['hooks']:3d} {r['uses']:5d}")


if __name__ == "__main__":
    main()
