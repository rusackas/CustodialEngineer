"""Octicon SVG loader + Jinja helper.

Octicons (GitHub's icon set, MIT-licensed) are vendored as SVG files
under `repobot/static/icons/`. We inline them directly into the HTML
so CSS `currentColor` + `color:` can tint them alongside the text.

Usage from a template:
    {{ icon('git-pull-request') }}
    {{ icon('gear', size=18, cls='settings-gear-icon') }}
"""
import re
from functools import lru_cache

from .config import PROJECT_ROOT

_ICONS_DIR = PROJECT_ROOT / "repobot" / "static" / "icons"

# `fill="currentColor"` is what lets CSS color the icon. Octicons ship
# without a fill attribute, so the browser picks black by default.
_SVG_OPEN = re.compile(r"<svg\b([^>]*)>", re.IGNORECASE)


@lru_cache(maxsize=128)
def _load(name: str) -> str:
    path = _ICONS_DIR / f"{name}.svg"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def render(name: str, size: int | str = 16, cls: str = "",
           title: str | None = None) -> str:
    """Return an inline SVG string for the named icon.

    Missing icons return an empty string rather than raising — the UI
    stays functional if someone references a name that doesn't ship.
    """
    raw = _load(name)
    if not raw:
        return ""
    attrs = [f'width="{size}"', f'height="{size}"',
             'fill="currentColor"', 'aria-hidden="true"']
    if cls:
        attrs.append(f'class="icon icon-{name} {cls}"')
    else:
        attrs.append(f'class="icon icon-{name}"')
    if title:
        attrs.append(f'role="img"')
        attrs.append(f'aria-label="{title}"')
    # Splice our attrs onto the existing <svg> tag, stripping the
    # default width/height (we set our own above).
    def _replace(m: re.Match) -> str:
        existing = m.group(1)
        # Strip width="..." and height="..." from existing attrs.
        existing = re.sub(r'\s(width|height)="[^"]*"', "", existing)
        return f"<svg{existing} {' '.join(attrs)}>"
    return _SVG_OPEN.sub(_replace, raw, count=1)
