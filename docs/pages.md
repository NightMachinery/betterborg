# GitHub Pages Site

The repository publishes a small static site on GitHub Pages, at
<https://nightmachinery.github.io/betterborg/>.

## What is published

Only the `pages/` directory, as it is on `master`. Nothing else in the
repository reaches the site.

- `pages/index.html`: the landing page, which links to the guides.
- `pages/telegram_remote_shell/index.html`: the user guide for the Telegram
  shell (`stdplugins/advanced_get.py`), served at
  <https://nightmachinery.github.io/betterborg/telegram_remote_shell/>.
- `pages/.nojekyll`: tells Pages not to run Jekyll. The Actions deploy below
  does not run Jekyll anyway, so this is only a guard.

## How it deploys

`.github/workflows/pages.yml` uploads `pages/` as the Pages artifact and
deploys it. It runs on a push to `master` that changes `pages/**` or the
workflow itself, and by hand from the Actions tab (`workflow_dispatch`).
Concurrent runs queue in one `pages` group instead of racing.

One-time setup: in the repository's Settings, under Pages, set "Build and
deployment" to "GitHub Actions". Until then the workflow fails.

## Updating a page

1. Edit the files under `pages/`.
2. Open the file in a browser and check it at phone width (about 360 px) and
   in both light and dark mode.
3. Commit and push to `master`. The workflow deploys the change; its run in
   the Actions tab shows the URL.

## Conventions for pages

- Each page is one self-contained HTML file: inline CSS and JavaScript, the
  favicon as a data URI, and no requests to other sites when it loads.
- Phone first: no horizontal page scroll at 360 px; code blocks scroll inside
  their own box.
- Light and dark mode through `prefers-color-scheme`.
- Top-level sections are collapsible `<details>` with an "Expand all /
  Collapse all" control; a link to a section's id opens it.
- The site is public. Keep out hostnames, chat ids, user ids, admin lists,
  paths under a home directory and anything about how a deployment is
  secured.

## Keeping the shell guide true

The shell guide describes `stdplugins/advanced_get.py` and the helpers it
uses in `uniborg/util.py` (running commands, downloading inputs, sending
output and files, admin checks) and `uniborg/guest_util.py` (guest mode).
When their behaviour changes, update the guide in the same commit.
