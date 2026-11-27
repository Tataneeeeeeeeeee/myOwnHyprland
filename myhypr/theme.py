"""UI visual identity: Catppuccin Mocha palette, rich / prompt_toolkit styles, logo."""

from prompt_toolkit.styles import Style
from rich.text import Text
from rich.theme import Theme

# https://catppuccin.com/palette — Mocha
C = {
    "rosewater": "#f5e0dc", "mauve": "#cba6f7", "pink": "#f5c2e7", "red": "#f38ba8", "peach": "#fab387",
    "yellow": "#f9e2af", "green": "#a6e3a1", "teal": "#94e2d5", "sky": "#89dceb", "blue": "#89b4fa",
    "lavender": "#b4befe", "text": "#cdd6f4", "subtext": "#a6adc8", "overlay2": "#9399b2",
    "overlay1": "#7f849c", "overlay0": "#6c7086", "surface2": "#585b70", "surface1": "#45475a",
    "surface0": "#313244", "base": "#1e1e2e", "mantle": "#181825",
}

ACCENT = C["mauve"]

RICH_THEME = Theme({
    "accent": C["mauve"],
    "muted": C["overlay0"],
    "soft": C["subtext"],
    "ok": C["green"],
    "err": C["red"],
    "warn": C["peach"],
    "info": C["teal"],
    "path": C["blue"],
    "markdown.code": f"{C['peach']}",
    "markdown.code_block": f"{C['text']} on {C['mantle']}",
    "markdown.item.bullet": C["mauve"],
    "markdown.item.number": C["mauve"],
    "markdown.h1": f"bold {C['mauve']}",
    "markdown.h2": f"bold {C['lavender']}",
    "markdown.h3": f"bold {C['blue']}",
    "markdown.strong": f"bold {C['text']}",
    "markdown.em": f"italic {C['subtext']}",
    "markdown.link": C["blue"],
    "markdown.block_quote": C["overlay1"],
    "markdown.hr": C["surface1"],
    "repr.number": C["peach"],
})

CODE_THEME = "one-dark"

PT_STYLE = Style.from_dict({
    "rule": C["surface1"],
    "prompt": f"{C['mauve']} bold",
    "placeholder": f"{C['overlay0']} italic",
    "bottom-toolbar": f"noreverse bg:default {C['overlay0']}",
    "tb.model": C["lavender"],
    "tb.on": C["green"],
    "tb.off": C["overlay0"],
    "tb.ok": C["green"],
    "tb.err": C["red"],
    "tb.key": C["subtext"],
    "completion-menu": f"bg:{C['mantle']} {C['text']}",
    "completion-menu.completion.current": f"bg:{C['surface0']} {C['mauve']} bold",
    "completion-menu.meta.completion": f"bg:{C['mantle']} {C['overlay0']}",
    "completion-menu.meta.completion.current": f"bg:{C['surface0']} {C['subtext']}",
    "scrollbar.background": f"bg:{C['mantle']}",
    "scrollbar.button": f"bg:{C['surface1']}",
})

GRADIENT = [C["mauve"], C["pink"], C["blue"], C["teal"]]


def _hex(c: str) -> tuple[int, int, int]:
    return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)


def gradient(text: str, stops: list[str] = GRADIENT, style: str = "bold") -> Text:
    """Text coloured with a horizontal gradient (like a Hyprland active border)."""
    out = Text()
    visible = max(1, len(text.rstrip()) - 1)
    for i, ch in enumerate(text):
        t = min(i / visible, 1.0) * (len(stops) - 1)
        a, b = _hex(stops[int(t)]), _hex(stops[min(int(t) + 1, len(stops) - 1)])
        f = t - int(t)
        rgb = tuple(round(a[k] + (b[k] - a[k]) * f) for k in range(3))
        out.append(ch, style=f"{style} #{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}")
    return out


LOGO = [
    "█ █ █▄█ █▀█ █▀█",
    "█▀█  █  █▀▀ █▀▄",
]
