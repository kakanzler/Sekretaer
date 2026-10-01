"""Fake ``claude`` CLI for tests (never calls a model).

Invoked as ``python fake_claude.py --control <control.json> <claude args...>``. The control file selects
behaviour; every invocation is appended to ``<control>.log`` (argv, env var names, stdin size) so tests
can assert on flags and on the minimal environment.

modes: ok | auth | invalid | garbage | fail | sleep | bad_evidence | exit_auth
``modes`` (list) is consumed one entry per model call; ``mode`` is the fallback.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HELP = """Usage: claude [options] [command] [prompt]
Options:
  -p, --print                 Print response and exit
  --output-format <format>    Output format
  --tools <tools...>          Specify the list of available tools
  --no-session-persistence    Disable session persistence
  --strict-mcp-config         Only use MCP servers from --mcp-config
  --disable-slash-commands    Disable all skills
  --safe-mode                 Start with all customizations disabled
  --system-prompt <prompt>    System prompt to use for the session
  --model <model>             Model for the current session
"""


def main() -> int:
    argv = sys.argv[1:]
    i = argv.index("--control")
    control_path = Path(argv[i + 1])
    args = argv[i + 2 :]
    control = json.loads(control_path.read_text(encoding="utf-8")) if control_path.exists() else {}
    if "--version" in args:
        print(control.get("version", "9.9.9 (Claude Code)"))
        return 0
    if "--help" in args:
        print(control.get("help", HELP))
        return 0
    raw = sys.stdin.buffer.read()
    stdin = raw.decode("utf-8")
    modes = control.get("modes") or []
    if modes:
        mode = modes.pop(0)
        control["modes"] = modes
        control_path.write_text(json.dumps(control), encoding="utf-8")
    else:
        mode = control.get("mode", "ok")
    with open(str(control_path) + ".log", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": args, "env": sorted(os.environ), "stdinBytes": len(raw), "mode": mode,
                             "cwd": os.getcwd()}) + "\n")
    if mode == "sleep":
        time.sleep(float(control.get("sleep", 5)))
        mode = "ok"
    if mode == "auth":
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                          "result": "Invalid API key · Please run /login"}))
        return 1
    if mode == "exit_auth":
        sys.stderr.write("Error: not logged in. Please run claude /login\n")
        return 1
    if mode == "fail":
        sys.stderr.write("network error\n")
        return 3
    if mode == "garbage":
        print("this is not an envelope")
        return 0
    if mode == "invalid":
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "result": "申し訳ありませんが JSON ではありません"}))
        return 0
    payload = json.loads(stdin)
    draft = build_draft(payload, bad_evidence=(mode == "bad_evidence"))
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": "```json\n" + json.dumps(draft, ensure_ascii=False) + "\n```", "total_cost_usd": 0}))
    return 0


def build_draft(payload: dict, bad_evidence: bool = False) -> dict:
    transcript = payload["transcript"]
    prev = payload.get("previousNote") or {}
    ids = [t["id"] for t in transcript]
    first = ids[:1]
    texts = [t["text"] for t in transcript]
    prev_cornell = prev.get("cornell") or {}
    cues = list(prev_cornell.get("cues") or []) + [{"text": f"区間 {payload['chunk']['index']} の論点",
                                                    "evidence": first}]
    notes = list(prev_cornell.get("notes") or []) + [
        {"text": t[:40], "evidence": [i], "certainty": "stated"} for i, t in zip(ids[:2], texts[:2], strict=False)
    ]
    bullets = list(prev.get("bullets") or []) + [
        {"ref": None, "text": f"{len(transcript)} 件の発話を整理", "evidence": ids[:2], "certainty": "inferred",
         "children": [{"ref": None, "text": texts[0][:30], "evidence": first, "children": []}]}
    ]
    decisions = list(prev.get("decisions") or [])
    actions = [a for a in (prev.get("actions") or [])]
    questions = list(prev.get("openQuestions") or [])
    for t in transcript:
        if "合意" in t["text"] or "決定" in t["text"]:
            decisions.append({"ref": None, "text": t["text"], "status": "agreed", "certainty": 0.9,
                              "evidence": [t["id"]]})
        if "までに" in t["text"] or "報告します" in t["text"]:
            assignee = next((n for n in ("田中", "佐藤") if n in t["text"]), None)
            due = "来週水曜" if "来週水曜" in t["text"] else ("次回" if "次回" in t["text"] else None)
            actions.append({"ref": None, "text": t["text"], "assignee": assignee, "dueDate": due, "status": "open",
                            "evidence": [t["id"]]})
        if "確認が必要" in t["text"]:
            questions.append({"text": t["text"], "evidence": [t["id"]]})
    if bad_evidence:
        decisions.append({"ref": None, "text": "存在しない根拠の決定", "status": "agreed", "certainty": 0.95,
                          "evidence": ["S999", "00000000-0000-0000-0000-000000000000"]})
        bullets.append({"ref": "B999", "text": "混在する根拠", "evidence": [*first, "S404"], "children": []})
    return {
        "cornell": {"cues": cues, "notes": notes, "summary": f"これまでの会議の要約（{len(texts)} 件の新しい発話）",
                    "sectionSummary": f"この区間では {len(texts)} 件の発話がありました。"},
        "bullets": bullets,
        "decisions": decisions,
        "actions": actions,
        "openQuestions": questions,
    }


if __name__ == "__main__":
    sys.exit(main())
