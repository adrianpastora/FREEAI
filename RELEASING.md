# Releasing — push to main vs deploy to VPS

Two distinct actions in this repo. Don't confuse them.

| Action | What it does | Affects users? |
|---|---|---|
| **Push to `main`** | Publishes commits to GitHub | Anyone who clones / pulls |
| **Tag `vX.Y.Z`** | Triggers `deploy-personal.yml` → SSH+Docker rebuild on the maintainer's VPS | Only the maintainer's running install |

`tests.yml` is **manual-only** — neither action runs it. Open the Actions tab if you want to run it.

---

## Push to `main` (publish source, no deploy)

Day-to-day work. After committing locally:

```bash
git push origin main
```

That's it. The code is on GitHub. **Nothing is deployed yet.**

---

## Deploy to the VPS (push a tag)

Only do this when you want the running install at the maintainer's VPS to pick up new code. The workflow only triggers on **semver tags matching `v*`**.

### Standard release flow

```bash
# 1. Make sure main is pushed first.
git push origin main

# 2. Tag the release (annotated tag with a short message).
git tag -a v0.7.2 -m "v0.7.2 — short description"

# 3. Push the tag. This is what triggers the deploy workflow.
git push origin v0.7.2
```

The Actions tab will show a new run of *Deploy FREEAI (maintainer-personal SSH+Docker)*. It SSHs to the VPS, pulls `origin/main`, rebuilds the Docker image and restarts. Takes ~4–8 min.

### Re-run without bumping the version

If a deploy failed (bad secret, transient SSH issue) and you want to retry without creating a new tag:

```bash
gh workflow run "Deploy FREEAI (maintainer-personal SSH+Docker)"
```

Or click **Run workflow** from the Actions tab.

### Picking the next version

| Change since last tag | Bump |
|---|---|
| Bugfix only, no public surface change | `v0.7.X+1` (patch) |
| New feature, backward-compatible | `v0.X+1.0` (minor) |
| Breaking change | `v1.0.0` once you're ready to commit to stability; before that, minor bump + CHANGELOG note |

Pre-1.0 only: keep minor bumps for anything visible, patch for invisible fixes.

### Common mistakes

- **Pushing main does NOT deploy.** Only tags do.
- **Tags are immutable in practice.** Don't reuse `v0.7.0` — bump the version.
- **Don't tag before pushing main.** The VPS does `git fetch origin main && git reset --hard origin/main`, so a tag pointing at a commit that hasn't reached `origin` will pull an older state.

---

## Forks / other contributors

If you cloned this repo for your own use: **delete `.github/workflows/deploy-personal.yml`** from your fork, or it will keep failing on every tag you push (it needs SSH secrets that only the maintainer has). The `tests.yml` workflow is fine to keep.

For self-hosting in your own VPS see [README.md § Self-host in production](README.md#self-host-in-production) — `docker-compose.prod.yml` with Caddy is the supported path.
