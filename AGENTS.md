<!-- rtk-instructions v2 -->
# RTK

Prefix every shell command with `rtk`: `rtk git status`, `rtk cargo test`,
`rtk npm run build`, `rtk ls src/`. Keep the prefix inside chains:
`rtk git add . && rtk git commit -m "msg"`. Commands RTK has no filter for
run as-is, so the prefix is always safe.

# Command output

Command output here is condensed to save tokens, keeping every signal and
dropping costly noise. Treat it as the complete result: run commands
normally, and batch related commands into one call to avoid extra turns.
Truncated results state their recovery path in their own output. Re-run a
command as `rtk proxy <cmd>` only when its result is unusable: empty when
output was clearly expected, contradicting its exit code, or garbled.

## About RTK

RTK (Rust Token Killer) is a CLI proxy that filters command output to save
tokens; behavior and exit code are unchanged.

- `rtk gain` / `rtk gain --history` — token savings, overall and per command.
- `rtk proxy <cmd>` — run a command unfiltered, still tracked.
- `RTK_DISABLED=1 <cmd>` — skip RTK for one command.
- `rtk discover` — find past commands RTK could have condensed.
<!-- /rtk-instructions -->

# Versioning and commits

This project uses **Semantic Versioning** (SemVer): versions follow
`MAJOR.MINOR.PATCH` (e.g. `0.5.0`).

- **MAJOR** — incompatible/breaking changes.
- **MINOR** — new functionality, backwards compatible.
- **PATCH** — backwards-compatible bug fixes.

Commits follow **Conventional Commits** so version bumps can be derived from
history: `<type>(<optional scope>): <description>`

- `feat:` — new feature (bumps MINOR)
- `fix:` — bug fix (bumps PATCH)
- `feat!:` / `fix!:` or a `BREAKING CHANGE:` footer — breaking change (bumps MAJOR)
- Other types: `chore:`, `docs:`, `refactor:`, `test:`, `build:`, `ci:`, `perf:`

