## 製品・技術仕様書

# Sekretär

AI 会議支援デスクトップアプリケーション

Next.js フロントエンド / Python サイドカー / Tauri デスクトップシェル

基準仕様  •  2026年9月  •  文書版 1.0

| 対象 | 内容 |
| --- | --- |
| 目的 | 会議音声をローカルで文字起こしし、会話の区切りごとに構造化ノートを更新する。 |
| 想定利用者 | 個人・小規模チーム。会議の流れを保ちながら記録、決定事項、担当タスクを確認したい利用者。 |
| 技術前提 | Tauri 2.x、Next.js（静的エクスポートを基本）、Python 3.11+ サイドカー、SQLite、faster-whisper、Silero VAD、Claude Code CLI の claude -p。 |
| 仕様の位置づけ | 実装開始に使える基準案。OS、配布方式、Claude CLI の認証・利用条件など、変化しうる選定は未決事項に明記する。 |

### この仕様書の結論

Sekretär は、録音・認識・要約を明示的な状態機械と永続キューでつなぐ。音声取り込みとローカル STT を基本ローカル処理とし、要約を外部 CLI に送る場合は送信先・対象テキスト・同意状態を利用者が理解できる設計にする。ライブ画面は逐語記録と暫定ノートを分け、AI 出力は根拠の発話範囲へ追跡可能にする。

対象外は、会議への自動参加、話者の本人特定、無人での意思決定、クラウド同期、法務上の録音同意判断である。これらは後続の明示的な製品判断が必要。

## 1. 目的と製品範囲

本書は Sekretär の MVP 実装・検証に必要な振る舞い、データ形式、コンポーネント境界、運用制約を定める。利用者は会議を開始し、マイクおよび許可されたシステム音声を取得し、認識中の発話と整理済みノートを同じ画面で確認する。会議終了後はノートを編集し、Markdown と JSON で書き出せる。

### 成功状態

- 録音中でも認識結果が順次表示され、区切りの後にノートが非同期更新される。
- 決定事項・アクション項目に根拠発話と時刻が付き、誤りを人が修正できる。
- ネットワーク断や要約 CLI の不在でも、録音・ローカル文字起こしを継続して後から再試行できる。
- 会議データの保存・削除・外部送信状態が画面から理解できる。
### 用語

| 用語 | 定義 |
| --- | --- |
| セグメント | STT が返す時刻付きの発話候補。暫定・確定の状態を持つ。 |
| チャンク | 音声の連続した処理単位。録音アーカイブ用と STT 入力用の双方に用いる。 |
| 区切り | 無音、VAD、発話長、意味的な完結度から推定する要約候補点。 |
| ノートリビジョン | 要約ジョブの出力を適用するたびに追加される会議ノートの版。 |
| 根拠参照 | ノート項目を支持する transcript segment ID と時間範囲。 |

## 2. 利用シナリオと画面

### 会議開始

1. 新規会議でタイトル、言語（自動または指定）、音声入力デバイスを設定する。
1. マイク／システム音声のソース別に権限を確認し、録音同意チェックを済ませる。開始時に録音中表示と経過時間を出す。
1. 音声ソース、ローカル保存、Claude CLI へのテキスト送信を別々に示し、CLI が有効なら送信前に対象範囲と外部送信の確認を行う。
### ライブ会議画面

| 領域 | 表示・操作 |
| --- | --- |
| 上部ステータス | 会議タイトル、録音状態、経過時間、入力ソース、CPU/キュー警告、録音停止。停止は確認なしで可能。 |
| 逐語記録 | 確定発話、暫定発話、話者ラベル（任意の手動ラベル）、時刻。検索、手動修正、訂正履歴。 |
| 要約ノート | Cornell（手がかり／ノート／要約）、Bullet（階層箇条書き）、決定事項、アクションログをタブで表示。更新中の版は「暫定」と明記。 |
| アクション | 担当者、期限、内容、状態、根拠を編集。AI 推定値は未確認状態で作成する。 |
| プライバシー | 録音対象・保存先・外部処理の現在状態、会議単位の削除、同意記録への導線。 |

### 会議後

会議一覧ではタイトル、開始日時、処理状態を表示する。詳細ではノートと逐語記録を行き来でき、各ノート項目から根拠箇所へジャンプする。手修正後の書き出し形式は Markdown とスキーマ版付き JSON。音声ファイルの保持・削除は会議単位で選択でき、初期値は音声を保持しない。

