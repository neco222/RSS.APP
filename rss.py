#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WebサイトからRSS/Atomを取得・生成してGitHubに保存するワンファイルスクリプト。

Site.json の各サイトに tz/timezone（IANA名 or +HH:MM or 9 等）と rss_time_tz（"utc"/"local"）を指定可能。
- パススルー（既存RSS/Atom）の場合でも日時を指定TZ/UTCに書き換え可能（rss_time_tz で制御）。
- HTMLはリンク抽出でRSS(2.0)生成。生成時の pubDate/lastBuildDate も rss_time_tz に従う。
- booth.pm の検索/一覧ページは商品詳細ページを個別取得し、タイトル=商品名、description=価格にする。
- GitHub Search API(JSON) は “archive” を state.json に蓄積して、既存PRもRSSに保持する。
  （ただし、クローズ/マージを新規イベントとして追加はしない：is:open の結果から消えるだけ）

依存: 標準ライブラリのみ（Python 3.9+ 推奨: zoneinfo 使用）
"""
from __future__ import annotations

import base64
import datetime as dt
import email.utils
import hashlib
import html
import json
import re
import sys
import time
import typing as t
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
import xml.etree.ElementTree as ET

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:
    ZoneInfo = None  # type: ignore

# ====== 定数/ユーティリティ ======
JST = dt.timezone(dt.timedelta(hours=9), name="JST")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/119.0.0.0 Safari/537.36"
)
GITHUB_API = "https://api.github.com"


def now_jst() -> dt.datetime:
    return dt.datetime.now(tz=JST)


def rfc2822_utc(d: dt.datetime) -> str:
    return email.utils.format_datetime(d.astimezone(dt.timezone.utc))


def rfc2822_local(d: dt.datetime) -> str:
    return email.utils.format_datetime(d)


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def safe_filename(name: str) -> str:
    bad = "/\\:\n\r\t\0\x0b\x0c"
    out = name
    for ch in bad:
        out = out.replace(ch, "_")
    return (out.strip() or "site")


def parse_tz(spec: t.Optional[t.Union[str, int, float]]) -> dt.tzinfo:
    """Site.json の tz/timezone 指定を tzinfo に変換。未指定/不正は JST。"""
    if spec is None:
        return JST
    if isinstance(spec, (int, float)):
        try:
            return dt.timezone(dt.timedelta(hours=float(spec)))
        except Exception:
            return JST
    s = str(spec).strip()
    if not s:
        return JST
    if ZoneInfo is not None:
        try:
            return ZoneInfo(s)
        except Exception:
            pass
    m = re.fullmatch(r'([+-])(\d{2}):?(\d{2})', s)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hh = int(m.group(2))
        mm = int(m.group(3))
        return dt.timezone(sign * dt.timedelta(hours=hh, minutes=mm))
    try:
        return dt.timezone(dt.timedelta(hours=float(s)))
    except Exception:
        return JST


def fmt_human(dtobj: dt.datetime) -> str:
    return dtobj.strftime("%Y-%m-%d %H:%M:%S %Z")


# ---- 日時パース/フォーマット補助（RSS/Atom両対応）----
_ISO_Z_RE = re.compile(r"Z$")  # ...Z → +00:00
_ISO_COLONLESS = re.compile(r"([+-]\d{2}):?(\d{2})$")  # +0900 / +09:00 正規化


def _parse_atom_datetime(s: str) -> t.Optional[dt.datetime]:
    """Atom（RFC3339/ISO8601）文字列を datetime へ。TZ無はUTC扱い。"""
    if not s:
        return None
    s2 = _ISO_Z_RE.sub("+00:00", s.strip())
    m = _ISO_COLONLESS.search(s2)
    if m and ":" not in m.group(0):
        s2 = s2[: m.start()] + f"{m.group(1)}:{m.group(2)}"
    try:
        dtobj = dt.datetime.fromisoformat(s2)
        if dtobj.tzinfo is None:
            dtobj = dtobj.replace(tzinfo=dt.timezone.utc)
        return dtobj
    except Exception:
        return None


def _parse_rss_datetime(s: str) -> t.Optional[dt.datetime]:
    """RSS（RFC2822）文字列を datetime へ。TZ無はUTC扱い。"""
    try:
        d = email.utils.parsedate_to_datetime(s)
        if d is None:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d
    except Exception:
        return None


def _to_target(dtobj: dt.datetime, target: str, tz: dt.tzinfo) -> dt.datetime:
    """target: 'utc' → UTCへ, 'local' → tzへ。"""
    if target == "local":
        return dtobj.astimezone(tz)
    return dtobj.astimezone(dt.timezone.utc)


def _fmt_atom(dtobj: dt.datetime, target: str) -> str:
    """Atom出力: target='utc' は 'Z'、'local' はオフセット付き。"""
    if target == "utc":
        return dtobj.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return dtobj.isoformat()


def _fmt_rss(dtobj: dt.datetime, target: str) -> str:
    """RSS出力: RFC2822。targetに応じてUTC/ローカル。"""
    if target == "utc":
        return rfc2822_utc(dtobj)
    return rfc2822_local(dtobj)


def rewrite_feed_datetimes(xml_bytes: bytes, target: str, tz: dt.tzinfo) -> bytes:
    """
    RSS/Atom の主要日時フィールドを target('utc'/'local') に合わせて一括変換。
    RSS: channel/lastBuildDate, item/pubDate
    Atom: feed/updated, entry/updated, entry/published
    """
    text = xml_bytes.decode("utf-8", errors="ignore")
    sample = text[:4096].lower()
    is_rss = "<rss" in sample
    is_atom = "<feed" in sample and "http://www.w3.org/2005/atom" in sample
    if not (is_rss or is_atom):
        return xml_bytes
    try:
        root = ET.fromstring(text)
    except Exception:
        return xml_bytes

    changed = False
    if is_rss:
        ch = root.find("channel")
        if ch is not None:
            el = ch.find("lastBuildDate")
            if el is not None and el.text:
                d = _parse_rss_datetime(el.text)
                if d:
                    el.text = _fmt_rss(_to_target(d, target, tz), target)
                    changed = True
            for it in ch.findall("item"):
                el = it.find("pubDate")
                if el is not None and el.text:
                    d = _parse_rss_datetime(el.text)
                    if d:
                        el.text = _fmt_rss(_to_target(d, target, tz), target)
                        changed = True
    else:
        ns = {"a": "http://www.w3.org/2005/atom"}
        el = root.find("a:updated", ns)
        if el is not None and el.text:
            d = _parse_atom_datetime(el.text)
            if d:
                el.text = _fmt_atom(_to_target(d, target, tz), target)
                changed = True
        for entry in root.findall("a:entry", ns):
            for tag in ("updated", "published"):
                el = entry.find(f"a:{tag}", ns)
                if el is not None and el.text:
                    d = _parse_atom_datetime(el.text)
                    if d:
                        el.text = _fmt_atom(_to_target(d, target, tz), target)
                        changed = True

    if not changed:
        return xml_bytes
    try:
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)
    except Exception:
        return xml_bytes


# ====== HTML → リンク抽出 ======
class LinkCollector(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[tuple[str, str]] = []
        self._capture_text = False
        self._current_href: str | None = None
        self._current_text_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = None
            for k, v in attrs:
                if k.lower() == "href":
                    href = v
                    break
            if href and not href.lower().startswith("javascript:"):
                self._current_href = urllib.parse.urljoin(self.base_url, href)
                self._current_text_parts = []
                self._capture_text = True

    def handle_data(self, data):
        if self._capture_text:
            self._current_text_parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._capture_text:
            text = " ".join(p.strip() for p in self._current_text_parts).strip()
            href = self._current_href
            if href:
                self.links.append((href, text if text else href))
            self._capture_text = False
            self._current_href = None
            self._current_text_parts = []


def extract_links(
    html_bytes: bytes,
    base_url: str,
    limit: t.Optional[int] = 20,
    include: t.Optional[list[re.Pattern[str]]] = None,
    exclude: t.Optional[list[re.Pattern[str]]] = None,
) -> list[dict[str, str]]:
    """汎用のリンク抽出（title=アンカーテキスト、link=href）"""
    text = html_bytes.decode("utf-8", errors="ignore")
    parser = LinkCollector(base_url)
    parser.feed(text)

    seen = set()
    items: list[dict[str, str]] = []
    for href, title in parser.links:
        key = href.split("#")[0]
        if key in seen:
            continue
        if include and not any(p.search(href) for p in include):
            continue
        if exclude and any(p.search(href) for p in exclude):
            continue
        seen.add(key)
        items.append({"link": href, "title": title})
        try:
            lim = int(limit) if limit is not None else None
        except Exception:
            lim = None
        if lim is not None and lim > 0 and len(items) >= lim:
            break
    return items


# ====== GitHub Contents API ======
class GitHubClient:
    def __init__(self, token: str, repo: str, branch: str = "main"):
        self.token = token
        self.repo = repo
        self.branch = branch

    def _request(
        self,
        method: str,
        url: str,
        data: t.Optional[bytes] = None,
        ok_404: bool = False,
        extra_headers: t.Optional[dict[str, str]] = None,
    ) -> dict:
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"token {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", UA)
        if extra_headers:
            for k, v in extra_headers.items():
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
                return {} if not body else json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if ok_404 and e.code == 404:
                return {"_status": 404}
            try:
                err = e.read().decode("utf-8", errors="ignore")
                print(f"[GitHub API ERROR] {e.code} {e.reason}: {err}", file=sys.stderr)
            except Exception:
                pass
            raise

    def _contents_url(self, path: str, ref: t.Optional[str] = None, include_ref: bool = True) -> str:
        path_enc = urllib.parse.quote(path)
        url = f"{GITHUB_API}/repos/{self.repo}/contents/{path_enc}"
        if include_ref and (ref or self.branch):
            url += f"?ref={urllib.parse.quote(ref or self.branch)}"
        return url

    def get_file(self, path: str) -> tuple[str | None, bytes | None]:
        data = self._request("GET", self._contents_url(path, include_ref=True), ok_404=True)
        if data.get("_status") == 404:
            return None, None
        content_b64 = data.get("content")
        if content_b64 is None:
            return data.get("sha"), None
        return data.get("sha"), base64.b64decode(content_b64)

    def put_file(self, path: str, content_bytes: bytes, message: str, sha: str | None = None) -> dict:
        payload = {
            "message": message,
            "content": base64.b64encode(content_bytes).decode("ascii"),
            "branch": self.branch,
        }
        if sha:
            payload["sha"] = sha
        return self._request(
            "PUT",
            self._contents_url(path, include_ref=False),
            data=json.dumps(payload).encode("utf-8"),
        )


# ====== RSS 生成 ======
def build_rss_xml(
    site_name: str,
    site_url: str,
    items: list[dict[str, str]],
    build_dt: dt.datetime,
    rss_time_tz: str = "utc",  # "utc" | "local"
) -> bytes:
    """items: {title, link, description?, pubDate?, guid?} を想定。descriptionがあれば出力。"""
    def esc(s: str) -> str:
        return html.escape(s, quote=True)

    to_rfc = rfc2822_local if rss_time_tz.lower() == "local" else rfc2822_utc

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "  <channel>",
        f"    <title>{esc(site_name)}</title>",
        f"    <link>{esc(site_url)}</link>",
        "    <description>Auto-generated feed</description>",
        f"    <lastBuildDate>{to_rfc(build_dt)}</lastBuildDate>",
        "    <generator>SiteRSSBot</generator>",
    ]

    for it in items:
        link_raw = it.get("link", "")
        link = esc(link_raw)
        title = esc(it.get("title", link_raw))

        guid_raw = it.get("guid") or link_raw
        guid = esc(guid_raw)

        desc_raw = it.get("description") or it.get("content") or it.get("price") or ""
        desc = esc(desc_raw)

        if it.get("pubDate"):
            pub = esc(str(it["pubDate"]))
        else:
            pub = to_rfc(build_dt)

        parts += [
            "    <item>",
            f"      <title>{title}</title>",
            f"      <link>{link}</link>",
            f'      <guid isPermaLink="true">{guid}</guid>',
            f"      <pubDate>{pub}</pubDate>",
        ]
        if desc:
            parts += [f"      <description>{desc}</description>"]
        parts += ["    </item>"]

    parts += ["  </channel>", "</rss>"]
    return "\n".join(parts).encode("utf-8")


# ====== フィード判定 ======
def sniff_feed_type(content_bytes: bytes, headers: dict) -> t.Optional[str]:
    ct = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
    if "rss+xml" in ct:
        return "rss"
    if "atom+xml" in ct:
        return "atom"
    sample = content_bytes[:4096].decode("utf-8", errors="ignore").lower()
    if "<rss" in sample:
        return "rss"
    if "<feed" in sample:
        return "atom"
    return None


def sniff_json_type(content_bytes: bytes, headers: dict) -> t.Optional[str]:
    ct = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
    if "application/json" in ct or "application/vnd.github+json" in ct:
        return "json"
    sample = content_bytes[:64].lstrip()
    if sample.startswith(b"{") or sample.startswith(b"["):
        return "json"
    return None


# ====== GitHub Search JSON → archive 蓄積 → RSS items 生成 ======
def github_search_json_update_archive(
    body: bytes,
    tz: dt.tzinfo,
    rss_time_tz: str,
    site_state: dict,
    site_name: str,
    include: t.Optional[list[re.Pattern[str]]] = None,
    exclude: t.Optional[list[re.Pattern[str]]] = None,
    max_keep: int = 2000,
) -> tuple[list[dict[str, str]], int, bool]:
    """
    GitHub Search API (/search/issues) のJSONから:
      - state.json の site_state[site_name]["archive"] に既知PRを蓄積
      - RSS表示用 items（archiveから生成、重複なし）を返す

    返り値: (rss_items, new_count, archive_changed)
    """
    try:
        data = json.loads(body.decode("utf-8", errors="ignore"))
    except Exception:
        return [], 0, False

    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return [], 0, False

    parsed_now: list[tuple[dt.datetime, dict[str, str]]] = []
    newest_dt: dt.datetime | None = None

    for it in data["items"]:
        if not isinstance(it, dict):
            continue
        if "pull_request" not in it:
            continue

        created_at = it.get("created_at") or ""
        html_url = it.get("html_url") or ""
        title = (it.get("title") or "").strip()
        user = (it.get("user") or {}).get("login") if isinstance(it.get("user"), dict) else None

        if not html_url:
            continue

        if include and not any(p.search(html_url) for p in include):
            continue
        if exclude and any(p.search(html_url) for p in exclude):
            continue

        d = _parse_atom_datetime(created_at)
        if not d:
            continue

        if newest_dt is None or d > newest_dt:
            newest_dt = d

        target = "local" if rss_time_tz == "local" else "utc"
        d2 = _to_target(d, target, tz)
        pub = _fmt_rss(d2, target)

        desc_parts: list[str] = []
        if user:
            desc_parts.append(f"author: {user}")
        try:
            parts = urllib.parse.urlparse(html_url).path.strip("/").split("/")
            if len(parts) >= 4:
                desc_parts.append(f"{parts[0]}/{parts[1]}#{parts[3]}")
        except Exception:
            pass

        created_iso = d.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")

        parsed_now.append(
            (d, {
                "title": title or html_url,
                "link": html_url,
                "guid": html_url,
                "pubDate": pub,
                "description": " / ".join(desc_parts),
                "_created_at": created_iso,  # ソート用
            })
        )

    st = site_state.setdefault(site_name, {})
    archive = st.get("archive")
    if not isinstance(archive, dict):
        archive = {}
        st["archive"] = archive

    changed = False
    new_count = 0

    for _d, item in parsed_now:
        key = item.get("link", "")
        if not key:
            continue
        if key not in archive:
            archive[key] = item
            changed = True
            new_count += 1

    if newest_dt is not None:
        newest_s = newest_dt.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        if st.get("last_created_at") != newest_s:
            st["last_created_at"] = newest_s
            # ここで changed=True にすると「順序だけ変わる/同じ」でもコミットが起き得るので、
            # “新規追加が無い限りRSS更新しない” の方針なら changed にしないのが無難。

    # archive の肥大化を防ぐ（新しい順に max_keep）
    if max_keep > 0 and len(archive) > max_keep:
        def key_dt(v: dict) -> dt.datetime:
            d = _parse_atom_datetime(v.get("_created_at", ""))
            return d or dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)

        keep_keys = [k for k, _v in sorted(archive.items(), key=lambda kv: key_dt(kv[1]), reverse=True)[:max_keep]]
        keep_set = set(keep_keys)
        for k in list(archive.keys()):
            if k not in keep_set:
                archive.pop(k, None)
                changed = True

    def created_dt_from_item(v: dict) -> dt.datetime:
        d = _parse_atom_datetime(v.get("_created_at", ""))
        return d or dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)

    rss_items: list[dict[str, str]] = []
    for _k, v in sorted(archive.items(), key=lambda kv: created_dt_from_item(kv[1]), reverse=True):
        rss_items.append({kk: vv for kk, vv in v.items() if not kk.startswith("_")})

    return rss_items, new_count, changed


# ====== Runner ======
class Runner:
    def __init__(self, cfg: dict):
        gh = cfg.get("github", {})
        token, repo, branch = gh.get("token"), gh.get("repo"), gh.get("branch", "main")
        if not token or not repo:
            raise SystemExit("config.github.token / config.github.repo を設定してください")
        self.gh = GitHubClient(token=token, repo=repo, branch=branch)

        self.ua = cfg.get("user_agent", UA)
        self.poll_interval = int(cfg.get("poll_interval_sec", 600))
        self.rss_items_default = int(cfg.get("rss_items_default", 20))
        self.site_interval_default = int(cfg.get("site_interval_default", 600))
        self.readme_path = cfg.get("readme_path", "Read.me")
        self.state_path = cfg.get("state_path", "state.json")
        self.daily_flag_path = cfg.get("daily_flag_path", "00.txtt")
        self.site_manifest_path = cfg.get("site_manifest_path", "Site.json")
        self.site_manifest_ttl = int(cfg.get("site_manifest_ttl_sec", 43200))  # 12h

    # ---- HTTP（リダイレクト追従 & UA/Accept）----
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

    # ---- state.json ----
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

    # ---- Site.json 取得（12hキャッシュ）----
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
                    raise ValueError('Site.json の形式が不正です（配列または {"sites": [...]} を期待）')
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

    # ---- Read.me 追記（サイトTZ表示）----
    def append_readme_updates(self, updates: list[tuple[str, dt.datetime]]):
        if not updates:
            return
        cur_sha, content = self.gh.get_file(self.readme_path)
        body = "" if content is None else content.decode("utf-8", errors="ignore")
        header = "## 更新ログ\n"
        if header not in body:
            body = header + "\n" + body
        lines = [l for l in body.splitlines()]
        insert_idx = 1
        new_lines = [f"- {fmt_human(when)} — {name}" for name, when in updates]
        lines[insert_idx:insert_idx] = new_lines + [""]
        new_body = "\n".join(lines)
        self.gh.put_file(
            self.readme_path,
            new_body.encode("utf-8"),
            message=f"chore: update Read.me ({len(updates)} site(s))",
            sha=cur_sha,
        )

    # ---- 00.txtt（日次：JST）----
    def update_daily_flag(self):
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

    # ====== Booth 専用：商品ページからタイトル/価格を抽出 ======
    @staticmethod
    def _parse_meta(text: str, name: str, attr: str = "property") -> t.Optional[str]:
        pat = re.compile(
            rf'<meta\s+(?:[^>]*?\s)?{attr}\s*=\s*["\']{re.escape(name)}["\']\s+[^>]*?content\s*=\s*["\'](.*?)["\']',
            re.IGNORECASE | re.DOTALL,
        )
        m = pat.search(text)
        return html.unescape(m.group(1).strip()) if m else None

    @staticmethod
    def _parse_title_fallback(text: str) -> t.Optional[str]:
        m = re.search(r"<title>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        if m:
            return html.unescape(m.group(1).strip())
        return None

    @staticmethod
    def _parse_price_guess(text: str) -> t.Optional[str]:
        for pat in (r"¥\s?\d[\d,]*", r"\d[\d,]*\s*円"):
            m = re.search(pat, text)
            if m:
                return m.group(0).replace(" ", "")
        amt = Runner._parse_meta(text, "product:price:amount")
        cur = Runner._parse_meta(text, "product:price:currency")
        if amt:
            if cur and cur.upper() in ("JPY",):
                return f"¥{amt}"
            return amt
        return None

    def _scrape_booth_item(self, url: str, ua: str) -> tuple[str, str]:
        try:
            body, _ = self.fetch(url, ua=ua)
        except Exception:
            return "", ""
        text = body.decode("utf-8", errors="ignore")
        title = (
            self._parse_meta(text, "og:title", "property")
            or self._parse_meta(text, "twitter:title", "name")
            or self._parse_title_fallback(text)
            or ""
        ).strip()
        price = (
            self._parse_meta(text, "product:price:amount", "property")
            or self._parse_price_guess(text)
            or ""
        ).strip()
        return title, price

    def _generate_from_html_general(
        self,
        body: bytes,
        base_url: str,
        limit_arg: t.Optional[int],
        inc_patterns: list[re.Pattern[str]],
        exc_patterns: list[re.Pattern[str]],
    ) -> list[dict[str, str]]:
        items = extract_links(
            body,
            base_url=base_url,
            limit=limit_arg,
            include=inc_patterns if inc_patterns else None,
            exclude=exc_patterns if exc_patterns else None,
        )
        return [{"title": it["title"], "link": it["link"]} for it in items]

    def _generate_from_html_booth(
        self,
        body: bytes,
        base_url: str,
        limit_arg: t.Optional[int],
        ua: str,
        inc_patterns: list[re.Pattern[str]],
        exc_patterns: list[re.Pattern[str]],
    ) -> list[dict[str, str]]:
        default_inc = [re.compile(r"^https?://(?:www\.)?booth\.pm/(?:ja/)?items/\d+$")]
        use_inc = inc_patterns if inc_patterns else default_inc
        links = extract_links(
            body,
            base_url=base_url,
            limit=limit_arg,
            include=use_inc,
            exclude=exc_patterns if exc_patterns else None,
        )
        out: list[dict[str, str]] = []
        for it in links:
            link = it["link"]
            title, price = self._scrape_booth_item(link, ua=ua)
            title_final = title or it.get("title") or link
            desc = price or "価格不明"
            out.append({"title": title_final, "link": link, "description": desc})
        return out

    # ---- 1巡回 ----
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
            interval = site.get("interval_sec", self.site_interval_default)
            try:
                interval = int(interval)
            except Exception:
                interval = self.site_interval_default

            last_checked_s = (site_state.get(name) or {}).get("last_checked")
            if last_checked_s:
                try:
                    last_checked = dt.datetime.fromisoformat(last_checked_s)
                    if last_checked.tzinfo is None:
                        last_checked = last_checked.replace(tzinfo=tz)
                except Exception:
                    last_checked = None
            else:
                last_checked = None

            now_local = dt.datetime.now(tz=tz)
            if last_checked:
                elapsed = (now_local - last_checked).total_seconds()
                if elapsed < max(0, interval):
                    remaining = int(max(0, interval - elapsed))
                    print(f"[info] not due: {name} (next in ~{remaining}s)")
                    continue

            # 取得
            try:
                site_ua = site.get("user_agent") or self.ua
                body, headers = self.fetch(url, ua=site_ua)
            except Exception as e:
                print(f"[error] fetch failed for {name}: {e}", file=sys.stderr)
                site_state.setdefault(name, {})["last_checked"] = now_local.isoformat()
                continue

            try:
                rss_path = f"RSS/{safe_filename(name)}.rss"
                rss_time_tz = str(site.get("rss_time_tz", "utc")).lower()  # "utc" or "local"

                # ★ JSON(GitHub Search) は hash比較を使わず、archive基準で更新
                if sniff_json_type(body, headers) == "json":
                    inc_patterns = [re.compile(p) for p in site.get("include_regex", []) if p]
                    exc_patterns = [re.compile(p) for p in site.get("exclude_regex", []) if p]
                    max_keep = int(site.get("archive_max_items", 2000))

                    rss_items, new_count, arch_changed = github_search_json_update_archive(
                        body=body,
                        tz=tz,
                        rss_time_tz=rss_time_tz,
                        site_state=site_state,
                        site_name=name,
                        include=inc_patterns if inc_patterns else None,
                        exclude=exc_patterns if exc_patterns else None,
                        max_keep=max_keep,
                    )

                    st = site_state.setdefault(name, {})
                    st["last_checked"] = now_local.isoformat()

                    if arch_changed:
                        limit = site.get("rss_items", self.rss_items_default)
                        try:
                            limit = int(limit) if limit is not None else 0
                        except Exception:
                            limit = 0
                        out_items = rss_items[:limit] if limit > 0 else rss_items

                        content_to_save = build_rss_xml(
                            name, url, out_items, now_local, rss_time_tz=rss_time_tz
                        )
                        sha, _ = self.gh.get_file(rss_path)
                        self.gh.put_file(
                            rss_path,
                            content_to_save,
                            message=f"feat: update RSS(json) for {name} at {fmt_human(now_local)}",
                            sha=sha,
                        )
                        st["last_updated"] = now_local.isoformat()
                        updates.append((name, now_local))
                        print(f"[info] updated: {name} (json) new={new_count} total={len(rss_items)}")
                    else:
                        print(f"[info] no new items (json): {name}")
                    continue  # ★ 次サイトへ

                # ★ ここから RSS/Atom/HTML（従来ルート）
                page_hash = sha256_hex(body)
                prev_hash = (site_state.get(name) or {}).get("hash")
                changed = (prev_hash != page_hash)
                if not changed:
                    print(f"[info] no change: {name}")
                    site_state.setdefault(name, {})["last_checked"] = now_local.isoformat()
                    continue

                feed_type = sniff_feed_type(body, headers)

                if feed_type:
                    target = "local" if rss_time_tz == "local" else "utc"
                    content_to_save = rewrite_feed_datetimes(body, target=target, tz=tz)
                    commit_msg = f"feat: pass-through feed ({feed_type}) for {name} at {fmt_human(now_local)}"
                else:
                    limit = site.get("rss_items", self.rss_items_default)
                    try:
                        limit = int(limit) if limit is not None else 0
                    except Exception:
                        limit = 0
                    limit_arg: t.Optional[int] = None if limit <= 0 else limit
                    inc_patterns = [re.compile(p) for p in site.get("include_regex", []) if p]
                    exc_patterns = [re.compile(p) for p in site.get("exclude_regex", []) if p]

                    host = urllib.parse.urlparse(url).netloc.lower()
                    if host.endswith("booth.pm"):
                        items = self._generate_from_html_booth(
                            body,
                            base_url=url,
                            limit_arg=limit_arg,
                            ua=site_ua,
                            inc_patterns=inc_patterns,
                            exc_patterns=exc_patterns,
                        )
                    else:
                        items = self._generate_from_html_general(
                            body,
                            base_url=url,
                            limit_arg=limit_arg,
                            inc_patterns=inc_patterns,
                            exc_patterns=exc_patterns,
                        )

                    content_to_save = build_rss_xml(
                        name, url, items, now_local, rss_time_tz=rss_time_tz
                    )
                    commit_msg = f"feat: update RSS for {name} at {fmt_human(now_local)}"

                # 保存
                try:
                    sha, _ = self.gh.get_file(rss_path)
                    self.gh.put_file(rss_path, content_to_save, message=commit_msg, sha=sha)
                except Exception as e:
                    print(f"[error] save failed for {name}: {e}", file=sys.stderr)
                    site_state.setdefault(name, {})["last_checked"] = now_local.isoformat()
                    continue

                st = site_state.setdefault(name, {})
                st["hash"] = page_hash
                st["last_updated"] = now_local.isoformat()
                st["last_checked"] = now_local.isoformat()

                updates.append((name, now_local))
                print(f"[info] updated: {name} ({'pass-through' if feed_type else 'generated'})")

            except Exception as e:
                print(f"[error] process failed for {name}: {e}", file=sys.stderr)
                site_state.setdefault(name, {})["last_checked"] = now_local.isoformat()
                continue

        # 後処理
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

    # ---- 実行ループ ----
    def run(self, watch: bool = True):
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


# ====== エントリポイント ======
def load_config(path: str = "config.json") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(
        description="Generate RSS/Atom feeds and push to GitHub (with timezone rewriting & Booth price)"
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--watch", dest="watch", action="store_true", help="continuous mode (default)")
    g.add_argument("--once", dest="watch", action="store_false", help="run one cycle then exit")
    p.set_defaults(watch=True)
    p.add_argument("--config", default="config.json", help="path to config.json")
    args = p.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except Exception as e:
        print(f"[fatal] failed to load config: {e}", file=sys.stderr)
        return 0

    try:
        Runner(cfg).run(watch=args.watch)
    except Exception as e:
        print(f"[fatal] runner error: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
