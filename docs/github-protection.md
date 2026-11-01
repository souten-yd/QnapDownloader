# Protect main and restrict pull request creation (admin procedure)

Repository: souten-yd/QnapDownloader
Status: This file is documentation. GitHub-side settings MUST be applied and verified independently.

## Security policy

- All main changes require a pull request.
- No direct main push even for documentation or administrators.
- Pull request creation is allowed to repository collaborators with write access and administrators only. In a personal repository GitHub counts invited collaborators; do not confuse this with excluding the repository owner.
- Reject force-push and branch deletion. Require resolution of review conversations. Enforce admin rules.
- A solo developer may configure required review approvals to 0; increase to 1+ only if at least one independent approving reviewer exists.
- Keep pull requests enabled; disabling the whole feature would prevent even collaborators' PR creation.

## Step 1: Restrict creating pull requests

Visit https://github.com/souten-yd/QnapDownloader/settings
Settings > General > Features > Pull requests > select Collaborators only.

Official GitHub changelog (2026-02-13):
https://github.blog/changelog/2026-02-13-new-repository-settings-for-configuring-pull-request-access/

This setting is distinct from branch protection and may not be exposed by the available connector's write API.

## Step 2: Protect main

Use GitHub Settings > Branches (or repository Rules / Rulesets) to set:
- Require a pull request before merging = yes.
- Include administrators / no bypass for everyone.
- Require conversation resolution = yes.
- Allow force pushes = no; allow deletions = no.
- Required status checks: only enable checks actually run by the project. The QnapSelfCare local-first/manual-Actions policy should not be broken by a permanently pending CI check.
- Do not lock main (a permanent read-only lock blocks normal merges).

### Shell command with admin PAT

Fine-grained GitHub token must allow the QnapDownloader repository, Administration: read/write. Never paste the token into an issue/PR or log. Before PUT, query and save any existing protection configuration; PUT replaces protection settings. This example is suitable only when the current configuration has no needed restrictions to retain.

    read -rsp 'GitHub admin PAT: ' GH_TOKEN; echo
    API='https://api.github.com/repos/souten-yd/QnapDownloader/branches/main/protection'
    curl --fail -sS -H 'Accept: application/vnd.github+json' -H "Authorization: Bearer ${GH_TOKEN}" "$API"
    curl --fail -sS -X PUT -H 'Accept: application/vnd.github+json' -H "Authorization: Bearer ${GH_TOKEN}" -H 'X-GitHub-Api-Version: 2026-03-10' "$API" -d '{
      "required_status_checks": null,
      "enforce_admins": true,
      "required_pull_request_reviews": {
        "dismiss_stale_reviews": true,
        "require_code_owner_reviews": false,
        "required_approving_review_count": 0,
        "require_last_push_approval": false
      },
      "restrictions": null,
      "required_conversation_resolution": true,
      "required_linear_history": false,
      "allow_force_pushes": false,
      "allow_deletions": false,
      "block_creations": false,
      "lock_branch": false
    }'
    curl --fail -sS -H 'Accept: application/vnd.github+json' -H "Authorization: Bearer ${GH_TOKEN}" "$API"
    unset GH_TOKEN

If the first GET returns 404 it may simply mean no protection exists yet; if 403, the token may not have permission. Do not assume success after failed PUT.

Docs: https://docs.github.com/en/rest/branches/branch-protection

## Step 3: Verify actual enforcement

- Authenticated branch protection GET returns 200.
- required_pull_request_reviews is present.
- enforce_admins.enabled is true.
- allow_force_pushes.enabled and allow_deletions.enabled are false.
- required_conversation_resolution.enabled is true.
- GitHub repository settings visibly indicate Collaborators only for pull requests.
- Make a normal documentation branch + PR, review the changed files and complete a protected merge. Verify that a direct main push from a non-bypass account is rejected, without forcibly editing protected code.

## Tool limitation verified 2026-11-01

The available GitHub connector can write repository files, create branches and PRs, but attempting to read main protection returned HTTP 403 (resource not accessible by integration); no administration write action was available. Therefore **do not claim protections are applied by the connector**. They remain administrator configuration tasks until the actual GitHub settings and API output have been checked.
