# CloudDownloader UI and Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the approved responsive UI, startup animation, persistent settings, secure credentials, download policies, logs, diagnostics, and an installable fnOS x86 package.

**Architecture:** Keep the FastAPI application and dependency-free browser client. Split configuration, downloading, and diagnostics into focused Python modules; split the static client into HTML, CSS, API, and interaction modules. Persist `/config/settings.json`, `/config/credentials.json`, and rotated logs on a dedicated fnOS-mounted directory.

**Tech Stack:** Python 3.11, FastAPI, httpx, asyncio, HTML5, CSS custom properties, ES modules, SVG, Python unittest.

## Global Constraints

- Target platform is fnOS 1.1.3107 x86.
- No frontend framework, external CDN, online font, or icon library.
- Default reserve space is exactly 50GB.
- Default concurrency and per-file connections are exactly 3.
- Segment size is exactly 5MB and the global segment-request cap is exactly 30.
- Startup animation is about 3 seconds and plays once per browser session.
- The current disclaimer must be explicitly accepted before business features can be used.
- Credentials never appear in settings responses, logs, or diagnostic exports.
- The 1600×1000 light desktop layout must match the approved mockup structure and visual tokens.

---

### Task 1: Persistent settings and credentials

**Files:**
- Create: `app/docker/src/config_store.py`
- Create: `tests/test_config_store.py`

**Interfaces:**
- Produces: `AppSettings`, `SettingsStore.load()`, `SettingsStore.update(data)`, `CredentialStore.status()`, `CredentialStore.update(provider, data)`, `CredentialStore.clear(provider)`.

- [ ] Write failing unit tests for defaults, validation, atomic persistence, and credential redaction.
- [ ] Run `python -m unittest -v tests.test_config_store` and confirm missing-module failure.
- [ ] Implement dataclass-backed validation for numeric ranges, `HH:MM` values, duplicate policies, and logging values.
- [ ] Store JSON through a same-directory temporary file followed by `os.replace`.
- [ ] Restrict credential files to application-user read/write where the platform supports `chmod`.
- [ ] Run the tests and require all cases to pass.

### Task 2: Download policy helpers and scheduler

**Files:**
- Rewrite: `app/docker/src/downloader.py`
- Create: `tests/test_downloader_policy.py`

**Interfaces:**
- Consumes: `SettingsStore.load()`.
- Produces: `DownloadManager.start_download(url, filename, headers, expected_size, remote_hash)`, `is_schedule_allowed(settings, now)`, `resolve_destination(...)`.

- [ ] Write failing tests for all-day and cross-midnight schedules, four duplicate policies, rename numbering from left to right, and the 50GB disk rule.
- [ ] Run tests and confirm failures against the current compressed downloader.
- [ ] Implement explicit task states and safe destination resolution.
- [ ] Implement a dynamically sized task gate and a shared 30-request segment semaphore.
- [ ] Implement 5MB Range segments, per-file connection count, shared byte-rate limiter, cancellation, and atomic finalization.
- [ ] Pause while outside the schedule or below the reserve threshold and resume when conditions recover.
- [ ] Run policy and manager tests.

### Task 3: Logging and diagnostics

**Files:**
- Create: `app/docker/src/diagnostics.py`
- Create: `tests/test_diagnostics.py`

**Interfaces:**
- Produces: `configure_logging(settings, config_dir)`, `tail_log(lines)`, `clear_log()`, `run_diagnostics()`, `build_diagnostic_zip()`.

- [ ] Write failing tests proving secrets and download URLs are absent from logs and ZIP exports.
- [ ] Implement rotating logs using the standard library.
- [ ] Implement writable-directory, free-space, aria2 availability, and network diagnostics with bounded timeouts.
- [ ] Build diagnostic ZIP bytes in memory with only redacted settings, system data, results, and logs.
- [ ] Run diagnostics tests.

### Task 4: Settings, credentials, logs, and diagnostics API

**Files:**
- Rewrite: `app/docker/src/app.py`
- Create: `tests/test_api.py`

**Interfaces:**
- Produces: `/api/settings`, `/api/credentials/{provider}`, `/api/logs`, `/api/logs/download`, `/api/logs/clear`, `/api/diagnostics`, `/api/diagnostics/export`.

- [ ] Write failing FastAPI tests for settings validation, credential redaction, clearing, and diagnostic responses.
- [ ] Initialize stores and `DownloadManager` during lifespan with `/config` and `/downloads`.
- [ ] Restore configured credentials on startup without logging secret values.
- [ ] Update clients immediately after valid credential changes.
- [ ] Update Baidu and Quark download endpoints to pass `filename`, size, headers, and optional hash through the stable manager interface.
- [ ] Run all API tests.

### Task 5: Onboarding and mandatory disclaimer

**Files:**
- Create: `app/docker/src/onboarding.py`
- Modify: `app/docker/src/app.py`
- Create: `tests/test_onboarding.py`

**Interfaces:**
- Produces: `OnboardingStore.status()`, `OnboardingStore.accept(version)`, `/api/onboarding/status`, `/api/onboarding/accept`.