## 3. システム構成

| 構成要素 | 責務 | 実装方針 |
| --- | --- | --- |
| Tauri shell (Rust) | ウィンドウ、OS権限、アプリ起動、サイドカー生存監視、許可済み IPC ブリッジ | Tauri 2。capability を最小化。WebView から任意コマンド実行を許さない。 |
| Next.js UI | ライブ画面、履歴、設定、ノート編集 | 静的エクスポートして Tauri asset として同梱。サーバー機能・ブラウザ API 前提の SSR は使わない。 |
| Python sidecar | 音声取得アダプター、リングバッファ、VAD、STT、区切り検知、要約キュー、HTTP/WebSocket API | ユーザー権限で起動。localhost のランダムポートとランダム bearer token を利用。 |
| faster-whisper | ローカル音声認識 | CPU 初期対応、モデルを初回取得する前に容量・提供元・ライセンス・ネットワークを説明。GPU は後続任意。 |
| Silero VAD | 発話区間候補検出 | 音声チャンク単位で実行。無音判定と組み合わせ、VAD 単独では意味区切りを決めない。 |
| claude -p | 増分構造化要約 | Python から非同期 subprocess として起動。シェル文字列連結禁止、タイムアウト・キャンセル・バージョン検出を行う。 |
| SQLite | 会議、音声区間、transcript、要約ジョブ、ノート、同意・設定 | ユーザーデータ領域に DB を作成。トランザクション、WAL、バックアップ・移行を実装。 |

### プロセス・音声フロー

Tauri 起動時に sidecar を起動し、stdio の起動ハンドシェイクでランダムポートと token を親プロセスだけに返す。Python API は loopback のみ bind し、token を要求する。UI は Tauri ブリッジ経由で資格情報を受け取り、localhost API に接続する。ブラウザー単独モードは開発用に限定する。

入力音声はソース別に取得し、共通形式（mono PCM float32 または int16、16 kHz の STT 用派生ストリーム）へ変換する。入力元のサンプルレート・チャンネル・時刻オフセットを記録する。STT と VAD はリアルタイム優先キューを使い、録音アーカイブは別キューで欠落を検出する。

## 4. 音声取り込み・VAD・区切り判定

### 音声取得要件

- デバイス列挙、選択、権限拒否・切断の検出、レベルメーターを提供する。
- マイクとシステム音声は独立ソースとして識別し、OS が許可しない経路を迂回しない。MVP のシステム音声取得は OS 別アダプターに分離する。
- 複数ソースを合成して STT に渡す場合も、元ソースと時刻オフセットを保存して追跡可能にする。二重取り込みの可能性を設定時に案内する。
- 音声リングバッファの初期値は 30 秒。STT 遅延時は録音を止めず、キューの増加を表示する。データ損失が起きた場合は時刻と欠落長を記録する。
### VAD と発話確定

Silero VAD は 16 kHz mono を基本入力とし、フレーム結果を時間範囲へ集約する。初期閾値はモデル推奨値を採用し、前後 300 ms のパディング、約 500 ms の連続無音で発話候補終端を作る。ノイズ環境用に感度プリセットを設け、実機評価で閾値を調整する。

### 要約区切りポリシー

| トリガー | 初期条件 | 動作 |
| --- | --- | --- |
| 無音終端 | VAD 発話終了から 1.2 秒。直近の要約対象が 15 秒以上 | 区切り候補をキューへ投入 |
| 長時間発話 | 未処理発話が 90 秒に到達 | 安全区切りを作成し、後続に重複コンテキストを含める |
| 会議操作 | 利用者が「今まとめる」を押す | 進行中 STT 確定後の境界でジョブ投入 |
| 会議終了 | 録音停止後、保留音声の STT を完了 | 最終要約ジョブを作る |

意味的区切りは、発話完了の手掛かり（文末、話題遷移、質疑応答の完了、決定表現）を軽量ルールで補助する。MVP では別の LLM 呼び出しで区切り検出しない。過剰な短文要約を抑えるため、前回要約後のテキスト長・経過時間のどちらかが閾値未満なら待つ。ただし手動操作と会議終了は例外とする。

## 5. ローカル STT

