from __future__ import annotations

import base64
import datetime as dt
import email.utils
import hashlib
import html
import json
import re
import sys
import typing as t
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
import xml.etree.ElementTree as ET

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python builds without zoneinfo are rare.
    ZoneInfo = None  # type: ignore


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
    return out.strip() or "site"


def parse_tz(spec: t.Optional[t.Union[str, int, float]]) -> dt.tzinfo:
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

    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", s)
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


_ISO_Z_RE = re.compile(r"Z$")
_ISO_COLONLESS = re.compile(r"([+-]\d{2}):?(\d{2})$")


def parse_atom_datetime(s: str) -> t.Optional[dt.datetime]:
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


def parse_rss_datetime(s: str) -> t.Optional[dt.datetime]:
    try:
        d = email.utils.parsedate_to_datetime(s)
        if d is None:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d
    except Exception:
        return None


def to_target(dtobj: dt.datetime, target: str, tz: dt.tzinfo) -> dt.datetime:
    if target == "local":
        return dtobj.astimezone(tz)
    return dtobj.astimezone(dt.timezone.utc)


def fmt_atom(dtobj: dt.datetime, target: str) -> str:
    if target == "utc":
        return dtobj.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return dtobj.isoformat()


def fmt_rss(dtobj: dt.datetime, target: str) -> str:
    if target == "utc":
        return rfc2822_utc(dtobj)
    return rfc2822_local(dtobj)


def rewrite_feed_datetimes(xml_bytes: bytes, target: str, tz: dt.tzinfo) -> bytes:
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
        channel = root.find("channel")
        if channel is not None:
            el = channel.find("lastBuildDate")
            if el is not None and el.text:
                d = parse_rss_datetime(el.text)
                if d:
                    el.text = fmt_rss(to_target(d, target, tz), target)
                    changed = True
            for item in channel.findall("item"):
                el = item.find("pubDate")
                if el is not None and el.text:
                    d = parse_rss_datetime(el.text)
                    if d:
                        el.text = fmt_rss(to_target(d, target, tz), target)
                        changed = True
    else:
        ns = {"a": "http://www.w3.org/2005/atom"}
        el = root.find("a:updated", ns)
        if el is not None and el.text:
            d = parse_atom_datetime(el.text)
            if d:
                el.text = fmt_atom(to_target(d, target, tz), target)
                changed = True
        for entry in root.findall("a:entry", ns):
            for tag in ("updated", "published"):
                el = entry.find(f"a:{tag}", ns)
                if el is not None and el.text:
                    d = parse_atom_datetime(el.text)
                    if d:
                        el.text = fmt_atom(to_target(d, target, tz), target)
                        changed = True

    if not changed:
        return xml_bytes
    try:
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)
    except Exception:
        return xml_bytes


class LinkCollector(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[tuple[str, str]] = []
        self._capture_text = False
        self._current_href: t.Optional[str] = None
        self._current_text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, t.Optional[str]]]) -> None:
        if tag.lower() != "a":
            return
        href = None
        for key, value in attrs:
            if key.lower() == "href":
                href = value
                break
        if href and not href.lower().startswith("javascript:"):
            self._current_href = urllib.parse.urljoin(self.base_url, href)
            self._current_text_parts = []
            self._capture_text = True

    def handle_data(self, data: str) -> None:
        if self._capture_text:
            self._current_text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
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
    text = html_bytes.decode("utf-8", errors="ignore")
    parser = LinkCollector(base_url)
    parser.feed(text)

    seen: set[str] = set()
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
            for key, value in extra_headers.items():
                req.add_header(key, value)
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

    def _contents_url(
        self,
        path: str,
        ref: t.Optional[str] = None,
        include_ref: bool = True,
    ) -> str:
        path_enc = urllib.parse.quote(path)
        url = f"{GITHUB_API}/repos/{self.repo}/contents/{path_enc}"
        if include_ref and (ref or self.branch):
            url += f"?ref={urllib.parse.quote(ref or self.branch)}"
        return url

    def get_file(self, path: str) -> tuple[t.Optional[str], t.Optional[bytes]]:
        data = self._request("GET", self._contents_url(path, include_ref=True), ok_404=True)
        if data.get("_status") == 404:
            return None, None
        content_b64 = data.get("content")
        if content_b64 is None:
            return data.get("sha"), None
        return data.get("sha"), base64.b64decode(content_b64)

    def put_file(
        self,
        path: str,
        content_bytes: bytes,
        message: str,
        sha: t.Optional[str] = None,
    ) -> dict:
        payload: dict[str, t.Any] = {
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


def build_rss_xml(
    site_name: str,
    site_url: str,
    items: list[dict[str, str]],
    build_dt: dt.datetime,
    rss_time_tz: str = "utc",
) -> bytes:
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

    for item in items:
        link_raw = item.get("link", "")
        link = esc(link_raw)
        title = esc(item.get("title", link_raw))
        guid_raw = item.get("guid") or link_raw
        guid = esc(guid_raw)
        desc_raw = item.get("description") or item.get("content") or item.get("price") or ""
        desc = esc(desc_raw)
        pub = esc(str(item["pubDate"])) if item.get("pubDate") else to_rfc(build_dt)

        parts += [
            "    <item>",
            f"      <title>{title}</title>",
            f"      <link>{link}</link>",
            f'      <guid isPermaLink="true">{guid}</guid>',
            f"      <pubDate>{pub}</pubDate>",
        ]
        if desc:
            parts.append(f"      <description>{desc}</description>")
        parts.append("    </item>")

    parts += ["  </channel>", "</rss>"]
    return "\n".join(parts).encode("utf-8")


def sniff_feed_type(content_bytes: bytes, headers: dict) -> t.Optional[str]:
    content_type = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
    if "rss+xml" in content_type:
        return "rss"
    if "atom+xml" in content_type:
        return "atom"
    sample = content_bytes[:4096].decode("utf-8", errors="ignore").lower()
    if "<rss" in sample:
        return "rss"
    if "<feed" in sample:
        return "atom"
    return None


def sniff_json_type(content_bytes: bytes, headers: dict) -> t.Optional[str]:
    content_type = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
    if "application/json" in content_type or "application/vnd.github+json" in content_type:
        return "json"
    sample = content_bytes[:64].lstrip()
    if sample.startswith(b"{") or sample.startswith(b"["):
        return "json"
    return None
