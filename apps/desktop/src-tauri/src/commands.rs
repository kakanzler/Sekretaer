//! The complete IPC surface exposed to the webview (spec §8: invoke is limited
//! to launch info and native features). Each command is granted explicitly in
//! capabilities/main.json via the app manifest declared in build.rs.

use tauri::State;

use crate::privacy::{self, PrivacySettingsKind};
use crate::sidecar::{SidecarHandle, SidecarStatus};

/// `{state, port, token, error}`; port/token are non-null only when ready.
#[tauri::command]
pub fn sidecar_connection(sidecar: State<'_, SidecarHandle>) -> SidecarStatus {
    sidecar.status()
}

/// Restarts the sidecar (also recovers from `failed`).
#[tauri::command]
pub fn sidecar_restart(sidecar: State<'_, SidecarHandle>) -> Result<(), String> {
    sidecar.restart()
}

/// Opens a fixed OS privacy settings page. `kind` must be `"microphone"`.
#[tauri::command]
pub fn open_privacy_settings(kind: PrivacySettingsKind) -> Result<(), String> {
    privacy::open_privacy_settings(kind)
}
