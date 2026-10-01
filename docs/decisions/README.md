# Architecture Decision Records

仕様書 §16 に従い、仕様変更と未決事項の仮決めを ADR として記録する。各 ADR は「状況 / 選択肢 / 決定 / 影響 / 状態」を持つ。状態は `提案` `採用` `仮決め（期限付き）` `廃止` のいずれか。

| ADR | 題目 | 状態 |
| --- | --- | --- |
| [0001](0001-api-contract-and-auth.md) | サイドカー API の認証とトークン受け渡し | 採用 |
| [0002](0002-summarizer-segment-aliases.md) | 要約 CLI への根拠 ID の渡し方と検証 | 採用 |
| [0003](0003-optional-native-components.md) | ネイティブ依存（STT/VAD/音声取得）のアダプター化と縮退 | 採用 |
| [0004](0004-target-os-and-distribution.md) | 対応 OS と配布形態（P0） | 仮決め（段階 0 終了まで） |
| [0005](0005-audio-archive-default.md) | 音声アーカイブ既定値と保持期間（P0） | 仮決め |
| [0006](0006-claude-cli-support-conditions.md) | Claude CLI の対応条件（P0） | 仮決め |
| [0007](0007-open-p1-p2-items.md) | P1/P2 未決事項の扱い | 提案 |
