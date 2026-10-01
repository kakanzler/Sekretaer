//! Opens fixed OS privacy settings pages (spec §9 "マイク権限拒否": guide the
//! user to the settings). Only compile-time constant URIs are ever opened; the
//! caller selects a page by enum, never by URL or path.

use std::process::Command;

use serde::Deserialize;

/// Settings page selectable from the UI. Unknown values fail deserialization
/// before reaching this module.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PrivacySettingsKind {
    Microphone,
}

/// The fixed settings URI for `kind` on this platform, if supported.
pub fn settings_uri(kind: PrivacySettingsKind) -> Option<&'static str> {
    match kind {
        PrivacySettingsKind::Microphone => {
            if cfg!(target_os = "windows") {
                Some("ms-settings:privacy-microphone")
            } else if cfg!(target_os = "macos") {
                Some("x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")
            } else {
                None
            }
        }
    }
}

/// Opens the settings page with the OS opener (fixed executable, fixed
/// argument, no shell).
pub fn open_privacy_settings(kind: PrivacySettingsKind) -> Result<(), String> {
    let uri = settings_uri(kind)
        .ok_or_else(|| "opening privacy settings is not supported on this platform".to_owned())?;
    let mut command = opener_command(uri)?;
    let mut child = command
        .spawn()
        .map_err(|e| format!("failed to open privacy settings: {e}"))?;
    // Reap the opener in the background (explorer.exe returns non-zero even
    // on success, so its status is not checked).
    std::thread::spawn(move || {
        let _ = child.wait();
    });
    Ok(())
}

#[cfg(target_os = "windows")]
fn opener_command(uri: &'static str) -> Result<Command, String> {
    let system_root = std::env::var_os("SystemRoot").unwrap_or_else(|| "C:\\Windows".into());
    let mut command = Command::new(std::path::PathBuf::from(system_root).join("explorer.exe"));
    command.arg(uri);
    Ok(command)
}

#[cfg(target_os = "macos")]
fn opener_command(uri: &'static str) -> Result<Command, String> {
    let mut command = Command::new("/usr/bin/open");
    command.arg(uri);
    Ok(command)
}

#[cfg(not(any(target_os = "windows", target_os = "macos")))]
fn opener_command(_uri: &'static str) -> Result<Command, String> {
    Err("opening privacy settings is not supported on this platform".to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_known_kinds_deserialize() {
        let kind: PrivacySettingsKind = serde_json::from_str("\"microphone\"").unwrap();
        assert_eq!(kind, PrivacySettingsKind::Microphone);
        for bad in [
            "\"camera\"",
            "\"https://example.com\"",
            "\"Microphone\"",
            "1",
            "null",
        ] {
            assert!(
                serde_json::from_str::<PrivacySettingsKind>(bad).is_err(),
                "{bad}"
            );
        }
    }

    #[test]
    fn microphone_uri_is_fixed() {
        let uri = settings_uri(PrivacySettingsKind::Microphone);
        if cfg!(target_os = "windows") {
            assert_eq!(uri, Some("ms-settings:privacy-microphone"));
        } else if cfg!(target_os = "macos") {
            assert_eq!(
                uri,
                Some("x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")
            );
        } else {
            assert_eq!(uri, None);
        }
    }
}
