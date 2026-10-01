# Sekretär

AI 会議支援デスクトップアプリケーション。会議音声をローカルで文字起こしし、会話の区切りごとに構造化ノート（Cornell / 箇条書き / 決定事項 / アクション）を更新する。

- 製品・技術仕様書: [`specifications/Sekretaer_製品技術仕様書.md`](specifications/Sekretaer_製品技術仕様書.md)
- 境界契約: [`contracts/`](contracts/)（API v1、ノート JSON Schema、イベント、OpenAPI）
- 設計判断・未決事項: [`docs/decisions/`](docs/decisions/)、脅威モデル: [`docs/threat-model.md`](docs/threat-model.md)

## 構成

| パス | 内容 | 技術 |
| --- | --- | --- |
| `apps/desktop` | デスクトップシェル。サイドカーの監視、最小 IPC、厳格 CSP | Tauri 2 (Rust) |
| `apps/web` | 会議一覧 / 新規会議 / ライブ画面 / 会議後 / 設定 | Next.js 静的エクスポート |
| `sidecar` | 音声取得、VAD、STT、区切り判定、要約キュー、SQLite、認証付き loopback API | Python 3.11+ / FastAPI / faster-whisper / Silero VAD / `claude -p` |
| `contracts` | UI ⇄ サイドカーの契約 | Markdown / JSON Schema / OpenAPI |
| `tests/integration` | UI クライアント ⇄ 実サイドカーの結合テスト用ランナー | |
| `packaging` | サイドカーのバイナリ化、モデルマニフェスト、署名（後続段階） | |

## 開発

前提: Node 20+ と pnpm、[uv](https://docs.astral.sh/uv/)、Rust（stable）、Windows では WebView2。要約を使う場合は Claude Code CLI（`claude`）でログインしておく。

```sh
pnpm install
uv sync --project sidecar --extra dev --extra audio --extra stt --extra vad --extra crypto

# デスクトップアプリ（Next.js dev サーバーとサイドカーを自動起動）
pnpm --filter @sekretaer/desktop tauri dev

# ブラウザー単独（開発用）: サイドカーを --dev で起動し、表示された port/token を画面に入力する
uv run --project sidecar python -m sekretaer --data-dir .sekretaer-data --dev --synthetic-audio --no-stdin-watch
pnpm dev:web
```

初回起動時、STT モデルは未取得の状態で始まる。ホーム画面にモデルのサイズ、提供元、ライセンス、保存先が表示されるので、内容を確認してから取得する。

## テスト

```sh
# サイドカー（AC-01, 03〜10 を含む）
cd sidecar && uv run pytest -q && uv run ruff check .

# UI（ユニットテスト / 型 / lint / 静的ビルド）
pnpm --filter @sekretaer/web test
pnpm --filter @sekretaer/web typecheck
pnpm --filter @sekretaer/web lint
pnpm --filter @sekretaer/web build

# UI クライアント ⇄ 実サイドカーの結合テスト（合成音声、スクリプト STT、偽 CLI）
pnpm --filter @sekretaer/web test:integration

# デスクトップシェル
cd apps/desktop/src-tauri && cargo test && cargo clippy --all-targets -- -D warnings

# 契約の同期確認
uv run --project sidecar python scripts/export-openapi.py --check
```

## 現状と未決事項

MVP の段階 1〜4 の実装が一通り揃っている。次の項目は実機評価や製品判断を待っている（仕様 §14/§16、ADR 0004〜0007）。

- 対応 OS と配布形態は未確定。開発は Windows 11 で行っており、macOS のシステム音声は未実装。
- PyInstaller によるサイドカーの同梱、署名、自動更新は未実装（`packaging/README.md`）。
- 実音声での STT 精度・遅延（p95）と 30 分会議の実機試験（AC-10 実機版）は未実施。自動テストは合成音声で行っている。
- STT 復旧後に保存済み音声を再処理する機能（仕様 §9）は未実装。
