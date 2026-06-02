# RSS.APP

Python script for fetching site data, generating RSS feeds, and writing the
generated files back to this GitHub repository.

## Structure

- `rss.py`: command-line entry point.
- `rss_app/core.py`: shared RSS, time, HTTP parsing, and GitHub API helpers.
- `rss_app/runner.py`: scheduling, state management, and GitHub writes.
- `rss_app/sites/`: site-specific handlers.

Handlers are selected automatically in this order:

1. `github_search`: GitHub Search API JSON.
2. `pass_through`: existing RSS/Atom feeds.
3. `booth`: `booth.pm` HTML pages.
4. `general`: fallback HTML link extraction.

You can also force a handler in `Site.json`:

```json
{
  "name": "Example Booth",
  "url": "https://example.booth.pm/items",
  "handler": "booth",
  "rss_items": 20
}
```

## GitHub Actions

The workflow in `.github/workflows/rss.yml` runs `python rss.py --once` every
15 minutes and can also be started manually from the Actions tab.

The default configuration writes generated files into the `RSS/` folder:

- `RSS/*.rss`: generated feeds.
- `RSS/state.json`: crawler state.
- `RSS/00.txt`: daily run marker.
- `RSS/README.md`: update log.

Because the workflow writes to this same repository, it uses the built-in
`GITHUB_TOKEN`. No personal access token is required unless branch protection
or cross-repository writes are added later.