- faster-whisper を sidecar 内でロードし、言語自動検出と ja/en 指定をサポートする。追加言語はモデル品質確認後に有効化する。
- モデル名・量子化・デバイスを設定可能にする。既定は実機性能評価で決め、初期プロファイルは small/int8/CPU を暫定候補とする。モデル未取得時はダウンロードサイズと保存先を表示する。
- 短い更新間隔の音声を用い、暫定結果を低遅延表示し、発話終了後に確定結果で置き換える。確定済みセグメントは再処理で履歴を失わない。
- 各 segment は開始・終了時刻（会議開始からのミリ秒）、テキスト、言語、平均 log probability、no-speech 確率、処理状態を持つ。confidence を真の確率と誤表示しない。
- 話者分離は MVP では自動提供しない。ソース（マイク/システム音声）を手掛かりとして別表示し、必要に応じて利用者が話者名を付与する。
## 6. 増分構造化要約

### 非同期ジョブ

区切り判定器は summary job を永続キューに登録し、ワーカーは transcript 確定イベントを購読して入力 snapshot を作る。UI の録音処理は CLI の完了を待たない。既定 concurrency=1 とし、同一会議のジョブを順序保証する。処理中に新しい区切りが来た場合は、同一ジョブへ統合するか後続ジョブに直列化する。

| 項目 | 仕様 |
| --- | --- |
| 入力 | 直近要約以降の確定 transcript、直前ノート、会議メタデータ、前回ノート版 ID。長文時は設定上限で分割し、重複文脈を含める。 |
| 実行 | claude -p を asyncio subprocess で起動。引数配列で固定プロンプト・JSON 出力指示を渡し、stdin または一時ファイルで入力する。秘密情報・環境変数を最小化。 |
| 出力 | 厳格な JSON Schema で検証。Markdown の自由形式出力を正規データとして扱わない。検証失敗は 1 回まで修復プロンプトで再試行。 |
| タイムアウト | 標準 120 秒。利用者キャンセル、会議削除、アプリ終了に連動してプロセスを終了する。 |
| 重複排除 | job_id と input_snapshot_hash に一意制約。再起動時 queued/running を回収し、running は再試行可能状態に戻す。 |
| 適用 | ノート版はトランザクションで追加。手動編集と競合したら自動上書きせず、差分候補として提示する。 |

### ノート形式

| 形式 | 内容 |
| --- | --- |
| Cornell 式 | cue: 質問・キーワード、notes: 要点と根拠、summary: セクションの短い要約。会議全体と会話区切り単位を区別する。 |
| バレット式 | 階層 bullet の配列。各項目は簡潔な主張、必要なら子項目、根拠、確度ラベルを含む。 |
| 決定事項 | 内容、状態（提案/合意/撤回/要確認）、会議内時刻、根拠 ID、抽出信頼度。AI は「合意」と断定できない場合「要確認」にする。 |
| アクションログ | タスク、担当者（不明可）、期限（不明可）、状態、根拠 ID、抽出元、確認済みフラグ。相対日付を勝手に絶対日付へ変換しない。 |

各項目は根拠 segment ID を最低 1 件、該当箇所が特定できない場合は空配列と「根拠なし」理由を持つ。要約は逐語記録の代替ではなく、利用者が編集・確認する作業仮説として表示する。

## 7. データモデルと JSON Schema

### SQLite エンティティ

| テーブル | 主要列 / 制約 |
| --- | --- |
| meetings | id UUID、title、started_at、ended_at、timezone、language、state、created_at、updated_at。state: preparing/recording/finalizing/completed/error/deleted。 |
| audio_sources | id、meeting_id、kind(microphone/system)、device_key、sample_rate、channels、permission_state、offset_ms。 |
| audio_chunks | id、meeting_id、source_id、start_ms、end_ms、storage_path nullable、sha256 nullable、status。音声不保持時は path/hash を残さない。 |
| transcript_segments | id、meeting_id、source_id、start_ms、end_ms、text、language、is_final、revision、stt_metadata_json、created_at。 |
| summary_jobs | id、meeting_id、trigger、status、input_hash、range_start_ms、range_end_ms、attempt、created_at、started_at、finished_at、error_code、next_retry_at。 |
| note_revisions | id、meeting_id、job_id、schema_version、payload_json、created_at、source_hash、supersedes_id nullable。 |
| consent_events | id、meeting_id、event_type、scope、granted、recorded_at、policy_version。PII 最小化し、同意本文全体や不要な端末情報は記録しない。 |
| settings | key、value_json、updated_at。秘密情報は OS credential store に保存し DB に置かない。 |

