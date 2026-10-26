"""Outil de dev : pilote l'interface dans un pseudo-terminal et prend des captures PNG (avec pyte + rsvg-convert).

Usage : uv run --with pyte python scripts/ui_snapshot.py OUT_DIR '[["wait",5],["key","/help\\r"],["snap","aide"]]'
Étapes : ["wait", s] · ["until", "texte", s] · ["key", "texte"] · ["snap", "nom"]
"""
import fcntl, html, json, os, pty, select, struct, subprocess, sys, termios, time
import pyte

W, H = 104, int(os.environ.get("ROWS", 46))
OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)
pid, fd = pty.fork()
if pid == 0:
    os.environ.update(TERM="xterm-256color", COLORTERM="truecolor")
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.execvp("uv", ["uv", "run", "myhypr"])
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", H, W, 0, 0))
screen = pyte.Screen(W, H)
stream = pyte.ByteStream(screen)


def pump(t, until=None):
    end = time.time() + t
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                data = os.read(fd, 65536)
            except OSError:
                return
            stream.feed(data)
            if b"\x1b[6n" in data:  # répond aux demandes de position du curseur (prompt_toolkit)
                os.write(fd, f"\x1b[{screen.cursor.y + 1};{screen.cursor.x + 1}R".encode())
        if until and until in "\n".join(screen.display):
            pump(0.8)
            return


NAMED = {"default": None, "black": "#1e1e2e", "red": "#f38ba8", "green": "#a6e3a1", "brown": "#f9e2af",
         "blue": "#89b4fa", "magenta": "#f5c2e7", "cyan": "#94e2d5", "white": "#cdd6f4"}


def col(c, fallback):
    if c in NAMED:
        return NAMED[c] or fallback
    return "#" + c if len(c) == 6 else fallback


def snap(name):
    cw, ch = 8.4, 18
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W * cw + 20}" height="{H * ch + 20}">'
           '<rect width="100%" height="100%" fill="#1e1e2e"/>'
           '<g font-family="JetBrains Mono, DejaVu Sans Mono, monospace" font-size="14">']
    for y in range(H):
        row = screen.buffer[y]
        for x in range(W):
            c = row[x]
            fg, bg = col(c.fg, "#cdd6f4"), col(c.bg, None)
            if c.reverse:
                fg, bg = (bg or "#1e1e2e"), fg
            if bg:
                out.append(f'<rect x="{10 + x * cw}" y="{10 + y * ch}" width="{cw + 0.5}" height="{ch}" fill="{bg}"/>')
            if c.data.strip():
                st = (' font-weight="bold"' if c.bold else '') + (' font-style="italic"' if c.italics else '')
                out.append(f'<text x="{10 + x * cw}" y="{10 + y * ch + 14}" fill="{fg}"{st}>{html.escape(c.data)}</text>')
    out.append("</g></svg>")
    svg = os.path.join(OUT, f"{name}.svg")
    open(svg, "w").write("".join(out))
    subprocess.run(["rsvg-convert", svg, "-o", os.path.join(OUT, f"{name}.png")], check=True)


for step in json.loads(sys.argv[2]):
    kind, arg = step[0], step[1]
    if kind == "wait":
        pump(arg)
    elif kind == "until":
        pump(step[2], until=arg)
    elif kind == "key":
        os.write(fd, arg.encode())
        pump(0.5)
    elif kind == "snap":
        snap(arg)
os.write(fd, b"\x04")
pump(1)
