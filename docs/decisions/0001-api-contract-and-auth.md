# ADR 0001: サイドカー API の認証とトークン受け渡し

- 状態: 採用
- 決定日: 2026-09-30

## 状況
仕様 §3/§11 は、サイドカーが loopback のランダムポートで待ち受けること、起動時ランダム token、Origin 検査、CSRF 対策を実施すること、認証情報を URL とログに出さないことを求める。WebView の WebSocket API では任意のヘッダーを付けられない。

## 選択肢
1. token をクエリ文字列に入れる。実装は簡単だが URL に残るため §11 に反する。
2. Cookie を使う。WebView とサイドカーでオリジンが異なり、SameSite の扱いも複雑になる。
3. REST は `Authorization: Bearer`、WebSocket は `Sec-WebSocket-Protocol` に `bearer.<token>` を載せる。

## 決定
3 を採用する。token は stdout の ready 行でのみ親プロセス（Tauri）に渡し、UI は `invoke('sidecar_connection')` で取得する。Origin は Tauri のオリジンに限定する（開発モードでのみ `http://localhost:3000` を追加）。変更系の要求には `Content-Type: application/json` を必須にし、未知のオリジンから単純リクエストを送れないようにする。

## 影響
- ブラウザー単独モードでは token を手入力する必要がある（開発用途に限る）。
- token の有効期間はサイドカープロセスの寿命と同じ。再起動すると新しい token とポートが発行される。
