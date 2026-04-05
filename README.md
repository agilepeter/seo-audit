# seo-audit

A comprehensive SEO scanner for static HTML sites. Zero external dependencies for core auditing — pure Python stdlib.

## Features

**38+ checks across 8 categories:**

- **Core SEO** — title length, meta description, canonical, OG tags, Twitter cards, JSON-LD schema, H1, favicon
- **Technical** — `<main>` landmark, `noopener` on external links, noindex/sitemap conflicts, canonical/noindex conflicts, heading hierarchy, viewport meta
- **Content** — image alt text, generic image filenames, thin content detection, meta description truncation
- **E-E-A-T** — author attribution, contact page, article dates, HTTPS canonicals
- **Link Graph** — cross-link checking, link depth (BFS from index), orphan pages, internal link count
- **Schema** — @graph and array JSON-LD support, deprecated FAQPage/HowTo detection, Article required fields
- **GEO/AI** — llms.txt, question-format headings
- **Mobile** — zoom-blocking viewport, apple-touch-icon, image CLS (missing width/height), lazy loading, responsive CSS, font size, fixed-width containers, box-sizing

**Optional features:**
- `--fix` — auto-fix mechanical issues (titles, descriptions, OG tags, schema, favicons, viewport, noopener)
- `--diff` — compare against last snapshot, track regressions and fixes over time
- `--lighthouse` — run Lighthouse CI for Core Web Vitals (requires `npx` + lighthouse)
- `--gsc` — pull Google Search Console clicks/impressions/CTR/position data

## Installation

```bash
pip install seo-audit
```

For Google Search Console support:
```bash
pip install "seo-audit[gsc]"
```

Or install from source:
```bash
git clone https://github.com/petersaddington/seo-audit
cd seo-audit
pip install -e .
```

## Usage

```
seo-audit PATH [PATH...] [options]
```

### Basic audit

```bash
seo-audit ./my-site
seo-audit ./my-site --verbose        # show MEDIUM/LOW issues too
seo-audit ./my-site --json           # JSON output
```

### Multiple sites

```bash
seo-audit ./site-a ./site-b ./site-c
```

### Auto-fix issues

```bash
seo-audit ./my-site --fix --dry-run  # preview what would change
seo-audit ./my-site --fix            # apply fixes
```

Bulk fixes include:
- Truncate titles >60 chars (word-boundary aware)
- Truncate meta descriptions >155 chars
- Fix mid-word description truncation
- Add missing OG + Twitter card tags
- Add missing og:image
- Add missing favicon links
- Add missing viewport meta
- Remove zoom-blocking viewport attributes
- Wrap content in `<main>` where missing
- Fix JSON-LD syntax issues (HTML entities, newlines)
- Fix Article schema (add missing image/author/datePublished)
- Add WebPage schema where no schema exists
- Remove canonical from noindex pages

### Domain flag

Used for Lighthouse URLs, GSC queries, and OG/schema URL generation in `--fix`:

```bash
seo-audit ./my-site --domain example.com --lighthouse
seo-audit ./my-site --domain example.com --fix
```

If not provided, the domain is auto-detected from canonical URLs found in the HTML.

### Related domains (cross-link checking)

Check whether your site links to a set of related sites:

```bash
seo-audit ./my-site --related-domains "site-b.com,site-c.com,site-d.com"
```

The report will show which related domains are linked and which are missing.

### Snapshot diff

Track SEO health over time:

```bash
# First run establishes baseline
seo-audit ./my-site

# Future runs show what improved and what regressed
seo-audit ./my-site --diff
```

Snapshots are stored in `~/.seo-audit/`.

### Lighthouse (Core Web Vitals)

Requires `npx` and Lighthouse installed globally:

```bash
npm install -g lighthouse
seo-audit ./my-site --domain example.com --lighthouse
```

Audits index + up to 4 worst-scoring pages. Reports Performance, SEO, Accessibility scores and LCP/CLS/TBT/FCP/SI metrics.

### Google Search Console

Requires OAuth credentials:

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project and enable the Search Console API
3. Create OAuth 2.0 credentials (Desktop app)
4. Download the client secret JSON

```bash
# Place secret at default location
cp ~/Downloads/client_secret_*.json ~/.seo-audit/client_secret.json

# Or specify path directly
seo-audit ./my-site --domain example.com --gsc --gsc-secret ~/path/to/client_secret.json
```

First run opens a browser for OAuth authorization. Token is cached at `~/.seo-audit/gsc_token.json`.

### Skip directories

```bash
seo-audit ./my-site --skip-dirs "dist,build,vendor"
```

Default skipped dirs: `node_modules`, `.git`, `__pycache__`, `.venv`, `venv`, `hooks`, `.claude`

## All options

```
positional arguments:
  paths                 One or more site directories to audit

options:
  --json                Output as JSON
  --verbose, -v         Show all issues including MEDIUM/LOW
  --fix                 Auto-fix safe issues
  --dry-run             Preview fixes (use with --fix)
  --diff                Compare against last snapshot
  --lighthouse          Run Lighthouse CWV
  --gsc                 Pull Google Search Console data
  --domain DOMAIN       Site domain (for Lighthouse/GSC/fixes). Auto-detected if not set.
  --related-domains     Comma-separated related domains for cross-link checking
  --gsc-secret PATH     Path to GSC client secret JSON
  --skip-dirs DIRS      Comma-separated directories to skip (added to defaults)
```

## Self-learning

The tool tracks recurring issues across audit runs. Issues that appear in 3+ consecutive audits are automatically escalated in severity and flagged as `[RECURRING x3]`. This surfaces long-standing unfixed problems.

Learning history is stored in `~/.seo-audit/learnings.json`.

## Python API

```python
from seo_audit import audit_site, format_report, apply_bulk_fixes

# Audit a site
result = audit_site("./my-site", domain="example.com")

# Print report
print(format_report(result, verbose=True))

# Auto-fix issues
changes = apply_bulk_fixes(result, dry_run=False, domain="example.com")

# Audit with related domain cross-link checking
result = audit_site(
    "./my-site",
    related_domains=["partner-site.com", "affiliate.com"],
    domain="example.com",
)

# JSON output
import json
print(json.dumps(result, indent=2))
```

## Grading

Sites are graded A+ through F based on weighted issue counts:
- CRITICAL: -10 points each
- HIGH: -5 points each
- MEDIUM: -2 points each
- LOW: -1 point each

## Data stored locally

All data is stored in `~/.seo-audit/`:
- `audit_YYYY-MM-DD_HHMMSS.json` — timestamped snapshots
- `latest.json` — symlink to most recent snapshot
- `learnings.json` — recurring issue history
- `gsc_token.json` — cached GSC OAuth token (if using --gsc)
- `client_secret.json` — your GSC OAuth credentials (if placed here)

## License

MIT — Copyright 2026 Peter Saddington
