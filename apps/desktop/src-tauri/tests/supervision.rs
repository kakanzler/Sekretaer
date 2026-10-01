//! Supervision smoke tests against a fake sidecar (tests/fixtures/fake_sidecar.py).
//! The interpreter is `SEKRETAER_TEST_PYTHON`, else the first runnable of
//! `python`, `python3`, `py` (shims such as pyenv-win `.bat` files cannot be
//! spawned directly), else `uv python find`. Tests that need Python are
//! skipped with a message when none is found.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use sekretaer_desktop_lib::sidecar::{
    supervisor, with_sidecar_args, BuildProfile, LaunchSpec, RestartPolicy, SidecarHandle,
    SidecarState, SidecarStatus, StatusListener, SupervisorConfig,
};
use tokio::sync::mpsc;

const WAIT: Duration = Duration::from_secs(30);

fn runs(program: &str) -> bool {
    std::process::Command::new(program)
        .arg("--version")
        .output()
        .map(|out| out.status.success())
        .unwrap_or(false)
}

fn python() -> Option<String> {
    if let Ok(explicit) = std::env::var("SEKRETAER_TEST_PYTHON") {
        return Some(explicit);
    }
    if let Some(found) = ["python", "python3", "py"]
        .into_iter()
        .find(|candidate| runs(candidate))
    {
        return Some(found.to_owned());
    }
    let out = std::process::Command::new("uv")
        .args(["python", "find"])
        .output()
        .ok()?;
    let path = String::from_utf8(out.stdout).ok()?.trim().to_owned();
    (out.status.success() && runs(&path)).then_some(path)
}

fn fixture() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/fake_sidecar.py")
}

/// Scratch directory owned by one test; removed (file by file) at the end.
struct Scratch {
    dir: PathBuf,
}

