from __future__ import annotations

import datetime as dt
import json
import sys
import time
import typing as t
import urllib.error
import urllib.parse
import urllib.request

from rss_app.core import (
    GitHubClient,
    JST,
    UA,
    fmt_human,
    now_jst,
    parse_tz,
    safe_filename,
    sha256_hex,
)
from rss_app.sites import get_requested_handler, select_handler
from rss_app.sites.base import HandlerContext


class Runner:
    def __init__(self, cfg: dict):
        gh = cfg.get("github", {})
        token = gh.get("token")
        repo = gh.get("repo")
        branch = gh.get("branch", "main")
        if not token or not repo:
            raise SystemExit(
                "Set github.repo and a GitHub token via config github.token "
                "or GITHUB_TOKEN."
            )
        self.gh = GitHubClient(token=token, repo=repo, branch=branch)

        self.ua = cfg.get("user_agent", UA)
        self.poll_interval = int(cfg.get("poll_interval_sec", 600))
        self.rss_items_default = int(cfg.get("rss_items_default", 20))
        self.site_interval_default = int(cfg.get("site_interval_default", 600))
        self.readme_path = cfg.get("readme_path", "Read.me")
        self.state_path = cfg.get("state_path", "state.json")
        self.daily_flag_path = cfg.get("daily_flag_path", "00.txtt")
        self.rss_output_dir = str(cfg.get("rss_output_dir", "RSS")).strip().strip("/\\") or "RSS"
        self.site_manifest_path = cfg.get("site_manifest_path", "Site.json")
        self.site_manifest_ttl = int(cfg.get("site_manifest_ttl_sec", 43200))
        self.readme_log_header = cfg.get("readme_log_header", "## Update log\n")

    def fetch(self, url: str, ua: t.Optional[str] = None) -> tuple[bytes, dict]:
        cur = url
        for _ in range(5):
            req = urllib.request.Request(cur)
            req.add_header("User-Agent", ua or self.ua)
            req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
            req.add_header("Accept-Language", "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.read(), dict(resp.headers)
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308):
                    loc = e.headers.get("Location")
                    if loc:
                        cur = urllib.parse.urljoin(cur, loc)
                        continue
                raise
        raise RuntimeError(f"too many redirects: {url}")

    def load_state(self) -> dict:
        sha, content = self.gh.get_file(self.state_path)
        if content is None:
            return {"_sha": sha} if sha else {}
        try:
            data = json.loads(content.decode("utf-8"))
        except Exception:
            data = {}
        data["_sha"] = sha
        return data

    def save_state(self, state: dict, msg: str) -> None:
        sha = state.pop("_sha", None)
        blob = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
        res = self.gh.put_file(self.state_path, blob, message=msg, sha=sha)
        state["_sha"] = (res.get("content") or {}).get("sha")

    def load_sites_manifest(self, state: dict) -> list[dict]:
        cache = state.get("site_manifest_cache") or {}
        fetched_at_s = cache.get("fetched_at")
        sites_cached = cache.get("sites")
        if fetched_at_s:
            try:
                fetched_at = dt.datetime.fromisoformat(fetched_at_s)
                if fetched_at.tzinfo is None:
                    fetched_at = fetched_at.replace(tzinfo=JST)
            except Exception:
                fetched_at = None
            if fetched_at:
                age = (now_jst() - fetched_at).total_seconds()
                if age < self.site_manifest_ttl and isinstance(sites_cached, list):
                    return sites_cached

        try:
            sha, content = self.gh.get_file(self.site_manifest_path)
            if content:
                text = content.decode("utf-8", errors="ignore")
                data = json.loads(text)
                sites = data.get("sites") if isinstance(data, dict) else data
                if not isinstance(sites, list):
                    raise ValueError('Site.json must be an array or {"sites": [...]}')
                state["site_manifest_cache"] = {
                    "sha": sha,
                    "fetched_at": now_jst().isoformat(),
                    "sites": sites,
                }
                return sites
        except Exception as e:
            print(f"[error] load Site.json failed: {e}", file=sys.stderr)

        if isinstance(sites_cached, list):
            print("[warn] using cached Site.json (previous)", file=sys.stderr)
            return sites_cached
        print("[warn] Site.json unavailable; no sites to process", file=sys.stderr)
        return []

    def append_readme_updates(self, updates: list[tuple[str, dt.datetime]]) -> None:
        if not updates:
            return
        cur_sha, content = self.gh.get_file(self.readme_path)
        body = "" if content is None else content.decode("utf-8", errors="ignore")
        header = self.readme_log_header
        if header not in body:
            body = header + "\n" + body
        lines = [line for line in body.splitlines()]
        insert_idx = 1
        new_lines = [f"- {fmt_human(when)} - {name}" for name, when in updates]
        lines[insert_idx:insert_idx] = new_lines + [""]
        new_body = "\n".join(lines)
        self.gh.put_file(
            self.readme_path,
            new_body.encode("utf-8"),
            message=f"chore: update Read.me ({len(updates)} site(s))",
            sha=cur_sha,
        )

    def update_daily_flag(self) -> None:
        today = now_jst().strftime("%Y-%m-%d")
        cur_sha, content = self.gh.get_file(self.daily_flag_path)
        cur = content.decode("utf-8", errors="ignore").strip() if content else ""
        if cur == today:
            return
        self.gh.put_file(
            self.daily_flag_path,
            (today + "\n").encode("utf-8"),
            message=f"chore: daily flag {today}",
            sha=cur_sha,
        )

    def process_once(self) -> None:
        try:
            state = self.load_state()
        except Exception as e:
            print(f"[error] load state failed: {e}", file=sys.stderr)
            state = {}

        site_state: dict = state.get("sites", {}) if isinstance(state.get("sites"), dict) else {}
        sites = self.load_sites_manifest(state)
        updates: list[tuple[str, dt.datetime]] = []

        for site in sites:
            name = str(site.get("name") or site.get("url") or "site").strip()
            url = site.get("url")
            if not url:
                print(f"[warn] skip: site '{name}' has no url", file=sys.stderr)
                continue

            tz = parse_tz(site.get("tz") or site.get("timezone"))
            interval = self._site_interval(site)
            entry_state = site_state.setdefault(name, {})

            now_local = dt.datetime.now(tz=tz)
            if self._not_due(name, entry_state, interval, now_local, tz):
                continue

            site_ua = site.get("user_agent") or self.ua
            try:
                requested_handler = get_requested_handler(site)
            except Exception as e:
                print(f"[error] handler selection failed for {name}: {e}", file=sys.stderr)
                entry_state["last_checked"] = now_local.isoformat()
                continue

            body = b""
            headers: dict = {}
            if requested_handler is None or requested_handler.requires_pre_fetch:
                try:
                    body, headers = self.fetch(url, ua=site_ua)
                except Exception as e:
                    print(f"[error] fetch failed for {name}: {e}", file=sys.stderr)
                    entry_state["last_checked"] = now_local.isoformat()
                    continue

            rss_time_tz = str(site.get("rss_time_tz", "utc")).lower()
            ctx = HandlerContext(
                site=site,
                name=name,
                url=url,
                body=body,
                headers=headers,
                timezone=tz,
                now_local=now_local,
                rss_time_tz=rss_time_tz,
                site_state=site_state,
                entry_state=entry_state,
                fetch=self.fetch,
                user_agent=site_ua,
                rss_items_default=self.rss_items_default,
            )

            try:
                handler = requested_handler or select_handler(ctx)
                page_hash: t.Optional[str] = None
                if handler.uses_page_hash:
                    page_hash = sha256_hex(body)
                    if entry_state.get("hash") == page_hash:
                        print(f"[info] no change: {name}")
                        entry_state["last_checked"] = now_local.isoformat()
                        continue

                result = handler.build(ctx)
                entry_state["last_checked"] = now_local.isoformat()
                if result.content is None:
                    print(result.message or f"[info] no update: {name}")
                    continue

                rss_path = f"{self.rss_output_dir}/{safe_filename(name)}.rss"
                sha, _content = self.gh.get_file(rss_path)
                self.gh.put_file(
                    rss_path,
                    result.content,
                    message=result.commit_message or f"feat: update RSS for {name}",
                    sha=sha,
                )

                if page_hash:
                    entry_state["hash"] = page_hash
                entry_state["last_updated"] = now_local.isoformat()
                updates.append((name, now_local))
                print(f"[info] updated: {name} ({result.log_label})")

            except Exception as e:
                print(f"[error] process failed for {name}: {e}", file=sys.stderr)
                entry_state["last_checked"] = now_local.isoformat()
                continue

        try:
            self.append_readme_updates(updates)
        except Exception as e:
            print(f"[error] update Read.me failed: {e}", file=sys.stderr)
        try:
            state["sites"] = site_state
            self.save_state(state, msg="chore: update state.json")
        except Exception as e:
            print(f"[error] save state.json failed: {e}", file=sys.stderr)
        try:
            self.update_daily_flag()
        except Exception as e:
            print(f"[error] update daily flag failed: {e}", file=sys.stderr)

    def run(self, watch: bool = True) -> None:
        if watch:
            print(f"[info] watch mode: tick={self.poll_interval}s (per-site interval controls actual fetch)")
            while True:
                try:
                    self.process_once()
                except Exception as e:
                    print(f"[fatal] {e}", file=sys.stderr)
                time.sleep(self.poll_interval)
        else:
            try:
                self.process_once()
            except Exception as e:
                print(f"[fatal] {e}", file=sys.stderr)

    def _site_interval(self, site: dict) -> int:
        raw = site.get("interval_sec", self.site_interval_default)
        try:
            return int(raw)
        except Exception:
            return self.site_interval_default

    def _not_due(
        self,
        name: str,
        entry_state: dict,
        interval: int,
        now_local: dt.datetime,
        tz: dt.tzinfo,
    ) -> bool:
        last_checked_s = entry_state.get("last_checked")
        if not last_checked_s:
            return False
        try:
            last_checked = dt.datetime.fromisoformat(last_checked_s)
            if last_checked.tzinfo is None:
                last_checked = last_checked.replace(tzinfo=tz)
        except Exception:
            return False

        elapsed = (now_local - last_checked).total_seconds()
        if elapsed >= max(0, interval):
            return False
        remaining = int(max(0, interval - elapsed))
        print(f"[info] not due: {name} (next in ~{remaining}s)")
        return True
