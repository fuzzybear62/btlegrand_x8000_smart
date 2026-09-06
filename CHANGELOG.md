# Changelog

All notable changes to this project are documented here. This project adheres
to [Semantic Versioning](https://semver.org/).

## [1.5.9] - 2026-09-06

First tagged release (HACS-installable).

### Fixed
- **C2C subscription now auto-recovers from transient cloud failures.** When the
  Legrand/Bticino cloud returns a persistent error (e.g. HTTP 500) while
  registering the C2C webhook subscription at setup, the integration no longer
  stays unsubscribed until the next manual reload. It retries in the background
  with backoff (60s → 120s → 300s → 600s×3), re-attempting only the plants that
  failed, and defers (without consuming an attempt) while a rate-limit Cool Down
  is active. Polling remains active throughout; the pending retry timer is
  cancelled on unload.

[1.5.9]: https://github.com/fuzzybear62/btlegrand_x8000_smart/releases/tag/v1.5.9
