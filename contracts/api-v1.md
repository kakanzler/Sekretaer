# Sekretär sidecar API v1 — contract

Normative source for the UI ⇄ sidecar boundary (spec §8). The generated
OpenAPI document lives at `contracts/openapi/openapi.json` and must match this
file; `scripts/export-openapi` regenerates it.

## Launch handshake (spec §3)

The Tauri shell starts the sidecar as a child process:

```
python -m sekretaer --data-dir <app data dir> [--allowed-origin <origin>]...
```

The sidecar binds `127.0.0.1` on port 0 (OS-chosen random port), generates a
32-byte random token, and writes **exactly one line** to stdout:

```json
{"type":"ready","apiVersion":"1","port":53124,"token":"<base64url>","pid":1234}
```

After that line stdout is not used; logs go to stderr as structured JSON
lines (no transcript text, no audio, no CLI I/O, no token). The sidecar exits
when stdin reaches EOF (parent died) or on SIGTERM / `POST /api/v1/shutdown`.

`--dev` mode (browser-only development, spec §3) additionally prints the
connection info to stderr and allows the `http://localhost:3000` origin.

## Authentication and transport security (spec §11, AC-08)

- Bind address is always `127.0.0.1`. Never `0.0.0.0`.
- REST: `Authorization: Bearer <token>`. Missing/invalid → `401` with code `unauthorized`.
- WebSocket: token in the `Sec-WebSocket-Protocol` header as two
  subprotocols: `sekretaer.v1, bearer.<token>`. The server echoes
  `sekretaer.v1`. The token never appears in URLs.
- `Origin` header, when present, must be in the allowed set
  (defaults: `tauri://localhost`, `http://tauri.localhost`,
  `https://tauri.localhost`; plus `http://localhost:3000` in `--dev`). Otherwise `403 origin_forbidden`.
- Mutating requests (`POST`/`PUT`/`PATCH`/`DELETE`, even without a body) require
  `Content-Type: application/json` (CSRF defence: forces a CORS preflight, which
  the server does not approve for unknown origins).
- `Host` must be `127.0.0.1:<port>` or `localhost:<port>` (DNS-rebinding defence);
  otherwise `403 origin_forbidden`.
- Tokens live only for the lifetime of the sidecar process.

## Envelope

Every HTTP response body:

```json
{"requestId": "uuid", "data": { ... }}
{"requestId": "uuid", "error": {"code": "consent_required", "message": "日本語の利用者向け説明", "details": {}}}
```

`requestId` is also sent as the `X-Request-Id` response header. Error `code`
values are the spec appendix B codes plus `unauthorized`, `origin_forbidden`,
`not_found`, `invalid_request`, `invalid_state`, `conflict`, and `cli_failed`
(generic, transient CLI failure such as network or rate limit — retried automatically).

## Resources

Base path: `/api/v1`. Times: `*Ms` = integer ms from meeting start; `*At` = ISO-8601 with offset.

### Meeting object

```json
{
  "id": "uuid", "title": "string", "language": "auto|ja|en",
  "state": "preparing|recording|finalizing|completed|error|deleting|deleted",
  "degraded": ["stt_queue_backlog"],
  "startedAt": null, "endedAt": null, "timezone": "Asia/Tokyo",
  "createdAt": "...", "updatedAt": "...",
  "elapsedMs": 0,
  "sources": [{"id": "uuid", "kind": "microphone|system", "deviceKey": "string",
               "sampleRate": 48000, "channels": 1,
               "permissionState": "granted|denied|unknown|unavailable", "offsetMs": 0}],
  "consent": {"recording": "unconfirmed|granted|denied", "externalProcessing": "unconfirmed|granted|denied"},
  "settings": {"summarizationEnabled": false, "retainAudio": false, "audioRetentionDays": 7},
  "processing": {"sttBacklogMs": 0, "pendingJobs": 0, "lastError": null}
}
```

### Endpoints

