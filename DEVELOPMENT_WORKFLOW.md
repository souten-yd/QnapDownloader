# Development workflow — QnapDownloader

Reference: souten-yd/QnapSelfCare DEVELOPMENT_WORKFLOW.md on main, checked 2026-11-01. Adopt its pull-request-only, local-first validation, QPKG verification and release rules, plus QnapDownloader-specific safety boundaries.

## GitHub governance (mandatory)

- NEVER push application or documentation changes directly to main. Create a task-specific branch from latest main, validate, open PR, review diff and tests, merge, and re-read actual main files. Initial README commit exists only because this repository was empty.
- Only collaborators with write access and repository administrators may create PRs. Repository administrator must explicitly enable Settings / General / Features / Pull requests / Collaborators only. Do not disable the whole pull-request feature.
- Protect main with Require a pull request before merging, administrator enforcement, forbid force-push and deletion, and require review conversation resolution. For a solo project, zero required approvals avoids self-review deadlock; raise later if reviewers are available.
- GitHub SETTINGS ARE NOT ENFORCED BY THIS DOCUMENT. Apply and verify them in GitHub UI or an admin-authorized API request (see docs/github-protection.md).
- No PR bypass for convenience. Do not reduce or disable protection to merge; report the exact blocker instead.
- Before declaring completion, compare prior requirements and explicit scope, allowed paths, Do NOT constraints and expected final state to the actual result. Read PR files and merged main implementation, manifests, docs and tests. Identify missing fields, stale wording, pointer drift and unrequested behavior changes.
- Keep PRs cohesive. Add regression tests for changed behavior, and include small corrections in the next planned PR when safe rather than proliferating one-line PRs.
- Cross-repository change: one PR per repository, with documented dependency order.

## Normal delivery process (adopted from QnapSelfCare)

1. Inspect existing main code and specs, then write scope and non-goals.
2. Implement, write regression/safety tests, and verify changes locally.
3. Prefer local tests and local QDK/QPKG builds; record commands, source SHA, result and SHA-256 checksum. Never claim a package was built without actually building.
4. GitHub Actions are manual-only fallback. Do not dispatch without explicit user authorization. Avoid redundant automatic push/PR builds, but do not bypass truly required checks.
5. Create PR and verify actual diff, test assertions, scope, security checks and version alignment before merge.
6. Merge only after protection rules are satisfied. Re-read actual main files and recheck the merged revision, not just PR metadata.
7. For application changes, by default deliver through validated main to a published release with correct QPKG, checksum and linked source revision unless a newer user instruction explicitly stops earlier. Documentation-only PRs need no new binary release; state that clearly.
8. If testing, publication or permissions block a step, report exactly what remains; never claim it was completed.

## QNAP implementation requirements

- Target TS-253Be (x86_64), QTS 5.x. QDK version pinned and downloaded with verified checksum. Keep app version, QPKG version and Release asset version consistent.
- On reboot, wait for required mounted shares and runtime dependencies. Do not create a directory under an unmounted /share location.
- Resolve QPKG install path using QTS facilities, not hard-coded paths. Use safe start/stop/status/restart service control; match executable, command line, cwd and process start time before stopping only an owned process. Never kill unrelated processes.
- Do not run npm build during QPKG server startup. Build JS/static assets in local build workflow. Startup must be deterministic.
- Store configuration, bookmark tree, saved NAS download path, download queue and credential references in persistent app data separate from the QPKG installation directory. Preserve on updates. Never store passwords in normal logs.
- Avoid creating a new JS runtime, background daemon or proxy with unnecessary power. Backend remains authoritative for permissions, valid save paths, account state, policy decisions and download actions. The browser must not execute untrusted remote scripts in the admin UI origin.
- Low HDD I/O must not compromise durability: progress caches in RAM, bounded logs, change-triggered DB commits, durable queue/settings, intact SQLite journal and JD2 partial files; no fsync disable, WAL manual removal or journal disabling.
- Maintain existing external JD2 containers without unapproved stopping, replacement, deletion or settings mutation. Recommend change previews and rollback.
- Prohibited: arbitrary system command execution from web pages, unrestricted localhost CNL endpoints on LAN, unsafe raw-source serving, Cloudflare or CAPTCHA circumvention, unbounded crawling/autonomous execution, root-by-default processes and auto-running downloaded files.

## Review checklist

- [ ] Intended scope, allowed paths, Do NOT and expected final state verified.
- [ ] Full diff and actual main files read; backend, web UI, package manifest, docs and tests align.
- [ ] Saved NAS folder, flat layout, no automatic start/extract/delete, persisted settings and reboot recovery tested.
- [ ] Authentication, authorization, XSS, CSRF, SSRF, popup/redirect blockers and file path restrictions tested.
- [ ] Need-to-save data survives crash; low-I/O measurements separated from physical read/write and journal activity.
- [ ] Local tests/build results, source SHA and artifacts/checksums documented.
- [ ] PR restrictions and branch protection respected; merged main verified.
- [ ] Correct release artifacts published or documentation-only release exemption stated.
