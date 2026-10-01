//! Restart policy for the sidecar (spec §9 "アプリ異常終了", shell side).

use std::time::Duration;

/// Exponential backoff with a give-up threshold for quick consecutive
/// failures.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RestartPolicy {
    /// Delay before the first restart attempt.
    pub initial_delay: Duration,
    /// Upper bound for any single delay.
    pub max_delay: Duration,
    /// After this many consecutive quick failures the supervisor stops
    /// retrying and reports `failed` until a manual restart.
    pub max_quick_failures: u32,
    /// A run that lasted at least this long is considered healthy: the
    /// failure counter is reset when it ends.
    pub stable_after: Duration,
}

impl Default for RestartPolicy {
    fn default() -> Self {
        Self {
            initial_delay: Duration::from_secs(1),
            max_delay: Duration::from_secs(30),
            max_quick_failures: 5,
            stable_after: Duration::from_secs(60),
        }
    }
}

impl RestartPolicy {
    /// Delay before restart attempt `attempt` (0-based):
    /// `initial * 2^attempt`, capped at `max_delay`.
    pub fn delay_for(&self, attempt: u32) -> Duration {
        let factor = 2u32.saturating_pow(attempt);
        self.initial_delay
            .checked_mul(factor)
            .unwrap_or(self.max_delay)
            .min(self.max_delay)
    }

    /// Whether `consecutive_failures` quick failures exhaust the budget.
    pub fn gives_up(&self, consecutive_failures: u32) -> bool {
        consecutive_failures >= self.max_quick_failures
    }

    /// Whether a run of `ran_for` counts as healthy (resets the counter).
    pub fn was_stable(&self, ran_for: Duration) -> bool {
        ran_for >= self.stable_after
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_schedule_doubles_and_caps_at_30s() {
        let policy = RestartPolicy::default();
        let secs: Vec<u64> = (0..8).map(|a| policy.delay_for(a).as_secs()).collect();
        assert_eq!(secs, vec![1, 2, 4, 8, 16, 30, 30, 30]);
    }

    #[test]
    fn huge_attempt_numbers_do_not_overflow() {
        let policy = RestartPolicy::default();
        assert_eq!(policy.delay_for(40), Duration::from_secs(30));
        assert_eq!(policy.delay_for(u32::MAX), Duration::from_secs(30));
    }

    #[test]
    fn gives_up_after_five_quick_failures() {
        let policy = RestartPolicy::default();
        assert!(!policy.gives_up(4));
        assert!(policy.gives_up(5));
        assert!(policy.gives_up(6));
    }

    #[test]
    fn long_runs_count_as_stable() {
        let policy = RestartPolicy::default();
        assert!(!policy.was_stable(Duration::from_secs(59)));
        assert!(policy.was_stable(Duration::from_secs(60)));
    }

    #[test]
    fn custom_schedule() {
        let policy = RestartPolicy {
            initial_delay: Duration::from_millis(10),
            max_delay: Duration::from_millis(25),
            ..RestartPolicy::default()
        };
        assert_eq!(policy.delay_for(0), Duration::from_millis(10));
        assert_eq!(policy.delay_for(1), Duration::from_millis(20));
        assert_eq!(policy.delay_for(2), Duration::from_millis(25));
    }
}