| Method & path | Body / query | Returns |
|---|---|---|
| `GET /health` | — | `{status:"ok"|"starting"|"degraded", apiVersion:"1", stt:{state:"not_downloaded|loading|ready|failed|unavailable", model, device, computeType}, vad:{state}, summarizer:{state:"disabled|ready|cli_not_found|cli_auth_required", cliVersion}, queue:{sttBacklogMs, pendingJobs}}` |
| `POST /stt/model/download` | — | `202` + the `stt` object of `/health`. Explicit user action only; `/health.stt` discloses `approxSizeMb, source, license, targetDir, network` beforehand (spec §5). |
| `GET /devices` | — | `{devices:[{key, name, kind:"microphone|system", defaultSampleRate, channels, available:boolean, note}]}` |
| `GET /meetings` | `?limit&cursor` | `{items:[Meeting], nextCursor}` (excludes `deleted`) |
| `POST /meetings` (`201`) | `{title, language, sources:[{kind, deviceKey}], consent:{recording:boolean, externalProcessing:boolean}, settings:{summarizationEnabled, retainAudio, audioRetentionDays}}` | `Meeting` (state `preparing`) |
| `GET /meetings/{id}` | — | `Meeting` |
| `POST /meetings/{id}/consent` | `{scope:"recording|external_processing", granted:boolean, policyVersion}` | `Meeting` — records a `consent_events` row. Withdrawing `external_processing` cancels queued summary jobs. |
| `PATCH /meetings/{id}` | `{title?, settings?}` | `Meeting` |
| `POST /meetings/{id}/start` | header `Idempotency-Key` (required) | `Meeting` state `recording`. `409 consent_required` / `permission_denied` / `source_unavailable` if checks fail (AC-01). Same key → same result. |
| `POST /meetings/{id}/stop` | — | `202 Meeting` state `finalizing`; later `meeting.state completed` event. |
| `POST /meetings/{id}/recover` | `{action:"finalize"}` | `202`. For meetings left `recording` after a crash or shutdown (marked `degraded:["interrupted"]` at sidecar start): finalize without auto-resuming capture. |
| `GET /meetings/{id}/transcript` | `?afterMs&limit&includePartial&cursor` (`cursor` = previous `nextCursor`) | `{items:[Segment], nextCursor}` |
| `PATCH /meetings/{id}/transcript/{segmentId}` | `{text?, speakerLabel?}` | `Segment` (revision+1, old version kept) |
| `GET /meetings/{id}/transcript/{segmentId}/history` | — | `{items:[{revision, text, speakerLabel, editedAt, origin:"stt|user"}]}` |
| `GET /meetings/{id}/summaries/preview` | — | `{fromMs, toMs, segmentCount, text}` — exactly the text that would be sent to the CLI next. |
| `POST /meetings/{id}/summaries` | `{}` | `202 {jobId, deduplicated:boolean}` — manual summary trigger. `409 consent_required` if external processing not granted or summarization disabled. |
| `GET /meetings/{id}/notes` | — | `{current: NoteRevision|null, pendingConflict: NoteRevision|null}` |
| `GET /meetings/{id}/notes/revisions` | — | `{items:[NoteRevisionMeta]}` |
| `PUT /meetings/{id}/notes` | `{baseRevisionId, note: Note}` | `NoteRevision` (origin `user`). `409 conflict` if base is stale. |
| `POST /meetings/{id}/notes/conflict/resolve` | `{action:"accept_ai"|"keep_mine", conflictRevisionId}` | `NoteRevision` |
| `GET /meetings/{id}/export` | `?format=markdown|json` | `{filename, contentType, content}` |
| `GET /jobs/{id}` | — | `{id, meetingId, trigger, status:"queued|running|retry_wait|succeeded|failed|canceled", attempt, errorCode, retryAfter, createdAt, startedAt, finishedAt}` |
| `GET /meetings/{id}/jobs` | — | `{items:[Job]}` |
| `POST /jobs/{id}/retry` | — | `Job` |
| `POST /jobs/{id}/cancel` | — | `Job` |
| `DELETE /meetings/{id}` | — | `200 {deleted:{dbRecords, audioFiles, tempFiles, exports}, failed:[{kind, path, reason}], status:"deleted|delete_incomplete"}` (AC-07) |
| `GET /settings` / `PUT /settings` | `{stt:{model, computeType, device}, vad:{sensitivity:"low|normal|high"}, summarizer:{cliPath?, timeoutSec}, privacy:{defaultRetainAudio:false}}` | settings object |
| `GET /privacy` | — | `{storage:{dataDir, dbPath}, audioArchive:{enabledMeetings}, externalProcessing:{enabledMeetings, destination:"Claude Code CLI (claude -p)"}}` |
| `POST /shutdown` | — | `202` |

`NoteRevision` = `{id, meetingId, jobId|null, origin:"ai|user", schemaVersion, createdAt, supersedesId|null, note: Note}`; `Note` follows `contracts/notes/note-1.0.schema.json`.

`Segment` = `{id, meetingId, sourceId, source:"microphone|system", startMs, endMs, text, language, isFinal, revision, speakerLabel|null, stt:{avgLogprob, noSpeechProb}}`.

## Events

`WS /api/v1/events?cursor=<seq>` — envelope schema `contracts/events/event-1.0.schema.json`.
On connect the server replays events with `seq > cursor` (retained for at
least the last 10 000 events) and then streams live. Events:
`meeting.state, transcript.partial, transcript.final, summary.queued,
summary.updated, job.failed, audio.gap, audio.level, privacy.state, warning`.
`transcript.partial` and `audio.level` are ephemeral (not replayed).

Payloads not constrained by the envelope schema:

- `audio.level`: `{sourceId, source:"microphone|system", rms:0..1, peak:0..1, db}` (≈ every 100–250 ms per source).
- `warning`: `{code, message (Japanese), details}` — `code` from appendix B (e.g. `stt_queue_backlog`, `db_write_failed`, `source_unavailable`).
- `audio.gap`: `{sourceId, atMs, gapMs, reason}`.

Only one meeting may be `recording` at a time (`409 invalid_state`).
`POST /shutdown` or stdin EOF during recording does **not** finalize the
meeting; on next start it is marked `interrupted` and requires `/recover`.
Clients that want a clean close should call `/stop` first.

## Tauri shell IPC (apps/desktop)

The WebView's only native surface:

| Command / event | Shape |
|---|---|
| `invoke("sidecar_connection")` | `{state:"starting|ready|crashed|restarting|failed", port:number|null, token:string|null, error:string|null}` — all keys always present |
| event `sidecar://state` | same object, emitted on every change (new port/token after each restart) |
| `invoke("sidecar_restart")` | restarts the supervisor; the way out of `failed` |
| `invoke("open_privacy_settings", {kind:"microphone"})` | opens a fixed OS settings URI; no caller-supplied URL |

