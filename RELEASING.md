# Releasing — maintainer cheatsheet

Two distinct actions. Don't mix them up.

| I want to… | Command |
|---|---|
| Push code to the public GitHub repo | `git push origin main` |
| Deploy to the VPS | `git tag -a vX.Y.Z -m "..." && git push origin vX.Y.Z` |

**Pushing `main` does NOT deploy.** Only `v*` tags trigger
`deploy-personal.yml`.

---

## Release flow to the VPS

```bash
# 1. Make sure main is pushed first.
git push origin main

# 2. Annotated tag with a short message.
git tag -a vX.Y.Z -m "vX.Y.Z — what changes"

# 3. Push the tag. This triggers the deploy.
git push origin vX.Y.Z
```

Watch the run at https://github.com/adrianpastora/FREEAI/actions. Takes 4–8
minutes.

---

## Re-running the deploy without bumping the version

If the deploy failed (broken secret, SSH down) and you want to retry:

```bash
gh workflow run "Deploy FREEAI (maintainer-personal SSH+Docker)"
```

Or the **Run workflow** button on the Actions tab.

---

## Pull-based deploy (recommended): Portainer GitOps

GitHub can't always reach the server (the SSH route depends on a public
hostname). With a pull-based deploy, nothing has to reach in:

1. Every pushed `v*` tag runs `release-branch.yml`, which points the
   **`release`** branch at that tag.
2. In Portainer, open the FreeAI stack, then **Editor / Git** settings:
   - Repository reference: `refs/heads/release`
   - **GitOps updates**: on, mechanism **Polling**, interval `5m`
   - Enable **Re-pull image and redeploy** / **Force redeployment** so the
     image is rebuilt.

   Keep the stack environment variables as they are, in particular
   `FREEAI_MASTER_KEY` and `POSTGRES_PASSWORD`.
3. Release as usual (`git tag -a vX.Y.Z … && git push origin vX.Y.Z`). Within
   about 5 minutes Portainer notices that `release` moved and redeploys.
   Migrations run on boot.

`deploy-personal.yml` (SSH) is still there as an optional path. When
`DEPLOY_SSH_HOST` doesn't resolve, it skips with a warning instead of
failing.

## Monitoring

`healthcheck.yml` probes `$HEALTHCHECK_URL/api/health` and `/v1/models`
every 30 minutes (3 attempts). A failure fails the run, and GitHub emails
the repo owner. Set or change the target with
`gh variable set HEALTHCHECK_URL --body https://…`. It's free on a public
repo.

After a deploy, run the real-provider smoke test:

```bash
FREEAI_URL=https://… FREEAI_KEY=fai_… python scripts/smoke_test.py
```

---

## Picking the version number

| Change since the last tag | Bump |
|---|---|
| Bugfix only, nothing user-visible | patch — `v0.7.X+1` |
| New feature, nothing breaks | minor — `v0.X+1.0` |
| Something breaks | minor bump in pre-1.0 + note in CHANGELOG |

---

## Common mistakes to avoid

- Pushing `main` and waiting for a deploy — nothing happens, you have to tag.
- Tagging before pushing `main` — the VPS runs `git reset --hard origin/main`,
  so the tag would point at a commit not yet on the remote and the deploy
  would ship stale code.
- Reusing an existing tag (`v0.7.0` already exists) — don't; bump the number.
