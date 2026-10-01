//! Python sidecar supervision (spec §3 process flow, §9 crash recovery, §11).
//!
//! The supervisor starts the sidecar with `tokio::process` (never a shell),
//! reads the single ready line from stdout, keeps the child's stdin open for
//! its whole lifetime (the sidecar exits on stdin EOF), forwards stderr to the
//! log with the token redacted, restarts on unexpected exit with backoff, and
//! stops the child gracefully (`POST /api/v1/shutdown`, stdin EOF, then kill)
//! on shutdown.
//!
//! It does not depend on Tauri: status changes are reported through a
//! listener callback and the run loop is a plain future, so it can be driven
//! by any tokio runtime (the app uses Tauri's, integration tests their own).

mod backoff;
mod handshake;
mod launch;

use std::future::Future;
use std::net::Ipv4Addr;
use std::process::{ExitStatus, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::Serialize;
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::TcpStream;
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::{mpsc, oneshot, watch};

pub use backoff::RestartPolicy;
pub use handshake::{
    parse_ready_line, sanitize_untrusted_line, truncate_chars, ReadyInfo, ReadyLine,
    ReadyLineError, SecretToken, API_VERSION, MAX_LOG_LINE_CHARS, REDACTED,
};
pub use launch::{
    bundled_sidecar_path, parse_command_override, repo_root_from_manifest_dir,
    resolve_base_command, with_sidecar_args, BuildProfile, LaunchContext, LaunchError, LaunchSpec,
    BUNDLED_SIDECAR_NAME, SIDECAR_CMD_ENV, WEBVIEW_ORIGINS,
};

/// Tauri event carrying [`SidecarStatus`] on every state change.
pub const STATE_EVENT: &str = "sidecar://state";

/// Log target for lines forwarded from the sidecar.
const SIDECAR_LOG_TARGET: &str = "sekretaer::sidecar";

/// Max bytes read per stdout/stderr line; longer lines are split.
const MAX_LINE_BYTES: usize = 64 * 1024;

/// Max stderr lines buffered while waiting to learn the token.
const MAX_PENDING_STDERR_LINES: usize = 256;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum SidecarState {
    Starting,
    Ready,
    Crashed,
    Restarting,
    Failed,
}

/// Connection info / state as seen by the UI. `port` and `token` are only set
/// in `ready`; `error` only in `crashed` / `failed`. All keys are always
/// serialized (absent values as `null`). `Debug` redacts the token.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct SidecarStatus {
    pub state: SidecarState,
    pub port: Option<u16>,
    pub token: Option<SecretToken>,
    pub error: Option<String>,
}

impl SidecarStatus {
    fn plain(state: SidecarState) -> Self {
        Self {
            state,
            port: None,
            token: None,
            error: None,
        }
    }

    fn with_error(state: SidecarState, error: impl Into<String>) -> Self {
        Self {
            error: Some(error.into()),
            ..Self::plain(state)
        }
    }

    fn ready(port: u16, token: SecretToken) -> Self {
        Self {
            port: Some(port),
            token: Some(token),
            ..Self::plain(SidecarState::Ready)
        }
    }
}

/// Produces the command for each launch attempt (re-evaluated on restart).
pub type Launcher = Arc<dyn Fn() -> Result<LaunchSpec, String> + Send + Sync>;

/// Called (from the supervisor task) after every status change.
pub type StatusListener = Arc<dyn Fn(&SidecarStatus) + Send + Sync>;

#[derive(Clone)]
pub struct SupervisorConfig {
    pub launcher: Launcher,
    pub policy: RestartPolicy,
    /// How long to wait for the ready line (`uv run` may sync first).
    pub handshake_timeout: Duration,
    /// Budget for the `POST /api/v1/shutdown` request.
    pub shutdown_request_timeout: Duration,
    /// How long to wait for the child to exit before killing it.
    pub exit_grace: Duration,
}

impl SupervisorConfig {
    pub fn new(launcher: Launcher) -> Self {
        Self {
            launcher,
            policy: RestartPolicy::default(),
            handshake_timeout: Duration::from_secs(60),
            shutdown_request_timeout: Duration::from_secs(1),
            exit_grace: Duration::from_secs(3),
        }
    }
}

enum Control {
    Restart,
    Shutdown(oneshot::Sender<()>),
}

struct Shared {
    status: Mutex<SidecarStatus>,
    listener: StatusListener,
}

impl Shared {
    fn status(&self) -> SidecarStatus {
        self.status
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone()
    }

