# Publish histfix with CI

## Direction

The operator approved creating a public repository, adding CI, and opening PRs.
Keep implementation and packaging in separate repositories, with the plugin PR merged first.

## Steps

1. Initialize the standalone `histfix` checkout with minimal repository files.
   Verify the Git root and personal origin before publication.
2. Move the existing plugin and tests into the standalone layout.
   Root owns `histfix.py`, `histfix.plugin.zsh`, `tests/`, `README.md`, and `AGENTS.md`.
   Verify equivalent behavior with `make check`; retain the observed failing regression evidence from initial implementation.
3. Add `.github/workflows/ci.yml`, `Makefile`, and `.trunk/` checks.
   Exercise Linux/Python 3.9, Linux/Python 3.14, and macOS/Python 3.14 in hosted CI.
4. Review history persistence and shell integration independently before publication.
   Reviewers are read-only and return findings to the root.
   Root owns repairs, staging, commits, and remote writes.
5. Update `homebrew-tap/Formula/histfix.rb` and its README to use the standalone repository.
   Remove the temporary in-tap implementation only after the destination is verified and committed.
6. Create the public remote and push the bootstrap and feature branches.
   Open the plugin PR and the dependent tap PR.
   Monitor current-head CI and report remaining review or merge prerequisites.

## Verification

```bash
make check
trunk check --no-fix
```

For tap packaging, use `brew style`, a disposable local source archive, `brew install`, and `brew test --force` for an unlinked validation package.
Set `HOMEBREW_NO_AUTO_UPDATE=1`, `HOMEBREW_NO_INSTALL_CLEANUP=1`, and `HOMEBREW_NO_AUTOREMOVE=1` for package validation and cleanup.
Never use `brew audit`.
