## Change

Describe the resulting behavior and why it is needed.

Related issue, if any:

For a suspected security vulnerability, follow the security reporting policy in
`.github/SECURITY.md` before opening a public pull request.

## Verification

For a bug fix, give reproduction steps and the result after the change. List the checks run,
the operating system, and any failures or checks you could not run.

- Windows: the full `scripts/check.py` check.
- Linux: `scripts/linux_check.py` in Docker, with `--suite package` or `--suite compile` for
  installer or compilation changes.
- Changes under `desktop/`: a maintainer runs the Windows desktop e2e before merge.

See `AGENTS.md` for the commands. Update the documentation that describes any changed behavior.
Remove tokens, API keys, private paths, notes and conversation content from evidence.

## Contribution rights

- [ ] Third-party material, its licenses and any restrictions are identified.
- [ ] Each external rights holder has posted the acceptance required by CONTRIBUTING.md for the
      current head commit, or an authorized representative covers those rights.

Ticking these boxes does not accept the CLA; post the acceptance comment described in
CONTRIBUTING.md.

Acceptance comment links, or explanation that this is the Project Owner's own work:
