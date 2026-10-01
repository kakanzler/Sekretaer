//! Resolution of the sidecar command line.
//!
//! Order:
//! 1. `SEKRETAER_SIDECAR_CMD` — a JSON array of argv strings (no shell
//!    parsing, ever).
//! 2. Debug builds: `uv run --project <repo>/sidecar --extra audio --extra stt --extra vad --extra crypto python -m sekretaer`.
//! 3. Release builds: the bundled `sekretaer-sidecar` executable next to the
//!    app executable (Tauri `externalBin` naming; produced by the packaging
//!    step, see packaging/README.md). Missing binary is a clear error, not a
//!    build failure.
//!
//! The shell always appends `--data-dir`, one `--allowed-origin` per webview
//! origin and, in debug builds, `--dev`.

use std::ffi::OsString;
use std::fmt;
use std::path::{Path, PathBuf};

/// Environment variable holding a JSON argv array that overrides the command.
pub const SIDECAR_CMD_ENV: &str = "SEKRETAER_SIDECAR_CMD";

/// Base name of the bundled sidecar executable (Tauri `externalBin`).
pub const BUNDLED_SIDECAR_NAME: &str = "sekretaer-sidecar";

/// Origins the Tauri webview can have (macOS/Linux custom protocol, Windows
/// http and https variants). Passed to the sidecar's Origin allow-list.
pub const WEBVIEW_ORIGINS: [&str; 3] = [
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BuildProfile {
    Debug,
    Release,
}

impl BuildProfile {
    pub fn current() -> Self {
        if cfg!(debug_assertions) {
            Self::Debug
        } else {
            Self::Release
        }
    }
}

/// A fully resolved program + argv. Never run through a shell.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LaunchSpec {
    pub program: PathBuf,
    pub args: Vec<OsString>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LaunchError {
    InvalidOverride(String),
    BundledSidecarMissing(PathBuf),
    NoExecutableDir,
    DataDir(PathBuf, String),
}

impl fmt::Display for LaunchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidOverride(reason) => write!(
                f,
                "{SIDECAR_CMD_ENV} must be a non-empty JSON array of strings: {reason}"
            ),
            Self::BundledSidecarMissing(path) => write!(
                f,
                "bundled sidecar executable not found at {} (the packaged build must include `{BUNDLED_SIDECAR_NAME}`; set {SIDECAR_CMD_ENV} to override)",
                path.display()
            ),
            Self::NoExecutableDir => {
                write!(f, "cannot determine the application executable directory")
            }
            Self::DataDir(path, reason) => write!(
                f,
                "cannot create sidecar data directory {}: {reason}",
                path.display()
            ),
        }
    }
}

impl std::error::Error for LaunchError {}

/// Parses the `SEKRETAER_SIDECAR_CMD` JSON argv array.
pub fn parse_command_override(json: &str) -> Result<LaunchSpec, LaunchError> {
    let argv: Vec<String> = serde_json::from_str(json)
        .map_err(|_| LaunchError::InvalidOverride("not a JSON array of strings".into()))?;
    let mut argv = argv.into_iter();
    let program = argv
        .next()
        .ok_or_else(|| LaunchError::InvalidOverride("array is empty".into()))?;
    if program.trim().is_empty() {
        return Err(LaunchError::InvalidOverride("program is empty".into()));
    }
    Ok(LaunchSpec {
        program: PathBuf::from(program),
        args: argv.map(OsString::from).collect(),
    })
}

/// Repository root derived from `CARGO_MANIFEST_DIR`
/// (`<repo>/apps/desktop/src-tauri`).
pub fn repo_root_from_manifest_dir(manifest_dir: &Path) -> PathBuf {
    manifest_dir
        .ancestors()
        .nth(3)
        .unwrap_or(manifest_dir)
        .to_path_buf()
}

/// Path of the bundled sidecar inside `exe_dir`.
pub fn bundled_sidecar_path(exe_dir: &Path) -> PathBuf {
    exe_dir.join(format!(
        "{BUNDLED_SIDECAR_NAME}{}",
        std::env::consts::EXE_SUFFIX
    ))
}

