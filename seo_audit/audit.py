#!/usr/bin/env python3
"""
SEO Audit Tool — comprehensive SEO scanner for any static site.
Core audit uses only Python stdlib. Optional features:
  --fix         Auto-fix all mechanical SEO issues (titles, descriptions, OG tags, schema, favicons, etc.)
  --diff        Compare against last audit snapshot, show regressions/fixes
  --lighthouse  Run Lighthouse CI for Core Web Vitals (requires npx)
  --gsc         Pull Google Search Console data (requires google-api-python-client)

Usage:
    seo-audit ./my-site [--json] [--verbose]
    seo-audit ./my-site --fix --dry-run     # preview fixes
    seo-audit ./my-site --fix               # apply safe fixes
    seo-audit ./my-site --diff              # compare vs last run
    seo-audit ./my-site --lighthouse        # CWV scores
    seo-audit ./my-site --gsc               # Search Console data
    seo-audit ./site-a ./site-b             # audit multiple sites

38+ checks across 8 categories:
  Core SEO (8)    — title, meta desc, canonical, OG, Twitter, schema, h1, favicon
  Technical (6)   — main landmark, noopener, noindex/sitemap conflict, canonical/noindex,
                    heading hierarchy, sitemap trailing slash
  Content (5)     — image alt, image filenames, thin content, meta desc truncation, word count
  E-E-A-T (4)    — author info, contact page, dates on articles, HTTPS canonical
  Link Graph (4)  — related cross-links, link depth, orphan pages, internal link count
  Schema (2)      — deprecated FAQPage, Article required fields
  GEO/AI (3)      — llms.txt, direct-answer opening, structured headings
  Mobile (8+)     — viewport zoom, touch icon, image CLS, lazy loading, responsive CSS,
                    base font size, fixed-width containers, box-sizing
"""

import argparse
import collections
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from html.parser import HTMLParser

SNAPSHOT_DIR = os.path.expanduser("~/.seo-audit")

SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    "hooks", ".claude",
}

# Generic image filenames that hurt SEO
GENERIC_IMG_NAMES = {
    "img", "image", "photo", "picture", "pic", "screenshot",
    "screen", "untitled", "download", "file", "asset", "hero",
    "banner", "bg", "background", "thumb", "thumbnail",
}

# ── Severity Levels ──────────────────────────────────────────────────

CRITICAL = "CRITICAL"
HIGH = "HIGH"
MEDIUM = "MEDIUM"
LOW = "LOW"

SEVERITY_RANK = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3}
SEVERITY_SYMBOL = {CRITICAL: "!!!", HIGH: "✗", MEDIUM: "⚠", LOW: "·"}


# ── HTML Parser ──────────────────────────────────────────────────────

class SEOParser(HTMLParser):
    """Single-pass HTML parser that extracts all SEO-relevant data."""

    def __init__(self):
        super().__init__()
        self.title = None
        self._in_title = False
        self._title_parts = []
        self.meta_desc = None
        self.meta_robots = None
        self.canonical = None
        self.og = {}
        self.twitter = {}
        self.jsonld = []
        self.headings = []       # list of (level, text)
        self._current_heading = None
        self._heading_parts = []
        self.has_main = False
        self.images = []
        self.ext_links = []
        self.all_links = []
        self.has_favicon = False
        self._in_script_jsonld = False
        self._script_parts = []
        self._in_body = False
        self._body_text_parts = []
        self.has_contact_link = False
        self.has_author_info = False
        self.has_date = False
        self._in_time = False
        self._in_address = False
        self.has_address = False
        self.viewport = None
        # Mobile-specific attributes
        self._style_parts_all = []
        self._in_style = False
        self._style_current = []
        self.has_apple_touch_icon = False
        self.linked_css = []
        self.inputs = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)

        if tag == "body":
            self._in_body = True

        elif tag == "title":
            self._in_title = True
            self._title_parts = []

        elif tag == "meta":
            name = (a.get("name") or "").lower()
            prop = (a.get("property") or "").lower()
            content = a.get("content", "")
            if name == "description":
                self.meta_desc = content
            elif name == "robots":
                self.meta_robots = content.lower()
            elif name == "viewport":
                self.viewport = content
            elif name == "author" and content.strip():
                self.has_author_info = True
            elif prop.startswith("og:"):
                self.og[prop] = content
            elif name.startswith("twitter:"):
                self.twitter[name] = content
            elif prop == "article:published_time" or prop == "article:modified_time":
                self.has_date = True

        elif tag == "link":
            rel = (a.get("rel") or "").lower()
            href = a.get("href", "")
            if "canonical" in rel:
                self.canonical = href
            if "icon" in rel or "shortcut" in rel:
                self.has_favicon = True
            if "apple-touch-icon" in rel:
                self.has_apple_touch_icon = True
            if "stylesheet" in rel:
                self.linked_css.append(href)

        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            self._current_heading = level
            self._heading_parts = []

        elif tag == "main":
            self.has_main = True

        elif tag == "img":
            self.images.append({
                "src": a.get("src", ""),
                "alt": a.get("alt"),
                "width": a.get("width"),
                "height": a.get("height"),
                "loading": a.get("loading"),
            })

        elif tag == "a":
            href = a.get("href", "")
            target = a.get("target", "")
            rel = (a.get("rel") or "").lower()
            self.all_links.append(href)
            # Check for contact links
            href_lower = href.lower()
            if "contact" in href_lower or "mailto:" in href_lower:
                self.has_contact_link = True
            # External link checks
            if href.startswith("http") and target == "_blank":
                self.ext_links.append({
                    "href": href,
                    "has_noopener": "noopener" in rel,
                })

        elif tag == "script":
            stype = (a.get("type") or "").lower()
            if stype == "application/ld+json":
                self._in_script_jsonld = True
                self._script_parts = []

        elif tag == "time":
            self._in_time = True
            if a.get("datetime"):
                self.has_date = True

        elif tag == "address":
            self._in_address = True
            self.has_address = True

        elif tag == "style":
            self._in_style = True
            self._style_current = []

        elif tag in ("input", "select", "textarea", "button"):
            self.inputs.append({"tag": tag, "type": a.get("type", ""), "style": a.get("style", "")})

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self._in_title = False
            self.title = "".join(self._title_parts).strip()

        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._current_heading:
            text = "".join(self._heading_parts).strip()
            self.headings.append((self._current_heading, text))
            self._current_heading = None

        elif tag == "script" and self._in_script_jsonld:
            self._in_script_jsonld = False
            raw = "".join(self._script_parts).strip()
            if raw:
                self.jsonld.append(raw)

        elif tag == "body":
            self._in_body = False

        elif tag == "time":
            self._in_time = False

        elif tag == "address":
            self._in_address = False

        elif tag == "style" and self._in_style:
            self._in_style = False
            self._style_parts_all.append("".join(self._style_current))

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)
        if self._current_heading is not None:
            self._heading_parts.append(data)
        if self._in_script_jsonld:
            self._script_parts.append(data)
        if self._in_style:
            self._style_current.append(data)
        if self._in_body and not self._in_script_jsonld:
            self._body_text_parts.append(data)

    @property
    def body_text(self):
        return " ".join(self._body_text_parts)

    @property
    def word_count(self):
        text = self.body_text
        return len(re.findall(r'\b\w+\b', text))

    @property
    def h1_count(self):
        return sum(1 for level, _ in self.headings if level == 1)

    @property
    def h1_texts(self):
        return [text for level, text in self.headings if level == 1]

    @property
    def inline_css(self):
        return "\n".join(self._style_parts_all)

    @property
    def first_paragraph_text(self):
        """Get first ~200 chars of body text for GEO direct-answer check."""
        text = self.body_text.strip()
        # Skip very short text
        if len(text) < 50:
            return ""
        return text[:500]


# ── Issue Class ──────────────────────────────────────────────────────

class Issue:
    __slots__ = ("severity", "category", "message", "fix")

    def __init__(self, severity, category, message, fix=""):
        self.severity = severity
        self.category = category
        self.message = message
        self.fix = fix

    def to_dict(self):
        d = {"severity": self.severity, "category": self.category, "message": self.message}
        if self.fix:
            d["fix"] = self.fix
        return d


# ── Audit Logic ──────────────────────────────────────────────────────

def parse_file(filepath):
    parser = SEOParser()
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            parser.feed(f.read())
    except Exception as e:
        parser._error = str(e)
    return parser


def load_sitemap_urls(site_path):
    sitemap = os.path.join(site_path, "sitemap.xml")
    if not os.path.exists(sitemap):
        return None
    try:
        with open(sitemap, "r") as f:
            content = f.read()
        return re.findall(r"<loc>(.*?)</loc>", content)
    except Exception:
        return None


def check_llms_txt(site_path):
    """Check for llms.txt and llms-full.txt presence and quality."""
    results = {}
    for fname in ["llms.txt", "llms-full.txt"]:
        fpath = os.path.join(site_path, fname)
        if os.path.exists(fpath):
            try:
                with open(fpath, "r") as f:
                    content = f.read()
                results[fname] = {
                    "exists": True,
                    "size": len(content),
                    "lines": content.count("\n") + 1,
                    "has_urls": bool(re.search(r'https?://', content)),
                }
            except Exception:
                results[fname] = {"exists": True, "size": 0, "lines": 0}
        else:
            results[fname] = {"exists": False}
    return results


