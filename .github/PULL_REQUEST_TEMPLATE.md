<!--
Thanks for the PR. Keep the description tight — the reviewer should be able
to understand what changed and why without reading every hunk.

If this is a security fix, please coordinate through a private Security
Advisory first (see SECURITY.md) rather than opening a public PR.
-->

## Summary

<!-- One or two sentences. What changed, why, who asked for it. -->

## Checklist

- [ ] Opened an issue first if the change is non-trivial
- [ ] `pytest` passes locally (`cd backend && pytest`)
- [ ] New behaviour is covered by at least one test
- [ ] Docs updated if a public endpoint, config var, or env var changed
- [ ] No `console.log`, `print()`, or leftover debug code
- [ ] No secrets in the diff (including test fixtures)
- [ ] Commit messages are imperative mood with a blank-line body if needed

## Testing notes

<!-- How did you verify it? -->

## Related issues

<!-- Closes #123 / Related to #456 -->
