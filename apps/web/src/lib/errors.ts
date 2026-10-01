// Error model for the sidecar API (api-v1.md "Envelope", spec §9 / appendix B).

export interface ErrorGuidance {
  /** Short Japanese explanation used when the server message is missing. */
  message: string;
  /** Where the user can fix the cause, if anywhere. */
  settingsHint?: "os_privacy" | "app_settings" | "consent" | "devices";
}

export const ERROR_GUIDANCE: Record<string, ErrorGuidance> = {
  permission_denied: {
    message: "OS の音声取得権限がありません。OS のプライバシー設定でマイクへのアクセスを許可してから、もう一度開始してください。",
    settingsHint: "os_privacy",
  },
  source_unavailable: {
    message: "選択した入力ソースを利用できません。デバイスの接続を確認するか、マイクのみで会議を開始してください。",
    settingsHint: "devices",
  },
  consent_required: {
    message: "必要な同意がありません。録音同意（または外部送信の同意）を確認してください。",
    settingsHint: "consent",
  },
  audio_gap_detected: { message: "音声チャンクの欠落を検出しました。" },
  stt_model_unavailable: {
    message: "音声認識モデルが導入されていないか取得できません。設定画面を確認してください。",
    settingsHint: "app_settings",
  },
  stt_queue_backlog: { message: "音声認識の処理が遅れています。録音は継続しています。" },
  db_write_failed: { message: "データの保存に失敗しました。録音の停止を検討してください。" },
  cli_not_found: {
    message: "Claude Code CLI（claude）が見つかりません。要約のみ停止しています。",
    settingsHint: "app_settings",
  },
  cli_auth_required: {
    message: "Claude Code CLI の認証が必要です。ターミナルで claude にログインしてから再試行してください。",
  },
  cli_failed: { message: "Claude Code CLI の実行に失敗しました（ネットワークや利用制限など）。自動で再試行します。" },
  cli_timeout: { message: "要約処理が制限時間を超えました。後から再試行できます。" },
  summary_invalid_schema: { message: "要約結果の形式が不正でした。ノートは更新されていません。後から再試行できます。" },
  timeout: { message: "要約処理が制限時間を超えました。後から再試行できます。" },
  invalid_output: { message: "要約結果の形式が不正でした。ノートは更新されていません。" },
  delete_incomplete: { message: "一部のデータを削除できませんでした。" },
  unauthorized: { message: "サイドカーへの認証に失敗しました。アプリを再起動するか、接続情報を確認してください。" },
  origin_forbidden: { message: "この画面からサイドカーへの接続は許可されていません。" },
  not_found: { message: "対象が見つかりません。削除された可能性があります。" },
  invalid_request: { message: "入力内容に誤りがあります。" },
  invalid_state: { message: "現在の状態ではこの操作を実行できません。" },
  conflict: { message: "ほかの更新と競合しました。最新の内容を確認してください。" },
  network_error: { message: "サイドカーに接続できません。起動状態を確認してください。" },
  invalid_response: { message: "サイドカーから不正な応答を受け取りました。" },
};

export function guidanceFor(code: string): ErrorGuidance {
  return ERROR_GUIDANCE[code] ?? { message: `エラーが発生しました（${code}）。` };
}

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly requestId: string | null;
  readonly details: Record<string, unknown>;
  /** Japanese message for display: server text when present, else our guidance. */
  readonly userMessage: string;

  constructor(opts: {
    code: string;
    status: number;
    requestId: string | null;
    serverMessage?: string | null;
    details?: Record<string, unknown>;
  }) {
    const userMessage =
      opts.serverMessage && opts.serverMessage.trim() !== ""
        ? opts.serverMessage
        : guidanceFor(opts.code).message;
    super(`${opts.code}: ${userMessage}`);
    this.name = "ApiError";
    this.code = opts.code;
    this.status = opts.status;
    this.requestId = opts.requestId;
    this.details = opts.details ?? {};
    this.userMessage = userMessage;
  }

  get settingsHint(): ErrorGuidance["settingsHint"] {
    return guidanceFor(this.code).settingsHint;
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}

/** Normalise anything thrown into an ApiError for display. */
export function toApiError(e: unknown): ApiError {
  if (e instanceof ApiError) return e;
  return new ApiError({
    code: "network_error",
    status: 0,
    requestId: null,
    serverMessage: null,
    details: { cause: e instanceof Error ? e.message : String(e) },
  });
}