def audit_page(filepath, site_path, sitemap_urls=None, own_domain=None, related_domains=None):
    """Audit a single HTML page. Returns structured results with severity."""
    p = parse_file(filepath)
    rel_path = os.path.relpath(filepath, site_path)
    is_404 = "404" in os.path.basename(filepath)
    issues = []

    if related_domains is None:
        related_domains = []

    def add(severity, category, message, fix=""):
        issues.append(Issue(severity, category, message, fix))

    # ── CORE SEO ─────────────────────────────────────────────────

    # 1. Title
    if not p.title:
        add(CRITICAL, "Core", "Missing <title>", "Add a unique <title> tag under 60 chars")
    elif len(p.title) > 60:
        add(HIGH, "Core", f"Title too long: {len(p.title)} chars — \"{p.title[:65]}...\"",
            f"Trim to 60 chars. Current: {len(p.title)}")
    elif len(p.title) < 20:
        add(LOW, "Core", f"Title very short: {len(p.title)} chars", "Aim for 30-60 chars with keywords")

    # 2. Meta description
    if p.meta_desc is None:
        if not is_404:
            add(HIGH, "Core", "Missing <meta description>", "Add description, 50-155 chars")
    elif len(p.meta_desc) > 155:
        add(MEDIUM, "Core", f"Meta description too long: {len(p.meta_desc)} chars",
            "Truncate at sentence boundary, max 155 chars")
    elif len(p.meta_desc) < 50:
        add(LOW, "Core", f"Meta description short: {len(p.meta_desc)} chars", "Aim for 50-155 chars")
    # Check truncation quality — does it end mid-word?
    if p.meta_desc and len(p.meta_desc) > 100:
        if not p.meta_desc.rstrip().endswith((".", "!", "?", "...", "\u2026")):
            last_char = p.meta_desc[-1]
            if last_char.isalpha():
                add(LOW, "Content", "Meta description may truncate mid-word",
                    "End at sentence boundary (. ! ?)")

    # 3. Canonical
    if not p.canonical:
        if not is_404:
            add(HIGH, "Core", "Missing <link rel=\"canonical\">",
                "Add self-referential canonical URL")
    else:
        # HTTPS check (E-E-A-T)
        if p.canonical.startswith("http://"):
            add(HIGH, "E-E-A-T", "Canonical uses HTTP, not HTTPS",
                "Switch to https://")
        # Sitemap consistency
        if sitemap_urls:
            canon_stripped = p.canonical.rstrip("/")
            sitemap_match = None
            for url in sitemap_urls:
                if url.rstrip("/") == canon_stripped:
                    sitemap_match = url
                    break
            if sitemap_match is None and not is_404:
                add(MEDIUM, "Technical", f"Canonical not in sitemap: {p.canonical}",
                    "Add to sitemap.xml or fix canonical URL")
            elif sitemap_match and sitemap_match != p.canonical:
                add(HIGH, "Technical", f"Trailing slash mismatch — canonical: {p.canonical} vs sitemap: {sitemap_match}",
                    "Make canonical and sitemap URLs identical")

    # Canonical → noindex conflict
    if p.canonical and p.meta_robots and "noindex" in p.meta_robots:
        add(CRITICAL, "Technical", "Page has canonical but also noindex — conflicting signals",
            "Remove noindex or remove canonical")

    # 4. OG tags
    required_og = ["og:title", "og:description", "og:image", "og:url", "og:type"]
    missing_og = [t for t in required_og if t not in p.og]
    if missing_og:
        if not is_404:
            sev = CRITICAL if len(missing_og) == len(required_og) else HIGH
            add(sev, "Core", f"Missing OG tags: {', '.join(missing_og)}",
                "Add all 5 OG tags for social sharing")
    # og:url vs canonical
    if p.og.get("og:url") and p.canonical:
        if p.og["og:url"] != p.canonical:
            add(HIGH, "Core", f"og:url != canonical ({p.og['og:url']} vs {p.canonical})",
                "Make og:url match canonical exactly")

    # 5. Twitter cards
    required_tw = ["twitter:card", "twitter:title", "twitter:description", "twitter:image"]
    missing_tw = [t for t in required_tw if t not in p.twitter]
    if missing_tw:
        if not is_404:
            sev = HIGH if len(missing_tw) == len(required_tw) else MEDIUM
            add(sev, "Core", f"Missing Twitter tags: {', '.join(missing_tw)}",
                "Add all 4 Twitter card tags")

    # 6. Schema.org JSON-LD
    if not p.jsonld:
        if not is_404:
            add(HIGH, "Schema", "No JSON-LD schema found",
                "Add appropriate schema (WebSite, Article, Person, etc.)")
    else:
        for i, raw in enumerate(p.jsonld):
            try:
                schema = json.loads(raw)
                stype = schema.get("@type", "unknown")

                # Article checks
                if stype in ("Article", "NewsArticle", "BlogPosting"):
                    article_required = ["headline", "datePublished", "author", "image"]
                    missing = [f for f in article_required if f not in schema]
                    if missing:
                        add(HIGH, "Schema", f"Article schema missing: {', '.join(missing)}",
                            "Google won't show rich results without these")

                # DEPRECATED: FAQPage (restricted to gov/health since Aug 2023)
                if stype == "FAQPage":
                    add(HIGH, "Schema", "FAQPage schema is deprecated for non-gov/health sites (Aug 2023)",
                        "Remove FAQPage schema — Google ignores it for commercial sites")

                # HowTo deprecated Sept 2023
                if stype == "HowTo":
                    add(HIGH, "Schema", "HowTo schema deprecated (Sept 2023)",
                        "Remove HowTo schema — no longer generates rich results")

            except json.JSONDecodeError:
                add(HIGH, "Schema", f"Invalid JSON-LD in block {i+1}",
                    "Fix JSON syntax error in schema block")

    # 7. H1
    if p.h1_count == 0:
        if not is_404:
            add(HIGH, "Core", "No <h1> found", "Add exactly one <h1> per page")
    elif p.h1_count > 1:
        add(MEDIUM, "Core", f"Multiple <h1> tags: {p.h1_count}", "Use exactly one <h1>, use <h2>+ for subsections")

    # 8. Favicon
    if not p.has_favicon:
        add(MEDIUM, "Core", "No favicon link found", "Add <link rel=\"icon\"> in <head>")

    # ── TECHNICAL ────────────────────────────────────────────────

    # 9. Main landmark
    if not p.has_main:
        add(MEDIUM, "Technical", "No <main> landmark", "Wrap primary content in <main>")

    # 10. External links noopener
    unsafe_links = [l for l in p.ext_links if not l["has_noopener"]]
    if unsafe_links:
        add(MEDIUM, "Technical",
            f"{len(unsafe_links)} external target=\"_blank\" link(s) missing rel=\"noopener\"",
            "Add rel=\"noopener\" to all target=\"_blank\" links")

    # 11. Noindex in sitemap conflict
    if sitemap_urls and p.meta_robots and "noindex" in p.meta_robots:
        # Check if this page's canonical is in the sitemap
        if p.canonical and any(p.canonical.rstrip("/") == u.rstrip("/") for u in sitemap_urls):
            add(CRITICAL, "Technical", "Page is noindexed but listed in sitemap.xml",
                "Remove from sitemap or remove noindex")

    # 12. Heading hierarchy — no skipping levels
    if p.headings:
        prev_level = 0
        for level, text in p.headings:
            if prev_level > 0 and level > prev_level + 1:
                add(MEDIUM, "Technical",
                    f"Heading hierarchy skip: H{prev_level} → H{level} (missing H{prev_level + 1})",
                    f"Add H{prev_level + 1} between H{prev_level} and H{level}")
                break  # only report first skip
            prev_level = level

    # 13. Viewport meta
    if not p.viewport:
        add(HIGH, "Technical", "Missing viewport meta tag",
            "Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">")

    # ── CONTENT ──────────────────────────────────────────────────

    # 14. Image alt text
    imgs_no_alt = [img for img in p.images if img["alt"] is None]
    if imgs_no_alt:
        sev = HIGH if len(imgs_no_alt) > 3 else MEDIUM
        add(sev, "Content", f"{len(imgs_no_alt)} image(s) missing alt text",
            "Add descriptive alt text to every <img>")

    # 15. Generic image filenames
    for img in p.images:
        src = img.get("src", "")
        if not src:
            continue
        basename = os.path.splitext(os.path.basename(src))[0].lower()
        # Strip trailing numbers: img-001 → img, photo2 → photo
        clean = re.sub(r'[-_]?\d+$', '', basename)
        if clean in GENERIC_IMG_NAMES:
            add(LOW, "Content",
                f"Generic image filename: {os.path.basename(src)}",
                "Rename to descriptive keyword filename (e.g., karting-houston-track.webp)")
            break  # one warning per page is enough

    # 16. Thin content
    wc = p.word_count
    if not is_404 and "index" not in os.path.basename(rel_path):
        if wc < 50:
            add(HIGH, "Content", f"Very thin content: {wc} words",
                "Page needs substantive content — aim for 300+ words")
        elif wc < 100:
            add(MEDIUM, "Content", f"Thin content: only {wc} words",
                "Aim for 300+ words of substantive content")

    # ── E-E-A-T ──────────────────────────────────────────────────

    # 17. Author info (for content pages, not 404/index)
    has_article_schema = any(
        _safe_json_type(raw) in ("Article", "NewsArticle", "BlogPosting")
        for raw in p.jsonld
    )
    if has_article_schema and not p.has_author_info:
        # Check JSON-LD for author
        has_jsonld_author = any(
            "author" in raw for raw in p.jsonld
        )
        if not has_jsonld_author:
            add(MEDIUM, "E-E-A-T", "Article page has no author attribution",
                "Add <meta name=\"author\"> or author in JSON-LD schema")

    # 18. Contact page accessibility
    # (checked at site level, not page level — see audit_site)

    # 19. Dates on article pages
    if has_article_schema and not p.has_date:
        add(MEDIUM, "E-E-A-T", "Article page has no visible date",
            "Add <time datetime=\"...\"> or article:published_time meta")

    # ── LINK GRAPH ───────────────────────────────────────────────

    # 20. Related cross-links
    related_links_found = set()
    for href in p.all_links:
        for domain in related_domains:
            if domain in href:
                related_links_found.add(domain)

    # 21. Internal link count
    internal_count = len([l for l in p.all_links
                          if l and not l.startswith(("http", "#", "javascript:", "mailto:", "tel:"))])
    if internal_count == 0 and not is_404:
        add(LOW, "Link Graph", "No internal links on page", "Add links to related pages")

    # ── GEO / AI READINESS ───────────────────────────────────────

    # 22. Structured headings (question format helps AI snippets)
    question_headings = sum(1 for _, text in p.headings if text.strip().endswith("?"))
    # Just track — not an issue, used for site-level reporting

    # ── MOBILE ──────────────────────────────────────────────────

    # M1. Viewport blocks zoom (accessibility violation, Google penalty)
    if p.viewport:
        vp_lower = p.viewport.lower()
        if "user-scalable=no" in vp_lower or "user-scalable=0" in vp_lower:
            add(HIGH, "Mobile", "Viewport blocks zoom (user-scalable=no)",
                "Remove user-scalable=no — required for WCAG accessibility")
        if re.search(r'maximum-scale\s*=\s*1(?:[^.]|$)', vp_lower):
            add(HIGH, "Mobile", "Viewport blocks zoom (maximum-scale=1)",
                "Remove maximum-scale=1 or set to 5.0+")

    # M2. Apple touch icon
    if not p.has_apple_touch_icon:
        add(LOW, "Mobile", "No apple-touch-icon found",
            "Add <link rel=\"apple-touch-icon\" href=\"/images/apple-touch-icon.png\">")

    # M3. Image dimensions for CLS prevention
    imgs_no_dims = [img for img in p.images
                    if img.get("src") and not (img.get("width") and img.get("height"))]
    if imgs_no_dims:
        count = len(imgs_no_dims)
        sev = HIGH if count > 5 else MEDIUM
        add(sev, "Mobile", f"{count} image(s) missing width/height attributes (CLS risk)",
            "Add explicit width and height to every <img> to prevent layout shift")

    # M4. Hero/LCP image has loading="lazy" (kills LCP score)
    if p.images:
        first_img = p.images[0]
        if first_img.get("loading") == "lazy":
            add(HIGH, "Mobile", "First image has loading=\"lazy\" — hurts LCP score",
                "Remove loading=\"lazy\" from above-fold/hero images")

    # M5. Below-fold images missing lazy loading
    if len(p.images) > 1:
        non_lazy = [img for img in p.images[1:] if not img.get("loading")]
        if len(non_lazy) >= 3:
            add(LOW, "Mobile", f"{len(non_lazy)} below-fold image(s) missing loading=\"lazy\"",
                "Add loading=\"lazy\" to images below the fold for faster page load")

    # M6. Inline CSS responsive check
    all_css = p.inline_css
    # Also read linked CSS files for this page
    for css_href in p.linked_css:
        if css_href and not css_href.startswith(("http://", "https://")):
            css_path = os.path.join(site_path, css_href.lstrip("/"))
            if os.path.exists(css_path):
                try:
                    with open(css_path, "r", encoding="utf-8", errors="ignore") as f:
                        all_css += "\n" + f.read()
                except Exception:
                    pass

    if all_css:
        has_media = bool(re.search(r'@media\s*[\s(]', all_css))
        if not has_media and not is_404:
            add(MEDIUM, "Mobile", "No CSS media queries found — page may not be responsive",
                "Add @media breakpoints for mobile (min-width/max-width)")

    # M7. Form inputs font-size check (iOS auto-zoom at <16px)
    if p.inputs:
        # Check inline styles for small font sizes on inputs
        for inp in p.inputs:
            style = inp.get("style", "")
            if style:
                font_match = re.search(r'font-size\s*:\s*(\d+(?:\.\d+)?)\s*px', style)
                if font_match and float(font_match.group(1)) < 16:
                    add(MEDIUM, "Mobile",
                        f"<{inp['tag']}> has inline font-size {font_match.group(1)}px — iOS will auto-zoom",
                        "Set input font-size to 16px+ to prevent iOS zoom on focus")
                    break

    # M8. Modern image formats check
    legacy_imgs = [img for img in p.images
                   if img.get("src") and
                   img["src"].lower().endswith((".jpg", ".jpeg", ".png")) and
                   not img["src"].startswith("data:")]
    if legacy_imgs and len(legacy_imgs) >= 3:
        add(LOW, "Mobile", f"{len(legacy_imgs)} image(s) using JPEG/PNG — WebP/AVIF would be smaller",
            "Convert images to WebP format for 25-50% smaller file sizes on mobile")

    # Collect raw hrefs for BFS link graph (avoids re-parsing)
    # Include same-domain absolute URLs converted to relative paths
    raw_hrefs = []
    for href in p.all_links:
        if not href:
            continue
        clean = href.split("?")[0].split("#")[0]
        if clean.startswith(("http://", "https://")):
            # Convert same-domain absolute URLs to relative
            if own_domain:
                for prefix in [f"https://{own_domain}", f"http://{own_domain}"]:
                    if clean.startswith(prefix):
                        rel = clean[len(prefix):].lstrip("/")
                        raw_hrefs.append(rel)
                        break
        elif not clean.startswith(("javascript:", "mailto:", "tel:")):
            raw_hrefs.append(clean)

    return {
        "file": rel_path,
        "title": p.title,
        "meta_desc": p.meta_desc,
        "canonical": p.canonical,
        "word_count": wc,
        "issues": [i.to_dict() for i in issues],
        "related_links_found": list(related_links_found),
        "internal_link_count": internal_count,
        "question_headings": question_headings,
        "heading_count": len(p.headings),
        "h1_count": p.h1_count,
        "issue_counts": _count_severities(issues),
        "raw_hrefs": raw_hrefs,
    }


def _safe_json_type(raw):
    try:
        return json.loads(raw).get("@type", "")
    except Exception:
        return ""


def _count_severities(issues):
    counts = {CRITICAL: 0, HIGH: 0, MEDIUM: 0, LOW: 0}
    for i in issues:
        counts[i.severity] = counts.get(i.severity, 0) + 1
    return counts


def _auto_detect_domain(results):
    """Auto-detect domain from canonical URLs found in page results."""
    for r in results:
        canonical = r.get("canonical")
        if canonical and canonical.startswith(("https://", "http://")):
            # Extract domain from canonical
            m = re.match(r'https?://([^/]+)', canonical)
            if m:
                return m.group(1)
    return None


# ── Site-Level Checks ────────────────────────────────────────────────

def audit_site(site_path, related_domains=None, domain=None, extra_skip_dirs=None):
    """Audit all HTML files in a site directory."""
    site_path = os.path.abspath(site_path)
    site_name = os.path.basename(site_path) or site_path

    if related_domains is None:
        related_domains = []

    # Build effective skip dirs
    skip_dirs = SKIP_DIRS.copy()
    if extra_skip_dirs:
        skip_dirs.update(extra_skip_dirs)

    html_files = find_html_files(site_path, skip_dirs=skip_dirs)
    if not html_files:
        return {"site": site_name, "path": site_path, "error": "No HTML files found", "pages": []}

    sitemap_urls = load_sitemap_urls(site_path)

    # Parse all pages first for cross-page analysis
    # Pass own_domain=None initially; we'll auto-detect after first pass if not provided
    results = []
    for f in html_files:
        result = audit_page(f, site_path, sitemap_urls, own_domain=domain, related_domains=related_domains)
        results.append(result)

    # Auto-detect domain from canonical URLs if not provided
    own_domain = domain or _auto_detect_domain(results)

    # If domain was auto-detected (or provided), re-run pages that need it for BFS hrefs
    # Only re-run if own_domain was not available on first pass
    if own_domain and not domain:
        results = []
        for f in html_files:
            result = audit_page(f, site_path, sitemap_urls, own_domain=own_domain, related_domains=related_domains)
            results.append(result)

    # ── Site-level checks ────────────────────────────────────────

    site_issues = []

    # Contact page check (E-E-A-T)
    has_contact = any("contact" in r["file"].lower() for r in results)
    if not has_contact:
        site_issues.append(Issue(MEDIUM, "E-E-A-T", "No contact page found",
                                 "Add a contact.html with email/form for trust signals"))

    # About page check (E-E-A-T)
    has_about = any("about" in r["file"].lower() for r in results)
    if not has_about:
        site_issues.append(Issue(LOW, "E-E-A-T", "No about page found",
                                 "Add an about page with author/org credentials"))

    # llms.txt (GEO/AI readiness)
    llms = check_llms_txt(site_path)
    if not llms.get("llms.txt", {}).get("exists"):
        site_issues.append(Issue(MEDIUM, "GEO/AI", "No llms.txt found",
                                 "Add llms.txt for AI crawler discoverability"))
    elif llms["llms.txt"].get("lines", 0) < 5:
        site_issues.append(Issue(LOW, "GEO/AI", f"llms.txt is thin ({llms['llms.txt']['lines']} lines)",
                                 "Expand llms.txt with more context about the site"))

    # robots.txt
    robots_path = os.path.join(site_path, "robots.txt")
    if not os.path.exists(robots_path):
        site_issues.append(Issue(HIGH, "Technical", "No robots.txt found",
                                 "Add robots.txt with sitemap reference"))
    else:
        try:
            with open(robots_path, "r") as f:
                robots_content = f.read().lower()
            if "sitemap:" not in robots_content and sitemap_urls:
                site_issues.append(Issue(MEDIUM, "Technical",
                                         "robots.txt has no Sitemap: directive",
                                         "Add 'Sitemap: https://yoursite.com/sitemap.xml' to robots.txt"))
        except Exception:
            pass

    # Sitemap
    if not sitemap_urls:
        site_issues.append(Issue(HIGH, "Technical", "No sitemap.xml found",
                                 "Add sitemap.xml listing all indexable pages"))

    # Link depth analysis (BFS from index.html)
    link_depth = _compute_link_depth(results)
    deep_pages = [(page, depth) for page, depth in link_depth.items() if depth > 3]
    orphan_pages = [r["file"] for r in results
                    if r["file"] not in link_depth and r["file"] != "index.html"
                    and "404" not in r["file"]]

    if deep_pages:
        deep_pages.sort(key=lambda x: -x[1])
        top3 = deep_pages[:3]
        examples = ", ".join(f"{p} (depth {d})" for p, d in top3)
        site_issues.append(Issue(MEDIUM, "Link Graph",
                                 f"{len(deep_pages)} page(s) are >3 clicks from homepage: {examples}",
                                 "Add internal links to reduce click depth"))

    if orphan_pages:
        examples = ", ".join(orphan_pages[:5])
        site_issues.append(Issue(HIGH, "Link Graph",
                                 f"{len(orphan_pages)} orphan page(s) unreachable from index: {examples}",
                                 "Link to these pages from navigation or content"))

    # Duplicate titles
    titles = collections.Counter()
    for r in results:
        if r["title"]:
            titles[r["title"]] += 1
    dupes = {t: c for t, c in titles.items() if c > 1}
    if dupes:
        for title, count in dupes.items():
            site_issues.append(Issue(HIGH, "Content",
                                     f"Duplicate title ({count}x): \"{title[:60]}\"",
                                     "Make each page title unique"))

    # Duplicate meta descriptions
    descs = collections.Counter()
    for r in results:
        if r.get("meta_desc") and len(r["meta_desc"]) > 20:
            descs[r["meta_desc"]] += 1
    desc_dupes = {d: c for d, c in descs.items() if c > 1}
    if desc_dupes:
        for desc, count in desc_dupes.items():
            site_issues.append(Issue(HIGH, "Content",
                                     f"Duplicate meta description ({count}x): \"{desc[:60]}...\"",
                                     "Make each page's meta description unique"))

    # ── MOBILE (site-level) ───────────────────────────────────────
    site_css = _read_site_css(site_path, skip_dirs=skip_dirs)
    if site_css:
        # Responsive breakpoints
        breakpoints = re.findall(r'@media[^{]*\(\s*(?:max|min)-width\s*:\s*(\d+)', site_css)
        if not breakpoints:
            site_issues.append(Issue(HIGH, "Mobile",
                "No responsive breakpoints found in CSS files",
                "Add @media queries for mobile (768px), tablet (1024px)"))

        # Global img max-width
        if not re.search(r'img\s*\{[^}]*max-width\s*:\s*100%', site_css, re.DOTALL):
            # Also check for img within a broader rule
            if not re.search(r'img[^{]*\{[^}]*max-width\s*:\s*100%', site_css, re.DOTALL):
                site_issues.append(Issue(MEDIUM, "Mobile",
                    "No global img { max-width: 100% } rule found",
                    "Add img { max-width: 100%; height: auto; } to prevent overflow on mobile"))

        # box-sizing: border-box
        if "box-sizing" not in site_css:
            site_issues.append(Issue(LOW, "Mobile",
                "No box-sizing: border-box declaration",
                "Add *, *::before, *::after { box-sizing: border-box; }"))

        # Base font size check
        body_font = re.search(
            r'(?:body|html)\s*\{[^}]*font-size\s*:\s*(\d+(?:\.\d+)?)\s*px',
            site_css, re.DOTALL)
        if body_font:
            size = float(body_font.group(1))
            if size < 16:
                site_issues.append(Issue(HIGH, "Mobile",
                    f"Base font size {size}px is below 16px minimum",
                    "Set body font-size to 16px — prevents iOS auto-zoom and improves readability"))

        # Fixed-width containers
        fixed_hits = re.findall(
            r'(?:body|main|\.container|\.wrapper|\.content|\.page)\s*\{[^}]*?'
            r'(?<![max-])width\s*:\s*(\d+)\s*px',
            site_css, re.DOTALL)
        for w in fixed_hits:
            if int(w) > 500:
                site_issues.append(Issue(MEDIUM, "Mobile",
                    f"Fixed container width {w}px may cause horizontal scroll on mobile",
                    "Use max-width instead of width for containers"))
                break

    # Related cross-links summary
    all_related = set()
    for r in results:
        all_related.update(r["related_links_found"])
    missing_related = set(related_domains) - all_related - ({own_domain} if own_domain else set())

    # GEO/AI summary
    total_question_headings = sum(r["question_headings"] for r in results)

    # Severity tallies
    total_by_severity = {CRITICAL: 0, HIGH: 0, MEDIUM: 0, LOW: 0}
    for r in results:
        for sev, count in r["issue_counts"].items():
            total_by_severity[sev] = total_by_severity.get(sev, 0) + count
    for si in site_issues:
        total_by_severity[si.severity] = total_by_severity.get(si.severity, 0) + 1

    total_issues = sum(total_by_severity.values())
    total_pages = len(results)

    return {
        "site": site_name,
        "path": site_path,
        "domain": own_domain,
        "pages_scanned": total_pages,
        "total_by_severity": total_by_severity,
        "total_issues": total_issues,
        "has_sitemap": sitemap_urls is not None,
        "sitemap_url_count": len(sitemap_urls) if sitemap_urls else 0,
        "related_links_found": sorted(all_related),
        "missing_related_links": sorted(missing_related),
        "llms_txt": llms,
        "question_headings": total_question_headings,
        "orphan_pages": len(orphan_pages),
        "deep_pages": len(deep_pages) if deep_pages else 0,
        "site_issues": [i.to_dict() for i in site_issues],
        "pages": results,
    }


def _compute_link_depth(results):
    """BFS from index.html using pre-collected hrefs. No re-parsing."""
    file_set = {r["file"] for r in results}

    # Build adjacency from stored raw_hrefs
    adjacency = {}
    for r in results:
        linked = set()
        for href in r.get("raw_hrefs", []):
            if not href:
                continue
            # Resolve path
            if href.startswith("/"):
                candidate = href.lstrip("/")
            elif href == "./" or href == ".":
                candidate = os.path.dirname(r["file"]) or ""
            else:
                candidate = os.path.normpath(os.path.join(os.path.dirname(r["file"]), href))

            # Normalize: remove trailing slash for matching
            candidate = candidate.rstrip("/")

            # Try multiple resolutions
            candidates = [
                candidate,                          # exact: tracks/foo.html
                candidate + ".html",                # tracks/foo → tracks/foo.html
                candidate + "/index.html",          # tracks/ → tracks/index.html
                os.path.join(candidate, "index.html"),  # tracks → tracks/index.html
            ]
            if candidate == "":
                candidates = ["index.html"]

            for c in candidates:
                if c in file_set:
                    linked.add(c)
                    break
        adjacency[r["file"]] = linked

    # BFS from index.html
    start = "index.html"
    if start not in file_set:
        return {}

    depth = {start: 0}
    queue = collections.deque([start])
    while queue:
        current = queue.popleft()
        for neighbor in adjacency.get(current, set()):
            if neighbor not in depth:
                depth[neighbor] = depth[current] + 1
                queue.append(neighbor)

    return depth


def find_html_files(site_path, skip_dirs=None):
    if skip_dirs is None:
        skip_dirs = SKIP_DIRS
    results = []
    for root, dirs, files in os.walk(site_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".") and not d.startswith("_")]
        for f in files:
            if f.endswith(".html"):
                results.append(os.path.join(root, f))
    return sorted(results)