impl Scratch {
    fn new(name: &str) -> Self {
        let dir = std::env::temp_dir().join(format!(
            "sekretaer-desktop-it-{}-{name}",
            std::process::id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        Self { dir }
    }

    fn marker(&self) -> PathBuf {
        self.dir.join("marker.txt")
    }

    fn marker_lines(&self) -> Vec<String> {
        std::fs::read_to_string(self.marker())
            .unwrap_or_default()
            .lines()
            .map(str::to_owned)
            .collect()
    }

    fn pid(&self) -> u32 {
        self.marker_lines()
            .iter()
            .find_map(|line| line.strip_prefix("pid ").map(|p| p.parse().unwrap()))
            .expect("fake sidecar wrote its pid")
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(self.marker());
        let _ = std::fs::remove_dir(&self.dir);
    }
}

struct Harness {
    handle: SidecarHandle,
    task: tokio::task::JoinHandle<()>,
    events: mpsc::UnboundedReceiver<SidecarStatus>,
}

impl Harness {
    fn start(config: SupervisorConfig) -> Self {
        let (tx, events) = mpsc::unbounded_channel();
        let listener: StatusListener = Arc::new(move |status: &SidecarStatus| {
            let _ = tx.send(status.clone());
        });
        let (handle, run) = supervisor(config, listener);
        let task = tokio::spawn(run);
        Self {
            handle,
            task,
            events,
        }
    }

    async fn next(&mut self) -> SidecarStatus {
        tokio::time::timeout(WAIT, self.events.recv())
            .await
            .expect("timed out waiting for a sidecar state")
            .expect("supervisor ended")
    }

    async fn wait_for(&mut self, state: SidecarState) -> SidecarStatus {
        loop {
            let status = self.next().await;
            if status.state == state {
                return status;
            }
        }
    }
}

fn fake_config(python: &str, mode: &str, scratch: &Scratch) -> SupervisorConfig {
    let base = LaunchSpec {
        program: PathBuf::from(python),
        args: vec![
            fixture().into_os_string(),
            mode.into(),
            scratch.marker().into_os_string(),
        ],
    };
    let spec = with_sidecar_args(base, &scratch.dir.join("data"), BuildProfile::Release);
    SupervisorConfig::new(Arc::new(move || Ok(spec.clone())))
}

fn process_alive(pid: u32) -> bool {
    if cfg!(windows) {
        let out = std::process::Command::new("tasklist")
            .args(["/FI", &format!("PID eq {pid}"), "/NH", "/FO", "CSV"])
            .output()
            .expect("tasklist");
        String::from_utf8_lossy(&out.stdout).contains(&format!("\"{pid}\""))
    } else {
        std::process::Command::new("kill")
            .args(["-0", &pid.to_string()])
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
    }
}

async fn assert_gone(pid: u32) {
    for _ in 0..50 {
        if !process_alive(pid) {
            return;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    panic!("sidecar process {pid} is still running");
}

macro_rules! require_python {
    () => {
        match python() {
            Some(python) => python,
            None => {
                eprintln!("skipping: no python interpreter on PATH");
                return;
            }
        }
    };
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn handshake_then_graceful_shutdown() {
    let python = require_python!();
    install_capture_logger();
    let scratch = Scratch::new("graceful");
    let mut harness = Harness::start(fake_config(&python, "normal", &scratch));

    assert_eq!(harness.next().await.state, SidecarState::Starting);
    let ready = harness.wait_for(SidecarState::Ready).await;
    let port = ready.port.expect("port");
    let token = ready.token.clone().expect("token");
    assert!(port > 0);
    assert!(token.expose().len() >= 32);
    assert_eq!(ready.error, None);
    assert_eq!(harness.handle.status(), ready);
    assert!(!format!("{ready:?}").contains(token.expose()));

    let args = scratch
        .marker_lines()
        .into_iter()
        .find(|l| l.starts_with("args "))
        .unwrap();
    for expected in [
        "--data-dir",
        "--allowed-origin",
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
    ] {
        assert!(args.contains(expected), "{args}");
    }
    assert!(!args.contains("--dev"));

    let pid = scratch.pid();
    tokio::time::timeout(WAIT, harness.handle.shutdown())
        .await
        .expect("shutdown finished");
    let lines = scratch.marker_lines();
    assert!(
        lines.iter().any(|l| l == "shutdown-request"),
        "authenticated POST /api/v1/shutdown not received: {lines:?}"
    );
    assert_gone(pid).await;
    assert_eq!(harness.handle.status().state, SidecarState::Failed);
    harness.task.await.unwrap();

    // The fake prints the token on stderr (like `--dev`); the forwarded log
    // line must carry it redacted, and no log line may contain it.
    let logs = wait_for_log(|line| line.contains("dev connection")).await;
    let forwarded = logs
        .iter()
        .find(|line| line.contains("dev connection"))
        .unwrap();
    assert!(forwarded.contains("[REDACTED]"), "{forwarded}");
    for line in &logs {
        assert!(
            !line.contains(token.expose()),
            "token leaked into log: {line}"
        );
    }
}

/// Global logger capturing every record of this test binary.
struct CaptureLogger;

static CAPTURED: std::sync::Mutex<Vec<String>> = std::sync::Mutex::new(Vec::new());

impl log::Log for CaptureLogger {
    fn enabled(&self, _: &log::Metadata<'_>) -> bool {
        true
    }
    fn log(&self, record: &log::Record<'_>) {
        CAPTURED
            .lock()
            .unwrap()
            .push(format!("{} {}", record.target(), record.args()));
    }
    fn flush(&self) {}
}

fn install_capture_logger() {
    static INSTALL: std::sync::Once = std::sync::Once::new();
    INSTALL.call_once(|| {
        log::set_boxed_logger(Box::new(CaptureLogger)).unwrap();
        log::set_max_level(log::LevelFilter::Trace);
    });
}

async fn wait_for_log(predicate: impl Fn(&str) -> bool) -> Vec<String> {
    for _ in 0..50 {
        let logs = CAPTURED.lock().unwrap().clone();
        if logs.iter().any(|l| predicate(l)) {
            return logs;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    panic!(
        "expected log line not found: {:?}",
        CAPTURED.lock().unwrap()
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn crash_restarts_with_backoff_then_gives_up() {
    let python = require_python!();
    let scratch = Scratch::new("crash");
    let mut config = fake_config(&python, "crash", &scratch);
    config.policy = RestartPolicy {
        initial_delay: Duration::from_millis(20),
        max_delay: Duration::from_millis(50),
        max_quick_failures: 3,
        stable_after: Duration::from_secs(60),
    };
    let mut harness = Harness::start(config);

    let mut states = Vec::new();
    let failed = loop {
        let status = harness.next().await;
        states.push(status.state);
        if status.state == SidecarState::Crashed {
            let error = status.error.clone().unwrap();
            assert!(error.contains("exit code 3"), "{error}");
            assert_eq!(status.port, None);
            assert_eq!(status.token, None);
        }
        if status.state == SidecarState::Failed {
            break status;
        }
    };
    use SidecarState::*;
    assert_eq!(
        states,
        [Starting, Ready, Crashed, Restarting, Ready, Crashed, Restarting, Ready, Failed]
    );
    assert!(failed.error.unwrap().contains("3 times"));

    // A manual restart leaves `failed` and resets the budget.
    harness.handle.restart().unwrap();
    assert_eq!(harness.next().await.state, Restarting);
    harness.wait_for(Ready).await;
    tokio::time::timeout(WAIT, harness.handle.shutdown())
        .await
        .expect("shutdown finished");
    harness.task.await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn handshake_timeout_kills_child_and_fails() {
    let python = require_python!();
    let scratch = Scratch::new("timeout");
    let mut config = fake_config(&python, "silent", &scratch);
    config.handshake_timeout = Duration::from_secs(2);
    config.policy.max_quick_failures = 1;
    let mut harness = Harness::start(config);

    let failed = harness.wait_for(SidecarState::Failed).await;
    assert!(
        failed
            .error
            .as_deref()
            .unwrap()
            .contains("did not report ready"),
        "{failed:?}"
    );
    assert_gone(scratch.pid()).await;
    harness.handle.shutdown().await;
    harness.task.await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn ready_line_without_token_is_rejected() {
    let python = require_python!();
    let scratch = Scratch::new("badready");
    let mut config = fake_config(&python, "badready", &scratch);
    config.policy.max_quick_failures = 1;
    let mut harness = Harness::start(config);

    let failed = harness.wait_for(SidecarState::Failed).await;
    assert!(
        failed
            .error
            .as_deref()
            .unwrap()
            .contains("missing field `token`"),
        "{failed:?}"
    );
    assert_gone(scratch.pid()).await;
    harness.handle.shutdown().await;
    harness.task.await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn aborting_the_supervisor_kills_the_child() {
    let python = require_python!();
    let scratch = Scratch::new("abort");
    let mut harness = Harness::start(fake_config(&python, "normal", &scratch));
    harness.wait_for(SidecarState::Ready).await;
    let pid = scratch.pid();
    assert!(process_alive(pid));

    harness.task.abort();
    assert!(harness.task.await.unwrap_err().is_cancelled());
    assert_gone(pid).await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn launch_errors_fail_without_retry_and_restart_re_resolves() {
    let calls = Arc::new(AtomicUsize::new(0));
    let counter = calls.clone();
    let config = SupervisorConfig::new(Arc::new(move || {
        counter.fetch_add(1, Ordering::SeqCst);
        Err("bundled sidecar executable not found".to_owned())
    }));
    let mut harness = Harness::start(config);

    let failed = harness.wait_for(SidecarState::Failed).await;
    assert_eq!(
        failed.error.as_deref(),
        Some("bundled sidecar executable not found")
    );
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(calls.load(Ordering::SeqCst), 1, "no automatic retry");

    harness.handle.restart().unwrap();
    assert_eq!(harness.next().await.state, SidecarState::Restarting);
    assert_eq!(harness.next().await.state, SidecarState::Failed);
    assert_eq!(calls.load(Ordering::SeqCst), 2);

    harness.handle.shutdown().await;
    harness.task.await.unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn spawn_failure_is_reported() {
    let config = SupervisorConfig {
        policy: RestartPolicy {
            max_quick_failures: 1,
            ..RestartPolicy::default()
        },
        ..SupervisorConfig::new(Arc::new(|| {
            Ok(LaunchSpec {
                program: PathBuf::from("sekretaer-definitely-missing-binary"),
                args: Vec::new(),
            })
        }))
    };
    let mut harness = Harness::start(config);
    let failed = harness.wait_for(SidecarState::Failed).await;
    assert!(
        failed
            .error
            .as_deref()
            .unwrap()
            .contains("failed to start sidecar"),
        "{failed:?}"
    );
    harness.handle.shutdown().await;
    harness.task.await.unwrap();
}
