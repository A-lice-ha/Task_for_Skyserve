"""Export every AI chat used for this repository into Markdown (assignment AI record).

Sources:
  * GitHub Copilot (VS Code agent host) sessions under ~/.copilot/session-state
    whose recorded working directory is this repository.
  * Claude Code sessions under ~/.claude/projects/<this repository>.

User prompts and assistant replies are written in full and in time order.
Tool calls and tool results are included but truncated (marked as such);
text attachments (e.g. pasted text) are inlined. Tool-injected system context
is removed and e-mail addresses are redacted. Re-run at the end of the work to
capture the latest exchanges.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOME = Path.home()
COPILOT = HOME / ".copilot" / "session-state"
CLAUDE = HOME / ".claude" / "projects"
OUT_DIR = ROOT / "docs" / "ai_usage"
TOOL_ARGS_LIMIT = 3000
TOOL_RESULT_LIMIT = 2000
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
INJECTED = re.compile(r"<(system-reminder|system_reminder)>.*?</\1>", re.S)


EXTRA_REDACTIONS: list[re.Pattern] = []


def clean(text: str) -> str:
    text = INJECTED.sub("", text or "")
    text = EMAIL.sub("[email redacted]", text)
    for pattern in EXTRA_REDACTIONS:
        text = pattern.sub("[redacted]", text)
    return text.strip()


def clip(text: str, limit: int) -> str:
    text = clean(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} characters]"


def fence(text: str) -> str:
    ticks = "````" if "```" in text else "```"
    return f"{ticks}\n{text}\n{ticks}"


def same_repo(path: str) -> bool:
    return Path(path).resolve() == ROOT.resolve()


# --------------------------------------------------------------- Copilot ----

def copilot_sessions() -> list[Path]:
    sessions = []
    for workspace in COPILOT.glob("*/workspace.yaml"):
        match = re.search(r"^cwd:\s*(.+)$", workspace.read_text(encoding="utf-8"), re.M)
        if match and same_repo(match.group(1).strip()):
            if (workspace.parent / "events.jsonl").is_file():
                sessions.append(workspace.parent)
    return sorted(sessions)


def export_copilot(session: Path) -> tuple[str, dict]:
    lines, models, meta = [], Counter(), {"tool": "GitHub Copilot (VS Code agent host)"}
    first = last = None
    for raw in (session / "events.jsonl").open(encoding="utf-8"):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind, data, ts = event.get("type"), event.get("data", {}), event.get("timestamp", "")
        if kind == "session.start":
            meta["copilot_version"] = data.get("copilotVersion")
            meta["selected_model"] = data.get("selectedModel")
        elif kind == "user.message":
            first = first or ts
            last = ts
            lines.append(f"\n---\n\n### User · {ts}\n\n{clean(data.get('content', ''))}\n")
            for att in data.get("attachments") or []:
                path = Path(att.get("path", ""))
                name = att.get("displayName") or path.name
                body = ""
                if path.suffix.lower() in {".txt", ".md", ".log", ".csv", ".json"} and path.is_file():
                    body = clean(path.read_text(encoding="utf-8", errors="replace"))
                lines.append(f"**Attachment: {name}**\n\n" + (fence(body) if body else
                             "_(non-text or unavailable attachment; identified by name only)_") + "\n")
        elif kind == "assistant.message":
            last = ts
            model = data.get("model") or "unknown"
            models[model] += 1
            text = clean(data.get("content", ""))
            if text:
                lines.append(f"\n**Assistant ({model}) · {ts}**\n\n{text}\n")
            for req in data.get("toolRequests") or []:
                args = json.dumps(req.get("arguments"), ensure_ascii=False, indent=1)
                lines.append(f"\n<details><summary>Tool call: <code>{req.get('name')}</code></summary>\n\n"
                             f"{fence(clip(args, TOOL_ARGS_LIMIT))}\n\n</details>\n")
        elif kind == "tool.execution_complete":
            result = data.get("result") or {}
            content = result.get("content") if isinstance(result, dict) else result
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            lines.append(f"<details><summary>Tool result</summary>\n\n"
                         f"{fence(clip(content, TOOL_RESULT_LIMIT))}\n\n</details>\n")
    meta.update({"session": session.name, "start": first, "end": last, "models": dict(models)})
    return "\n".join(lines), meta


# ----------------------------------------------------------- Claude Code ----

def claude_sessions() -> list[Path]:
    key = re.sub(r"[^A-Za-z0-9]", "-", str(ROOT))
    folder = next((p for p in CLAUDE.glob("*") if p.name.lower() == key.lower()), None)
    return sorted(folder.glob("*.jsonl")) if folder else []


def export_claude(path: Path) -> tuple[str, dict]:
    lines, models = [], Counter()
    first = last = None
    for raw in path.open(encoding="utf-8"):
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if entry.get("isSidechain"):
            continue
        kind, ts = entry.get("type"), entry.get("timestamp", "")
        message = entry.get("message") or {}
        content = message.get("content")
        if kind == "user":
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
            for block in blocks:
                if block.get("type") == "text":
                    text = clean(block.get("text", ""))
                    if text:
                        first = first or ts
                        last = ts
                        lines.append(f"\n---\n\n### User · {ts}\n\n{text}\n")
                elif block.get("type") == "tool_result":
                    body = block.get("content")
                    if isinstance(body, list):
                        body = "\n".join(b.get("text", "[image]") if b.get("type") == "text"
                                         else "[image]" for b in body)
                    lines.append(f"<details><summary>Tool result</summary>\n\n"
                                 f"{fence(clip(str(body), TOOL_RESULT_LIMIT))}\n\n</details>\n")
        elif kind == "assistant":
            model = message.get("model") or "unknown"
            last = ts
            for block in content or []:
                if block.get("type") == "text" and clean(block.get("text", "")):
                    models[model] += 1
                    lines.append(f"\n**Assistant ({model}) · {ts}**\n\n{clean(block['text'])}\n")
                elif block.get("type") == "tool_use":
                    args = json.dumps(block.get("input"), ensure_ascii=False, indent=1)
                    lines.append(f"\n<details><summary>Tool call: <code>{block.get('name')}</code></summary>\n\n"
                                 f"{fence(clip(args, TOOL_ARGS_LIMIT))}\n\n</details>\n")
    meta = {"tool": "Claude Code (VS Code extension)", "session": path.stem,
            "start": first, "end": last, "models": dict(models)}
    return "\n".join(lines), meta


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--redact", nargs="*", default=[],
                        help="extra private terms to redact (case-insensitive)")
    args = parser.parse_args(argv)
    EXTRA_REDACTIONS[:] = [re.compile(re.escape(t), re.I) for t in args.redact if t]
    args.out.mkdir(parents=True, exist_ok=True)
    index = []
    jobs = [("copilot", s, export_copilot) for s in copilot_sessions()]
    jobs += [("claude_code", s, export_claude) for s in claude_sessions()]
    for prefix, source, exporter in jobs:
        body, meta = exporter(source)
        if not meta.get("start"):
            continue
        name = f"{meta['start'][:10]}_{prefix}_{meta['session'][:8]}.md"
        header = (f"# {meta['tool']} session {meta['session']}\n\n"
                  f"- Time range (UTC): {meta['start']} → {meta['end']}\n"
                  f"- Models: {', '.join(f'{m} ({n} replies)' for m, n in meta['models'].items())}\n"
                  f"- Tool calls/results truncated at {TOOL_ARGS_LIMIT}/{TOOL_RESULT_LIMIT} characters; "
                  "prompts and replies are complete. E-mail addresses redacted; injected system context removed.\n")
        (args.out / name).write_text(header + body, encoding="utf-8")
        index.append((meta, name))
    index.sort(key=lambda item: item[0]["start"])
    rows = "\n".join(
        f"| {m['start'][:16].replace('T', ' ')} | {m['end'][:16].replace('T', ' ')} | {m['tool']} | "
        f"{', '.join(m['models'])} | [{n}](docs/ai_usage/{n}) |" for m, n in index)
    (ROOT / "AI_USAGE.md").write_text(
        "# AI usage record\n\n"
        "AI assistants were used throughout this assignment. Every chat session tied to this "
        "repository is exported in time order below, generated by "
        "[`src/utils/export_ai_usage.py`](src/utils/export_ai_usage.py) from the tools' local "
        "session logs.\n\n"
        "| Start (UTC) | End (UTC) | Tool | Models | Transcript |\n|---|---|---|---|---|\n"
        f"{rows}\n\n"
        "## What each transcript contains\n\n"
        "- Every user prompt (including pasted text attachments) and every assistant reply, "
        "complete and in time order, with the model that produced each reply.\n"
        f"- Each tool call (file reads/edits, shell commands) with arguments, and its result, "
        f"truncated to {TOOL_ARGS_LIMIT} and {TOOL_RESULT_LIMIT} characters respectively.\n"
        "- Removed: tool-injected system context (environment/workspace reminders, system "
        "prompts) and e-mail addresses. Assistant private reasoning is not part of the logs' "
        "visible conversation and is not included.\n\n"
        "## Verification\n\n"
        "All code and results were run and checked in this repository: the test suite, the "
        "recorded command outputs in each transcript, and the experiment logs under `outputs/`. "
        "Decisions taken with AI assistance are recorded in `docs/DECISION_LOG.md`.\n",
        encoding="utf-8")
    for meta, name in index:
        print(name, meta["models"])


if __name__ == "__main__":
    main()