def _find_css_files(site_path, skip_dirs=None):
    """Find all .css files in a site directory."""
    if skip_dirs is None:
        skip_dirs = SKIP_DIRS
    css_files = []
    for root, dirs, files in os.walk(site_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".") and not d.startswith("_")]
        for f in files:
            if f.endswith(".css"):
                css_files.append(os.path.join(root, f))
    return css_files


def _read_site_css(site_path, skip_dirs=None):
    """Read and concatenate all CSS from a site's .css files."""
    combined = ""
    for css_file in _find_css_files(site_path, skip_dirs=skip_dirs):
        try:
            with open(css_file, "r", encoding="utf-8", errors="ignore") as f:
                combined += f.read() + "\n"
        except Exception:
            pass
    return combined


# ── Output Formatting ────────────────────────────────────────────────

def format_report(site_result, verbose=False):
    lines = []
    s = site_result

    if "error" in s:
        lines.append(f"\n## {s['site']} — ERROR: {s['error']}")
        return "\n".join(lines)

    # Score calculation — weighted by severity
    weights = {CRITICAL: 10, HIGH: 5, MEDIUM: 2, LOW: 1}
    weighted_penalty = sum(s["total_by_severity"].get(sev, 0) * w for sev, w in weights.items())
    max_possible = s["pages_scanned"] * 30 * 2  # rough max
    score = max(0, 100 - (weighted_penalty / max(max_possible, 1) * 100))
    # Clamp to 0-100 and use letter grade
    grade = _letter_grade(score)

    lines.append(f"\n{'='*60}")
    lines.append(f"  {s['site']} — Grade: {grade}")
    lines.append(f"{'='*60}")
    lines.append(f"  Pages: {s['pages_scanned']} | Sitemap: {s['sitemap_url_count'] if s['has_sitemap'] else 'NONE'}")
    lines.append(f"  CRITICAL: {s['total_by_severity'].get(CRITICAL, 0)} | "
                 f"HIGH: {s['total_by_severity'].get(HIGH, 0)} | "
                 f"MEDIUM: {s['total_by_severity'].get(MEDIUM, 0)} | "
                 f"LOW: {s['total_by_severity'].get(LOW, 0)}")

    # Related links (only shown when related_domains were provided)
    if s.get("related_links_found"):
        lines.append(f"  Related links: {', '.join(s['related_links_found'])}")
    if s.get("missing_related_links"):
        lines.append(f"  Missing links to: {', '.join(s['missing_related_links'])}")

    # GEO/AI summary
    llms_status = []
    for fname in ["llms.txt", "llms-full.txt"]:
        info = s["llms_txt"].get(fname, {})
        if info.get("exists"):
            llms_status.append(f"{fname} ({info.get('lines', 0)} lines)")
    if llms_status:
        lines.append(f"  AI readiness: {', '.join(llms_status)} | {s['question_headings']} question-format headings")
    else:
        lines.append(f"  AI readiness: No llms.txt")

    # Link graph summary
    if s["orphan_pages"] or s["deep_pages"]:
        lines.append(f"  Link depth: {s['orphan_pages']} orphan pages, {s['deep_pages']} deep (>3 clicks)")

    # Site-level issues
    if s["site_issues"]:
        lines.append(f"\n  Site-Level Issues:")
        for issue in sorted(s["site_issues"], key=lambda x: SEVERITY_RANK.get(x["severity"], 9)):
            sym = SEVERITY_SYMBOL.get(issue["severity"], "?")
            lines.append(f"    [{issue['severity']}] {sym} {issue['message']}")
            if issue.get("fix") and verbose:
                lines.append(f"            Fix: {issue['fix']}")

    # Per-page issues — group by severity
    pages_with_issues = [p for p in s["pages"] if p["issue_counts"].get(CRITICAL, 0) + p["issue_counts"].get(HIGH, 0) + p["issue_counts"].get(MEDIUM, 0) + p["issue_counts"].get(LOW, 0) > 0]

    if pages_with_issues:
        # Sort: most severe first
        pages_with_issues.sort(key=lambda p: (
            -p["issue_counts"].get(CRITICAL, 0),
            -p["issue_counts"].get(HIGH, 0),
            -p["issue_counts"].get(MEDIUM, 0),
        ))

        # Show critical/high always, medium/low in verbose
        lines.append(f"\n  Page Issues:")
        shown = 0
        hidden_medium_low = 0
        for page in pages_with_issues:
            page_issues = page["issues"]
            critical_high = [i for i in page_issues if i["severity"] in (CRITICAL, HIGH)]
            medium_low = [i for i in page_issues if i["severity"] in (MEDIUM, LOW)]

            if not critical_high and not verbose:
                hidden_medium_low += len(medium_low)
                continue

            lines.append(f"\n    {page['file']}  ({page['word_count']}w)")
            for issue in sorted(page_issues, key=lambda x: SEVERITY_RANK.get(x["severity"], 9)):
                if issue["severity"] in (MEDIUM, LOW) and not verbose:
                    hidden_medium_low += 1
                    continue
                sym = SEVERITY_SYMBOL.get(issue["severity"], "?")
                lines.append(f"      [{issue['severity']:>8}] {sym} [{issue['category']}] {issue['message']}")
                if issue.get("fix") and verbose:
                    lines.append(f"                  Fix: {issue['fix']}")
            shown += 1

        if hidden_medium_low > 0:
            lines.append(f"\n    ({hidden_medium_low} MEDIUM/LOW issues hidden — use --verbose to see all)")

    # Clean pages
    clean_count = sum(1 for p in s["pages"]
                      if sum(p["issue_counts"].values()) == 0)
    if clean_count > 0:
        lines.append(f"\n  {clean_count} page(s) passed all checks")

    return "\n".join(lines)


