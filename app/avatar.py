"""Deterministic SVG avatars: a symmetric identicon in the agent's color with its emoji on top."""
import hashlib
from html import escape

SHAPES = {"snmp": 6, "syslog": 3, "webhook": 10, "cron": 50, "llm": 50, "api": 12, "bot": 18, "human": 50}


def avatar_svg(name: str, color: str = "#FFB300", emoji: str = "", kind: str = "api") -> str:
    h = hashlib.sha256(name.encode()).digest()
    cells = []
    for row in range(5):
        for col in range(3):
            if h[row * 3 + col] % 2:
                for c in {col, 4 - col}:
                    cells.append(f'<rect x="{12 + c * 15.2:.1f}" y="{12 + row * 15.2:.1f}" width="14" height="14" rx="2"/>')
    radius = SHAPES.get(kind, 12)
    badge = (f'<circle cx="50" cy="50" r="20" fill="#0a0a09" stroke="{escape(color)}" stroke-width="2"/>'
             f'<text x="50" y="51" font-size="22" text-anchor="middle" dominant-baseline="central">{escape(emoji)}</text>'
             if emoji else "")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100">'
            f'<rect width="100" height="100" rx="{radius}" fill="#151411"/>'
            f'<g fill="{escape(color)}" opacity="0.55">{"".join(cells)}</g>{badge}</svg>')
