//! Sekretär desktop shell (spec §3): window, OS permissions, sidecar
//! supervision and the minimal IPC bridge.

mod commands;
pub mod privacy;
pub mod sidecar;

use std::sync::Arc;
use std::time::Duration;

use tauri::{Emitter, Manager, RunEvent};

use crate::sidecar::{
    LaunchContext, Launcher, SidecarHandle, StatusListener, SupervisorConfig, STATE_EVENT,
};

/// Label of the single application window (matches tauri.conf.json and
/// capabilities/main.json).
pub const MAIN_WINDOW: &str = "main";

/// Upper bound for stopping the sidecar when the app exits.
const EXIT_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(8);

pub fn run() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();

    let app = tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            commands::sidecar_connection,
            commands::sidecar_restart,
            commands::open_privacy_settings,
        ])
        .setup(|app| {
            let data_dir = app.path().app_data_dir()?.join("data");
            let context = LaunchContext::for_current_process(data_dir);
            let launcher: Launcher = Arc::new(move || context.resolve().map_err(|e| e.to_string()));

            let emitter = app.handle().clone();
            let listener: StatusListener = Arc::new(move |status| {
                if let Err(error) = emitter.emit_to(MAIN_WINDOW, STATE_EVENT, status) {
                    log::warn!("failed to emit {STATE_EVENT}: {error}");
                }
            });

            let (handle, supervision) =
                sidecar::supervisor(SupervisorConfig::new(launcher), listener);
            tauri::async_runtime::spawn(supervision);
            app.manage(handle);
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build the Sekretär application");

    app.run(|app_handle, event| {
        if let RunEvent::Exit = event {
            if let Some(handle) = app_handle.try_state::<SidecarHandle>() {
                let handle = handle.inner().clone();
                tauri::async_runtime::block_on(async move {
                    if tokio::time::timeout(EXIT_SHUTDOWN_TIMEOUT, handle.shutdown())
                        .await
                        .is_err()
                    {
                        log::warn!("sidecar shutdown timed out during exit");
                    }
                });
            }
        }
    });
}