def _letter_grade(score):
    if score >= 95: return "A+"
    if score >= 90: return "A"
    if score >= 85: return "A-"
    if score >= 80: return "B+"
    if score >= 75: return "B"
    if score >= 70: return "B-"
    if score >= 65: return "C+"
    if score >= 60: return "C"
    if score >= 50: return "D"
    return "F"


# ── Feature 1: --fix (Auto-Fix) ──────────────────────────────────────

# Fixable issue patterns and their fix functions
FIXABLE_PATTERNS = {
    "FAQPage schema is deprecated": "fix_remove_schema_type",
    "HowTo schema deprecated": "fix_remove_schema_type",
    "missing rel=\"noopener\"": "fix_add_noopener",
    "Trailing slash mismatch": "fix_trailing_slash",
}


def apply_fixes(site_result, dry_run=False):
    """Apply safe auto-fixes to HTML files. Returns list of changes made."""
    changes = []
    site_path = site_result["path"]

    for page in site_result["pages"]:
        filepath = os.path.join(site_path, page["file"])
        page_fixes = []

        for issue in page["issues"]:
            msg = issue["message"]
            for pattern, fix_func in FIXABLE_PATTERNS.items():
                if pattern in msg:
                    page_fixes.append((fix_func, issue))
                    break

        if not page_fixes:
            continue

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            changes.append({"file": page["file"], "error": str(e)})
            continue

        original = content
        for fix_func, issue in page_fixes:
            if fix_func == "fix_remove_schema_type":
                content = _fix_remove_deprecated_schema(content, issue["message"])
            elif fix_func == "fix_add_noopener":
                content = _fix_add_noopener(content)
            elif fix_func == "fix_trailing_slash":
                content = _fix_trailing_slash(content, issue["message"])

        if content != original:
            diff_lines = _simple_diff(original, content, page["file"])
            if dry_run:
                changes.append({"file": page["file"], "action": "would fix", "fixes": [i["message"] for _, i in page_fixes], "diff": diff_lines})
            else:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
                changes.append({"file": page["file"], "action": "fixed", "fixes": [i["message"] for _, i in page_fixes], "diff": diff_lines})

    return changes


def _fix_remove_deprecated_schema(content, message):
    """Remove JSON-LD script blocks containing FAQPage or HowTo."""
    deprecated_types = []
    if "FAQPage" in message:
        deprecated_types.append("FAQPage")
    if "HowTo" in message:
        deprecated_types.append("HowTo")

    for dtype in deprecated_types:
        # Find and remove <script type="application/ld+json"> blocks containing the deprecated type
        pattern = r'<script\s+type=["\']application/ld\+json["\']>\s*\{[^<]*"@type"\s*:\s*"' + dtype + r'"[^<]*\}\s*</script>'
        content = re.sub(pattern, '', content, flags=re.DOTALL | re.IGNORECASE)
        # Clean up blank lines left behind
        content = re.sub(r'\n\s*\n\s*\n', '\n\n', content)

    return content


def _fix_add_noopener(content):
    """Add rel="noopener" to external target="_blank" links missing it."""
    def _add_noopener(match):
        tag = match.group(0)
        if 'target="_blank"' not in tag and "target='_blank'" not in tag:
            return tag
        if 'rel=' in tag:
            # rel exists but might not have noopener
            if 'noopener' not in tag:
                tag = re.sub(r'rel="([^"]*)"', r'rel="\1 noopener"', tag)
                tag = re.sub(r"rel='([^']*)'", r"rel='\1 noopener'", tag)
        else:
            # No rel at all — add before closing >
            tag = tag.rstrip(">").rstrip() + ' rel="noopener">'
        return tag

    return re.sub(r'<a\s[^>]*target=["\']_blank["\'][^>]*>', _add_noopener, content)


