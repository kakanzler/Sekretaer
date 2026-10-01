"""API error codes (spec appendix B + contract additions) and user-facing Japanese messages."""

from __future__ import annotations

from typing import Any

DEFAULT_STATUS: dict[str, int] = {
    "unauthorized": 401,
    "origin_forbidden": 403,
    "not_found": 404,
    "invalid_request": 400,
    "invalid_state": 409,
    "conflict": 409,
    "consent_required": 409,
    "permission_denied": 409,
    "source_unavailable": 409,
    "audio_gap_detected": 409,
    "stt_model_unavailable": 409,
    "stt_queue_backlog": 409,
    "db_write_failed": 500,
    "cli_not_found": 409,
    "cli_auth_required": 409,
    "cli_timeout": 504,
    "summary_invalid_schema": 502,
    "delete_incomplete": 500,
    "internal_error": 500,
}

MESSAGES_JA: dict[str, str] = {
    "unauthorized": "認証トークンがないか、無効です。",
    "origin_forbidden": "許可されていない接続元からの要求です。",
    "not_found": "対象が見つかりません。",
    "invalid_request": "要求の形式が正しくありません。",
    "invalid_state": "現在の状態ではこの操作を実行できません。",
    "conflict": "ほかの変更と競合しました。最新の版を読み込み直してください。",
    "consent_required": "必要な同意が記録されていません。会議のプライバシー設定で同意を確認してください。",
    "permission_denied": "音声入力の権限がありません。OS の設定でマイク／システム音声へのアクセスを許可してください。",
    "source_unavailable": "選択した入力ソースを利用できません。",
    "audio_gap_detected": "音声の欠落を検出しました。",
    "stt_model_unavailable": "音声認識モデルが導入されていないか、利用できません。録音は継続できます。",
    "stt_queue_backlog": "音声認識が遅れています。録音は継続しています。",
    "db_write_failed": "データの保存に失敗しました。",
    "cli_not_found": "Claude CLI (claude) が見つかりません。インストールと設定のパスを確認してください。",
    "cli_auth_required": "Claude CLI の認証が必要です。ターミナルで claude にログインしてから再試行してください。",
    "cli_timeout": "要約処理が制限時間を超えました。",
    "cli_failed": "Claude CLI の実行に失敗しました。時間をおいて再試行してください。",
    "summary_invalid_schema": "要約結果が規定の形式に合いませんでした。",
    "delete_incomplete": "一部のデータを削除できませんでした。",
    "internal_error": "内部エラーが発生しました。",
}


class ApiError(Exception):
    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status = status or DEFAULT_STATUS.get(code, 400)
        self.message = message or MESSAGES_JA.get(code, MESSAGES_JA["internal_error"])
        self.details = details or {}


def not_found(what: str = "resource") -> ApiError:
    return ApiError("not_found", details={"resource": what})