- [ ] Write failing tests for new-install status, explicit acceptance, persisted version/time, stale-version re-prompt, and `403` business API gating.
- [ ] Persist acceptance in `/config/onboarding.json` through atomic replacement.
- [ ] Add middleware that permits static files and onboarding endpoints but blocks `/api/*` business endpoints until the current disclaimer version is accepted.
- [ ] Return the exact current disclaimer version and required notice content to the browser.
- [ ] Run onboarding and API tests.

### Task 6: Persistent fnOS configuration mount

**Files:**
- Modify: `app/docker/docker-compose.yaml`
- Modify: `cmd/install_callback`
- Modify: `app/docker/src/start.sh`
- Test: `tests/test_fpk_package.py`

**Interfaces:**
- Produces: writable `/config` mount and `CONFIG_DIR=/config`.

- [ ] Add a package-owned persistent config directory to Compose and ensure the install callback creates it.
- [ ] Set restrictive permissions without changing the user-selected download directory.
- [ ] Update package tests to require the mount and environment value.
- [ ] Rebuild and run FPK tests.

### Task 7: Approved Web UI shell and responsive layout

**Files:**
- Rewrite: `app/docker/src/static/index.html`
- Create: `app/docker/src/static/styles.css`
- Create: `app/docker/src/static/api.js`
- Create: `app/docker/src/static/app.js`
- Create: `app/docker/src/static/assets/logo.svg`
- Create: `tests/test_static_ui.py`

**Interfaces:**
- Consumes: existing file/download APIs and Task 4 APIs.
- Produces: semantic navigation targets `baidu`, `quark`, `downloads`, `settings`.

- [ ] Write static-contract tests for local assets, navigation labels, settings fields, no inline event handlers, and no external resources.
- [ ] Create the flat SVG cloud-download logo from the approved silhouette.
- [ ] Implement the 220px sidebar, 80px compact rail, mobile bottom navigation, main header, account chip, file table/cards, task list, and settings sections.
- [ ] Implement `prefers-color-scheme` auto theme and optional local override.
- [ ] Ensure all controls have labels, focus styles, keyboard support, and touch targets.
- [ ] Run static UI tests.

### Task 8: Startup animation and browser behavior

**Files:**
- Modify: `app/docker/src/static/styles.css`
- Modify: `app/docker/src/static/app.js`
- Test: `tests/test_static_ui.py`

**Interfaces:**
- Produces: `StartupController` behavior backed by `sessionStorage["clouddl:intro-seen"]`.

- [ ] Add failing tests for the intro container, skip control, session key, and reduced-motion branch.
- [ ] Implement the 3-second cloud draw, arrow drop, brand reveal, logo docking, and shell reveal.
- [ ] Start API preloading before the animation completes.
- [ ] Skip on later loads in the same session and reduce to a short fade under reduced motion.
- [ ] Run static UI tests.

### Task 9: First-use guide and disclaimer UI

**Files:**
- Modify: `app/docker/src/static/index.html`
- Modify: `app/docker/src/static/styles.css`
- Modify: `app/docker/src/static/app.js`
- Test: `tests/test_static_ui.py`

**Interfaces:**
- Consumes: `/api/onboarding/status`, `/api/onboarding/accept`.

- [ ] Add failing static tests for the five guide steps, acceptance checkbox, disabled continue button, disclaimer version, and keyboard-accessible dialog.
- [ ] Show onboarding after the startup animation only when the server reports acceptance is required.
- [ ] Implement steps for welcome, credentials, download protection, risk notice, and full disclaimer.
- [ ] Require an unchecked-by-default acceptance checkbox before enabling the final confirmation.
- [ ] Persist through the server API and reveal the application only after a successful response.
- [ ] Run onboarding and static UI tests.

### Task 10: Settings UI, logs, and diagnostics interactions

**Files:**
- Modify: `app/docker/src/static/index.html`
- Modify: `app/docker/src/static/styles.css`
- Modify: `app/docker/src/static/app.js`
- Test: `tests/test_static_ui.py`

**Interfaces:**
- Consumes: Task 4 APIs.

- [ ] Render account, download, schedule, logging, and diagnostics sections using the approved visual system.
- [ ] Implement front-end validation for ranges, times, and required credential fields.
- [ ] Implement save, cancel/reset, credential overwrite/clear, live log refresh, clear, download, diagnostics, and export.
- [ ] Mask account status and never place stored credential text back into form values.
- [ ] Run static UI and Python unit tests.

### Task 11: Final package verification

**Files:**
- Modify: `README.md`
- Regenerate: `clouddl_x86.fpk`

**Interfaces:**
- Produces: final installable artifact and SHA-256.

- [ ] Update installation, config persistence, settings, and diagnostic documentation.
- [ ] Run `python -m unittest discover -v`.
- [ ] Run AST parsing over every Python source file.
- [ ] Run `python build_fpk.py`.
- [ ] Run `python -m unittest -v tests.test_fpk_package` against the fresh artifact.
- [ ] Inspect archive roots, modes, LF endings, JSON files, and calculate SHA-256.