def _fix_trailing_slash(content, message):
    """Fix canonical trailing slash to match sitemap."""
    # Extract the expected URL from the message
    # Message format: "Trailing slash mismatch — canonical: X vs sitemap: Y"
    match = re.search(r'sitemap:\s*(\S+)', message)
    if not match:
        return content
    sitemap_url = match.group(1)

    # Replace canonical href
    content = re.sub(
        r'<link\s+rel="canonical"\s+href="[^"]*"',
        f'<link rel="canonical" href="{sitemap_url}"',
        content
    )

    # Also fix og:url to match
    content = re.sub(
        r'(<meta\s+(?:property|name)="og:url"\s+content=")[^"]*(")',
        lambda m: m.group(1) + sitemap_url + m.group(2),
        content
    )

    return content


# ── Bulk Fix Functions ────────────────────────────────────────────────

FAVICON_BLOCK = """  <link rel="icon" href="/images/favicon.ico" sizes="any">
  <link rel="icon" type="image/png" sizes="32x32" href="/images/favicon-32.png">
  <link rel="icon" type="image/png" sizes="192x192" href="/images/favicon-192.png">
  <link rel="apple-touch-icon" href="/images/apple-touch-icon.png">"""


def _truncate_title(title, max_len=60):
    """Truncate title at word boundary, preserving domain suffix if possible."""
    if len(title) <= max_len:
        return title

    # Decode HTML entities for clean truncation
    clean = title.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    clean = clean.replace('&#x27;', "'").replace('&#39;', "'")

    # For "Name — Middle Part | domain" pattern, try progressive trimming
    pipe_match = re.search(r'\s*\|\s*\S+[\.\w]*$', clean)
    dash_match = re.match(r'^(.+?)\s*[—–]\s*(.+?)(\s*\|\s*.+)$', clean)

    if dash_match:
        name_part = dash_match.group(1).strip()
        middle_part = dash_match.group(2).strip()
        suffix_part = dash_match.group(3).strip()

        # Try progressively removing words from middle part's right side
        middle_words = middle_part.split()
        while middle_words:
            mid_text = ' '.join(middle_words).rstrip(' ,;:')
            candidate = f"{name_part} — {mid_text} {suffix_part}"
            if len(candidate) <= max_len:
                return candidate
            middle_words.pop()

        # All middle words removed — just name + suffix
        short = f"{name_part} {suffix_part}"
        if len(short) <= max_len:
            return short

    # Fallback: truncate the part before the pipe at word boundary
    if pipe_match:
        suffix = clean[pipe_match.start():]
        available = max_len - len(suffix)
        if available > 15:
            prefix = clean[:pipe_match.start()].strip()
            if len(prefix) > available:
                truncated = prefix[:available].rsplit(' ', 1)[0].rstrip(' ,;—–-&')
                result = f"{truncated}{suffix}"
                if len(result) <= max_len:
                    return result

    # Last resort: hard truncate at word boundary
    truncated = clean[:max_len].rsplit(' ', 1)[0].rstrip(' ,;—–-&')
    return truncated


def _truncate_description(desc, max_len=155):
    """Truncate description at sentence boundary, then word boundary."""
    if len(desc) <= max_len:
        return desc

    candidate = desc[:max_len]

    # Try sentence boundaries (. ! ?)
    for punct in ['. ', '! ', '? ']:
        last_idx = candidate.rfind(punct)
        if last_idx > 60:
            return candidate[:last_idx + 1].strip()

    # No sentence boundary — truncate at word boundary, add ellipsis
    truncated = candidate[:max_len - 3].rsplit(' ', 1)[0].rstrip(' ,;:—–-')
    return truncated + "..."


def _fix_midword_truncation(desc):
    """Fix descriptions that end mid-word (no punctuation, no ellipsis)."""
    if not desc:
        return desc
    desc = desc.strip()
    if desc.endswith('...') or desc[-1] in '.!?':
        return desc
    cleaned = desc.rstrip(' ,;:—–-')
    if ' ' in cleaned:
        cleaned = cleaned.rsplit(' ', 1)[0].rstrip(' ,;:—–-')
    return cleaned + "..."


