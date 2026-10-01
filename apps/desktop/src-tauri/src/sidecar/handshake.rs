//! Launch handshake parsing (contracts/api-v1.md "Launch handshake") and
//! credential redaction helpers.
//!
//! The ready line carries the bearer token. Nothing in this module ever puts
//! the token into a `Debug`/`Display` string or an error message: the token is
//! wrapped in [`SecretToken`] and field errors use fixed texts.

use std::fmt;

use serde::{Serialize, Serializer};
use serde_json::{Map, Value};

/// API version this shell speaks (`apiVersion` in the ready line).
pub const API_VERSION: &str = "1";

/// Replacement text for credentials in logs.
pub const REDACTED: &str = "[REDACTED]";

/// Upper bound for a single log line forwarded from the sidecar.
pub const MAX_LOG_LINE_CHARS: usize = 4096;

/// Bearer token for the sidecar API.
///
/// `Debug` never prints the value and there is intentionally no `Display`
/// impl. `Serialize` does emit the value: that is only used for the
/// `sidecar_connection` command / `sidecar://state` event, i.e. the one place
/// the webview is supposed to receive it (spec §3).
#[derive(Clone, PartialEq, Eq)]
pub struct SecretToken(String);

impl SecretToken {
    pub fn new(value: impl Into<String>) -> Self {
        Self(value.into())
    }

    /// Returns the raw token. Callers must not log the result.
    pub fn expose(&self) -> &str {
        &self.0
    }

    /// Replaces every occurrence of the token in `text` with [`REDACTED`].
    pub fn redact(&self, text: &str) -> String {
        if self.0.is_empty() {
            text.to_owned()
        } else {
            text.replace(&self.0, REDACTED)
        }
    }
}

impl fmt::Debug for SecretToken {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "SecretToken({REDACTED})")
    }
}

impl Serialize for SecretToken {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.0)
    }
}

/// Parsed `{"type":"ready",...}` line.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReadyInfo {
    pub api_version: String,
    pub port: u16,
    pub token: SecretToken,
    pub pid: u32,
}

/// Classification of one stdout line read before the handshake completed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReadyLine {
    Ready(ReadyInfo),
    /// Not a ready line (not JSON, not an object, or `type` != "ready").
    /// Such lines are skipped.
    NotReady,
}

/// A line that claims to be the ready line but is unusable. Fatal for this
/// launch attempt. Messages never contain the token.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReadyLineError {
    MissingField(&'static str),
    InvalidField(&'static str),
    UnsupportedApiVersion(String),
}

impl fmt::Display for ReadyLineError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MissingField(field) => {
                write!(f, "sidecar ready line is missing field `{field}`")
            }
            Self::InvalidField(field) => {
                write!(f, "sidecar ready line has an invalid `{field}` value")
            }
            Self::UnsupportedApiVersion(version) => write!(
                f,
                "sidecar speaks API version {version:?}, this app requires {API_VERSION:?}"
            ),
        }
    }
}

impl std::error::Error for ReadyLineError {}

/// Parses one stdout line of the sidecar.
pub fn parse_ready_line(line: &str) -> Result<ReadyLine, ReadyLineError> {
    let Ok(value) = serde_json::from_str::<Value>(line.trim()) else {
        return Ok(ReadyLine::NotReady);
    };
    let Some(object) = value.as_object() else {
        return Ok(ReadyLine::NotReady);
    };
    if object.get("type").and_then(Value::as_str) != Some("ready") {
        return Ok(ReadyLine::NotReady);
    }

    let api_version = required(object, "apiVersion")?
        .as_str()
        .ok_or(ReadyLineError::InvalidField("apiVersion"))?;
    if api_version != API_VERSION {
        return Err(ReadyLineError::UnsupportedApiVersion(truncate_chars(
            api_version,
            32,
        )));
    }

    let port = required(object, "port")?
        .as_u64()
        .and_then(|p| u16::try_from(p).ok())
        .filter(|p| *p != 0)
        .ok_or(ReadyLineError::InvalidField("port"))?;

    let token = required(object, "token")?
        .as_str()
        .filter(|t| !t.is_empty())
        .ok_or(ReadyLineError::InvalidField("token"))?;

    let pid = required(object, "pid")?
        .as_u64()
        .and_then(|p| u32::try_from(p).ok())
        .ok_or(ReadyLineError::InvalidField("pid"))?;

    Ok(ReadyLine::Ready(ReadyInfo {
        api_version: api_version.to_owned(),
        port,
        token: SecretToken::new(token),
        pid,
    }))
}