### JSON 出力スキーマ

JSON は schemaVersion による版管理を行う。時刻は会議開始からの整数ミリ秒、UUID は文字列、確度は 0〜1 の数値だが確率保証ではなくモデル由来の目安である。以下は主要フィールドの契約例。

```json
{
  "schemaVersion": "1.0",
  "meetingId": "uuid",
  "revisionId": "uuid",
  "generatedAt": "ISO-8601",
  "coverage": {"fromMs": 0, "toMs": 420000},
  "cornell": {
    "cues": [{"text": "検討テーマ", "evidenceSegmentIds": ["uuid"]}],
    "notes": [{"text": "要点", "evidenceSegmentIds": ["uuid"], "certainty": "stated"}],
    "summary": "区間の要約"
  },
  "bullets": [{"id": "uuid", "text": "論点", "children": [], "evidenceSegmentIds": ["uuid"]}],
  "decisions": [{"id": "uuid", "text": "決定事項", "status": "needs_review",
    "evidenceSegmentIds": ["uuid"], "certainty": 0.72}],
  "actions": [{"id": "uuid", "text": "担当タスク", "assignee": null,
    "dueDate": null, "status": "open", "confirmed": false,
    "evidenceSegmentIds": ["uuid"]}],
  "openQuestions": [{"text": "未解決事項", "evidenceSegmentIds": ["uuid"]}]
}
```

## 8. IPC・HTTP・イベント契約

UI と sidecar 間は loopback HTTP + WebSocket を基本契約とする。Tauri の invoke API は起動情報取得、OS 音声権限、ウィンドウ操作などネイティブ機能に限定する。API は /api/v1 を持ち、OpenAPI を成果物として同時管理する。すべての応答に requestId を含める。

| 操作 | 契約 | 要点 |
| --- | --- | --- |
| GET /health | REST | 起動状態、API 版、STT モデル状態、キュー件数。token 認証。 |
| POST /meetings | REST | 会議作成。title、language、source 設定、consent scopes。id と state を返す。 |
| POST /meetings/{id}/start | REST | 権限・同意・入力状態を検査後 recording へ遷移。冪等キー必須。 |
| POST /meetings/{id}/stop | REST | 録音停止、保留音声確定、finalizing へ。即時応答し後続状態イベントを通知。 |
| GET /meetings/{id}/transcript | REST | 時刻カーソルとページング。編集権限を会議ローカル利用者に限定。 |
| POST /meetings/{id}/summaries | REST | 手動要約要求。job_id を返却、重複要求は同一 snapshot で合流。 |
| GET /jobs/{id} | REST | queued/running/succeeded/failed/canceled と retryAfter、errorCode。 |
| WS /api/v1/events | WebSocket | meeting.state, transcript.partial, transcript.final, summary.queued, summary.updated, job.failed, audio.gap, privacy.state。再接続 cursor 対応。 |
| DELETE /meetings/{id} | REST | 音声・DB・キャッシュを消去する削除ジョブ。完了時に削除結果を返す。 |

### イベント例

```json
{"event":"transcript.final","eventId":"uuid","meetingId":"uuid","seq":18,
 "occurredAt":"2026-09-29T10:03:12+09:00","data":{"segmentId":"uuid",
 "startMs":18200,"endMs":23950,"text":"次回までに試作します。","source":"microphone"}}
```

## 9. キュー、再試行、障害処理