/// Resolves the base command (before the shell-owned arguments are added).
pub fn resolve_base_command(
    env_override: Option<&str>,
    profile: BuildProfile,
    repo_root: &Path,
    exe_dir: Option<&Path>,
) -> Result<LaunchSpec, LaunchError> {
    if let Some(json) = env_override.filter(|v| !v.trim().is_empty()) {
        return parse_command_override(json);
    }
    match profile {
        BuildProfile::Debug => Ok(LaunchSpec {
            program: PathBuf::from("uv"),
            args: vec![
                "run".into(),
                "--project".into(),
                repo_root.join("sidecar").into_os_string(),
                // Runtime extras: live capture, local STT, Silero VAD, encrypted archive.
                // Without them the sidecar only degrades (ADR 0003), so the real app needs them.
                "--extra".into(),
                "audio".into(),
                "--extra".into(),
                "stt".into(),
                "--extra".into(),
                "vad".into(),
                "--extra".into(),
                "crypto".into(),
                "python".into(),
                "-m".into(),
                "sekretaer".into(),
            ],
        }),
        BuildProfile::Release => {
            let exe_dir = exe_dir.ok_or(LaunchError::NoExecutableDir)?;
            let path = bundled_sidecar_path(exe_dir);
            if path.is_file() {
                Ok(LaunchSpec {
                    program: path,
                    args: Vec::new(),
                })
            } else {
                Err(LaunchError::BundledSidecarMissing(path))
            }
        }
    }
}

/// Appends the arguments the shell always controls.
pub fn with_sidecar_args(
    mut spec: LaunchSpec,
    data_dir: &Path,
    profile: BuildProfile,
) -> LaunchSpec {
    spec.args.push("--data-dir".into());
    spec.args.push(data_dir.as_os_str().to_owned());
    for origin in WEBVIEW_ORIGINS {
        spec.args.push("--allowed-origin".into());
        spec.args.push(origin.into());
    }
    if profile == BuildProfile::Debug {
        spec.args.push("--dev".into());
    }
    spec
}

/// Everything needed to (re-)resolve the command at each launch attempt.
#[derive(Debug, Clone)]
pub struct LaunchContext {
    pub data_dir: PathBuf,
    pub profile: BuildProfile,
    pub repo_root: PathBuf,
    pub exe_dir: Option<PathBuf>,
}

impl LaunchContext {
    /// Context for the running application; `data_dir` is
    /// `<app_data_dir>/data`.
    pub fn for_current_process(data_dir: PathBuf) -> Self {
        Self {
            data_dir,
            profile: BuildProfile::current(),
            repo_root: repo_root_from_manifest_dir(Path::new(env!("CARGO_MANIFEST_DIR"))),
            exe_dir: std::env::current_exe()
                .ok()
                .and_then(|exe| exe.parent().map(Path::to_path_buf)),
        }
    }

