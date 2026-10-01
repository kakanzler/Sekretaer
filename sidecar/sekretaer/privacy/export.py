"""Markdown and schema-versioned JSON export (spec §2 会議後, §7). Every export is also written to the
app-managed exports directory of the meeting so deletion can account for it (AC-07)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import EXPORT_SCHEMA_VERSION
from ..util import dumps, now_iso

DECISION_LABELS = {"proposed": "提案", "agreed": "合意", "withdrawn": "撤回", "needs_review": "要確認"}
ACTION_LABELS = {"open": "未着手", "in_progress": "進行中", "done": "完了", "canceled": "中止"}


def fmt_ms(ms: int | None) -> str:
    if ms is None:
        return "--:--"
    s = int(ms) // 1000
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def slug(title: str) -> str:
    s = re.sub(r"[^\w\-]+", "-", title, flags=re.UNICODE).strip("-_")
    return (s or "meeting")[:60]


def _md_escape(text: str) -> str:
    return text.replace("\r", " ").replace("\n", " ").strip()


def build_markdown(meeting: dict[str, Any], note: dict[str, Any] | None, segments: list[dict[str, Any]]) -> str:
    seg_index = {s["id"]: s for s in segments}

    def refs(item: dict[str, Any]) -> str:
        ids = item.get("evidenceSegmentIds") or []
        if not ids:
            reason = item.get("noEvidenceReason")
            return f" _(根拠なし: {_md_escape(reason)})_" if reason else ""
        times = [fmt_ms(seg_index[i]["startMs"]) for i in ids if i in seg_index]
        return f" _[根拠 {', '.join(times)}]_" if times else ""

    lines = [f"# {_md_escape(meeting['title']) or '(無題の会議)'}", ""]
    lines.append(f"- 開始: {meeting.get('startedAt') or '-'}")
    lines.append(f"- 終了: {meeting.get('endedAt') or '-'}")
    lines.append(f"- 言語: {meeting.get('language')}")
    if note:
        lines.append(f"- ノート版: {note['revisionId']} (生成 {note['generatedAt']})")
    lines.append("- 注意: AI が作成した要約は作業仮説です。逐語記録で確認してください。")
    lines.append("")
    if note:
        c = note["cornell"]
        lines += ["## 要約", "", _md_escape(c.get("summary") or "") or "(なし)", ""]
        if c.get("sections"):
            lines += ["### 区間ごとの要約", ""]
            for s in c["sections"]:
                lines.append(f"- {fmt_ms(s['fromMs'])}–{fmt_ms(s['toMs'])}: {_md_escape(s['summary'])}")
            lines.append("")
        lines += ["## Cornell ノート", "", "### 手がかり", ""]
        lines += [f"- {_md_escape(x['text'])}{refs(x)}" for x in c.get("cues", [])] or ["- (なし)"]
        lines += ["", "### ノート", ""]
        lines += [f"- {_md_escape(x['text'])}{' (' + x['certainty'] + ')' if x.get('certainty') else ''}{refs(x)}"
                  for x in c.get("notes", [])] or ["- (なし)"]
        lines += ["", "## 箇条書き", ""]

        def bullets(items: list[dict[str, Any]], depth: int) -> None:
            for b in items:
                lines.append(f"{'  ' * depth}- {_md_escape(b['text'])}{refs(b)}")
                bullets(b.get("children", []), depth + 1)

        if note.get("bullets"):
            bullets(note["bullets"], 0)
        else:
            lines.append("- (なし)")
        lines += ["", "## 決定事項", ""]
        if note.get("decisions"):
            lines += ["| 状態 | 内容 | 時刻 | 抽出信頼度(目安) |", "| --- | --- | --- | --- |"]
            for d in note["decisions"]:
                lines.append(f"| {DECISION_LABELS.get(d['status'], d['status'])} | {_md_escape(d['text'])}{refs(d)} "
                             f"| {fmt_ms(d.get('atMs'))} | {d['certainty']:.2f} |")
        else:
            lines.append("- (なし)")
        lines += ["", "## アクション", ""]
        if note.get("actions"):
            lines += ["| 状態 | タスク | 担当 | 期限 | 確認 |", "| --- | --- | --- | --- | --- |"]
            for a in note["actions"]:
                lines.append(
                    f"| {ACTION_LABELS.get(a['status'], a['status'])} | {_md_escape(a['text'])}{refs(a)} "
                    f"| {_md_escape(a.get('assignee') or '未定')} | {_md_escape(a.get('dueDate') or '未定')} "
                    f"| {'確認済み' if a.get('confirmed') else '未確認'} |")
        else:
            lines.append("- (なし)")
        lines += ["", "## 未解決事項", ""]
        lines += [f"- {_md_escape(q['text'])}{refs(q)}" for q in note.get("openQuestions", [])] or ["- (なし)"]
        lines.append("")
    else:
        lines += ["## ノート", "", "(まだノートはありません)", ""]
    lines += ["## 逐語記録", ""]
    for s in segments:
        speaker = f"{s['speakerLabel']}: " if s.get("speakerLabel") else ""
        lines.append(f"- [{fmt_ms(s['startMs'])}] ({s['source']}) {speaker}{_md_escape(s['text'])}")
    lines.append("")
    return "\n".join(lines)


def build_json(meeting: dict[str, Any], note: dict[str, Any] | None, segments: list[dict[str, Any]],
               revisions: list[dict[str, Any]]) -> str:
    doc = {
        "schemaVersion": EXPORT_SCHEMA_VERSION,
        "kind": "sekretaer.meeting-export",
        "exportedAt": now_iso(),
        "noteSchemaVersion": note["schemaVersion"] if note else None,
        "meeting": {k: meeting[k] for k in ("id", "title", "language", "state", "startedAt", "endedAt", "timezone",
                                            "createdAt", "sources")},
        "note": note,
        "noteRevisions": revisions,
        "transcript": segments,
    }
    return dumps(doc)


def write_export(directory: Path, filename: str, content: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(content, encoding="utf-8")
    return path
