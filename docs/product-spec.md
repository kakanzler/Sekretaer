# Sekretär 製品仕様

正本は [`specifications/Sekretaer_製品技術仕様書.md`](../specifications/Sekretaer_製品技術仕様書.md)（文書版 1.0, 2026年9月）。

実装上の境界契約は次のファイルで管理し、仕様書と矛盾する場合は ADR を起こして両方を更新する。

| 契約 | ファイル |
| --- | --- |
| HTTP / WebSocket API v1、起動ハンドシェイク、認証 | [`contracts/api-v1.md`](../contracts/api-v1.md)、生成物 [`contracts/openapi/openapi.json`](../contracts/openapi/openapi.json) |
| ノート JSON Schema 1.0 | [`contracts/notes/note-1.0.schema.json`](../contracts/notes/note-1.0.schema.json) |
| WebSocket イベント封筒 | [`contracts/events/event-1.0.schema.json`](../contracts/events/event-1.0.schema.json) |

設計判断と未決事項は [`decisions/`](decisions/) の ADR、脅威分析は [`threat-model.md`](threat-model.md) を参照。