| 障害 | 検知・画面表示 | 回復 |
| --- | --- | --- |
| マイク権限拒否 | permission_denied。録音開始禁止。設定への案内。 | 権限変更後に再確認。 |
| システム音声非対応 | source_unavailable と OS 別説明。 | マイク単独で会議を開始可能。 |
| STT モデル不足/ロード失敗 | model_unavailable / model_load_failed。録音と STT の状態を分ける。 | 保存済み音声があれば復旧後に再処理。 |
| STT 遅延 | キュー滞留秒数を表示。録音は継続し、閾値超過で警告。 | キューが空になるまで要約を抑制する。 |
| SQLite 書き込み失敗 | db_write_failed。新規音声処理を安全停止し、ユーザーに保存状態を明示。 | 一時リトライ後、録音停止を提案。破損疑い時は自動上書きしない。 |
| Claude CLI 不在/認証失敗 | cli_not_found / cli_auth_required。要約のみ停止。 | 逐語録を保持して後から再試行。認証情報をアプリに収集しない。 |
| CLI timeout/不正 JSON | timeout / invalid_output。ノート版は適用しない。 | 指数バックオフ（最大 3 試行）、手動再試行、失敗ログを秘匿化。 |
| アプリ異常終了 | 起動時に未完了 meeting/job を検出。 | 未完了ジョブを回収。録音状態は自動再開せず、ユーザー確認を求める。 |

再試行は一時的障害に限定し、初期バックオフを 2 秒、4 秒、8 秒、最大 60 秒とする。4xx 相当の同意不足、入力不正、権限拒否は自動再試行しない。エラー詳細はユーザー向け説明と診断コードに分離し、transcript やプロンプト全文を通常ログへ書かない。

## 10. プライバシー、同意、企業データ

### 初期プライバシー既定値

- 音声はローカルで取得・STT。音声アーカイブは既定無効。利用者が有効化した場合のみ暗号化保存し、保持期間を明示する。
- Claude CLI 連携は既定無効。会議ごとに有効化し、送られるのは transcript と必要最小限の直近ノート。生音声、連絡先、他会議データを送らない。
- アプリは録音開始前に参加者への通知・同意取得を利用者に促し、その確認操作とポリシー版を会議に記録する。地域・組織の法令遵守をアプリが保証する表示はしない。
- ログには原則として会議本文、音声、CLI 入出力を含めない。クラッシュ診断を共有する場合も個別同意と送信前プレビューを要求する。
### 企業利用の扱い

MVP は単一ユーザープロファイルのローカル保存を想定し、共有ワークスペースや管理者ポリシー配布は提供しない。企業導入では、Claude Code CLI の利用契約・組織設定・データ処理条件が会議テキストに適用されるか、組織管理者が確認する必要がある。CLI 連携を禁止する組織設定、送信前の redaction、監査ログは導入前の要件候補とする。

### 削除と保持

会議削除は SQLite のレコード、音声、派生キャッシュ、一時ファイル、エクスポート済みのアプリ管理ファイルを対象にする。OS バックアップやユーザー自身のコピーはアプリが消去できないと説明する。WAL checkpoint 後に削除完了を通知する。暗号化保存では会議ごとのデータ鍵を破棄する設計を将来拡張できる。

## 11. セキュリティ要件

- Tauri CSP を厳格化し、remote origin の任意読込、過剰な shell/fs capability、ワイルドカード IPC を禁止する。
- sidecar は localhost のランダムポートだけで listen。起動時ランダム token、Origin 検査、CSRF 対策、短い有効期間を採用し、認証情報を URL・ログへ出さない。
- claude -p は shell 経由で起動しない。固定 executable 解決、引数配列、タイムアウト、stdout/stderr サイズ制限、プロセス終了制御を実施する。
- CLI 出力と transcript は不可信入力として扱い、プロンプトインジェクションを命令として実行しない。構造化要約は抽出に限定し、ツール・ファイル・コマンド実行を要求しない。
- DB と音声ファイルはユーザープロファイル権限で保護し、ファイルパーミッションを OS の標準安全設定にする。鍵・トークンは OS credential store を優先する。
- 依存モデル、Python パッケージ、Tauri バイナリの供給元、ハッシュ、ライセンス、更新経路をリリース時に記録する。依存更新・署名・脆弱性対応の運用方針をリリース前に定める。
## 12. 非機能要件

