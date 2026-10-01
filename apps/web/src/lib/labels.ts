// Japanese user-facing labels and explanations. Pure data + lookups so they
// can be unit-tested and reused across screens.

import type {
  ActionStatus,
  CertaintyLabel,
  ConsentState,
  DecisionStatus,
  JobStatus,
  JobTrigger,
  MeetingLanguage,
  MeetingState,
  PermissionState,
  SourceKind,
  SttState,
  SummarizerState,
} from "./types";

export const MEETING_STATE_LABEL: Record<MeetingState, string> = {
  preparing: "準備中",
  recording: "録音中",
  finalizing: "確定処理中",
  completed: "完了",
  error: "エラー",
  deleting: "削除中",
  deleted: "削除済み",
};

export const LANGUAGE_LABEL: Record<MeetingLanguage, string> = {
  auto: "自動",
  ja: "日本語",
  en: "English",
};

export const SOURCE_LABEL: Record<SourceKind, string> = {
  microphone: "マイク",
  system: "システム音声",
};

export const PERMISSION_LABEL: Record<PermissionState, string> = {
  granted: "許可済み",
  denied: "拒否",
  unknown: "未確認",
  unavailable: "利用不可",
};

export const CONSENT_LABEL: Record<ConsentState, string> = {
  unconfirmed: "未確認",
  granted: "同意済み",
  denied: "同意なし",
};

export const JOB_STATUS_LABEL: Record<JobStatus, string> = {
  queued: "待機中",
  running: "要約中",
  retry_wait: "再試行待ち",
  succeeded: "完了",
  failed: "失敗",
  canceled: "キャンセル済み",
};

export const JOB_TRIGGER_LABEL: Record<JobTrigger, string> = {
  silence: "無音区切り",
  long_speech: "長時間発話",
  manual: "手動",
  final: "会議終了",
};

export const DECISION_STATUS_LABEL: Record<DecisionStatus, string> = {
  proposed: "提案",
  agreed: "合意",
  withdrawn: "撤回",
  needs_review: "要確認",
};

export const ACTION_STATUS_LABEL: Record<ActionStatus, string> = {
  open: "未着手",
  in_progress: "進行中",
  done: "完了",
  canceled: "中止",
};

export const CERTAINTY_LABEL: Record<CertaintyLabel, string> = {
  stated: "発言あり",
  inferred: "推定",
  uncertain: "不確か",
};

export const STT_STATE_TEXT: Record<SttState, { label: string; explain: string; ok: boolean }> = {
  ready: { label: "準備完了", explain: "ローカル音声認識を利用できます。", ok: true },
  loading: {
    label: "読み込み中",
    explain: "音声認識モデルを読み込んでいます。録音は開始できますが、文字起こしの表示が遅れることがあります。",
    ok: false,
  },
  not_downloaded: {
    label: "モデル未取得",
    explain:
      "音声認識モデルがまだ取得されていません。設定画面でモデルとダウンロード先を確認してください。録音と文字起こしは別に扱われ、モデル取得後に保存済み音声を再処理できます。",
    ok: false,
  },
  failed: {
    label: "読み込み失敗",
    explain: "音声認識モデルの読み込みに失敗しました。設定画面でモデル・計算方式・デバイスを確認してください。",
    ok: false,
  },
  unavailable: {
    label: "利用不可",
    explain: "この環境では音声認識を利用できません。設定画面を確認してください。",
    ok: false,
  },
};

export const SUMMARIZER_STATE_TEXT: Record<SummarizerState, { label: string; explain: string; ok: boolean }> = {
  ready: { label: "利用可能", explain: "Claude Code CLI による要約を利用できます（会議ごとに有効化が必要です）。", ok: true },
  disabled: {
    label: "無効",
    explain: "要約機能は無効です。録音とローカル文字起こしはそのまま利用できます。",
    ok: true,
  },
  cli_not_found: {
    label: "CLI が見つかりません",
    explain:
      "claude コマンド（Claude Code CLI）が見つかりません。要約だけが停止しており、録音と文字起こしは続けられます。CLI をインストールするか、設定画面で CLI のパスを指定してから、要約を再試行してください。",
    ok: false,
  },
  cli_auth_required: {
    label: "CLI の認証が必要",
    explain:
      "Claude Code CLI にログインしていません。ターミナルで claude を起動して認証を済ませてから、要約を再試行してください。Sekretär が認証情報を収集することはありません。録音と文字起こしは続けられます。",
    ok: false,
  },
};

/** Warning / degraded codes (spec appendix B + likely sidecar extras). */
export const WARNING_LABEL: Record<string, string> = {
  stt_queue_backlog: "認識キューが遅延しています（録音は継続中）",
  audio_gap_detected: "音声の欠落を検出しました",
  db_write_failed: "保存に失敗しています。録音の停止を検討してください",
  stt_model_unavailable: "音声認識モデルを利用できません（録音は継続中）",
  source_unavailable: "入力ソースが利用できなくなりました",
  permission_denied: "音声取得の権限がありません",
  cpu_high: "CPU 負荷が高くなっています",
  cpu_overload: "CPU 負荷が高くなっています",
  device_disconnected: "入力デバイスが切断されました",
};

export function warningLabel(code: string): string {
  return WARNING_LABEL[code] ?? `警告: ${code}`;
}
