## Purpose / expected final state

Describe the user-visible change, target files and final behavior.

## Scope / allowed paths

List exactly which paths are intentionally modified. Note cross-repo dependencies.

## Do NOT / safety invariants

- No direct push to main or force-push/branch deletion
- No unauthorized download, unbounded auto-crawl, external site JS in management origin
- No unsafe database durability reduction, QTS mount bypass or unauthorized existing JD2/Docker modifications
- No mutation of user-specified download path, existing downloaded files, archived originals, or custom Packagizer rules without explicit approval
- Hub QPKG updater must not update JD2; JD2 official updater must not update Hub QPKG
- JD2 update/preset controls remain disabled without a verified authenticated adapter

## Validation evidence

- [ ] Actual code/diff inspected, not PR text only
- [ ] All changed files in allowed scope
- [ ] Tests and Python/JS/shell syntax checks run locally (commands and output recorded)
- [ ] Security negative tests and relevant regression tests
- [ ] QDK package build/checksum and actual NAS test (if application release)
- [ ] Main re-read after merge; manifest/docs/UI/tests/runtime aligned
- [ ] No unrequested features or stale docs/pointers
- [ ] GitHub protection constraints verified or explicit administrator blocker documented

## Deployment

Source SHA, artifact checksum, any installation/rollback caveats, test evidence and follow-up work.

**Documentation-only PRs do not require a QPKG rebuild/release.**
