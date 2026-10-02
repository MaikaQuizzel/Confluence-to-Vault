#!/usr/bin/env python3
"""
confluence2obsidian.py - export Confluence Server / Data Center pages to Obsidian.

What it does
  * Fetches pages through the REST API in "storage" format (macros still structured)
  * Converts them to Obsidian-flavoured Markdown:
      - code / noformat macros      -> fenced code blocks (with language)
      - info / note / tip / warning -> Obsidian callouts  (> [!info] ...)
      - expand                      -> collapsible callout (> [!note]- ...)
      - tables                      -> Markdown tables (multi-line cells use <br>)
      - task lists                  -> - [ ] / - [x]
      - links between pages         -> [[Wikilinks]]   (also plain URLs to exported pages)
      - images / attachments        -> ![[attachments/<pageId>/file.png]]  (files downloaded)
  * Writes YAML front matter (title, source URL, space, version, labels as tags)
  * Reports anything it could not convert (unsupported macros, missing attachments, ...)

Setup
  pip install requests beautifulsoup4 markdownify

  export CONFLUENCE_BASE_URL="https://wiki.example.com"        # incl. context path, e.g. .../confluence
  export CONFLUENCE_TOKEN="<personal access token>"            # Profile > Personal Access Tokens (DC 7.9+)
  # or, for older Server versions:  CONFLUENCE_USER / CONFLUENCE_PASSWORD  (basic auth)

Usage
  python confluence2obsidian.py -o ~/Vault/Confluence  <url-or-id> [<url-or-id> ...]
  python confluence2obsidian.py -o ~/Vault/Confluence  -f pages.txt
  python confluence2obsidian.py -o ~/Vault/Confluence  --with-children <url-or-id>
  

  python confluence2obsidian.py --ca-bundle ~/ca-bundle.pem -o ~/Documents/"Obsidian Vault"/test --with-children <url-or-id>


  Accepted page references:
    https://host/pages/viewpage.action?pageId=12345
    https://host/spaces/KEY/pages/12345/Title
    https://host/display/KEY/Page+Title
    12345                                  (plain page id)

Tip: point -o at a folder INSIDE your vault so the ![[attachments/...]] embeds resolve.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString
from markdownify import markdownify as to_md

EXPAND = "body.storage,version,space,metadata.labels"
BR = "@@BR@@"  # temporary marker for <br> inside table cells

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
_BAD_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]')
_CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)

LANG_MAP = {
    "py": "python", "js": "javascript", "yml": "yaml", "c#": "csharp", "c++": "cpp",
    "none": "", "text": "", "plain": "", "sh": "bash", "shell": "bash", "ps": "powershell",
    "html/xml": "html", "actionscript3": "actionscript",
}
CALLOUTS = {"info": "info", "note": "note", "tip": "tip", "warning": "warning", "panel": "note"}
SILENT_MACROS = {"toc", "anchor", "style", "pagetree-search"}  # no Obsidian equivalent needed
EMOJI = {
    "smile": "🙂", "sad": "🙁", "cheeky": "😛", "laugh": "😄", "wink": "😉",
    "thumbs-up": "👍", "thumbs-down": "👎", "information": "ℹ️", "tick": "✅",
    "cross": "❌", "warning": "⚠️", "plus": "➕", "minus": "➖", "question": "❓",
    "light-on": "💡", "light-off": "💡", "yellow-star": "⭐", "red-star": "⭐",
    "green-star": "⭐", "blue-star": "⭐",
}


def safe_name(title: str) -> str:
    """File-name stem that Obsidian accepts and that is safe inside [[wikilinks]]."""
    s = _BAD_CHARS.sub("-", title)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s or "untitled"


def safe_filename(name: str) -> str:
    return _BAD_CHARS.sub("-", name)


def inline_code(line: str) -> str:
    if not line.strip():
        return ""
    if "`" in line:
        return f"`` {line} ``"
    return f"`{line}`"


# --------------------------------------------------------------------------- #
# Confluence REST client
# --------------------------------------------------------------------------- #
class Confluence:
    def __init__(self, base_url, token=None, user=None, password=None, ca_bundle=None):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        if token:
            self.s.headers["Authorization"] = f"Bearer {token}"
        elif user and password:
            self.s.auth = (user, password)
        if ca_bundle:
            self.s.verify = ca_bundle

    def get(self, path, **params):
        url = path if path.startswith("http") else self.base + path
        r = self.s.get(url, params=params or None, timeout=60)
        r.raise_for_status()
        return r

    def _paged(self, path, **params):
        start, limit = 0, 100
        while True:
            data = self.get(path, start=start, limit=limit, **params).json()
            yield from data.get("results", [])
            if "next" not in data.get("_links", {}) or not data.get("size"):
                return
            start += data["size"]

    def page(self, page_id):
        return self.get(f"/rest/api/content/{page_id}", expand=EXPAND).json()

    def find_page_id(self, space, title):
        res = self.get("/rest/api/content", spaceKey=space, title=title, type="page").json()["results"]
        if not res:
            raise LookupError(f"No page '{title}' in space '{space}'")
        return str(res[0]["id"])

    def children(self, page_id):
        return list(self._paged(f"/rest/api/content/{page_id}/child/page"))

    def attachments(self, page_id):
        return list(self._paged(f"/rest/api/content/{page_id}/child/attachment"))

    def download(self, rel_link, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.s.get(self.base + rel_link, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(65536):
                    fh.write(chunk)


def resolve_ref(cf: Confluence, ref: str) -> str:
    """Turn a URL / id into a page id."""
    ref = ref.strip()
    if ref.isdigit():
        return ref
    u = urlparse(ref)
    q = parse_qs(u.query)
    if "pageId" in q:
        return q["pageId"][0]
    m = re.search(r"/pages/(\d+)", u.path)
    if m:
        return m.group(1)
    m = re.search(r"/display/([^/]+)/([^/?#]+)", u.path)
    if m:
        return cf.find_page_id(m.group(1), unquote_plus(m.group(2)))
    raise ValueError(f"Cannot work out the page id from: {ref}\n"
                     "Use a viewpage.action?pageId=..., /pages/<id>/..., /display/KEY/Title URL or the numeric id.")


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Page:
    id: str
    title: str
    space: str
    version: int
    modified: str
    url: str
    labels: list
    storage: str
    stem: str = ""


def page_from_api(data: dict, base: str) -> Page:
    links = data.get("_links", {})
    return Page(
        id=str(data["id"]),
        title=data["title"],
        space=data.get("space", {}).get("key", ""),
        version=data.get("version", {}).get("number", 0),
        modified=data.get("version", {}).get("when", ""),
        url=links.get("base", base) + links.get("webui", ""),
        labels=[l["name"] for l in data.get("metadata", {}).get("labels", {}).get("results", [])],
        storage=data["body"]["storage"]["value"],
    )


def assign_stems(pages: dict) -> None:
    used = set()
    for p in pages.values():
        stem = safe_name(p.title)
        if stem.lower() in used:
            stem = f"{stem} ({p.space or p.id})"
        if stem.lower() in used:
            stem = f"{stem} {p.id}"
        used.add(stem.lower())
        p.stem = stem


# --------------------------------------------------------------------------- #
# Storage format -> Obsidian Markdown
# --------------------------------------------------------------------------- #
class Converter:
    def __init__(self, page: Page, pages: dict, base_url: str):
        self.page = page
        self.by_id = pages
        self.by_title = defaultdict(list)
        for p in pages.values():
            self.by_title[p.title.lower()].append(p)
        self.base = urlparse(base_url)
        self.blocks: list[str] = []
        self.attachments: set = set()   # (owner page id, filename) that must be downloaded
        self.unsupported: set = set()
        self.unresolved: set = set()    # wikilinks to pages that are not part of the export
        self.missing: set = set()       # attachments living on pages that are not exported
        self.soup = None

    # ---- public ---------------------------------------------------------- #
    def convert(self, storage: str) -> str:
        storage = storage.replace("&nbsp;", " ")
        storage = _CDATA.sub(lambda m: html.escape(m.group(1), quote=False), storage)
        self.soup = soup = BeautifulSoup(storage, "html.parser")

        self._inline_elements(soup)
        self._tables(soup)
        for macro in reversed(soup.find_all("ac:structured-macro")):  # innermost first
            self._macro(macro)
        for t in soup.find_all(lambda t: t.name and t.name.startswith("ri:")):
            t.decompose()
        for t in soup.find_all(lambda t: t.name and t.name.startswith("ac:")):
            t.unwrap()

        text = self._substitute(self._md(str(soup)))
        text = re.sub(r"\|[ \t]*(?:%s[ \t]*)+" % BR, "| ", text)          # leading <br> in a cell
        text = re.sub(r"(?:[ \t]*%s)+[ \t]*\|" % BR, " |", text)          # trailing <br> in a cell
        text = re.sub(r"(?:%s[ \t]*)+" % BR, "<br>", text)                 # collapse repeats
        text = text.replace("\xa0", " ")
        return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"

    # ---- plumbing -------------------------------------------------------- #
    @staticmethod
    def _md(html_str: str) -> str:
        return to_md(html_str, heading_style="ATX", bullets="-", escape_underscores=False)

    def _store(self, text: str) -> str:
        self.blocks.append(text)
        return f"@@B{len(self.blocks) - 1}@@"

    @staticmethod
    def _in_cell(node) -> bool:
        return node.find_parent(["td", "th"]) is not None

    def _inline(self, node, text: str):
        """Replace node by a placeholder for already-final inline Markdown."""
        if self._in_cell(node):
            text = text.replace("|", "\\|")
        node.replace_with(NavigableString(self._store(text)))

    def _emit_block(self, node, text: str):
        """Replace node by a placeholder for a multi-line Markdown block."""
        if self._in_cell(node):  # tables can't hold blocks: flatten to one line
            node.replace_with(NavigableString(self._store(text.replace("\n", BR).replace("|", "\\|"))))
        else:
            p = self.soup.new_tag("p")
            p.string = self._store(text)
            node.replace_with(p)

    _TOKEN = re.compile(r"@@B(\d+)@@")
    _BLOCK_LINE = re.compile(r"^(?P<prefix>[ \t]*(?:>[ \t]*)*(?:(?:[-*+]|\d+\.)[ \t]+)?)@@B(?P<n>\d+)@@[ \t]*$")

    def _substitute(self, text: str) -> str:
        """Put stored blocks back, keeping list indentation / blockquote prefixes."""
        for _ in range(20):  # blocks may contain further placeholders (nested macros)
            if "@@B" not in text:
                break
            out = []
            for line in text.split("\n"):
                m = self._BLOCK_LINE.match(line)
                if m:
                    prefix = m["prefix"]
                    cont = re.sub(r"(?:[-*+]|\d+\.)[ \t]+", lambda x: " " * len(x.group(0)), prefix)
                    lines = self.blocks[int(m["n"])].split("\n")
                    out.append((prefix + lines[0]).rstrip())
                    out.extend((cont + l).rstrip() for l in lines[1:])
                else:
                    out.append(self._TOKEN.sub(lambda x: self.blocks[int(x.group(1))].replace("\n", " "), line))
            text = "\n".join(out)
        return text

    # ---- page lookup ----------------------------------------------------- #
    def _lookup(self, space, title):
        target_space = (space or self.page.space).lower()
        for p in self.by_title.get((title or "").lower(), []):
            if p.space.lower() == target_space:
                return p
        return None

    def _page_from_url(self, u):
        q = parse_qs(u.query)
        if "pageId" in q:
            return self.by_id.get(q["pageId"][0])
        m = re.search(r"/pages/(\d+)", u.path)
        if m:
            return self.by_id.get(m.group(1))
        m = re.search(r"/display/([^/]+)/([^/?#]+)", u.path)
        if m:
            return self._lookup(m.group(1), unquote_plus(m.group(2)))
        return None

    @staticmethod
    def _wikilink(stem: str, text: str | None) -> str:
        text = re.sub(r"\s+", " ", (text or "").replace("|", " ")).strip()
        return f"[[{stem}|{text}]]" if text and text != stem else f"[[{stem}]]"

    def _attachment_path(self, att) -> str:
        filename = att.get("ri:filename", "")
        owner = self.page.id
        other = att.find("ri:page")
        if other is not None:
            target = self._lookup(other.get("ri:space-key"), other.get("ri:content-title"))
            if target is None:
                self.missing.add(filename)
                return safe_filename(filename)
            owner = target.id
        self.attachments.add((owner, filename))
        return f"attachments/{owner}/{safe_filename(filename)}"

    # ---- inline-ish Confluence elements ---------------------------------- #
    def _inline_elements(self, soup):
        for t in soup.find_all("ac:image"):
            self._image(t)
        for t in soup.find_all("ac:link"):
            self._link(t)
        for t in soup.find_all("ac:emoticon"):
            t.replace_with(t.get("ac:emoji-fallback") or EMOJI.get(t.get("ac:name"), ""))
        for t in soup.find_all("ac:task-list"):
            self._tasks(t)
        for t in soup.find_all("a", href=True):
            self._anchor(t)
        for t in soup.find_all("time"):
            t.replace_with(t.get("datetime") or t.get_text())
        for t in soup.find_all(["ac:placeholder", "ac:inline-comment-marker-ref"]):
            t.decompose()

    def _image(self, t):
        att, url = t.find("ri:attachment"), t.find("ri:url")
        width = t.get("ac:width")
        if att is not None:
            ref = self._attachment_path(att)
            out = f"![[{ref}|{width}]]" if width else f"![[{ref}]]"
        elif url is not None:
            out = f"![{t.get('ac:alt', '')}]({url.get('ri:value', '')})"
        else:
            out = ""
        self._inline(t, out)

    def _link(self, t):
        text = None
        for name in ("ac:plain-text-link-body", "ac:link-body"):
            b = t.find(name)
            if b is not None:
                text = b.get_text(" ", strip=True)
                break
        att, page, user = t.find("ri:attachment"), t.find("ri:page"), t.find("ri:user")
        if att is not None:
            ref = self._attachment_path(att)
            out = f"[[{ref}|{text}]]" if text else f"[[{ref}]]"
        elif page is not None:
            title = page.get("ri:content-title", "")
            target = self._lookup(page.get("ri:space-key"), title)
            if target is None:
                self.unresolved.add(title)
            out = self._wikilink(target.stem if target else safe_name(title), text or title)
        elif user is not None:
            out = "@" + (user.get("ri:username") or "user")
        else:
            out = text or ""
        self._inline(t, out)

    def _anchor(self, a):
        u = urlparse(a["href"])
        if u.netloc and u.netloc != self.base.netloc:
            return
        target = self._page_from_url(u)
        if target is not None:
            self._inline(a, self._wikilink(target.stem, a.get_text(" ", strip=True)))

    def _tasks(self, tl):
        ul = self.soup.new_tag("ul")
        for task in tl.find_all("ac:task", recursive=False):
            status = task.find("ac:task-status")
            done = status is not None and status.get_text(strip=True) == "complete"
            body = task.find("ac:task-body")
            li = self.soup.new_tag("li")
            li.append(NavigableString(self._store("[x]" if done else "[ ]") + " "))
            if body is not None:
                for child in list(body.contents):
                    li.append(child.extract())
            ul.append(li)
        tl.replace_with(ul)

    # ---- tables ---------------------------------------------------------- #
    def _tables(self, soup):
        for cell in soup.find_all(["td", "th"]):
            for s in list(cell.find_all(string=True)):
                if "|" in s and s.find_parent("ac:plain-text-body") is None:  # code bodies are escaped later
                    s.replace_with(NavigableString(str(s).replace("|", "\\|")))
            for li in cell.find_all("li"):
                li.insert(0, NavigableString("• "))
                li.append(NavigableString(BR))
            for t in cell.find_all(["ul", "ol", "li"]):
                t.unwrap()
            for p in cell.find_all("p"):
                p.append(NavigableString(BR))
                p.unwrap()
            for br in cell.find_all("br"):
                br.replace_with(NavigableString(BR))
        # markdownify needs a header row: promote the first row if it has none
        for table in soup.find_all("table"):
            first = table.find("tr")
            if first is not None and not first.find("th"):
                for td in first.find_all("td", recursive=False):
                    td.name = "th"

    # ---- macros ---------------------------------------------------------- #
    @staticmethod
    def _param(node, name):
        for p in node.find_all("ac:parameter", recursive=False):
            if p.get("ac:name") == name:
                return p.get_text(strip=True)
        return None

    def _macro(self, node):
        name = node.get("ac:name", "")
        body = node.find("ac:rich-text-body", recursive=False)
        title = self._param(node, "title")

        if name in ("code", "noformat"):
            self._code(node, name)
        elif name in CALLOUTS:
            self._callout(node, CALLOUTS[name], title, body)
        elif name == "expand":
            self._callout(node, "note", title or "Details", body, fold=True)
        elif name == "status":
            self._inline(node, f"`{title or 'status'}`")
        elif name == "jira" and self._param(node, "key"):
            self._inline(node, self._param(node, "key"))
        elif name in SILENT_MACROS:
            node.decompose()
        elif body is not None:  # section, column, excerpt, details, ... -> keep content only
            for p in node.find_all("ac:parameter", recursive=False):
                p.decompose()
            node.unwrap()
        else:
            self.unsupported.add(name)
            self._emit_block(node, f"> [!warning] Unsupported Confluence macro: `{name}`")

    def _code(self, node, name):
        body = node.find("ac:plain-text-body")
        code = (body.get_text() if body is not None else "").strip("\n").rstrip()
        lang = (self._param(node, "language") or "").lower() if name == "code" else ""
        lang = LANG_MAP.get(lang, lang)
        if self._in_cell(node):
            self._emit_block(node, "\n".join(inline_code(l) for l in code.split("\n")))
            return
        longest = max((len(m) for m in re.findall(r"`+", code)), default=0)
        fence = "`" * max(3, longest + 1)
        block = f"{fence}{lang}\n{code}\n{fence}"
        title = self._param(node, "title")
        if title:
            block = f"**{title}**\n\n{block}"
        self._emit_block(node, block)

    def _callout(self, node, kind, title, body, fold=False):
        inner = self._md(body.decode_contents()).strip() if body is not None else ""
        if self._in_cell(node):
            self._emit_block(node, f"**{title or kind.capitalize()}:** {inner}")
            return
        head = f"[!{kind}]{'-' if fold else ''}" + (f" {title}" if title else "")
        lines = [head] + (inner.split("\n") if inner else [])
        self._emit_block(node, "\n".join(("> " + l).rstrip() for l in lines))


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def front_matter(p: Page) -> str:
    lines = [
        "---",
        f"title: {json.dumps(p.title, ensure_ascii=False)}",
        f"confluence_id: {p.id}",
        f"confluence_url: {json.dumps(p.url)}",
        f"confluence_space: {json.dumps(p.space)}",
        f"confluence_version: {p.version}",
        f"last_modified: {json.dumps(p.modified)}",
    ]
    if p.labels:
        lines.append("tags:")
        lines += [f"  - {l}" for l in p.labels]
    lines.append("---\n\n")
    return "\n".join(lines)


def download_attachments(cf, out: Path, pages: dict, needed: set, everything: bool):
    by_owner = defaultdict(set)
    for owner, fn in needed:
        by_owner[owner].add(fn)
    ok, missing = 0, []
    for pid, page in pages.items():
        if not by_owner[pid] and not everything:
            continue
        listing = {a["title"]: a for a in cf.attachments(pid)}
        for fn in (listing if everything else by_owner[pid]):
            att = listing.get(fn)
            if att is None:
                missing.append(f"{page.title}: {fn}")
                continue
            dest = out / "attachments" / pid / safe_filename(fn)
            if not (dest.exists() and dest.stat().st_size):
                cf.download(att["_links"]["download"], dest)
            ok += 1
    return ok, missing


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args():
    ap = argparse.ArgumentParser(description="Export Confluence Server/DC pages to Obsidian Markdown.")
    ap.add_argument("pages", nargs="*", help="page URLs or ids")
    ap.add_argument("-f", "--pages-file", help="text file with one URL/id per line (# comments allowed)")
    ap.add_argument("-o", "--output", default="confluence_export", help="output folder (default: %(default)s)")
    ap.add_argument("--with-children", action="store_true", help="also export all descendant pages")
    ap.add_argument("--all-attachments", action="store_true", help="download every attachment, not just referenced ones")
    ap.add_argument("--base-url", default=os.getenv("CONFLUENCE_BASE_URL"))
    ap.add_argument("--token", default=os.getenv("CONFLUENCE_TOKEN"))
    ap.add_argument("--user", default=os.getenv("CONFLUENCE_USER"))
    ap.add_argument("--password", default=os.getenv("CONFLUENCE_PASSWORD"))
    ap.add_argument("--ca-bundle", help="path to a CA bundle for an internal / self-signed certificate")
    args = ap.parse_args()

    refs = list(args.pages)
    if args.pages_file:
        for line in Path(args.pages_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                refs.append(line)
    if not refs:
        ap.error("give at least one page URL/id (or --pages-file)")
    if not args.base_url:
        ap.error("set --base-url or CONFLUENCE_BASE_URL")
    if not (args.token or (args.user and args.password)):
        ap.error("set CONFLUENCE_TOKEN (personal access token) or CONFLUENCE_USER + CONFLUENCE_PASSWORD")
    args.refs = refs
    return args


def main():
    args = parse_args()
    cf = Confluence(args.base_url, args.token, args.user, args.password, args.ca_bundle)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    # 1) collect pages -------------------------------------------------------
    pages: dict[str, Page] = {}

    def collect(pid: str):
        if pid in pages:
            return
        pages[pid] = page_from_api(cf.page(pid), cf.base)
        print(f"  fetched  {pages[pid].title}")
        if args.with_children:
            for child in cf.children(pid):
                collect(str(child["id"]))

    print("Fetching pages ...")
    for ref in args.refs:
        collect(resolve_ref(cf, ref))
    assign_stems(pages)

    # 2) convert + write -----------------------------------------------------
    needed, unsupported, unresolved, missing_att = set(), defaultdict(set), defaultdict(set), []
    print("Converting ...")
    for p in pages.values():
        conv = Converter(p, pages, cf.base)
        md = conv.convert(p.storage)
        (out / f"{p.stem}.md").write_text(front_matter(p) + md, encoding="utf-8")
        needed |= conv.attachments
        for m in conv.unsupported:
            unsupported[m].add(p.title)
        for t in conv.unresolved:
            unresolved[t].add(p.title)
        missing_att += [f"{p.title}: {fn} (attached to a page that is not exported)" for fn in conv.missing]
        print(f"  wrote    {p.stem}.md")

    # 3) attachments ---------------------------------------------------------
    print("Downloading attachments ...")
    n, missing = download_attachments(cf, out, pages, needed, args.all_attachments)
    missing_att += [f"{m} (not found on the page)" for m in missing]

    # 4) report --------------------------------------------------------------
    print(f"\nDone: {len(pages)} pages, {n} attachments -> {out.resolve()}")
    if unsupported:
        print("\nUnsupported macros (left as a warning callout in the note):")
        for name, where in sorted(unsupported.items()):
            print(f"  {name}: {', '.join(sorted(where))}")
    if unresolved:
        print("\nLinks to pages that are NOT in this export (they will show as unresolved in Obsidian):")
        for title, where in sorted(unresolved.items()):
            print(f"  [[{title}]] <- {', '.join(sorted(where))}")
    if missing_att:
        print("\nAttachments that could not be fetched:")
        for m in missing_att:
            print(f"  {m}")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        code = e.response.status_code
        hint = {401: "check your token / credentials", 403: "no permission for this page",
                404: "page not found - check the base URL (context path!) and page id"}.get(code, "")
        sys.exit(f"HTTP {code} for {e.request.url}\n{hint}")
    except (ValueError, LookupError) as e:
        sys.exit(str(e))
