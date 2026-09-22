# Publishing this repository

This needs to run on a machine that can reach both remotes (the NUC itself, or any machine with
credentials for both). Nothing here needs to happen from this codebase's build environment.

## One-time setup

```bash
cd michigan-law-mcp
git init
git add -A
git commit -m "Initial commit: MCP server scaffold for the Michigan Compiled Laws"
```

Gitea (private, on the NUC):

```bash
# create an empty repo in the Gitea web UI first (no README/license/gitignore, so histories don't conflict), then:
git remote add gitea git@<nuc-host>:<you>/michigan-law-mcp.git   # or the https:// clone URL Gitea shows you
git push gitea main
```

GitHub (public):

```bash
# create the repo at github.com/new: no README/license/gitignore, so histories don't conflict
git remote add origin git@github.com:<you>/michigan-law-mcp.git
git push origin main
```

A repo can have both remotes at once (`git remote -v` to check), so `git push gitea main && git
push origin main` keeps them in sync going forward.

## Before the first push to GitHub (public)

- Double check nothing under `data/`, `*.sqlite3*`, or `.env` is staged (`.gitignore` already
  excludes these; `git status` should show none). No real database, bearer token, or contact
  address beyond what's already in `docs/` should leave this machine.
- Skim `deploy/*.example` for anything you filled in with a real value while testing; the
  checked-in versions should stay templates.
- The GitHub repo description/topics are a good place to note "unofficial, not affiliated with
  the State of Michigan or the Michigan Legislature" up front, since the project name could
  otherwise read as official.

## Keeping the private and public copies in sync

Since Gitea is where day-to-day work happens (on the NUC) and GitHub is the public mirror,
either push to both remotes from the same clone as above, or push to Gitea normally and
periodically `git push origin main` when a batch of work is ready to be public. Either is fine;
just don't let GitHub silently diverge for so long that a merge gets messy.