fn required<'a>(
    object: &'a Map<String, Value>,
    field: &'static str,
) -> Result<&'a Value, ReadyLineError> {
    object.get(field).ok_or(ReadyLineError::MissingField(field))
}

/// Makes a line whose credentials are unknown safe to log: lines mentioning a
/// token (e.g. a malformed ready line or dev-mode connection info) are
/// withheld, everything else is truncated.
pub fn sanitize_untrusted_line(line: &str) -> String {
    let lower = line.to_ascii_lowercase();
    if lower.contains("token") || lower.contains("bearer") {
        format!(
            "[line withheld: may contain credentials, {} bytes]",
            line.len()
        )
    } else {
        truncate_chars(line, MAX_LOG_LINE_CHARS)
    }
}

/// Truncates on a char boundary and marks the cut.
pub fn truncate_chars(text: &str, max_chars: usize) -> String {
    match text.char_indices().nth(max_chars) {
        Some((cut, _)) => format!("{}…", &text[..cut]),
        None => text.to_owned(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const TOKEN: &str = "s3cr3t-T0ken_value-abcdefghijklmnopqrstuvw";

    fn ready_json() -> String {
        format!(r#"{{"type":"ready","apiVersion":"1","port":53124,"token":"{TOKEN}","pid":1234}}"#)
    }

    #[test]
    fn parses_valid_ready_line() {
        let parsed = parse_ready_line(&ready_json()).unwrap();
        let ReadyLine::Ready(info) = parsed else {
            panic!("expected ready");
        };
        assert_eq!(info.port, 53124);
        assert_eq!(info.pid, 1234);
        assert_eq!(info.api_version, "1");
        assert_eq!(info.token.expose(), TOKEN);
    }

    #[test]
    fn tolerates_crlf_and_whitespace() {
        let line = format!("  {}\r\n", ready_json());
        assert!(matches!(parse_ready_line(&line), Ok(ReadyLine::Ready(_))));
    }

    #[test]
    fn skips_garbage_lines() {
        for line in [
            "",
            "Resolved 42 packages in 3ms",
            "warning: something",
            "{not json",
            "[1,2,3]",
            "\"ready\"",
            r#"{"type":"log","msg":"hello"}"#,
            r#"{"port":1,"token":"x"}"#,
        ] {
            assert_eq!(parse_ready_line(line), Ok(ReadyLine::NotReady), "{line}");
        }
    }

    #[test]
    fn finds_ready_line_after_garbage() {
        let stdout = format!("uv: syncing\n{{\"type\":\"progress\"}}\n{}\n", ready_json());
        let found = stdout
            .lines()
            .map(parse_ready_line)
            .find_map(|r| match r {
                Ok(ReadyLine::Ready(info)) => Some(info),
                _ => None,
            })
            .expect("ready line");
        assert_eq!(found.port, 53124);
    }

    #[test]
    fn rejects_missing_fields() {
        let cases = [
            (
                r#"{"type":"ready","port":1,"token":"t","pid":1}"#,
                "apiVersion",
            ),
            (
                r#"{"type":"ready","apiVersion":"1","token":"t","pid":1}"#,
                "port",
            ),
            (
                r#"{"type":"ready","apiVersion":"1","port":1,"pid":1}"#,
                "token",
            ),
            (
                r#"{"type":"ready","apiVersion":"1","port":1,"token":"t"}"#,
                "pid",
            ),
        ];
        for (line, field) in cases {
            assert_eq!(
                parse_ready_line(line),
                Err(ReadyLineError::MissingField(field)),
                "{line}"
            );
        }
    }

    #[test]
    fn rejects_invalid_fields() {
        let cases = [
            (
                r#"{"type":"ready","apiVersion":1,"port":1,"token":"t","pid":1}"#,
                "apiVersion",
            ),
            (
                r#"{"type":"ready","apiVersion":"1","port":0,"token":"t","pid":1}"#,
                "port",
            ),
            (
                r#"{"type":"ready","apiVersion":"1","port":70000,"token":"t","pid":1}"#,
                "port",
            ),
            (
                r#"{"type":"ready","apiVersion":"1","port":"80","token":"t","pid":1}"#,
                "port",
            ),
            (
                r#"{"type":"ready","apiVersion":"1","port":1,"token":"","pid":1}"#,
                "token",
            ),
            (
                r#"{"type":"ready","apiVersion":"1","port":1,"token":7,"pid":1}"#,
                "token",
            ),
            (
                r#"{"type":"ready","apiVersion":"1","port":1,"token":"t","pid":-1}"#,
                "pid",
            ),
        ];
        for (line, field) in cases {
            assert_eq!(
                parse_ready_line(line),
                Err(ReadyLineError::InvalidField(field)),
                "{line}"
            );
        }
    }

    #[test]
    fn rejects_unsupported_api_version() {
        let line =
            format!(r#"{{"type":"ready","apiVersion":"2","port":1,"token":"{TOKEN}","pid":1}}"#);
        let err = parse_ready_line(&line).unwrap_err();
        assert_eq!(err, ReadyLineError::UnsupportedApiVersion("2".into()));
        assert!(!err.to_string().contains(TOKEN));
    }

    #[test]
    fn errors_never_contain_the_token() {
        for line in [
            format!(
                r#"{{"type":"ready","apiVersion":"1","port":"{TOKEN}","token":"{TOKEN}","pid":1}}"#
            ),
            format!(
                r#"{{"type":"ready","apiVersion":"1","port":1,"token":"{TOKEN}","pid":"{TOKEN}"}}"#
            ),
            format!(r#"{{"type":"ready","apiVersion":"1","port":1,"token":"{TOKEN}"}}"#),
        ] {
            let err = parse_ready_line(&line).unwrap_err();
            assert!(!err.to_string().contains(TOKEN));
            assert!(!format!("{err:?}").contains(TOKEN));
        }
    }

    #[test]
    fn debug_output_redacts_token() {
        let ReadyLine::Ready(info) = parse_ready_line(&ready_json()).unwrap() else {
            panic!("expected ready");
        };
        let debug = format!("{info:?} {:?} {:#?}", info.token, info);
        assert!(!debug.contains(TOKEN), "{debug}");
        assert!(debug.contains(REDACTED));
    }

    #[test]
    fn serialize_exposes_token_for_the_webview_only() {
        let token = SecretToken::new(TOKEN);
        assert_eq!(
            serde_json::to_string(&token).unwrap(),
            format!("\"{TOKEN}\"")
        );
    }

    #[test]
    fn redacts_token_in_log_lines() {
        let token = SecretToken::new(TOKEN);
        let line = format!(r#"{{"msg":"dev connection","token":"{TOKEN}","again":"{TOKEN}"}}"#);
        let redacted = token.redact(&line);
        assert!(!redacted.contains(TOKEN));
        assert_eq!(redacted.matches(REDACTED).count(), 2);
        assert_eq!(SecretToken::new("").redact("abc"), "abc");
    }

    #[test]
    fn untrusted_lines_mentioning_tokens_are_withheld() {
        let withheld = sanitize_untrusted_line(&ready_json());
        assert!(!withheld.contains(TOKEN));
        assert!(withheld.starts_with("[line withheld"));
        assert!(sanitize_untrusted_line("Authorization: Bearer abc").starts_with("[line withheld"));
        assert_eq!(
            sanitize_untrusted_line("Resolved 3 packages"),
            "Resolved 3 packages"
        );
    }

    #[test]
    fn truncates_on_char_boundaries() {
        assert_eq!(truncate_chars("äöü", 2), "äö…");
        assert_eq!(truncate_chars("abc", 3), "abc");
    }
}
