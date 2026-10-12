# Release process (owner guide)

Pushing a tag `vX.Y.Z` tests the code, publishes both images, and creates the GitHub Release.

## Cut a release
1. Land everything on `main`; wait for **CI** to be green.
2. Bump `VERSION` (e.g. `1.1.0`) and move the `## [Unreleased]` notes in `CHANGELOG.md`
   into a new `## [1.1.0] - YYYY-MM-DD` section. Commit and push to `main`.
3. Tag and push:
   ```bash
   git tag v1.1.0 && git push origin v1.1.0
   ```
4. Watch **Actions → Release**. When it is green, the images and the release page exist.

What the tag does:
- **Validate:** the tag must be semver, match `VERSION`, and point at a commit on `main`.
- **Test:** the same per-file test run as CI. Any failure stops here; nothing is published.
- **Publish:** `ghcr.io/wsturner88/fabrica` (Hub) and `ghcr.io/wsturner88/fabrica-detector`
  (adapter), each for `linux/amd64` + `linux/arm64`, tagged `X.Y.Z`, `X.Y` and `latest`.
- **Release:** GitHub Release with the `## [X.Y.Z]` section of `CHANGELOG.md` as notes
  (auto-generated notes if the section is missing).

## Try it first with a release candidate
Tag `v1.1.0-rc.1` (`VERSION` stays `1.1.0`). It publishes only `:1.1.0-rc.1` for both images
and a GitHub **prerelease**; `latest` and `X.Y` do not move. Pull it, test, then tag `v1.1.0`.

## One-time, before the first public announcement
GHCR packages start private, and GitHub's API cannot change that. After the first release, for
**each** of `fabrica` and `fabrica-detector`: your profile → **Packages** → the package →
**Package settings** → **Danger Zone** → **Change visibility** → **Public**.
Then check from a machine that is not logged in: `docker pull ghcr.io/wsturner88/fabrica:latest`.

## Good to know
- `edge.yml` pushes `ghcr.io/wsturner88/fabrica:edge` after CI passes on `main` (testing only).
- A bad release: fix forward with a new patch tag. Don't move or delete a published tag.
- Dependencies are in `requirements.txt` (compatible ranges). Raise them only after the full test run.
