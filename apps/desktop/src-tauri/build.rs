fn main() {
    // Declaring the app commands in an app manifest makes Tauri generate
    // `allow-<command>` / `deny-<command>` permissions for them and deny any
    // command that is not granted by a capability (capabilities/main.json).
    let manifest = tauri_build::AppManifest::new().commands(&[
        "sidecar_connection",
        "sidecar_restart",
        "open_privacy_settings",
    ]);
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(manifest))
        .expect("failed to run tauri-build");
}