| 領域 | MVP 目標 / 制約 |
| --- | --- |
| レイテンシ | 音声入力から暫定表示 p95 5 秒以内（基準 PC）。発話終了から要約ジョブ投入 3 秒以内。要約完了 p95 45 秒以内は外部 CLI 性能に依存するため目標値。 |
| 継続性 | 30 分会議で無音区間を含む音声の取りこぼしを検出可能。OS のスリープ・デバイス切断時に状態を明示する。 |
| 性能 | 4 コア CPU / 16 GB RAM を暫定基準。STT モデル選択で実測メモリを表示し、メモリ不足を検出する。 |
| アクセシビリティ | 主要操作をキーボードで実行、フォーカス可視化、録音状態を色だけに依存せずテキスト表示。日本語 UI。 |
| 保守性 | API / DB / note schema に版番号。ログは構造化 JSON、相関 requestId、機微データ除外。マイグレーションは前方互換性を検証。 |
| 可搬性 | MVP は Windows 11 と macOS 13+ を候補対象。Linux は後続。OS 音声取得の可否・権限 UX は対象 OS を確定してから保証する。 |
| 可用性 | 要約プロバイダー障害でローカル録音・STT を止めない。アプリ異常終了時は未完了データの整合性を優先し、自動録音再開しない。 |

## 13. MVP 範囲と受け入れ基準

### MVP に含める

- 新規会議・ローカル一覧・ライブ画面・会議後画面。
- マイク録音、OS が許可するシステム音声取得アダプター（対象 OS を限定して提供）。
- Silero VAD、faster-whisper ローカル認識、暫定/確定 transcript、無音・手動・最終区切り。
- 非同期 claude -p 要約、Cornell、bullet、決定事項、アクション項目、根拠参照、編集と版管理。
- SQLite、再起動時ジョブ回収、明示同意、外部送信制御、会議削除、Markdown/JSON export。
### 後続候補

- 自動話者分離、チーム共有、クラウド同期、ブラウザ会議の自動検知、モバイル版、カレンダー連携、管理者ポリシー、他の LLM プロバイダー。
### 受け入れ基準

| ID | 基準 |
| --- | --- |
| AC-01 | 同意と必要な権限がない状態では録音開始できず、理由と設定経路が表示される。 |
| AC-02 | 録音中、逐語記録の暫定・確定状態と会議経過時刻が区別されて表示される。 |
| AC-03 | 無音区切り・手動要約・終了時の各操作で永続 job が作成され、UI は処理中も音声処理を継続する。 |
| AC-04 | CLI 不在・認証失敗・不正 JSON が起きても transcript を保持し、要約を後から再試行できる。 |
| AC-05 | 要約 JSON が schema 検証を通り、決定・タスク・ノート項目の根拠 ID が存在する segment を指す。 |
| AC-06 | 手修正済みノートが後続要約で無断上書きされず、競合が確認可能である。 |
| AC-07 | 会議削除後にアプリ管理下の音声・ノート・DB レコード・一時ファイルが消え、成功/失敗範囲が表示される。 |
| AC-08 | sidecar API は loopback 外から到達できず、token なし要求を拒否する。 |
| AC-09 | デバイス切断・DB 書き込み失敗・STT 遅延で警告を示し、状態と欠落範囲を保存する。 |
| AC-10 | 30 分の代表会議シナリオで、録音終了・ノート確定・JSON と Markdown 書き出しまで完了する。 |

## 14. 段階的実装計画

| 段階 | 成果 | 終了条件 |
| --- | --- | --- |
| 0. 技術検証 | 対象 OS 決定、音声 API、システム音声権限、Tauri sidecar packaging、faster-whisper 性能、claude -p 契約の実測。 | OS ごとの録音経路と配布条件を決め、録音同意・外部送信画面をレビュー。 |
| 1. 基盤 | Tauri 起動、静的 Next.js UI、Python sidecar 起動/終了、認証済み API、SQLite migration、ログ。 | sidecar 異常終了・再起動・ポート競合の状態が安全に回復。 |
| 2. 音声と STT | マイク、許可済みシステム音声、VAD、STT、segment CRUD、ライブ transcript。 | 手動録音試験で時刻・欠落検出・遅延目標を測定。 |
| 3. 要約 | 永続キュー、claude -p、schema validation、Cornell/Bullet/decision/action、根拠ジャンプ。 | CLI 障害・キャンセル・不正出力・手編集競合の受け入れ確認。 |
| 4. プライバシーと仕上げ | 同意、外部送信制御、削除、export、アクセシビリティ、署名・パッケージ。 | 全 AC、データ保持・削除・更新手順、利用者向け説明を確認。 |
| 5. 限定配布 | 対象者を限定した実環境評価、音声品質・負荷・ノート品質の改善。 | 評価結果をもとに既定モデル、閾値、対応 OS、サポート範囲を確定。 |

## 15. 推奨ディレクトリ構造

