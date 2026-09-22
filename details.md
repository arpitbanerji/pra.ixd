The README is on screen. Two files for you, plus one question and one real bug I caught.

## 1. The root README

**`pra.ixd-root-README.md`** — copy it to your `pra.ixd` repo root as `README.md`. It's written as a monorepo index, not an ohbot doc:

- **Apps table** — one row per app, so adding app #2 is one line
- **Layout** tree
- **Conventions** — one dir per app, apps never import each other, each owns its README, secrets never committed, credentials-free test suites, non-root containers
- **CI section** — the subfolder rule (below)
- **Adding a new app** — 5-step checklist
- **Security** and **Licence** sections

## 2. A ready-made monorepo tree

**`pra.ixd-monorepo.zip`** (`70837175…`) — the layout already built and verified:

```
.github/workflows/ohio.yml
.gitignore
README.md          ← the root README above
ohio/              ← the app, with its own README, tests, Dockerfile
```

Drop it into `pra.ixd` and you're done. `ohbot.zip` (`3759f29b…`) also now includes the root README if you prefer the app-only copy.

## The bug worth knowing about

You'd have hit this, and it fails **silently**:

> **GitHub only reads workflows from `.github/workflows/` at the repo root.**

When ohbot moves into `ohio/`, its workflow travels with it to `ohio/.github/workflows/tests.yml` — where GitHub **ignores it with no error**. Your CI would simply stop running and nothing would say why. The file still looks right, which is what makes it nasty.

The fix, which I applied and verified in the rehearsal:

```yaml
on:
  push:
    branches: ["**"]
    paths: ["ohio/**", ".github/workflows/ohio.yml"]
  pull_request:
    paths: ["ohio/**", ".github/workflows/ohio.yml"]

defaults:
  run:
    working-directory: ohio
```

`working-directory` is what keeps the steps byte-identical to the standalone repo — `pip install -e ".[dev]"`, `pytest`, and `docker build .` all resolve inside `ohio/`. I confirmed in the real layout: **75 passed, 2 skipped**, pyflakes clean, `pip install -e .` works, imports work, and `docker compose`'s `build: .` still resolves.

## Verified move commands

I ran these on a fresh clone and confirmed the result:

```bash
cd pra.ixd
mkdir -p ohio
git ls-files | while read -r f; do mkdir -p "ohio/$(dirname "$f")"; git mv "$f" "ohio/$f"; done

# workflows must sit at the repo root — GitHub ignores .github/workflows elsewhere
mkdir -p .github/workflows && git mv ohio/.github/workflows/tests.yml .github/workflows/ohio.yml

rmdir ohio/.github/workflows ohio/.github scripts src tests 2>/dev/null
cp /path/to/pra.ixd-root-README.md README.md
```

Then add the `paths` + `working-directory` block above. `git mv` keeps history, so `git log --follow ohio/src/ohbot/app.py` works.

## One thing to confirm — the folder name

You wrote **`ohio`**; the project is **`ohbot`** everywhere (package name, `pyproject.toml`, image tag, every commit). I used `ohio` as you specified, but I want to flag it rather than silently pick — if that was a typo, tell me and it's `git mv ohio ohbot` plus one line in the README.

My honest take: **unify them**. A folder named `ohio` holding a package named `ohbot` costs you a small explanation every time someone new reads the tree. Either name works; matching names don't.

## Caveats

- **The restructure is the one thing I did not apply to your project** — you asked for a README, and moving files is a layout decision I'd rather you make knowingly. Everything above was verified in a scratch clone, not applied here. Say the word and I'll do it.
- **The `docker` CI job has still never actually run** (this sandbox blocks the container syscalls) — the Dockerfile remains unproven until your first build.
- **No LICENSE file exists**, so the repo is all-rights-reserved by default. The README says so plainly; add one at the `pra.ixd` root when you're ready.