    /// Only the supervisor task calls this, so listener calls are ordered.
    fn set(&self, next: SidecarStatus) {
        {
            let mut current = self
                .status
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            if *current == next {
                return;
            }
            *current = next.clone();
        }
        match &next.error {
            Some(error) => log::info!("sidecar state: {:?} ({error})", next.state),
            None => log::info!("sidecar state: {:?}", next.state),
        }
        (self.listener)(&next);
    }
}

/// Cheap, cloneable handle used by Tauri commands and the exit hook.
#[derive(Clone)]
pub struct SidecarHandle {
    shared: Arc<Shared>,
    control: mpsc::UnboundedSender<Control>,
}

impl SidecarHandle {
    /// Current state including port/token when ready.
    pub fn status(&self) -> SidecarStatus {
        self.shared.status()
    }

    /// Stops the current child (if any) and starts a fresh one, resetting the
    /// failure budget. Also leaves the `failed` state.
    pub fn restart(&self) -> Result<(), String> {
        self.control
            .send(Control::Restart)
            .map_err(|_| "sidecar supervisor is not running".to_owned())
    }

    /// Gracefully stops the child and ends the supervisor. Resolves once the
    /// child is gone (or immediately if the supervisor already ended).
    pub async fn shutdown(&self) {
        let (ack_tx, ack_rx) = oneshot::channel();
        if self.control.send(Control::Shutdown(ack_tx)).is_ok() {
            let _ = ack_rx.await;
        }
    }
}

/// Creates a supervisor. The returned future is the supervision loop; spawn
/// it on a tokio runtime. Dropping (aborting) the future kills the child
/// (`kill_on_drop`); dropping all handles makes it shut down gracefully.
pub fn supervisor(
    config: SupervisorConfig,
    listener: StatusListener,
) -> (SidecarHandle, impl Future<Output = ()> + Send + 'static) {
    let shared = Arc::new(Shared {
        status: Mutex::new(SidecarStatus::plain(SidecarState::Starting)),
        listener,
    });
    let (control_tx, control_rx) = mpsc::unbounded_channel();
    let handle = SidecarHandle {
        shared: shared.clone(),
        control: control_tx,
    };
    (handle, run(config, shared, control_rx))
}

/// What ended a wait: a control message or the channel closing.
enum Interrupt {
    Restart,
    Shutdown(Option<oneshot::Sender<()>>),
}

impl From<Option<Control>> for Interrupt {
    fn from(message: Option<Control>) -> Self {
        match message {
            Some(Control::Restart) => Self::Restart,
            Some(Control::Shutdown(ack)) => Self::Shutdown(Some(ack)),
            None => Self::Shutdown(None),
        }
    }
}

async fn run(
    config: SupervisorConfig,
    shared: Arc<Shared>,
    mut control: mpsc::UnboundedReceiver<Control>,
) {
    let policy = config.policy;
    let mut consecutive_failures: u32 = 0;
    let mut first_attempt = true;

    // The initial `starting` status is set at construction (so commands see
    // it immediately); announce it once so listeners get the full sequence.
    (shared.listener)(&shared.status());

    loop {
        shared.set(SidecarStatus::plain(if first_attempt {
            SidecarState::Starting
        } else {
            SidecarState::Restarting
        }));
        first_attempt = false;

        // Resolution errors (bad override, missing bundled binary, data dir)
        // are not transient: go straight to `failed`.
        let spec = match (config.launcher)() {
            Ok(spec) => spec,
            Err(error) => {
                log::error!("cannot launch sidecar: {error}");
                shared.set(SidecarStatus::with_error(SidecarState::Failed, error));
                match Interrupt::from(control.recv().await) {
                    Interrupt::Restart => {
                        consecutive_failures = 0;
                        continue;
                    }
                    Interrupt::Shutdown(ack) => return finish(&shared, ack),
                }
            }
        };

        let started_at = Instant::now();
        let started = tokio::select! {
            result = start_child(&spec, config.handshake_timeout) => result,
            message = control.recv() => {
                // Dropping the start future kills the half-started child.
                match Interrupt::from(message) {
                    Interrupt::Restart => {
                        consecutive_failures = 0;
                        continue;
                    }
                    Interrupt::Shutdown(ack) => return finish(&shared, ack),
                }
            }
        };

        let failure = match started {
            Err(error) => error,
            Ok(mut running) => {
                shared.set(SidecarStatus::ready(running.port, running.token.clone()));
                tokio::select! {
                    status = running.child.wait() => describe_unexpected_exit(status),
                    message = control.recv() => {
                        let interrupt = Interrupt::from(message);
                        if matches!(interrupt, Interrupt::Restart) {
                            shared.set(SidecarStatus::plain(SidecarState::Restarting));
                        }
                        stop_gracefully(running, &config).await;
                        match interrupt {
                            Interrupt::Restart => {
                                consecutive_failures = 0;
                                continue;
                            }
                            Interrupt::Shutdown(ack) => return finish(&shared, ack),
                        }
                    }
                }
            }
        };

        if policy.was_stable(started_at.elapsed()) {
            consecutive_failures = 0;
        }
        consecutive_failures += 1;
        log::warn!("sidecar failure {consecutive_failures}: {failure}");

        if policy.gives_up(consecutive_failures) {
            shared.set(SidecarStatus::with_error(
                SidecarState::Failed,
                format!(
                    "sidecar failed {consecutive_failures} times in a row; automatic restart stopped. Last error: {failure}"
                ),
            ));
            match Interrupt::from(control.recv().await) {
                Interrupt::Restart => {
                    consecutive_failures = 0;
                    continue;
                }
                Interrupt::Shutdown(ack) => return finish(&shared, ack),
            }
        }

        shared.set(SidecarStatus::with_error(SidecarState::Crashed, failure));
        let delay = policy.delay_for(consecutive_failures - 1);
        log::info!("restarting sidecar in {} ms", delay.as_millis());
        tokio::select! {
            _ = tokio::time::sleep(delay) => {}
            message = control.recv() => match Interrupt::from(message) {
                Interrupt::Restart => consecutive_failures = 0,
                Interrupt::Shutdown(ack) => return finish(&shared, ack),
            }
        }
    }
}

