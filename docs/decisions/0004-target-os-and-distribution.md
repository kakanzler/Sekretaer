# ADR 0004: 対応 OS と配布形態（P0）

- 状態: 仮決め（段階 0「技術検証」終了まで）
- 決定日: 2026-09-30（仮）

## 状況
仕様 §12/§16 は Windows 11 と macOS 13+ を候補とし、システム音声 API を調査した後に対象を 1 つ以上確定するよう求めている。

## 仮決め
- 開発と最初の検証は **Windows 11** で行う。マイクは PortAudio（sounddevice）、システム音声は WASAPI loopback のアダプターで取得する。
- macOS のシステム音声（ScreenCaptureKit / 仮想デバイス）はアダプター枠だけを用意し、`source_unavailable` を返す。
- 配布は Tauri のインストーラーに、PyInstaller でビルドしたサイドカーを `externalBin` として同梱する方針とする（`packaging/README.md`）。署名、ノータライズ、自動更新は未決。

## 決定期限
段階 0 の終了時。実機で録音経路と権限 UX を確認してから確定する。
