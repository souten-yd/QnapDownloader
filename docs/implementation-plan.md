> 現在の開発順序の正本は [docs/roadmap.md](roadmap.md) です。本書はその背景資料で、矛盾する場合はロードマップを優先します。

# Implementation roadmap and verification

**Status:** design only. The identifiers PR-1 ... PR-5 below are planning milestones, NOT existing GitHub PR numbers.

## Planned delivery order

| Milestone | Scope | Required acceptance |
| --- | --- | --- |
| PR-1 | Secure QPKG service, authentication, configuration DB, JD2 adapter, local/headless or existing engine mode | Safe QTS startup, storage mounts checked, PID ownership verified, existing containers untouched |
| PR-2 | Full JD2 LinkGrabber/Downloads UI, flat layout preset, NAS folder picker, durable queues and low I/O | No auto-folder, auto-start, extraction or deletion; preview and rollback; survives reboot |
| PR-3 | Sanitized lightweight browser, Rapidgator detection, click-to-add/CNL bridge, bookmarks | Explicit permission per source site, deduplication, popup/SSRF/XSS/CSRF protection |
| PR-4 | Optional sandboxed interactive browser, normal Safari/Chrome integration and legitimate Cloudflare challenges | Resource limits, sandbox retention, no bypass, safe fallbacks |
| PR-5 | TS-253Be live QPKG validation, HDD read/write measurements, regression/security suite, release | Real QNAP start/stop/restart/updates, checksum and assets verified against merged main |

### Work breakdown guidance

All PRs must identify expected final state, specific allowed paths and Do NOTs. Test the actual repo content rather than PR prose alone. Before merging, inspect main/PR diff, app behavior, QPKG manifest, docs and tests for consistency, underimplementation and overimplementation.

Never treat the existence of a PR or merged status as proof of correct implementation. Run local tests and package validation first. After merge, read merged main files directly, verify same source SHA and inspect release packages.

Design-to-implementation TODOs: confirm TS-253Be physical RAM (2/4/8 GB), QTS version and Java runtime, JD2 instance/API/version and legal integration constraints, JDownloader Packagizer setting names, permissions for QNAP shared folders, Chromium sandbox availability, iPhone Safari workflows, Site/Cloudflare limitations, local build environment and QDK pinned version.

### Low disk I/O performance acceptance

Measure 5-minute idle and active NAS disk reads/writes and jbd2, JVM/JD2 process I/O, app process I/O, swap, CPU and memory. Compare same baseline conditions pre/post. Do not promise zero HDD activity or compromise filesystem journaling. Persist confirmed settings/links and downloaded content while caching progress/filters in bounded RAM.

### Security and user acceptance

- Ads, malicious requests, unrequested popups/navigations, unrequested downloads must be blocked or halted with a clear reason.
- All Cloudflare access uses the target site's legitimate supported verification flow; impossible/incompatible cases must be disclosed.
- Rapidgator links are properly identified with visible add-to-queue controls and only explicitly approved automatic policies may start work.
- Selected NAS save folder, preset, bookmarks and queue survive reboot/release upgrade. No per-package folders, auto-extraction or completed-file cleanup by default.
- User file safety: no arbitrary execution, no cross-share traversal, no silent overwrite, no automatic changes to external JD2 containers.
- Local test/build/release result and each required security check must be documented, not assumed.

## Embedded headless JD2 + Java + account management roadmap (2026-11-01)

**Historical (v0.1.6): a Hub / external authenticated MyJD bridge only. Since v0.1.11 MyJD is removed and the Hub manages a locally installed JD2 engine (docs/engine-setup.md).** An embedded JDownloader.jar and full Java runtime are **not yet shipped**. The earlier PR-1...PR-5 table is historical planning, not evidence of implemented behavior or actual GitHub PR numbers.

The new detailed implementation source of truth is [embedded-engine architecture](embedded-jd2-architecture.md), with [premium account management](premium-accounts-design.md). The recommended order is Engine-0 (signed artifact / redistribution and local authenticated bridge spike) → Engine-1 (QPKG and immutable Java / persistent JD2 storage) → Engine-2 (supervisor + authenticated internal IPC) → Engine-3 (real download dashboard) → Engine-4 (premium account manager, read-only then HTTPS-gated mutations) → Engine-5 (JD2 independent updates and settings readback) → Engine-6 (optional RAM staging / low-I/O measurements) → Engine-7 (unified UI and actual TS-253Be acceptance). 

**Do NOT enable embedded mode by default, (external-MyJD compatibility mode was removed in v0.1.11 by owner decision), redistribute an unlicensed JD2 binary, open unauthenticated JD2 RemoteAPI, expose premium credentials on HTTP, or put persistent account/queue data in tmpfs.** These restrictions remain until explicit technical, license and QNAP hardware acceptance is documented.

## Release

Application changes continue through PR, protected merge, merged-main verification, local build and published QPKG release by default, provided required tools/validation actually succeed. Documentation-only edits do not cause a new application release.