fn finish(shared: &Shared, ack: Option<oneshot::Sender<()>>) {
    shared.set(SidecarStatus::with_error(
        SidecarState::Failed,
        "sidecar stopped (application is shutting down)",
    ));
    if let Some(ack) = ack {
        let _ = ack.send(());
    }
}

fn describe_unexpected_exit(status: std::io::Result<ExitStatus>) -> String {
    match status {
        Ok(status) => format!("sidecar exited unexpectedly ({})", describe_status(status)),
        Err(error) => format!("lost track of the sidecar process: {error}"),
    }
}

fn describe_status(status: ExitStatus) -> String {
    match status.code() {
        Some(code) => format!("exit code {code}"),
        None => "terminated by signal".to_owned(),
    }
}

/// A child that completed the handshake.
struct RunningSidecar {
    child: Child,
    /// Held open for the child's lifetime; dropping it sends EOF.
    stdin: Option<ChildStdin>,
    port: u16,
    token: SecretToken,
}

/// Redaction knowledge shared with the stderr forwarder.
#[derive(Clone)]
enum Redaction {
    /// Handshake still running: buffer stderr lines.
    Pending,
    /// Token known: redact it.
    Token(SecretToken),
    /// Handshake failed without a token: withhold credential-looking lines.
    Unknown,
}