def _re_get_title(content):
    m = re.search(r'<title>([^<]*)</title>', content, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _re_get_meta_desc(content):
    m = re.search(r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']*)["\']', content, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _re_is_noindex(content):
    return bool(re.search(r'<meta\s+name=["\']robots["\']\s+content=["\'][^"\']*noindex', content, re.IGNORECASE))


def _re_has_canonical(content):
    return bool(re.search(r'<link\s+rel=["\']canonical["\']', content, re.IGNORECASE))


def _re_has_jsonld(content):
    return bool(re.search(r'<script\s+type=["\']application/ld\+json["\']', content, re.IGNORECASE))


def apply_bulk_fixes(site_result, dry_run=False, domain=None):
    """Apply comprehensive bulk fixes to all HTML files in a site. Returns list of changes."""
    changes = []
    site_path = site_result["path"]

    # Use provided domain, or fall back to auto-detected domain from audit
    if domain is None:
        domain = site_result.get("domain", "")

    if not domain:
        print("  Warning: No domain set — OG/schema URL fixes will use placeholder. Use --domain to set.")
        domain = "example.com"

    for page in site_result["pages"]:
        filepath = os.path.join(site_path, page["file"])
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue

        original = content
        fixes_applied = []

        # --- 1. Remove canonical from noindex pages ---
        if _re_is_noindex(content) and _re_has_canonical(content):
            content = re.sub(r'\s*<link\s+rel=["\']canonical["\'][^>]*>\s*\n?', '\n', content)
            fixes_applied.append("Removed canonical from noindex page")

        # --- 2a. Clean trailing punctuation from titles ---
        title = _re_get_title(content)
        if title:
            cleaned = re.sub(r',(\s*\|)', r'\1', title)
            cleaned = cleaned.rstrip(',; ')
            if cleaned != title:
                content = content.replace(f'<title>{title}</title>', f'<title>{cleaned}</title>', 1)
                fixes_applied.append(f"Cleaned title punctuation: \"{cleaned}\"")
                title = cleaned

        # --- 2b. Truncate long titles ---
        if title and len(title) > 60:
            new_title = _truncate_title(title)
            if new_title != title and len(new_title) <= 60:
                content = content.replace(f'<title>{title}</title>', f'<title>{new_title}</title>', 1)
                # Also update og:title and twitter:title if they match
                for attr in ['property="og:title"', 'property=\'og:title\'',
                             'name="twitter:title"', 'name=\'twitter:title\'']:
                    og_m = re.search(rf'<meta\s+{re.escape(attr)}\s+content=["\']([^"\']*)["\']', content)
                    if og_m and len(og_m.group(1)) > 60:
                        new_og = _truncate_title(og_m.group(1))
                        if new_og != og_m.group(1):
                            content = content.replace(og_m.group(0), og_m.group(0).replace(og_m.group(1), new_og), 1)
                fixes_applied.append(f"Truncated title {len(title)}→{len(new_title)}: \"{new_title}\"")

        # --- 3. Truncate long descriptions ---
        desc = _re_get_meta_desc(content)
        if desc and len(desc) > 155:
            new_desc = _truncate_description(desc)
            if new_desc != desc:
                content = content.replace(f'content="{desc}"', f'content="{new_desc}"', 1)
                content = content.replace(f"content='{desc}'", f"content='{new_desc}'", 1)
                fixes_applied.append(f"Truncated description {len(desc)}→{len(new_desc)}")

        # --- 4. Fix mid-word truncation in descriptions ---
        desc = _re_get_meta_desc(content)
        if desc and len(desc) >= 100 and not desc.endswith('...') and desc[-1] not in '.!?':
            new_desc = _fix_midword_truncation(desc)
            if new_desc != desc:
                content = content.replace(f'content="{desc}"', f'content="{new_desc}"', 1)
                content = content.replace(f"content='{desc}'", f"content='{new_desc}'", 1)
                fixes_applied.append("Fixed mid-word truncation in description")

        # --- 5a. Add viewport meta where missing ---
        if not re.search(r'<meta\s+name=["\']viewport["\']', content, re.IGNORECASE) and '<head>' in content.lower():
            content = re.sub(
                r'(<meta\s+charset=["\']UTF-8["\']>)',
                r'\1\n  <meta name="viewport" content="width=device-width, initial-scale=1.0">',
                content, count=1, flags=re.IGNORECASE
            )
            fixes_applied.append("Added viewport meta tag")

        # --- 5a-mobile. Fix zoom-blocking viewport ---
        vp_match = re.search(r'<meta\s+name=["\']viewport["\']\s+content=["\']([^"\']*)["\']', content, re.IGNORECASE)
        if vp_match:
            vp_content = vp_match.group(1)
            new_vp = vp_content
            new_vp = re.sub(r',?\s*user-scalable\s*=\s*(?:no|0)', '', new_vp)
            new_vp = re.sub(r',?\s*maximum-scale\s*=\s*1(?:[^.]|$)', '', new_vp)
            new_vp = new_vp.strip(', ')
            if new_vp != vp_content:
                content = content.replace(vp_match.group(0),
                    vp_match.group(0).replace(vp_content, new_vp))
                fixes_applied.append("Fixed zoom-blocking viewport (removed user-scalable/maximum-scale)")

        # --- 5b. Add OG + Twitter tags where missing ---
        has_og = bool(re.search(r'<meta\s+property=["\']og:title["\']', content, re.IGNORECASE))
        if not has_og and '</head>' in content.lower():
            t = _re_get_title(content) or "Page"
            d = _re_get_meta_desc(content) or t
            canonical_m = re.search(r'<link\s+rel=["\']canonical["\']\s+href=["\']([^"\']*)["\']', content)
            url = canonical_m.group(1) if canonical_m else f"https://{domain}/"
            og_block = (
                f'  <meta property="og:title" content="{t}">\n'
                f'  <meta property="og:description" content="{d}">\n'
                f'  <meta property="og:type" content="website">\n'
                f'  <meta property="og:url" content="{url}">\n'
                f'  <meta property="og:image" content="https://{domain}/images/og-image.svg">\n'
                f'  <meta name="twitter:card" content="summary_large_image">\n'
                f'  <meta name="twitter:title" content="{t}">\n'
                f'  <meta name="twitter:description" content="{d}">\n'
                f'  <meta name="twitter:image" content="https://{domain}/images/og-image.svg">\n'
            )
            content = content.replace('</head>', f'{og_block}</head>', 1)
            fixes_applied.append("Added OG + Twitter card tags")

        # --- 5c. Add missing og:image where other OG tags exist ---
        if has_og and not re.search(r'<meta\s+property=["\']og:image["\']', content, re.IGNORECASE):
            img_tag = f'  <meta property="og:image" content="https://{domain}/images/og-image.svg">\n'
            content = re.sub(
                r'(<meta\s+property=["\']og:type["\'][^>]*>)',
                lambda m: m.group(0) + '\n' + img_tag.rstrip('\n'),
                content, count=1
            )
            fixes_applied.append("Added missing og:image tag")

        # --- 6. Add favicon where missing ---
        if not (re.search(r'<link\s+rel=["\']icon["\']', content, re.IGNORECASE) or
                re.search(r'<link\s+rel=["\']apple-touch-icon["\']', content, re.IGNORECASE)):
            if '</head>' in content.lower():
                content = content.replace('</head>', f'{FAVICON_BLOCK}\n</head>', 1)
                fixes_applied.append("Added favicon links")

        # --- 7. Add <main> landmark where missing ---
        if not re.search(r'<main[\s>]', content, re.IGNORECASE) and '<body>' in content.lower():
            if '</header>' in content and '<footer' in content:
                header_end = content.find('</header>')
                if header_end > 0:
                    header_end_full = content.index('>', header_end) + 1
                    footer_start = content.find('<footer')
                    if footer_start > header_end_full:
                        inner = content[header_end_full:footer_start]
                        if inner.strip():
                            content = (content[:header_end_full] +
                                       '\n  <main>\n' + inner + '  </main>\n\n  ' +
                                       content[footer_start:])
                            fixes_applied.append("Wrapped content in <main>")

        # --- 8. Fix JSON-LD issues (newlines, HTML entities) ---
        if _re_has_jsonld(content):
            def _fix_jsonld_block(match):
                block = match.group(1)
                fixed = re.sub(r'(?<=": ")(.*?)(?=")', lambda m: m.group(0).replace('\n', '\\n'), block, flags=re.DOTALL)
                for old, new in [('&#x27;', "'"), ('&#39;', "'"), ('&amp;', '&'), ('&quot;', '\\"'), ('&lt;', '<'), ('&gt;', '>')]:
                    fixed = fixed.replace(old, new)
                try:
                    json.loads(fixed)
                except json.JSONDecodeError:
                    return match.group(0)
                if fixed != block:
                    return f'<script type="application/ld+json">{fixed}</script>'
                return match.group(0)

            new_content = re.sub(
                r'<script\s+type=["\']application/ld\+json["\']>(.*?)</script>',
                _fix_jsonld_block, content, flags=re.DOTALL | re.IGNORECASE
            )
            if new_content != content:
                content = new_content
                fixes_applied.append("Fixed JSON-LD syntax issues")

        # --- 9. Fix Article schema (image/author/datePublished) ---
        if _re_has_jsonld(content):
            jsonld_m = re.search(
                r'(<script\s+type=["\']application/ld\+json["\']>)(.*?)(</script>)',
                content, re.DOTALL | re.IGNORECASE
            )
            if jsonld_m:
                try:
                    schema = json.loads(jsonld_m.group(2))
                    if schema.get("@type") == "Article":
                        changed = False
                        if "image" not in schema:
                            schema["image"] = f"https://{domain}/images/og-image.svg"
                            changed = True
                        if "author" not in schema:
                            schema["author"] = {"@type": "Person", "name": "Editorial Team"}
                            changed = True
                        if "datePublished" not in schema:
                            date_m = re.search(r'(\d{4}-\d{2}-\d{2})', page["file"])
                            schema["datePublished"] = date_m.group(1) if date_m else "2026-01-01"
                            changed = True
                        if changed:
                            new_json = json.dumps(schema, indent=2, ensure_ascii=False)
                            content = content.replace(
                                jsonld_m.group(0),
                                f'{jsonld_m.group(1)}\n{new_json}\n{jsonld_m.group(3)}'
                            )
                            fixes_applied.append("Fixed Article schema (image/author/datePublished)")
                except (json.JSONDecodeError, AttributeError):
                    pass

        # --- 10. Add WebPage schema if no schema at all ---
        if not _re_has_jsonld(content) and not _re_is_noindex(content):
            title_val = _re_get_title(content)
            canonical_m = re.search(r'<link\s+rel=["\']canonical["\']\s+href=["\']([^"\']*)["\']', content)
            url_val = canonical_m.group(1) if canonical_m else f"https://{domain}/"
            if title_val:
                schema = {"@context": "https://schema.org", "@type": "WebPage", "name": title_val, "url": url_val}
                desc_val = _re_get_meta_desc(content)
                if desc_val:
                    schema["description"] = desc_val
                schema_block = f'  <script type="application/ld+json">\n  {json.dumps(schema, indent=2, ensure_ascii=False)}\n  </script>\n'
                content = content.replace('</head>', f'{schema_block}</head>', 1)
                fixes_applied.append("Added WebPage JSON-LD schema")

        # --- Write if changed ---
        if content != original:
            diff_lines = _simple_diff(original, content, page["file"])
            if dry_run:
                changes.append({"file": page["file"], "action": "would fix", "fixes": fixes_applied, "diff": diff_lines})
            else:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
                changes.append({"file": page["file"], "action": "fixed", "fixes": fixes_applied, "diff": diff_lines})

    return changes


def _simple_diff(old, new, _filename=""):
    """Generate a simple unified-style diff."""
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    diff = []
    for i, (ol, nl) in enumerate(zip(old_lines, new_lines)):
        if ol != nl:
            diff.append(f"  L{i+1}:")
            diff.append(f"    - {ol.strip()[:120]}")
            diff.append(f"    + {nl.strip()[:120]}")
    # Handle length differences
    if len(new_lines) < len(old_lines):
        diff.append(f"  Removed {len(old_lines) - len(new_lines)} line(s)")
    elif len(new_lines) > len(old_lines):
        diff.append(f"  Added {len(new_lines) - len(old_lines)} line(s)")
    return diff


def format_fix_report(all_changes):
    """Format fix results as readable text."""
    lines = []
    total_fixed = 0
    for site_name, changes in all_changes:
        if not changes:
            continue
        lines.append(f"\n  {site_name}:")
        for ch in changes:
            if "error" in ch:
                lines.append(f"    [ERROR] {ch['file']}: {ch['error']}")
            else:
                total_fixed += len(ch["fixes"])
                lines.append(f"    [{ch['action'].upper()}] {ch['file']}")
                for fix in ch["fixes"]:
                    lines.append(f"      - {fix}")
                for dl in ch.get("diff", []):
                    lines.append(f"      {dl}")
    if total_fixed:
        lines.insert(0, f"\n  Total fixes: {total_fixed}")
    else:
        lines.insert(0, "\n  No fixable issues found.")
    return "\n".join(lines)


# ── Feature 2: --diff (Snapshot Comparison) ──────────────────────────

def save_snapshot(all_results):
    """Save current audit results as a timestamped JSON snapshot."""
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    snapshot_path = os.path.join(SNAPSHOT_DIR, f"audit_{ts}.json")

    # Strip raw_hrefs and other bulky data for compact snapshots
    compact = []
    for result in all_results:
        site = {
            "site": result["site"],
            "pages_scanned": result["pages_scanned"],
            "total_by_severity": result["total_by_severity"],
            "total_issues": result["total_issues"],
            "site_issues": result["site_issues"],
            "pages": [],
        }
        for page in result["pages"]:
            site["pages"].append({
                "file": page["file"],
                "title": page["title"],
                "issues": page["issues"],
                "issue_counts": page["issue_counts"],
            })
        compact.append(site)

    with open(snapshot_path, "w") as f:
        json.dump({"timestamp": ts, "sites": compact}, f, indent=2, default=str)

    # Also save as "latest" symlink
    latest_path = os.path.join(SNAPSHOT_DIR, "latest.json")
    if os.path.exists(latest_path) or os.path.islink(latest_path):
        os.remove(latest_path)
    os.symlink(snapshot_path, latest_path)

    return snapshot_path


def load_last_snapshot():
    """Load the most recent audit snapshot."""
    latest = os.path.join(SNAPSHOT_DIR, "latest.json")
    if not os.path.exists(latest):
        return None
    try:
        with open(latest, "r") as f:
            return json.load(f)
    except Exception:
        return None


def diff_results(current_results, snapshot):
    """Compare current audit against a snapshot. Returns diff report."""
    if not snapshot:
        return None

    old_sites = {s["site"]: s for s in snapshot.get("sites", [])}
    report = {"timestamp": snapshot.get("timestamp", "unknown"), "sites": []}

    for current in current_results:
        site_name = current["site"]
        old = old_sites.get(site_name)
        if not old:
            report["sites"].append({
                "site": site_name,
                "status": "NEW SITE",
                "new_issues": current["total_issues"],
            })
            continue

        # Build issue sets: (file, message) tuples
        def _issue_set(site_data):
            issues = set()
            for page in site_data.get("pages", []):
                for issue in page.get("issues", []):
                    issues.add((page["file"], issue["message"]))
            for issue in site_data.get("site_issues", []):
                issues.add(("__site__", issue["message"]))
            return issues

        old_issues = _issue_set(old)
        new_issues = _issue_set(current)

        fixed = old_issues - new_issues
        regressions = new_issues - old_issues
        unchanged = old_issues & new_issues

        report["sites"].append({
            "site": site_name,
            "old_total": old.get("total_issues", 0),
            "new_total": current["total_issues"],
            "fixed": sorted(fixed),
            "regressions": sorted(regressions),
            "unchanged_count": len(unchanged),
        })

    return report


def format_diff_report(diff):
    """Format diff results as readable text."""
    if not diff:
        return "\n  No previous snapshot found. Run without --diff first to create a baseline."

    lines = [f"\n  Comparing against snapshot: {diff['timestamp']}"]
    lines.append(f"{'='*60}")

    total_fixed = 0
    total_regressions = 0

    for site in diff["sites"]:
        if site.get("status") == "NEW SITE":
            lines.append(f"\n  {site['site']} — NEW SITE ({site['new_issues']} issues)")
            continue

        delta = site["new_total"] - site["old_total"]
        direction = f"+{delta}" if delta > 0 else str(delta)

        lines.append(f"\n  {site['site']} — {site['old_total']} → {site['new_total']} ({direction})")

        if site["fixed"]:
            total_fixed += len(site["fixed"])
            lines.append(f"    Fixed ({len(site['fixed'])}):")
            for f_file, f_msg in site["fixed"][:10]:
                label = f_file if f_file != "__site__" else "site-level"
                lines.append(f"      - [{label}] {f_msg[:100]}")
            if len(site["fixed"]) > 10:
                lines.append(f"      ... and {len(site['fixed']) - 10} more")

        if site["regressions"]:
            total_regressions += len(site["regressions"])
            lines.append(f"    Regressions ({len(site['regressions'])}):")
            for r_file, r_msg in site["regressions"][:10]:
                label = r_file if r_file != "__site__" else "site-level"
                lines.append(f"      + [{label}] {r_msg[:100]}")
            if len(site["regressions"]) > 10:
                lines.append(f"      ... and {len(site['regressions']) - 10} more")

        if not site["fixed"] and not site["regressions"]:
            lines.append(f"    No changes ({site['unchanged_count']} issues unchanged)")

    lines.append(f"\n{'='*60}")
    lines.append(f"  Summary: {total_fixed} fixed, {total_regressions} regressions")
    if total_regressions > 0:
        lines.append(f"  ACTION NEEDED: {total_regressions} new issue(s) introduced!")
    elif total_fixed > 0:
        lines.append(f"  Progress! {total_fixed} issue(s) resolved since last audit.")
    else:
        lines.append(f"  No change since last audit.")

    return "\n".join(lines)


# ── Feature 5: Self-Learning Loop ────────────────────────────────────

LEARNINGS_FILE = os.path.join(SNAPSHOT_DIR, "learnings.json")

# Severity escalation ladder
_ESCALATION = {LOW: MEDIUM, MEDIUM: HIGH, HIGH: CRITICAL}
_RECURRING_THRESHOLD = 3  # consecutive audits before escalation


def _normalize_issue_key(issue):
    """Create a stable key from an issue, removing variable counts/paths."""
    msg = issue.get("message", "") if isinstance(issue, dict) else issue.message
    cat = issue.get("category", "") if isinstance(issue, dict) else issue.category
    # Remove variable numbers (e.g., "5 image(s)" → "N image(s)")
    key = re.sub(r'\d+ (image|page|link|orphan|external|below)', 'N \\1', msg)
    # Remove quoted file paths/content that change between runs
    key = re.sub(r'"[^"]{20,}"', '"..."', key)
    # Remove specific counts in parens
    key = re.sub(r'\(\d+x\)', '(Nx)', key)
    return f"{cat}:{key}"


def load_learnings():
    """Load accumulated learning history from disk."""
    if not os.path.exists(LEARNINGS_FILE):
        return {"history": {}, "meta": {"audits_run": 0, "first_audit": None, "last_audit": None}}
    try:
        with open(LEARNINGS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"history": {}, "meta": {"audits_run": 0, "first_audit": None, "last_audit": None}}


def update_learnings(learnings, all_results):
    """Update learning history with current audit results. Returns updated learnings."""
    today = datetime.now().strftime("%Y-%m-%d")
    learnings["meta"]["audits_run"] = learnings["meta"].get("audits_run", 0) + 1
    if not learnings["meta"].get("first_audit"):
        learnings["meta"]["first_audit"] = today
    learnings["meta"]["last_audit"] = today

    for result in all_results:
        site = result["site"]
        if site not in learnings["history"]:
            learnings["history"][site] = {}

        site_history = learnings["history"][site]
        current_keys = set()

        # Collect all current issue keys
        for page in result["pages"]:
            for issue in page["issues"]:
                key = _normalize_issue_key(issue)
                current_keys.add(key)
                if key not in site_history:
                    site_history[key] = {
                        "count": 1,
                        "first_seen": today,
                        "last_seen": today,
                        "consecutive": 1,
                        "original_severity": issue["severity"],
                        "category": issue["category"],
                    }
                else:
                    entry = site_history[key]
                    entry["count"] = entry.get("count", 0) + 1
                    entry["last_seen"] = today
                    if entry.get("present_last_audit"):
                        entry["consecutive"] = entry.get("consecutive", 0) + 1
                    else:
                        entry["consecutive"] = 1

        for issue in result.get("site_issues", []):
            key = _normalize_issue_key(issue)
            current_keys.add(key)
            if key not in site_history:
                site_history[key] = {
                    "count": 1,
                    "first_seen": today,
                    "last_seen": today,
                    "consecutive": 1,
                    "original_severity": issue["severity"],
                    "category": issue["category"],
                }
            else:
                entry = site_history[key]
                entry["count"] = entry.get("count", 0) + 1
                entry["last_seen"] = today
                if entry.get("present_last_audit"):
                    entry["consecutive"] = entry.get("consecutive", 0) + 1
                else:
                    entry["consecutive"] = 1

        # Mark presence flags for next audit
        for key in site_history:
            site_history[key]["present_last_audit"] = key in current_keys

        # Prune stale entries (not seen in 6+ audits)
        stale = [k for k, v in site_history.items()
                 if not v.get("present_last_audit") and v.get("consecutive", 0) == 0
                 and v.get("count", 0) <= 1]
        for k in stale:
            del site_history[k]

    # Save to disk
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    with open(LEARNINGS_FILE, "w") as f:
        json.dump(learnings, f, indent=2)

    return learnings


def apply_learnings(all_results, learnings):
    """Apply learning insights — escalate recurring issues in-place. Returns count of escalations."""
    escalated = 0

    for result in all_results:
        site = result["site"]
        site_history = learnings.get("history", {}).get(site, {})

        for page in result["pages"]:
            for issue in page["issues"]:
                key = _normalize_issue_key(issue)
                entry = site_history.get(key, {})
                consecutive = entry.get("consecutive", 0)
                if consecutive >= _RECURRING_THRESHOLD:
                    new_sev = _ESCALATION.get(issue["severity"])
                    if new_sev:
                        issue["severity"] = new_sev
                        issue["message"] = f"[RECURRING x{consecutive}] {issue['message']}"
                        escalated += 1

        for issue in result.get("site_issues", []):
            key = _normalize_issue_key(issue)
            entry = site_history.get(key, {})
            consecutive = entry.get("consecutive", 0)
            if consecutive >= _RECURRING_THRESHOLD:
                new_sev = _ESCALATION.get(issue["severity"])
                if new_sev:
                    issue["severity"] = new_sev
                    issue["message"] = f"[RECURRING x{consecutive}] {issue['message']}"
                    escalated += 1

    return escalated


def format_learning_report(learnings, all_results):
    """Format learning insights as readable text."""
    lines = []
    meta = learnings.get("meta", {})

    if meta.get("audits_run", 0) < 2:
        lines.append("\n  Learning: Baseline established. Run again next month to see trends.")
        return "\n".join(lines)

    lines.append(f"\n{'='*60}")
    lines.append(f"  SELF-LEARNING INSIGHTS ({meta['audits_run']} audits since {meta.get('first_audit', '?')})")
    lines.append(f"{'='*60}")

    for result in all_results:
        site = result["site"]
        history = learnings.get("history", {}).get(site, {})
        if not history:
            continue

        # Recurring issues (consecutive >= threshold)
        recurring = {k: v for k, v in history.items()
                     if v.get("consecutive", 0) >= _RECURRING_THRESHOLD}

        # Category breakdown
        categories = {}
        for k, v in history.items():
            if v.get("present_last_audit"):
                cat = v.get("category", "?")
                categories[cat] = categories.get(cat, 0) + 1

        # Fix rate: issues that were seen before but are now gone
        fixed_count = sum(1 for v in history.values()
                         if not v.get("present_last_audit") and v.get("count", 0) > 1)
        active_count = sum(1 for v in history.values() if v.get("present_last_audit"))
        total_tracked = fixed_count + active_count
        fix_rate = (fixed_count / total_tracked * 100) if total_tracked > 0 else 0

        lines.append(f"\n  {site}")
        lines.append(f"    Active: {active_count} | Fixed: {fixed_count} | Fix rate: {fix_rate:.0f}%")

        if categories:
            top_cats = sorted(categories.items(), key=lambda x: -x[1])[:3]
            cats_str = ", ".join(f"{c}({n})" for c, n in top_cats)
            lines.append(f"    Top categories: {cats_str}")

        if recurring:
            lines.append(f"    Recurring ({len(recurring)} — unfixed for {_RECURRING_THRESHOLD}+ audits):")
            for key, entry in sorted(recurring.items(), key=lambda x: -x[1]["consecutive"])[:5]:
                short_msg = key.split(":", 1)[1][:80] if ":" in key else key[:80]
                lines.append(f"      [{entry.get('original_severity', '?')}->ESCALATED] {short_msg} (x{entry['consecutive']})")

    return "\n".join(lines)


# ── Feature 3: --lighthouse (Core Web Vitals) ────────────────────────

def run_lighthouse(site_result, domain=None, pages=None):
    """Run Lighthouse on live URLs for a site. Returns CWV data."""
    domain = domain or site_result.get("domain", "")
    if not domain:
        return {"error": f"No domain available for {site_result['site']}. Use --domain to set."}

    results = []
    # Default: audit index + up to 4 pages with most issues
    if pages is None:
        target_pages = ["index.html"]
        # Add worst pages
        worst = sorted(site_result["pages"],
                       key=lambda p: p["issue_counts"].get(CRITICAL, 0) * 10 + p["issue_counts"].get(HIGH, 0),
                       reverse=True)
        for p in worst:
            if p["file"] not in target_pages and "404" not in p["file"]:
                target_pages.append(p["file"])
            if len(target_pages) >= 5:
                break
    else:
        target_pages = pages

    for page_file in target_pages:
        # Convert file path to URL
        if page_file == "index.html":
            url = f"https://{domain}/"
        else:
            url_path = page_file.replace("index.html", "")
            url = f"https://{domain}/{url_path}"

        print(f"  Running Lighthouse: {url} ...", flush=True)
        try:
            proc = subprocess.run(
                ["npx", "lighthouse", url,
                 "--output=json", "--quiet",
                 "--chrome-flags=--headless --no-sandbox",
                 "--only-categories=performance,seo,accessibility"],
                capture_output=True, text=True, timeout=120
            )
            if proc.returncode != 0:
                results.append({"url": url, "error": proc.stderr[:200]})
                continue

            data = json.loads(proc.stdout)
            cats = data.get("categories", {})
            audits = data.get("audits", {})

            result = {
                "url": url,
                "file": page_file,
                "scores": {
                    "performance": int((cats.get("performance", {}).get("score") or 0) * 100),
                    "seo": int((cats.get("seo", {}).get("score") or 0) * 100),
                    "accessibility": int((cats.get("accessibility", {}).get("score") or 0) * 100),
                },
                "cwv": {},
            }

            # Extract Core Web Vitals
            for metric, audit_id in [
                ("LCP", "largest-contentful-paint"),
                ("CLS", "cumulative-layout-shift"),
                ("TBT", "total-blocking-time"),  # proxy for INP
                ("FCP", "first-contentful-paint"),
                ("SI", "speed-index"),
            ]:
                audit = audits.get(audit_id, {})
                if audit.get("numericValue") is not None:
                    result["cwv"][metric] = {
                        "value": round(audit["numericValue"], 1),
                        "unit": audit.get("numericUnit", "ms"),
                        "score": audit.get("score"),
                    }

            results.append(result)

        except subprocess.TimeoutExpired:
            results.append({"url": url, "error": "Timeout (120s)"})
        except json.JSONDecodeError:
            results.append({"url": url, "error": "Invalid Lighthouse JSON output"})
        except FileNotFoundError:
            results.append({"url": url, "error": "npx/lighthouse not found — run: npm i -g lighthouse"})
            break
        except Exception as e:
            results.append({"url": url, "error": str(e)[:200]})

    return {"site": site_result["site"], "domain": domain, "pages": results}


def format_lighthouse_report(lh_results):
    """Format Lighthouse results as readable text."""
    lines = []

    for lh in lh_results:
        if "error" in lh:
            lines.append(f"\n  {lh.get('site', '?')} — ERROR: {lh['error']}")
            continue

        lines.append(f"\n{'='*60}")
        lines.append(f"  Lighthouse: {lh['site']} ({lh['domain']})")
        lines.append(f"{'='*60}")

        for page in lh["pages"]:
            if "error" in page:
                lines.append(f"\n  {page['url']} — ERROR: {page['error']}")
                continue

            scores = page["scores"]
            perf = scores["performance"]
            seo = scores["seo"]
            a11y = scores["accessibility"]

            perf_grade = "GOOD" if perf >= 90 else ("OK" if perf >= 50 else "POOR")

            lines.append(f"\n  {page['file']}")
            lines.append(f"    Performance: {perf}/100 [{perf_grade}] | SEO: {seo}/100 | Accessibility: {a11y}/100")

            if page.get("cwv"):
                cwv_parts = []
                for metric, data in page["cwv"].items():
                    unit = "ms" if data["unit"] == "millisecond" else data["unit"]
                    if metric == "CLS":
                        cwv_parts.append(f"{metric}: {data['value']:.3f}")
                    elif data["value"] > 1000:
                        cwv_parts.append(f"{metric}: {data['value']/1000:.1f}s")
                    else:
                        cwv_parts.append(f"{metric}: {data['value']:.0f}{unit}")
                lines.append(f"    CWV: {' | '.join(cwv_parts)}")

    return "\n".join(lines)


# ── Feature 4: --gsc (Google Search Console) ─────────────────────────

GSC_SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
GSC_TOKEN_PATH = os.path.join(SNAPSHOT_DIR, "gsc_token.json")
GSC_CLIENT_SECRET = os.path.expanduser("~/.seo-audit/client_secret.json")


def get_gsc_service(client_secret_path=None):
    """Authenticate and return a GSC API service object."""
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        print("  GSC requires: pip install google-api-python-client google-auth-oauthlib")
        return None

    secret_path = client_secret_path or GSC_CLIENT_SECRET

    creds = None
    if os.path.exists(GSC_TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(GSC_TOKEN_PATH, GSC_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(secret_path):
                print(f"  GSC client secret not found: {secret_path}")
                print(f"  Place your OAuth client secret JSON at {GSC_CLIENT_SECRET}")
                print(f"  Or use --gsc-secret PATH to specify a different location.")
                return None
            flow = InstalledAppFlow.from_client_secrets_file(secret_path, GSC_SCOPES)
            creds = flow.run_local_server(port=0)
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        with open(GSC_TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    return build("searchconsole", "v1", credentials=creds)


def fetch_gsc_data(site_result, domain=None, days=28, client_secret_path=None):
    """Fetch Search Console data for a site."""
    domain = domain or site_result.get("domain", "")
    if not domain:
        return {"error": f"No domain available for {site_result['site']}. Use --domain to set."}

    service = get_gsc_service(client_secret_path=client_secret_path)
    if not service:
        return {"error": "Could not authenticate with Google Search Console"}

    # GSC site URL format
    site_url = f"sc-domain:{domain}"

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    try:
        # Page-level data
        response = service.searchanalytics().query(
            siteUrl=site_url,
            body={
                "startDate": start_date,
                "endDate": end_date,
                "dimensions": ["page"],
                "rowLimit": 100,
                "type": "web",
            }
        ).execute()

        pages = []
        for row in response.get("rows", []):
            url = row["keys"][0]
            # Convert URL to relative file path
            path = url.replace(f"https://{domain}/", "").replace(f"http://{domain}/", "")
            pages.append({
                "url": url,
                "file": path or "index.html",
                "clicks": row.get("clicks", 0),
                "impressions": row.get("impressions", 0),
                "ctr": round(row.get("ctr", 0) * 100, 1),
                "position": round(row.get("position", 0), 1),
            })

        # Query-level data (top keywords)
        query_response = service.searchanalytics().query(
            siteUrl=site_url,
            body={
                "startDate": start_date,
                "endDate": end_date,
                "dimensions": ["query"],
                "rowLimit": 20,
                "type": "web",
            }
        ).execute()

        queries = []
        for row in query_response.get("rows", []):
            queries.append({
                "query": row["keys"][0],
                "clicks": row.get("clicks", 0),
                "impressions": row.get("impressions", 0),
                "ctr": round(row.get("ctr", 0) * 100, 1),
                "position": round(row.get("position", 0), 1),
            })

        return {
            "site": site_result["site"],
            "domain": domain,
            "period": f"{start_date} to {end_date}",
            "pages": sorted(pages, key=lambda p: -p["clicks"]),
            "queries": sorted(queries, key=lambda q: -q["clicks"]),
        }

    except Exception as e:
        error_msg = str(e)
        if "403" in error_msg or "not verified" in error_msg.lower():
            return {"error": f"{domain} not verified in Google Search Console. Add it at https://search.google.com/search-console/"}
        return {"error": error_msg[:300]}


def format_gsc_report(gsc_results):
    """Format GSC data as readable text."""
    lines = []

    for gsc in gsc_results:
        if "error" in gsc:
            lines.append(f"\n  {gsc.get('site', gsc.get('domain', '?'))} — ERROR: {gsc['error']}")
            continue

        lines.append(f"\n{'='*60}")
        lines.append(f"  Search Console: {gsc['site']} ({gsc['domain']})")
        lines.append(f"  Period: {gsc['period']}")
        lines.append(f"{'='*60}")

        if gsc["pages"]:
            total_clicks = sum(p["clicks"] for p in gsc["pages"])
            total_impressions = sum(p["impressions"] for p in gsc["pages"])
            lines.append(f"\n  Totals: {total_clicks} clicks, {total_impressions:,} impressions")

            lines.append(f"\n  Top Pages by Clicks:")
            lines.append(f"  {'Page':<50} {'Clicks':>7} {'Impr':>8} {'CTR':>6} {'Pos':>5}")
            lines.append(f"  {'-'*50} {'-'*7} {'-'*8} {'-'*6} {'-'*5}")
            for p in gsc["pages"][:15]:
                path = p["file"][:48] or "/"
                lines.append(f"  {path:<50} {p['clicks']:>7} {p['impressions']:>8} {p['ctr']:>5.1f}% {p['position']:>5.1f}")

        if gsc["queries"]:
            lines.append(f"\n  Top Queries:")
            lines.append(f"  {'Query':<50} {'Clicks':>7} {'Impr':>8} {'CTR':>6} {'Pos':>5}")
            lines.append(f"  {'-'*50} {'-'*7} {'-'*8} {'-'*6} {'-'*5}")
            for q in gsc["queries"][:15]:
                query = q["query"][:48]
                lines.append(f"  {query:<50} {q['clicks']:>7} {q['impressions']:>8} {q['ctr']:>5.1f}% {q['position']:>5.1f}")

    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────

def resolve_site(path):
    """Validate that a directory path exists and return (name, path)."""
    abs_path = os.path.abspath(path)
    if not os.path.isdir(abs_path):
        return None
    name = os.path.basename(abs_path) or abs_path
    return (name, abs_path)


def main():
    parser = argparse.ArgumentParser(
        description="SEO Audit Tool — comprehensive SEO scanner for any static site",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  seo-audit ./my-site
  seo-audit ./site-a ./site-b --verbose
  seo-audit ./my-site --fix --dry-run
  seo-audit ./my-site --domain example.com --lighthouse
  seo-audit ./my-site --related-domains "site-b.com,site-c.com"
  seo-audit ./my-site --gsc --gsc-secret ~/Downloads/client_secret.json
        """
    )
    parser.add_argument("paths", nargs="+", help="One or more site directories to audit")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show all issues + fixes")
    parser.add_argument("--fix", action="store_true", help="Auto-fix safe issues")
    parser.add_argument("--dry-run", action="store_true", help="Preview fixes without writing (use with --fix)")
    parser.add_argument("--diff", action="store_true", help="Compare against last audit snapshot")
    parser.add_argument("--lighthouse", action="store_true", help="Run Lighthouse for Core Web Vitals")
    parser.add_argument("--gsc", action="store_true", help="Pull Google Search Console data")
    parser.add_argument("--domain", default=None,
                        help="Site domain (for Lighthouse/GSC/OG fixes). Auto-detected from canonical if not set.")
    parser.add_argument("--related-domains", default=None,
                        help="Comma-separated related domains for cross-link checking (e.g. 'site-b.com,site-c.com')")
    parser.add_argument("--gsc-secret", default=None,
                        help=f"Path to GSC OAuth client secret JSON (default: {os.path.expanduser('~/.seo-audit/client_secret.json')})")
    parser.add_argument("--skip-dirs", default=None,
                        help="Comma-separated additional directory names to skip during scan")
    args = parser.parse_args()

    # Parse related domains
    related_domains = []
    if args.related_domains:
        related_domains = [d.strip() for d in args.related_domains.split(",") if d.strip()]

    # Parse extra skip dirs
    extra_skip_dirs = set()
    if args.skip_dirs:
        extra_skip_dirs = {d.strip() for d in args.skip_dirs.split(",") if d.strip()}

    # Resolve all site paths
    sites = []
    for path in args.paths:
        resolved = resolve_site(path)
        if not resolved:
            print(f"Error: '{path}' is not a valid directory.")
            sys.exit(1)
        sites.append(resolved)

    # Run core audit
    all_results = []
    for name, path in sites:
        result = audit_site(
            path,
            related_domains=related_domains,
            domain=args.domain,
            extra_skip_dirs=extra_skip_dirs if extra_skip_dirs else None,
        )
        all_results.append(result)

    # ── Self-learning: apply insights from past audits ────────────
    learnings = load_learnings()
    escalated = apply_learnings(all_results, learnings)

    # ── Core report ──────────────────────────────────────────────
    if not args.fix and not args.lighthouse and not args.gsc:
        if args.json:
            print(json.dumps(all_results, indent=2, default=str))
        else:
            for result in all_results:
                print(format_report(result, verbose=args.verbose))

            if len(all_results) > 1:
                grand = {CRITICAL: 0, HIGH: 0, MEDIUM: 0, LOW: 0}
                total_pages = 0
                for r in all_results:
                    total_pages += r["pages_scanned"]
                    for sev in grand:
                        grand[sev] += r["total_by_severity"].get(sev, 0)
                total = sum(grand.values())
                print(f"\n{'='*60}")
                print(f"  TOTAL: {len(all_results)} sites, {total_pages} pages")
                print(f"  CRITICAL: {grand[CRITICAL]} | HIGH: {grand[HIGH]} | MEDIUM: {grand[MEDIUM]} | LOW: {grand[LOW]}")
                print(f"  Total issues: {total}")
                print(f"{'='*60}")

    # ── --diff: compare against last snapshot ────────────────────
    if args.diff:
        snapshot = load_last_snapshot()
        diff = diff_results(all_results, snapshot)
        print(format_diff_report(diff))

    # Save snapshot (always, so --diff has something to compare to next time)
    snapshot_path = save_snapshot(all_results)
    if not args.json:
        print(f"\n  Snapshot saved: {snapshot_path}")

    # ── Self-learning: update history + report ────────────────────
    learnings = update_learnings(learnings, all_results)
    if not args.json:
        if escalated > 0:
            print(f"\n  Learning: {escalated} issue(s) escalated (recurring {_RECURRING_THRESHOLD}+ audits)")
        print(format_learning_report(learnings, all_results))

    # ── --fix: apply safe auto-fixes + bulk fixes ───────────────
    if args.fix:
        dry_run = args.dry_run
        mode = "DRY RUN" if dry_run else "APPLYING FIXES"
        print(f"\n{'='*60}")
        print(f"  {mode}")
        print(f"{'='*60}")

        all_changes = []
        for result in all_results:
            # Phase 1: Pattern-based fixes (noopener, deprecated schema, trailing slashes)
            changes = apply_fixes(result, dry_run=dry_run)
            # Phase 2: Bulk fixes (titles, descriptions, OG, schema, favicons, etc.)
            bulk_changes = apply_bulk_fixes(result, dry_run=dry_run, domain=args.domain)
            all_changes.append((result["site"], changes + bulk_changes))

        print(format_fix_report(all_changes))

        if dry_run:
            print("\n  This was a dry run. Use --fix without --dry-run to apply changes.")

    # ── --lighthouse: Core Web Vitals ────────────────────────────
    if args.lighthouse:
        print(f"\n{'='*60}")
        print(f"  LIGHTHOUSE — Core Web Vitals")
        print(f"{'='*60}")

        lh_results = []
        for result in all_results:
            lh = run_lighthouse(result, domain=args.domain)
            lh_results.append(lh)

        if args.json:
            print(json.dumps(lh_results, indent=2, default=str))
        else:
            print(format_lighthouse_report(lh_results))

    # ── --gsc: Google Search Console ─────────────────────────────
    if args.gsc:
        print(f"\n{'='*60}")
        print(f"  GOOGLE SEARCH CONSOLE")
        print(f"{'='*60}")

        gsc_results = []
        for result in all_results:
            gsc = fetch_gsc_data(result, domain=args.domain, client_secret_path=args.gsc_secret)
            gsc_results.append(gsc)

        if args.json:
            print(json.dumps(gsc_results, indent=2, default=str))
        else:
            print(format_gsc_report(gsc_results))


if __name__ == "__main__":
    main()
