"""
seo-audit — Comprehensive SEO scanner for static sites.
"""

__version__ = "1.1.0"
__author__ = "Peter Saddington"
__license__ = "MIT"

from .audit import (
    audit_site,
    audit_page,
    format_report,
    format_fix_report,
    format_diff_report,
    apply_fixes,
    apply_bulk_fixes,
    save_snapshot,
    load_last_snapshot,
    diff_results,
    SEOParser,
    Issue,
)

__all__ = [
    "audit_site",
    "audit_page",
    "format_report",
    "format_fix_report",
    "format_diff_report",
    "apply_fixes",
    "apply_bulk_fixes",
    "save_snapshot",
    "load_last_snapshot",
    "diff_results",
    "SEOParser",
    "Issue",
    "__version__",
]