async fn start_child(
    spec: &LaunchSpec,
    handshake_timeout: Duration,
) -> Result<RunningSidecar, String> {
    let mut command = Command::new(&spec.program);
    command
        .args(&spec.args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    #[cfg(windows)]
    {
        // CREATE_NO_WINDOW: no console window for the child of a GUI app.
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    log::info!("starting sidecar: {}", spec.program.display());
    let mut child = command
        .spawn()
        .map_err(|e| format!("failed to start sidecar `{}`: {e}", spec.program.display()))?;

    let stdin = child.stdin.take();
    let stdout = child
        .stdout
        .take()
        .ok_or("sidecar stdout was not captured")?;
    let (redaction_tx, redaction_rx) = watch::channel(Redaction::Pending);
    if let Some(stderr) = child.stderr.take() {
        tokio::spawn(forward_stderr(stderr, redaction_rx));
    }

    let mut stdout = BufReader::new(stdout);
    let outcome = tokio::time::timeout(handshake_timeout, read_ready(&mut stdout)).await;
    let info = match outcome {
        Ok(Ok(info)) => info,
        Ok(Err(error)) => {
            let _ = redaction_tx.send(Redaction::Unknown);
            let exit = tokio::time::timeout(Duration::from_secs(2), child.wait()).await;
            return Err(match exit {
                Ok(Ok(status)) => format!("{error} ({})", describe_status(status)),
                _ => error,
            });
        }
        Err(_) => {
            let _ = redaction_tx.send(Redaction::Unknown);
            return Err(format!(
                "sidecar did not report ready within {} s",
                handshake_timeout.as_secs()
            ));
        }
    };

    let _ = redaction_tx.send(Redaction::Token(info.token.clone()));
    log::info!(
        "sidecar ready: apiVersion {}, port {}, pid {}",
        info.api_version,
        info.port,
        info.pid
    );
    tokio::spawn(drain_stdout(stdout, info.token.clone()));

    Ok(RunningSidecar {
        child,
        stdin,
        port: info.port,
        token: info.token,
    })
}

async fn read_ready<R: AsyncRead + Unpin>(stdout: &mut BufReader<R>) -> Result<ReadyInfo, String> {
    loop {
        let line = read_bounded_line(stdout)
            .await
            .map_err(|e| format!("failed to read sidecar stdout: {e}"))?
            .ok_or("sidecar exited before reporting ready")?;
        match parse_ready_line(&line) {
            Ok(ReadyLine::Ready(info)) => return Ok(info),
            Ok(ReadyLine::NotReady) => log::debug!(
                target: SIDECAR_LOG_TARGET,
                "stdout before ready: {}",
                sanitize_untrusted_line(&line)
            ),
            Err(error) => return Err(error.to_string()),
        }
    }
}

/// Reads one line of at most [`MAX_LINE_BYTES`] (longer lines are split),
/// without the line terminator. `None` on EOF.
async fn read_bounded_line<R: AsyncRead + Unpin>(
    reader: &mut BufReader<R>,
) -> std::io::Result<Option<String>> {
    let mut buffer = Vec::new();
    let read = (&mut *reader)
        .take(MAX_LINE_BYTES as u64)
        .read_until(b'\n', &mut buffer)
        .await?;
    if read == 0 {
        return Ok(None);
    }
    while matches!(buffer.last(), Some(b'\n' | b'\r')) {
        buffer.pop();
    }
    Ok(Some(String::from_utf8_lossy(&buffer).into_owned()))
}

/// Stdout is unused after the ready line; keep draining so the child never
/// blocks on a full pipe.
async fn drain_stdout<R: AsyncRead + Unpin>(mut stdout: BufReader<R>, token: SecretToken) {
    while let Ok(Some(line)) = read_bounded_line(&mut stdout).await {
        log::debug!(
            target: SIDECAR_LOG_TARGET,
            "unexpected stdout: {}",
            truncate_chars(&token.redact(&line), MAX_LOG_LINE_CHARS)
        );
    }
}

/// Forwards stderr lines to the log. Until the handshake resolves, lines are
/// buffered so a token printed before we know it (e.g. `--dev` connection
/// info) is still redacted.
async fn forward_stderr<R: AsyncRead + Unpin + Send + 'static>(
    stderr: R,
    mut redaction: watch::Receiver<Redaction>,
) {
    // Reading is not cancel-safe, so a separate task feeds a channel that can
    // be selected on together with the redaction watch.
    let (line_tx, mut line_rx) = mpsc::channel::<String>(64);
    tokio::spawn(async move {
        let mut reader = BufReader::new(stderr);
        while let Ok(Some(line)) = read_bounded_line(&mut reader).await {
            if line_tx.send(line).await.is_err() {
                break;
            }
        }
    });

    let mut pending: Vec<String> = Vec::new();
    let mut dropped: usize = 0;
    let mut resolved = false;
    loop {
        tokio::select! {
            line = line_rx.recv() => {
                let Some(line) = line else { break };
                let current = redaction.borrow().clone();
                if matches!(current, Redaction::Pending) {
                    if pending.len() < MAX_PENDING_STDERR_LINES {
                        pending.push(line);
                    } else {
                        dropped += 1;
                    }
                } else {
                    if !resolved {
                        // Keep order: earlier buffered lines go first.
                        resolved = true;
                        flush_pending(&mut pending, &mut dropped, &current);
                    }
                    log_stderr_line(&line, &current);
                }
            }
            changed = redaction.changed(), if !resolved => {
                // Err: the handshake was abandoned (start cancelled) without
                // ever learning the token.
                let current = match (changed, redaction.borrow().clone()) {
                    (Err(_), Redaction::Pending) => Redaction::Unknown,
                    (_, value) => value,
                };
                if !matches!(current, Redaction::Pending) {
                    resolved = true;
                    flush_pending(&mut pending, &mut dropped, &current);
                }
            }
        }
    }
    let current = match redaction.borrow().clone() {
        Redaction::Pending => Redaction::Unknown,
        value => value,
    };
    flush_pending(&mut pending, &mut dropped, &current);
}

fn flush_pending(pending: &mut Vec<String>, dropped: &mut usize, redaction: &Redaction) {
    for line in pending.drain(..) {
        log_stderr_line(&line, redaction);
    }
    if *dropped > 0 {
        log::warn!(target: SIDECAR_LOG_TARGET, "{dropped} early stderr lines were not logged");
        *dropped = 0;
    }
}

fn log_stderr_line(line: &str, redaction: &Redaction) {
    let safe = match redaction {
        Redaction::Token(token) => truncate_chars(&token.redact(line), MAX_LOG_LINE_CHARS),
        Redaction::Pending | Redaction::Unknown => sanitize_untrusted_line(line),
    };
    log::info!(target: SIDECAR_LOG_TARGET, "{safe}");
}

/// `POST /api/v1/shutdown`, then stdin EOF, then kill after the grace period.
async fn stop_gracefully(mut running: RunningSidecar, config: &SupervisorConfig) {
    match request_shutdown(
        running.port,
        &running.token,
        config.shutdown_request_timeout,
    )
    .await
    {
        Ok(status) => log::info!("sidecar shutdown request answered with HTTP {status}"),
        Err(error) => log::info!("sidecar shutdown request failed ({error}); closing stdin"),
    }
    drop(running.stdin.take());
    match tokio::time::timeout(config.exit_grace, running.child.wait()).await {
        Ok(Ok(status)) => log::info!("sidecar stopped ({})", describe_status(status)),
        _ => {
            log::warn!("sidecar did not exit in time; killing it");
            if let Err(error) = running.child.kill().await {
                log::warn!("failed to kill sidecar: {error}");
            }
        }
    }
}

/// Minimal HTTP/1.1 client for the single shutdown call (avoids pulling an
/// HTTP client crate into the shell). Returns the response status code.
async fn request_shutdown(port: u16, token: &SecretToken, limit: Duration) -> std::io::Result<u16> {
    let exchange = async {
        let mut stream = TcpStream::connect((Ipv4Addr::LOCALHOST, port)).await?;
        // Contains the token: never log `request`.
        let request = format!(
            "POST /api/v1/shutdown HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {}\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{{}}",
            token.expose()
        );
        stream.write_all(request.as_bytes()).await?;
        stream.flush().await?;

        let mut head = Vec::with_capacity(64);
        let mut chunk = [0u8; 64];
        while !head.windows(2).any(|w| w == b"\r\n") && head.len() < 256 {
            let read = stream.read(&mut chunk).await?;
            if read == 0 {
                break;
            }
            head.extend_from_slice(&chunk[..read]);
        }
        parse_status_code(&head).ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidData, "malformed HTTP response")
        })
    };
    tokio::time::timeout(limit, exchange)
        .await
        .map_err(|_| std::io::Error::new(std::io::ErrorKind::TimedOut, "timed out"))?
}