```text
sekretaer/
  apps/
    desktop/                 # Tauri shell, capabilities, Rust commands
    web/                     # Next.js static UI, components, pages
  sidecar/
    pyproject.toml
    sekretaer/
      api/                   # REST, WebSocket, auth, OpenAPI
      audio/                 # device adapters, ring buffer, chunk writer
      vad/                   # Silero adapter and segment boundaries
      stt/                   # faster-whisper adapter and model manager
      pipeline/              # orchestration, event bus, state machine
      summarization/         # claude CLI, prompt, schema validation
      jobs/                  # persistent queue, retries, cancellation
      storage/               # SQLite repositories and migrations
      privacy/               # consent, retention, deletion, export
  contracts/
    openapi/                 # HTTP contract
    events/                  # WebSocket JSON schema
    notes/                   # versioned note JSON Schema
  docs/
    product-spec.md
    threat-model.md
    decisions/               # ADRs and unresolved decisions
  tests/
    fixtures/                # synthetic audio and transcript fixtures
    integration/
    acceptance/
  packaging/                 # model manifest, signing, installers
  scripts/                   # build, schema checks, release utilities
```

## 16. 未決事項と推奨仮決め

| 優先度 | 決定事項 | 推奨仮決め / 決定期限 |
| --- | --- | --- |
| P0 | 対応 OS と配布形態 | Windows 11 と macOS 13+ を候補とするが、システム音声 API の実装調査後に対象を一つ以上確定。署名・ノータライズ・更新方式も同時決定。 |
| P0 | 音声アーカイブの既定と保持期間 | 既定 OFF。必要時のみ会議別 opt-in。保存上限は利用者が選び、既定は会議終了後 7 日または即時削除案。 |
| P0 | Claude CLI の対応条件 | インストール・認証・組織ポリシー・データ処理条件をサポート対象環境で確認。対応不能なら要約機能を明示的に無効化できる。 |
| P1 | STT モデル既定 | 日本語精度と 16 GB メモリで small/int8 候補を比較し、初回ダウンロード容量と p95 遅延で決定。 |
| P1 | 録音時の通知文・同意記録 | 地域法・組織規則のレビューを得て文面と必須フローを確定。アプリの汎用チェックを法的同意の代替としない。 |
| P1 | 外部送信時の redaction | MVP は送信対象プレビューと手動マスキングを用意。自動個人情報検出を必須化するか、企業導入前に決定。 |
| P2 | 複数参加者と共有 | MVP は単一ユーザー・ローカル。共有、共同編集、権限モデルは別仕様。 |
| P2 | ノートスキーマ進化 | schemaVersion と migration 方針を維持。既存 export を破壊しない互換性ポリシーをリリース前に定める。 |

仕様変更は ADR（Architecture Decision Record）として理由、選択肢、影響、決定日を記録する。特に P0 は製品実装を固定する前に解消する。

## 付録 A 主要状態遷移

| 対象 | 状態遷移 |
| --- | --- |
| 会議 | preparing → recording → finalizing → completed。入力障害は recording を維持したまま degraded を併記。致命的保存障害のみ error。削除はどの終了状態からも deleting → deleted。 |
| セグメント | partial → final。訂正は revision を増やし、過去版を監査可能にする。 |
| ジョブ | queued → running → succeeded / failed / canceled。一時失敗時は retry_wait → queued。 |
| 同意 | 未確認 → scope ごとに granted / denied。録音 scope と外部処理 scope は独立。撤回時は新規取得・送信を停止し、保持データの扱いを確認する。 |

## 付録 B エラーコード

| コード | 意味 |
| --- | --- |
| permission_denied | OS 音声取得権限なし |
| source_unavailable | 選択した入力ソースが利用不可 |
| audio_gap_detected | 音声チャンク欠落を検出 |
| stt_model_unavailable | 認識モデル未導入または取得不可 |
| stt_queue_backlog | 認識キューが遅延 |
| db_write_failed | 永続化に失敗 |
| cli_not_found | claude executable を検出できない |
| cli_auth_required | CLI 認証が必要 |
| cli_timeout | 要約処理が制限時間超過 |
| summary_invalid_schema | 構造化出力が schema 不適合 |
| consent_required | 必要な同意 scope がない |
| delete_incomplete | 一部のアプリ管理データ削除に失敗 |
