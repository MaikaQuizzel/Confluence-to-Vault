# 📤 Confluence → Obsidian

Export pages from a **self-hosted Confluence (Server / Data Center)** into your **Obsidian** vault as clean Markdown, with working `[[wikilinks]]`, embedded images, callouts, code blocks and tables.

One Python file, three small dependencies, no Confluence plugins required.

---

## ✨ What you get

| Confluence | Obsidian |
| --- | --- |
| Links between pages | `[[Wikilinks]]` (plain URLs to exported pages are converted too) |
| Images | `![[attachments/<pageId>/image.png]]` (width kept) |
| File attachments | `[[attachments/<pageId>/spec.pdf\|the spec]]` |
| Code / No-format macro | Fenced code block with language |
| Info / Note / Tip / Warning / Panel | Callouts: `> [!info]`, `> [!note]`, `> [!tip]`, `> [!warning]` |
| Expand macro | Collapsible callout: `> [!note]- Title` |
| Tables | Markdown tables (multi-line cells use `<br>`) |
| Task lists | `- [ ]` / `- [x]` |
| Status, Jira key, emoticons | Inline code / plain text / emoji |
| Labels, space, version | YAML front matter (labels become `tags`) |

Anything it can't convert (e.g. `children`, `include`) is left as a visible `> [!warning] Unsupported Confluence macro` callout **and** listed in the summary at the end, so nothing disappears silently.

---

## 🚀 Quick start

### 1. Install

On macOS (Homebrew Python) you need a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install requests beautifulsoup4 markdownify
```

### 2. Create a token

In Confluence: **Profile → Settings → Personal Access Tokens → Create token** (Data Center 7.9+).

Older Server versions without tokens can use `CONFLUENCE_USER` and `CONFLUENCE_PASSWORD` instead.

### 3. Configure

```bash
export CONFLUENCE_BASE_URL="https://wiki.example.com"   # include the context path if you have one, e.g. /confluence
export CONFLUENCE_TOKEN="your-token-here"
```

### 4. Run

```bash
python confluence2obsidian.py -o "$HOME/My Vault/Confluence" \
  "https://wiki.example.com/pages/viewpage.action?pageId=12345"
```

Open Obsidian. Your pages are in the `Confluence` folder.

---

## 📖 Usage

```bash
# one or more pages
python confluence2obsidian.py -o OUT  <url-or-id>  <url-or-id> ...

# many pages from a file (one per line, # comments allowed)
python confluence2obsidian.py -o OUT  -f pages.txt

# a page plus everything below it
python confluence2obsidian.py -o OUT  --with-children  <url-or-id>
```

### Accepted page references

| Format | Example |
| --- | --- |
| Page ID | `12345` |
| View URL | `https://host/pages/viewpage.action?pageId=12345` |
| Space URL | `https://host/spaces/KEY/pages/12345/Title` |
| Display URL | `https://host/display/KEY/Page+Title` |

### Options

| Option | Description |
| --- | --- |
| `-o`, `--output` | Output folder (default: `confluence_export`) |
| `-f`, `--pages-file` | Text file with one page URL or ID per line |
| `--with-children` | Also export all descendant pages |
| `--all-attachments` | Download every attachment, not just the referenced ones |
| `--ca-bundle PATH` | CA bundle for an internal / self-signed certificate |
| `--base-url` | Confluence base URL (or `CONFLUENCE_BASE_URL`) |
| `--token` | Personal access token (or `CONFLUENCE_TOKEN`) |
| `--user`, `--password` | Basic auth (or `CONFLUENCE_USER`, `CONFLUENCE_PASSWORD`) |

> 💡 **Tip:** point `-o` at a folder **inside** your vault, otherwise the attachment embeds won't resolve.

---

## 📁 What the output looks like

```
My Vault/Confluence/
├── Getting Started.md
├── Deployment Guide.md
└── attachments/
    ├── 12345/
    │   └── architecture.png
    └── 67890/
        └── runbook.pdf
```

Each note starts with front matter:

```yaml
---
title: "Deployment Guide"
confluence_id: 67890
confluence_url: "https://wiki.example.com/pages/viewpage.action?pageId=67890"
confluence_space: "DOC"
confluence_version: 12
last_modified: "2026-01-02T10:00:00.000+01:00"
tags:
  - howto
  - team-x
---
```

Attachments live in one folder per page ID, so two pages that both contain an `image.png` never collide.

---

## 🔒 Internal / self-signed certificates

If you see `CERTIFICATE_VERIFY_FAILED`, Python doesn't know your company's root certificate (your browser does, via the system keychain).

**macOS:** export the certificates your Mac trusts, then pass them in:

```bash
security find-certificate -a -p \
  /System/Library/Keychains/SystemRootCertificates.keychain \
  /Library/Keychains/System.keychain > ~/ca-bundle.pem

python confluence2obsidian.py --ca-bundle ~/ca-bundle.pem -o OUT <url-or-id>
```

Or set it once per terminal session: `export REQUESTS_CA_BUNDLE=~/ca-bundle.pem`

---

## 🛠 Troubleshooting

| Problem | Fix |
| --- | --- |
| `externally-managed-environment` when installing | Use a virtual environment (see Quick start) |
| `zsh: no matches found: https://...?pageId=...` | Put the URL in **quotes** |
| Folder named `~` appears | `~` isn't expanded inside quotes. Use `"$HOME/My Vault"` or `~/"My Vault"` |
| `CERTIFICATE_VERIFY_FAILED` | See the certificate section above |
| `HTTP 401` | Token missing, wrong or expired. `export` variables reset in every new terminal window |
| `HTTP 403` | Your account has no permission for that page |
| `HTTP 404` | Wrong base URL (check the **context path**, e.g. `/confluence`) or wrong page ID |
| Images show as unresolved | Output folder isn't inside the vault, or the image is attached to a page that wasn't exported |

---

## ⚠️ Known limitations

- Built for **Server / Data Center**. Confluence Cloud uses different auth and URLs and is not supported.
- Output is **flat**: no folder hierarchy, only links between notes.
- Macros without a Markdown equivalent (`children`, `include`, `jira` queries, draw.io, …) become warning callouts.
- Callouts, code blocks and nested lists inside **table cells** are flattened to one line, because Markdown tables can't hold blocks.
- Links to pages **outside** the export become unresolved `[[links]]`. The summary lists them so you can export those pages too.
- Re-running overwrites existing notes with the same name. Attachments that already exist are skipped.

---

## 🔍 How it works

```
Confluence REST API ──► storage format (XHTML + macros)
                              │
                 BeautifulSoup: macros, links, images → placeholders
                              │
                 markdownify: remaining HTML → Markdown
                              │
                 placeholders restored with correct indentation
                              │
                 .md + front matter + downloaded attachments
```

Using the `storage` format instead of rendered HTML is what keeps macros structured, so code blocks, callouts and page links convert properly.