fn parse_status_code(head: &[u8]) -> Option<u16> {
    let text = std::str::from_utf8(head).ok()?;
    let status_line = text.lines().next()?;
    let mut parts = status_line.split_whitespace();
    if !parts.next()?.starts_with("HTTP/") {
        return None;
    }
    parts.next()?.parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_serializes_all_keys() {
        let ready = SidecarStatus::ready(4242, SecretToken::new("tok"));
        assert_eq!(
            serde_json::to_value(&ready).unwrap(),
            serde_json::json!({"state":"ready","port":4242,"token":"tok","error":null})
        );
        let failed = SidecarStatus::with_error(SidecarState::Failed, "boom");
        assert_eq!(
            serde_json::to_value(&failed).unwrap(),
            serde_json::json!({"state":"failed","port":null,"token":null,"error":"boom"})
        );
    }

    #[test]
    fn status_debug_redacts_token() {
        let ready = SidecarStatus::ready(1, SecretToken::new("super-secret-token"));
        let debug = format!("{ready:?}");
        assert!(!debug.contains("super-secret-token"));
        assert!(debug.contains(REDACTED));
    }

    #[test]
    fn state_names_match_contract() {
        let names: Vec<String> = [
            SidecarState::Starting,
            SidecarState::Ready,
            SidecarState::Crashed,
            SidecarState::Restarting,
            SidecarState::Failed,
        ]
        .iter()
        .map(|s| {
            serde_json::to_value(s)
                .unwrap()
                .as_str()
                .unwrap()
                .to_owned()
        })
        .collect();
        assert_eq!(
            names,
            ["starting", "ready", "crashed", "restarting", "failed"]
        );
    }

    #[test]
    fn parses_http_status_line() {
        assert_eq!(parse_status_code(b"HTTP/1.1 202 Accepted\r\n"), Some(202));
        assert_eq!(
            parse_status_code(b"HTTP/1.0 401 Unauthorized\r\nx"),
            Some(401)
        );
        assert_eq!(parse_status_code(b"garbage"), None);
        assert_eq!(parse_status_code(b""), None);
    }
}