    /// Resolves the full command, reading the override env var now so a
    /// manual restart picks up changes. Creates the data directory.
    pub fn resolve(&self) -> Result<LaunchSpec, LaunchError> {
        let env_override = std::env::var(SIDECAR_CMD_ENV).ok();
        if env_override
            .as_deref()
            .is_some_and(|v| !v.trim().is_empty())
        {
            log::warn!("using sidecar command override from {SIDECAR_CMD_ENV}");
        }
        let base = resolve_base_command(
            env_override.as_deref(),
            self.profile,
            &self.repo_root,
            self.exe_dir.as_deref(),
        )?;
        std::fs::create_dir_all(&self.data_dir)
            .map_err(|e| LaunchError::DataDir(self.data_dir.clone(), e.to_string()))?;
        Ok(with_sidecar_args(base, &self.data_dir, self.profile))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn os(args: &[&str]) -> Vec<OsString> {
        args.iter().map(OsString::from).collect()
    }

    #[test]
    fn env_override_json_array() {
        let spec =
            parse_command_override(r#"["python", "C:\\dev\\fake sidecar.py", "--x"]"#).unwrap();
        assert_eq!(spec.program, PathBuf::from("python"));
        assert_eq!(spec.args, os(&["C:\\dev\\fake sidecar.py", "--x"]));
    }

    #[test]
    fn env_override_is_not_shell_parsed() {
        let spec = parse_command_override(r#"["uv", "run; rm -rf / && echo $HOME"]"#).unwrap();
        assert_eq!(spec.args, os(&["run; rm -rf / && echo $HOME"]));
    }

    #[test]
    fn env_override_rejects_invalid_values() {
        for bad in [
            "python -m sekretaer",
            "[]",
            r#"[""]"#,
            r#"["  "]"#,
            r#"["python", 1]"#,
            r#"{"cmd":"python"}"#,
            r#""python""#,
        ] {
            assert!(
                matches!(
                    parse_command_override(bad),
                    Err(LaunchError::InvalidOverride(_))
                ),
                "{bad}"
            );
        }
    }

    #[test]
    fn env_override_wins_over_profile_defaults() {
        let spec = resolve_base_command(
            Some(r#"["fake"]"#),
            BuildProfile::Release,
            Path::new("/repo"),
            None,
        )
        .unwrap();
        assert_eq!(spec.program, PathBuf::from("fake"));
    }

    #[test]
    fn blank_env_override_is_ignored() {
        let spec = resolve_base_command(Some("  "), BuildProfile::Debug, Path::new("/repo"), None)
            .unwrap();
        assert_eq!(spec.program, PathBuf::from("uv"));
    }

    #[test]
    fn debug_default_uses_uv_with_repo_sidecar_project() {
        let root = Path::new("/repo");
        let spec = resolve_base_command(None, BuildProfile::Debug, root, None).unwrap();
        assert_eq!(spec.program, PathBuf::from("uv"));
        let mut expected = os(&["run", "--project"]);
        expected.push(root.join("sidecar").into_os_string());
        expected.extend(os(&[
            "--extra",
            "audio",
            "--extra",
            "stt",
            "--extra",
            "vad",
            "--extra",
            "crypto",
            "python",
            "-m",
            "sekretaer",
        ]));
        assert_eq!(spec.args, expected);
    }

    #[test]
    fn release_requires_bundled_binary() {
        let dir =
            std::env::temp_dir().join(format!("sekretaer-launch-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let expected = bundled_sidecar_path(&dir);
        let missing =
            resolve_base_command(None, BuildProfile::Release, Path::new("/repo"), Some(&dir));
        assert_eq!(
            missing,
            Err(LaunchError::BundledSidecarMissing(expected.clone()))
        );
        assert!(missing
            .unwrap_err()
            .to_string()
            .contains(BUNDLED_SIDECAR_NAME));

        std::fs::write(&expected, b"").unwrap();
        let found =
            resolve_base_command(None, BuildProfile::Release, Path::new("/repo"), Some(&dir))
                .unwrap();
        assert_eq!(found.program, expected);
        assert!(found.args.is_empty());
        std::fs::remove_file(&expected).unwrap();
        std::fs::remove_dir(&dir).unwrap();

        assert_eq!(
            resolve_base_command(None, BuildProfile::Release, Path::new("/repo"), None),
            Err(LaunchError::NoExecutableDir)
        );
    }

    #[test]
    fn appends_shell_owned_arguments() {
        let base = LaunchSpec {
            program: "p".into(),
            args: os(&["a"]),
        };
        let data = Path::new("/data");
        let release = with_sidecar_args(base.clone(), data, BuildProfile::Release);
        assert_eq!(
            release.args,
            os(&[
                "a",
                "--data-dir",
                "/data",
                "--allowed-origin",
                "tauri://localhost",
                "--allowed-origin",
                "http://tauri.localhost",
                "--allowed-origin",
                "https://tauri.localhost",
            ])
        );
        let debug = with_sidecar_args(base, data, BuildProfile::Debug);
        assert_eq!(debug.args.last(), Some(&OsString::from("--dev")));
    }

    #[test]
    fn repo_root_is_three_levels_above_manifest() {
        let manifest = Path::new("/repo/apps/desktop/src-tauri");
        assert_eq!(
            repo_root_from_manifest_dir(manifest),
            PathBuf::from("/repo")
        );
        let actual = repo_root_from_manifest_dir(Path::new(env!("CARGO_MANIFEST_DIR")));
        assert!(actual
            .join("apps")
            .join("desktop")
            .join("src-tauri")
            .is_dir());
    }
}
