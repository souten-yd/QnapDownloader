# Default download preset and durable NAS folder selection

Status: specification, not yet implemented. This specification overrides contradictory defaults in earlier documents.

## User-facing preset: Flat / Manual / Keep Archives

| Item | Default | Required behavior |
|---|---|---|
| Package-name subfolders | OFF | Check both JD2 general folder setting and Packagizer auto rules |
| Website, date and page-based nested folders | OFF | Keep file layout flat but retain package grouping in the UI |
| LinkGrabber auto-confirm | OFF | Preview link additions before any commitment |
| Auto-start after links are detected | OFF | Site-specific explicit opt-in can override with bounded limits. v0.1.24: the Hub setting `send_autostart` (default ON) only affects links the Hub itself sends: `addLinks` with `autostart:true` plus `downloadcontroller/start`; JD2's own auto-confirm/auto-start settings stay OFF |
| Browser auto-download for all sites | Disallowed | Trusted origin and target host must both match |
| Auto-extract | OFF | Archives stay in original ZIP, CBZ, RAR or 7z form |
| Delete original archives or finished downloads | OFF | Never remove files just because an extraction or download ends |
| Execute completed files | Forbidden | Even if a file appears legitimate |
| Existing file overwrite | Forbidden | Offer skip or explicit new-name selection |
| Heavy re-hashing after completion | OFF | Do only when necessary or explicitly requested |
| Display progress persistence | RAM | Re-fetch actual JD2 state on reconnect |
| User settings, bookmarks, confirmed queue | Durable database | Commit before reporting success |

JD2 settings are version-dependent. The implementation must query supported Advanced Config keys, their types and current values, show an accurate diff, make a restorable snapshot, apply only supported changes, read settings back to verify, and allow rollback. Never erase user-created Packagizer rules or rewrite in-flight download paths without explicit confirmation. Existing external JD2 is read-only by default.

## Folder picker: server-side (not a browser local file picker)

- Present a NAS share/folder tree from an authenticated server API, showing currently selected path, free capacity, permission and mount status.
- Only explicitly permitted shared-folder roots are navigable. Never expose system paths, other app secrets, /etc, /dev, /proc, /sys, QPKG executable directories, or arbitrary root.
- Resolve the actual path and check symlink traversal, escapes, existence, permissions and mounted volume. Revalidate when saving and before starting a new download to prevent race / remount errors.
- Do not create unknown NAS shares implicitly. A new subfolder may be created only through authenticated user action inside a permitted root.
- Write a revisioned durable setting when the user saves, with a readback verification. On next app restart, QTS reboot and QPKG upgrade, the same configured folder must reappear. If offline/unmounted, block new downloads and show a repair action; NEVER silently fall back to another path.
- A changed default destination applies to new jobs only. Existing queue, incomplete files and completed files are not moved, overwritten or deleted automatically.
- If permissions or free space fail during transfer, stop safely without dropping confirmed queue entries.

## Handoff to QnapExtractor or docExtractor

Keep the original archive, filename, extension and bytes intact except minimal filename sanitization required by the OS. Never unpack, convert or reorganize by default. Use the final file only after JD2 reports completed and file existence/size checks pass; do not hand incomplete part files to another processor. Completion metadata belongs to the app DB, not lots of sidecar files.

The target processing app name/API/permission model must be verified separately before any automatic integration; this phase specifies compatible file layout, NOT an invented or unverified external API.

## Low disk I/O and recovery

Persist files, queue intent, settings, site policies and bookmarks. Only volatile progress, popup-block counters and derived UI state may be RAM-only. Avoid per-second DB writes and repeated file scans. Bound retry logs. Preserve SQLite correctness, JD2 resumable state and filesystem journal. Reconcile database and JD2 after crash without duplicating jobs. Compare application process read_bytes/write_bytes, actual HDD read/write, swap and jbd2 during idle and active workloads; physical HDD noise may come from both reads and writes.

## Regression acceptance tests

1. Both general folder settings and Packagizer rules cannot create unwanted nested folders.
2. Newly added links remain queued and are not automatically downloaded, extracted, deleted or executed.
3. All permitted archive types remain stored as-is and can be manually handled by downstream apps.
4. Folder picker rejects traversal, symlink escapes, system paths, unmounted shares and unauthorized users.
5. Saved path and preset survive app restart, NAS reboot and QPKG replacement.
6. Existing JD2/Docker setups, jobs and paths remain unchanged without explicit approval.
7. Low-I/O optimization retains all confirmed queue/settings data across crash/reboot.
