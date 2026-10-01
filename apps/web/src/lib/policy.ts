// Consent policy text version recorded with POST /meetings/{id}/consent
// (spec §10: record the confirmation and the policy version). Bump when the
// consent wording in the UI changes.
export const CONSENT_POLICY_VERSION = "ui-2026-09.1";

export const EXTERNAL_DESTINATION = "Claude Code CLI（claude -p）";

/** What is (and is not) sent when external processing is enabled (spec §10). */
export const EXTERNAL_SEND_SCOPE = {
  sent: ["この会議の確定した逐語記録（テキスト）", "必要最小限の直近ノート（前回の要約結果）", "会議タイトルなど最小限のメタデータ"],
  notSent: ["録音した音声そのもの", "連絡先などの個人情報", "ほかの会議のデータ"],
} as const;
