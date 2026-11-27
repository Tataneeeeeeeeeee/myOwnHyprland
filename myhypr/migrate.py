"""Migrates a hyprlang Hyprland config (hyprland.conf) to Lua (hl.*), version 0.55+.

DETERMINISTIC conversion of everything with a safe mapping (sections, variables, binds, monitor, exec-once,
env, rules, animations, sources…). Anything with no known equivalent is kept as a
`-- TODO migration:` comment with the original line, for the agent (or you) to finish. Check the result with
`Hyprland --verify-config`.

Only Hyprland moves to Lua: hyprlock, hypridle, hyprpaper stay in hyprlang (wiki FAQ).
"""

import glob
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# ----------------------------------------------------------------------------- mapping tables

# Window rule effects (wiki 0.56). Old hyprlang name = Lua name without the "_".
WINDOW_EFFECTS = [
    "allows_input", "animation", "border_color", "border_size", "center", "confine_pointer", "content", "decorate",
    "dim_around", "float", "focus_on_activate", "force_rgbx", "fullscreen", "fullscreen_state", "group",
    "idle_inhibit", "immediate", "keep_aspect_ratio", "max_size", "maximize", "min_size", "monitor", "move",
    "nearest_neighbor", "no_anim", "no_auto_hdr", "no_blur", "no_close_for", "no_dim", "no_focus",
    "no_follow_mouse", "no_glow", "no_initial_focus", "no_max_size", "no_screen_share", "no_shadow",
    "no_shortcuts_inhibit", "no_vrr", "no_wobble", "no_xdg_drags", "opacity", "opaque", "persistent_size", "pin",
    "pseudo", "render_unfocused", "rounding", "rounding_power", "scroll_mouse", "scroll_touchpad",
    "scrolling_width", "size", "stay_focused", "suppress_event", "sync_fullscreen", "tag", "tile", "tonemap",
    "workspace", "xray",
]
EFFECT = {n.replace("_", ""): n for n in WINDOW_EFFECTS} | {"windowdance": "allows_input", "dimaround": "dim_around"}
MATCH_PROPS = {"class": "class", "title": "title", "initialclass": "initial_class", "initialtitle": "initial_title",
               "floating": "float", "float": "float", "fullscreen": "fullscreen", "pinned": "pin", "pin": "pin",
               "xwayland": "xwayland", "focus": "focus", "workspace": "workspace", "onworkspace": "workspace",
               "tag": "tag", "content": "content", "xdgtag": "xdg_tag", "group": "group", "modal": "modal"}
STRING_EFFECTS = {"workspace", "monitor", "opacity", "size", "move", "max_size", "min_size", "suppress_event", "tag",
                  "group", "animation", "content", "idle_inhibit", "fullscreen_state", "tonemap"}
BOOL_PROPS = {"float", "fullscreen", "pin", "xwayland", "focus", "group", "modal"}

LAYER_EFFECT = {"blur": "blur", "blurpopups": "blur_popups", "ignorealpha": "ignore_alpha", "noanim": "no_anim",
                "animation": "animation", "dimaround": "dim_around", "xray": "xray", "order": "order",
                "abovelock": "above_lock", "noscreenshare": "no_screen_share"}

WORKSPACE_KEYS = {"monitor": "monitor", "default": "default", "persistent": "persistent", "gapsin": "gaps_in",
                  "gapsout": "gaps_out", "bordersize": "border_size", "decorate": "decorate", "layout": "layout",
                  "on-created-empty": "on_created_empty", "defaultname": "default_name", "floatgaps": "float_gaps"}

DIRECTION = {"l": "left", "r": "right", "u": "up", "d": "down", "left": "left", "right": "right", "up": "up",
             "down": "down", "t": "up", "b": "down"}
BIND_FLAGS = {"l": "locked", "r": "release", "e": "repeating", "n": "non_consuming", "m": "mouse",
              "t": "transparent", "i": "ignore_mods", "o": "long_press", "s": None, "d": "description",
              "p": "dont_inhibit", "c": "click", "g": "drag"}


MODS_RE = r"SHIFT|CAPS|CONTROL|CTRL|ALT|MOD2|MOD3|MOD4|MOD5|SUPER|WIN|LOGO|META"


class Raw(str):
    """Lua expression inserted verbatim (not a string)."""


# ----------------------------------------------------------------------------- values

def q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def strip_comment(line: str) -> str:
    """In hyprlang, "#" starts a comment and "##" is a literal "#"."""
    out, i = [], 0
    while i < len(line):
        if line[i] == "#":
            if line[i + 1:i + 2] == "#":
                out.append("#")
                i += 2
                continue
            break
        out.append(line[i])
        i += 1
    return "".join(out).rstrip()


def scalar(v: str):
    s = v.strip()
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d*\.\d+", s):
        return float(s)
    if s.lower() in ("true", "yes", "on"):
        return True
    if s.lower() in ("false", "no", "off"):
        return False
    if re.fullmatch(r"0x[0-9a-fA-F]{6,8}", s):
        return Raw(s)
    return s


def colors(v: str):
    """"rgba(aaaaaaee) rgba(bbbbbbee) 45deg" → { colors = {...}, angle = 45 }; a single color → string."""
    tokens = v.split()
    angle = None
    if tokens and re.fullmatch(r"\d+deg", tokens[-1]):
        angle = int(tokens.pop()[:-3])
    if tokens and all(re.fullmatch(r"(rgba?\([0-9a-fA-F, .]+\)|0x[0-9a-fA-F]{6,8})", t) for t in tokens):
        if len(tokens) == 1 and angle is None:
            return Raw(tokens[0]) if tokens[0].startswith("0x") else tokens[0]
        table = {"colors": [Raw(t) if t.startswith("0x") else t for t in tokens]}
        if angle is not None:
            table["angle"] = angle
        return table
    return None


def render(v, indent: int = 0) -> str:
    pad, inner = "    " * indent, "    " * (indent + 1)
    if isinstance(v, Raw):
        return str(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return q(v)
    if isinstance(v, list):
        return "{ " + ", ".join(render(x, indent + 1) for x in v) + " }"
    if isinstance(v, dict):
        if not v:
            return "{}"
        lines = []
        for k, val in v.items():
            if k.startswith("__comment"):
                lines.append(f"{inner}-- {val}")
                continue
            key = k if re.fullmatch(r"[A-Za-z_]\w*", k) else f"[{q(k)}]"
            lines.append(f"{inner}{key} = {render(val, indent + 1)},")
        return "{\n" + "\n".join(lines) + f"\n{pad}}}"
    return q(str(v))


def inline(table: dict) -> str:
    """Small one-line table: { leaf = "windows", enabled = true }."""
    return "{ " + ", ".join(f"{k} = {render(v)}" for k, v in table.items()) + " }"


# ----------------------------------------------------------------------------- conversion

@dataclass
class Todo:
    file: str
    line: int
    original: str
    reason: str


@dataclass
class Migration:
    files: dict[str, str] = field(default_factory=dict)      # relative .lua path → content
    todos: list[Todo] = field(default_factory=list)
    converted: int = 0
    sources: list[str] = field(default_factory=list)          # converted .conf files

    def summary(self) -> str:
        lines = [f"{self.converted} lignes converties dans {len(self.files)} fichier(s) Lua : "
                 + ", ".join(self.files)]
        if self.todos:
            lines.append(f"{len(self.todos)} ligne(s) à terminer à la main (commentaires « -- TODO migration ») :")
            lines += [f"  {t.file}:{t.line} [{t.reason}] {t.original}" for t in self.todos[:40]]
        else:
            lines.append("aucune ligne laissée en TODO")
        return "\n".join(lines)


class Converter:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.vars: dict[str, str] = {}
        self.result = Migration()
        self.rule_n = 0

    # -------------------------------------------------------------- expressions with $variables
    VAR = re.compile(r"\$([A-Za-z_][\w-]*)")

    def split_vars(self, text: str) -> list[tuple[str, bool]]:
        """[(chunk, is_variable)] — hyprlang allows "-" in names ($bg-dir)."""
        out, pos = [], 0
        for m in self.VAR.finditer(text):
            name = m.group(1)
            while name and name not in self.vars:      # "$term-suffix" → $term if only $term exists
                name = name[:-1].rstrip("-")
            if not name:
                continue
            out.append((text[pos:m.start()], False))
            out.append((name, True))
            pos = m.start() + 1 + len(name)
        out.append((text[pos:], False))
        return [(t, v) for t, v in out if t]

    @staticmethod
    def lua_name(name: str) -> str:
        return re.sub(r"\W", "_", name)
    def resolve(self, text: str, depth: int = 0) -> str:
        if depth > 10:
            return text
        return "".join(self.resolve(self.vars[c], depth + 1) if v else c for c, v in self.split_vars(text))

    def expr(self, text: str) -> Raw:
        """hyprlang string that may contain $variables → Lua expression (concatenation)."""
        # hyprlang only substitutes defined variables: "$2" (awk) or "$HOME" are left for the shell
        out = []
        for chunk, is_var in self.split_vars(text.strip()):
            if is_var:
                self.used.add(chunk)
                out.append(self.lua_name(chunk))
            else:
                out.append(q(chunk))
        return Raw(" .. ".join(out) if out else '""')

    def value(self, raw: str):
        raw = raw.strip()
        if any(is_var for _, is_var in self.split_vars(raw)):
            return self.expr(raw)
        c = colors(raw)
        return c if c is not None else scalar(raw)

    # -------------------------------------------------------------- file
    def convert(self, conf: Path, rel_out: str):
        self.used: set[str] = set()
        self.defined_here: set[str] = set()
        self.file = rel_out
        out: list[str] = [f"-- Converti depuis {conf.name} (hyprlang) par myOwnHyprland — à relire",
                          "-- Doc : https://wiki.hypr.land/Configuring/Start/", ""]
        stack: list[tuple[str, dict]] = []        # open sections
        exec_block: list[str] = []
        submap: tuple[str, list[str]] | None = None

        def emit(line: str):
            target = submap[1] if submap else out
            target.append(line)

        def flush_exec():
            if exec_block:
                emit('hl.on("hyprland.start", function()')
                for cmd in exec_block:
                    emit(f"    hl.exec_cmd({cmd})")
                emit("end)")
                exec_block.clear()

        lines = conf.read_text(errors="ignore").splitlines()
        for n, original in enumerate(lines, 1):
            line = strip_comment(original).strip()
            comment = original.strip().lstrip("#").strip() if original.strip().startswith("#") else None
            if comment is not None:
                line = ""
            if not line:
                if comment:
                    if stack:
                        stack[-1][1][f"__comment{n}"] = comment
                    else:
                        flush_exec()
                        emit(f"-- {comment}")
                elif not stack:
                    flush_exec()
                    if out and out[-1] != "":
                        emit("")
                continue

            # section open / close
            m = re.fullmatch(r"([\w.:\-\[\]]+)\s*\{", line)
            if m:
                flush_exec()
                stack.append((m.group(1), {}))
                continue
            if line == "}":
                if not stack:
                    continue
                name, table = stack.pop()
                if stack:
                    self.set_path(stack[-1][1], name, table)
                else:
                    self.emit_section(name, table, emit, n, original)
                continue

            m = re.fullmatch(r"([^=]+?)\s*=\s*(.*)", line)
            if not m:
                self.todo(n, original, "syntaxe non reconnue", emit)
                continue
            key, val = m.group(1).strip(), m.group(2).strip()
            self.result.converted += 1

            if key.startswith("$"):
                name = key[1:]
                self.vars[name] = val
                self.defined_here.add(name)
                flush_exec()
                emit(f"local {self.lua_name(name)} = {render(self.value(val))}")
                continue

            if stack and key not in ("bezier", "animation") and not key.startswith(("bind", "exec")):
                self.set_path(stack[-1][1], key, self.value(val))
                continue

            kw = key.lower()
            if kw in ("exec-once", "execr-once"):
                exec_block.append(render(self.expr(val)))
                continue
            flush_exec()
            if kw in ("exec", "execr"):
                emit(f'hl.on("config.reloaded", function() hl.exec_cmd({render(self.expr(val))}) end)')
                emit(f'hl.on("hyprland.start", function() hl.exec_cmd({render(self.expr(val))}) end)')
            elif kw == "submap":
                if val.strip() == "reset":
                    if submap:
                        name, body = submap
                        submap = None
                        emit(f"hl.define_submap({q(name)}, function()")
                        for b in body:
                            emit(f"    {b}" if b else "")
                        emit("end)")
                else:
                    submap = (val.strip(), [])
            elif kw.startswith("bind"):
                self.bind(kw[4:], val, emit, n, original)
            elif kw == "unbind":
                mods, _, k = val.partition(",")
                emit(f"hl.unbind({render(self.keys(mods, k))})")
            elif kw == "monitor":
                self.monitor(val, emit)
            elif kw == "env":
                name, _, v = val.partition(",")
                emit(f"hl.env({render(self.expr(name))}, {render(self.expr(v))})")
            elif kw == "source":
                self.source(val, emit, n, original)
            elif kw in ("windowrule", "windowrulev2"):
                self.window_rule(val, emit, n, original)
            elif kw == "layerrule":
                self.layer_rule(val, emit, n, original)
            elif kw == "workspace":
                self.workspace_rule(val, emit, n, original)
            elif kw == "bezier":
                p = [x.strip() for x in val.split(",")]
                if len(p) == 5:
                    emit(f"hl.curve({q(p[0])}, {{ type = \"bezier\", points = {{ {{{p[1]}, {p[2]}}}, {{{p[3]}, {p[4]}}} }} }})")
                else:
                    self.todo(n, original, "bezier incomplet", emit)
            elif kw == "animation":
                self.animation(val, emit, n, original)
            elif kw == "gesture":
                p = [x.strip() for x in val.split(",")]
                if len(p) == 3 and p[0].isdigit():
                    emit(f"hl.gesture({{ fingers = {p[0]}, direction = {q(p[1])}, action = {q(p[2])} }})")
                else:
                    self.todo(n, original, "geste avec options", emit)
            elif kw == "permission":
                p = [x.strip() for x in val.split(",")]
                if len(p) == 3:
                    emit(f"hl.permission({{ binary = {q(p[0])}, type = {q(p[1])}, mode = {q(p[2])} }})")
                else:
                    self.todo(n, original, "permission", emit)
            elif kw == "plugin":
                emit(f"hl.plugin.load({render(self.expr(val))})")
            elif ":" in key or "." in key:
                # "general:gaps_in = 5" at the top level
                path = re.split(r"[:]", key)
                table: dict = {}
                self.set_path(table, ".".join(path[1:]), self.value(val))
                emit(f"hl.config({{ {path[0]} = {render(table, 0)} }})")
            else:
                self.result.converted -= 1
                self.todo(n, original, f"mot-clé « {key} » sans équivalent connu", emit)
        flush_exec()
        if submap:
            name, body = submap
            emit_target = out
            emit_target.append(f"hl.define_submap({q(name)}, function()")
            emit_target += [f"    {b}" for b in body]
            emit_target.append("end)")

        # variables used here but defined in another file (hyprlang shares them, Lua doesn't)
        missing = sorted(v for v in self.used if v not in self.defined_here)
        header = []
        for v in missing:
            if v in self.vars:
                header.append(f"local {self.lua_name(v)} = {render(self.value(self.vars[v]))}  -- variable définie dans un autre fichier")
            else:
                header.append(f"local {self.lua_name(v)} = \"\"  -- TODO migration: ${v} n'est défini nulle part (source externe ?)")
                self.result.todos.append(Todo(rel_out, 0, f"${v}", "variable non définie"))
        if header:
            out[3:3] = header + [""]
        self.result.files[rel_out] = "\n".join(out).rstrip() + "\n"
        self.result.sources.append(str(conf))

    # -------------------------------------------------------------- helpers
    def todo(self, n: int, original: str, reason: str, emit):
        emit(f"-- TODO migration ({reason}) : {original.strip()}")
        self.result.todos.append(Todo(self.file, n, original.strip(), reason))

    @staticmethod
    def set_path(table: dict, key: str, value):
        parts = re.split(r"[.:]", key)
        for p in parts[:-1]:
            table = table.setdefault(p, {})
        if isinstance(table.get(parts[-1]), dict) and isinstance(value, dict):
            table[parts[-1]].update(value)
        else:
            table[parts[-1]] = value

    def emit_section(self, name: str, table: dict, emit, n: int, original: str):
        if name.startswith("device"):
            m = re.fullmatch(r"device\[(.+)\]", name)
            if m:
                table = {"name": m.group(1), **table}
            emit(f"hl.device({render(table)})")
        elif name == "monitorv2":
            emit(f"hl.monitor({render(table)})")
        elif name in ("windowrule", "layerrule"):
            self.todo(n, f"{name} {{ … }}", "règle en bloc : à réécrire en hl.window_rule / hl.layer_rule", emit)
        else:
            if name == "gestures" and "workspace_swipe" in table:
                # removed in Lua: replaced by hl.gesture
                enabled = table.pop("workspace_swipe")
                fingers = table.pop("workspace_swipe_fingers", 3)
                if enabled is True:
                    emit(f'hl.gesture({{ fingers = {render(fingers)}, direction = "horizontal", action = "workspace" }})')
                if not [k for k in table if not k.startswith("__comment")]:
                    return
            path = name.split(":")
            nested: dict = {}
            self.set_path(nested, ".".join(path), table)
            emit(f"hl.config({render(nested)})")

    def keys(self, mods: str, key: str) -> Raw:
        tokens = []
        for t in (re.split(r"[\s_]+", mods.strip()) if mods.strip() else []):
            # hyprlang tolerated run-together modifiers: "SUPERSHIFT", "CTRLALT"
            parts = re.findall(MODS_RE, t.upper()) if not t.startswith("$") else []
            tokens += parts if parts and "".join(parts) == t.upper() else [t] if t else []
        # "$mainMod SHIFT": variables stay variables
        joined = " + ".join(tokens + [key.strip()])
        return self.expr(joined)

    def bind(self, flags: str, val: str, emit, n: int, original: str):
        flags = flags.lower()
        has_desc = "d" in flags
        parts = [p.strip() for p in val.split(",", 4 if has_desc else 3)]
        if len(parts) < (4 if has_desc else 3):
            parts += [""] * ((4 if has_desc else 3) - len(parts))
        mods, key = parts[0], parts[1]
        desc = parts[2] if has_desc else None
        dispatcher = parts[3 if has_desc else 2].lower()
        args = parts[4 if has_desc else 3] if len(parts) > (4 if has_desc else 3) else ""
        opts = {}
        for f in flags:
            name = BIND_FLAGS.get(f)
            if name and name != "description":
                opts[name] = True
        if desc:
            opts["description"] = desc
        action = self.dispatcher(dispatcher, args, mouse="m" in flags)
        if action is None:
            self.todo(n, original, f"dispatcher « {dispatcher} » à convertir à la main", emit)
            return
        tail = f", {inline(opts)}" if opts else ""
        emit(f"hl.bind({render(self.keys(mods, key))}, {action}{tail})")

    def ws_value(self, a: str) -> str:
        a = a.strip()
        if "$" in a:
            return render(self.expr(a))
        return a if re.fullmatch(r"-?\d+", a) else q(a)

    def dispatcher(self, d: str, a: str, mouse: bool = False) -> str | None:
        a = a.strip()
        dsp = "hl.dsp"
        dir_ = DIRECTION.get(a.lower())
        simple = {
            "killactive": f"{dsp}.window.close()", "forcekillactive": f"{dsp}.window.kill()",
            "exit": f"{dsp}.exit()", "pseudo": f"{dsp}.window.pseudo()", "pin": f"{dsp}.window.pin()",
            "centerwindow": f"{dsp}.window.center()", "togglesplit": f'{dsp}.layout("togglesplit")',
            "swapsplit": f'{dsp}.layout("swapsplit")', "togglegroup": f"{dsp}.group.toggle()",
            "forcerendererreload": f"{dsp}.force_renderer_reload()", "togglefloating": f'{dsp}.window.float({{ action = "toggle" }})',
            "setfloating": f'{dsp}.window.float({{ action = "enable" }})', "settiled": f'{dsp}.window.float({{ action = "disable" }})',
            "focusurgentorlast": f"{dsp}.focus({{ urgent_or_last = true }})", "focuscurrentorlast": f"{dsp}.focus({{ last = true }})",
            "bringactivetotop": f'{dsp}.window.alter_zorder({{ mode = "top" }})', "toggleswallow": f"{dsp}.window.toggle_swallow()",
        }
        if d in ("exec", "execr"):
            m = re.fullmatch(r"hyprctl dispatch (\w+)\s*(.*)", a)
            if m:  # in Lua, "hyprctl dispatch" expects an hl.dsp expression: call the dispatcher directly
                return self.dispatcher(m.group(1).lower(), m.group(2).strip("'\" "))
            return f"{dsp}.{'exec_cmd' if d == 'exec' else 'exec_raw'}({render(self.expr(a))})"
        if mouse and d == "movewindow":
            return f"{dsp}.window.drag()"
        if mouse and d == "resizewindow":
            return f"{dsp}.window.resize()"
        if d in simple and (not a or d in ("togglefloating", "pseudo", "pin", "exit")):
            return simple[d]
        if d == "fullscreen":
            return f'{dsp}.window.fullscreen({{ mode = "maximized" }})' if a == "1" else f"{dsp}.window.fullscreen()"
        if d == "movefocus" and dir_:
            return f"{dsp}.focus({{ direction = {q(dir_)} }})"
        if d == "movewindow" and dir_:
            return f"{dsp}.window.move({{ direction = {q(dir_)} }})"
        if d == "swapwindow" and dir_:
            return f"{dsp}.window.swap({{ direction = {q(dir_)} }})"
        if d == "workspace" and a:
            return f"{dsp}.focus({{ workspace = {self.ws_value(a)} }})"
        if d in ("movetoworkspace", "movetoworkspacesilent") and a and "," not in a:
            follow = "" if d == "movetoworkspace" else ", follow = false"
            return f"{dsp}.window.move({{ workspace = {self.ws_value(a)}{follow} }})"
        if d == "togglespecialworkspace":
            return f"{dsp}.workspace.toggle_special({q(a) if a else ''})"
        if d == "focusmonitor" and a:
            return f"{dsp}.focus({{ monitor = {q(a)} }})"
        if d == "movecurrentworkspacetomonitor" and a:
            return f"{dsp}.workspace.move({{ monitor = {q(a)} }})"
        if d == "moveworkspacetomonitor" and len(a.split()) == 2:
            w, mon = a.split()
            return f"{dsp}.workspace.move({{ workspace = {self.ws_value(w)}, monitor = {q(mon)} }})"
        if d == "cyclenext":
            return f"{dsp}.window.cycle_next()" if not a else \
                f"{dsp}.window.cycle_next({{ next = false }})" if a.startswith("prev") else None
        if d in ("resizeactive", "moveactive") and len(a.split()) == 2:
            x, y = a.split()
            fn = "resize" if d == "resizeactive" else "move"
            if all(re.fullmatch(r"-?\d+", v) for v in (x, y)):
                return f"{dsp}.window.{fn}({{ x = {x}, y = {y}, relative = true }})"
            return None
        if d == "changegroupactive":
            return f"{dsp}.group.{'prev' if a.startswith('b') else 'next'}()"
        if d == "submap" and a:
            return f"{dsp}.submap({q(a)})"
        if d == "layoutmsg" and a:
            return f"{dsp}.layout({q(a)})"
        if d == "global" and a:
            return f"{dsp}.global({q(a)})"
        if d == "dpms":
            return f"{dsp}.dpms({{ action = {q(a or 'toggle')} }})"
        if d == "pass" and a:
            return f"{dsp}.pass({{ window = {q(a)} }})"
        if d == "tagwindow" and a:
            return f"{dsp}.window.tag({{ tag = {q(a)} }})"
        if d == "renameworkspace" and len(a.split(maxsplit=1)) == 2:
            w, name = a.split(maxsplit=1)
            return f"{dsp}.workspace.rename({{ workspace = {self.ws_value(w)}, name = {q(name)} }})"
        return None

    def monitor(self, val: str, emit):
        p = [x.strip() for x in val.split(",")]
        spec: dict = {"output": p[0]}
        if len(p) > 1 and p[1].lower() in ("disable", "disabled"):
            spec["disabled"] = True
        else:
            for k, v in zip(("mode", "position", "scale"), p[1:4]):
                if v:
                    spec[k] = scalar(v) if k == "scale" and v != "auto" else v
            rest = p[4:]
            for i in range(0, len(rest) - 1, 2):
                spec[rest[i]] = scalar(rest[i + 1])
        emit(f"hl.monitor({render(spec)})")

    def window_rule(self, val: str, emit, n: int, original: str):
        tokens = [t.strip() for t in val.split(",") if t.strip()]
        match, effects = {}, {}
        if any(t.startswith("match:") for t in tokens):          # recent hyprlang syntax
            for t in tokens:
                if t.startswith("match:"):
                    prop, _, v = t[6:].partition(" ")
                    self._match(match, prop, v)
                else:
                    eff, _, v = t.partition(" ")
                    if not self._effect(effects, eff, v):
                        return self.todo(n, original, f"effet « {eff} » inconnu", emit)
        else:                                                      # classic windowrule / windowrulev2
            eff, _, v = tokens[0].partition(" ")
            if not self._effect(effects, eff, v):
                return self.todo(n, original, f"effet « {eff} » inconnu", emit)
            for t in tokens[1:]:
                prop, sep, v = t.partition(":")
                if sep and prop.lower() in MATCH_PROPS:
                    self._match(match, prop, v)
                else:
                    match["class"] = t   # windowrule v1: "float, ^(kitty)$"
        if not match:
            return self.todo(n, original, "règle sans critère de fenêtre", emit)
        emit(f"hl.window_rule({render({'match': match, **effects})})")

    def _match(self, match: dict, prop: str, v: str):
        name = MATCH_PROPS.get(prop.lower(), prop.lower())
        v = v.strip()
        match[name] = (v in ("1", "true", "yes", "on")) if name in BOOL_PROPS and v in ("0", "1", "true", "false", "yes", "no", "on", "off") else v

    def _effect(self, effects: dict, eff: str, v: str) -> bool:
        flat = eff.lower().replace("_", "")
        if flat == "noborder":
            effects["border_size"] = 0
            return True
        name = EFFECT.get(flat)
        if not name:
            return False
        v = v.strip()
        if name in STRING_EFFECTS and v:
            effects[name] = v
        elif v in ("", "on", "1", "true"):
            effects[name] = True
        elif v in ("off", "0", "false") and name not in ("border_size", "rounding", "workspace", "monitor"):
            effects[name] = False
        else:
            c = colors(v) if name == "border_color" else None
            effects[name] = c if c is not None else v if name in STRING_EFFECTS else scalar(v)
        return True

    def layer_rule(self, val: str, emit, n: int, original: str):
        tokens = [t.strip() for t in val.split(",") if t.strip()]
        spec: dict = {"match": {}}
        if any(t.startswith("match:") for t in tokens):
            for t in tokens:
                if t.startswith("match:"):
                    prop, _, v = t[6:].partition(" ")
                    spec["match"][prop] = v.strip()
                else:
                    eff, _, v = t.partition(" ")
                    name = LAYER_EFFECT.get(eff.lower().replace("_", ""))
                    if not name:
                        return self.todo(n, original, f"effet de layer « {eff} » inconnu", emit)
                    spec[name] = scalar(v) if v.strip() and v.strip() not in ("on", "1") else True
        elif len(tokens) == 2:
            eff, _, v = tokens[0].partition(" ")
            flat = eff.lower().replace("_", "")
            if flat == "ignorezero":
                spec["ignore_alpha"] = 0
            elif flat in LAYER_EFFECT:
                spec[LAYER_EFFECT[flat]] = scalar(v) if v.strip() else True
            else:
                return self.todo(n, original, f"effet de layer « {eff} » inconnu", emit)
            spec["match"]["namespace"] = tokens[1]
        else:
            return self.todo(n, original, "layerrule", emit)
        emit(f"hl.layer_rule({render(spec)})")

    def workspace_rule(self, val: str, emit, n: int, original: str):
        tokens = [t.strip() for t in val.split(",") if t.strip()]
        spec: dict = {"workspace": tokens[0]}
        for t in tokens[1:]:
            k, sep, v = t.partition(":")
            k = k.lower()
            if not sep:
                return self.todo(n, original, "règle de workspace", emit)
            if k in ("border", "rounding", "shadow") and scalar(v) is False:
                spec[f"no_{k}"] = True
            elif k in WORKSPACE_KEYS:
                spec[WORKSPACE_KEYS[k]] = scalar(v)
            else:
                return self.todo(n, original, f"option de workspace « {k} »", emit)
        emit(f"hl.workspace_rule({render(spec)})")

    def animation(self, val: str, emit, n: int, original: str):
        p = [x.strip() for x in val.split(",")]
        if len(p) < 2:
            return self.todo(n, original, "animation", emit)
        spec: dict = {"leaf": p[0], "enabled": p[1] not in ("0", "false", "no")}
        if len(p) >= 4:
            spec["speed"] = scalar(p[2])
            spec["bezier"] = p[3]
        if len(p) >= 5 and p[4]:
            spec["style"] = p[4]
        emit(f"hl.animation({inline(spec)})")

    def source(self, val: str, emit, n: int, original: str):
        path = os.path.expandvars(os.path.expanduser(self.resolve(val.strip())))
        own = os.path.expanduser("~/.config/hypr/")
        if path.startswith(own) and not Path(path).resolve().is_relative_to(self.root):
            path = str(self.root / path[len(own):])   # the config refers to its own directory
        if not os.path.isabs(path):
            path = str(self.base / path)
        matches = sorted(glob.glob(path)) or [path]
        for p in matches:
            src = Path(p)
            if not src.exists():
                self.todo(n, original, "fichier sourcé introuvable", emit)
                continue
            try:
                rel = src.resolve().relative_to(self.root)
            except ValueError:
                self.todo(n, original, f"source hors du dossier de config ({src}) : fichier généré par un outil ? "
                                       "à convertir ou à remplacer", emit)
                continue
            module = rel.with_suffix("").as_posix()
            out_rel = f"{module}.lua"
            if out_rel not in self.result.files and src.suffix == ".conf":
                saved = (self.file, self.used, self.defined_here)
                self.convert(src, out_rel)
                self.file, self.used, self.defined_here = saved
            emit(f"require({q(module.replace('/', '.'))})")


def migrate(conf: Path) -> Migration:
    """Converts `conf` (and the files it sources from the same directory) to Lua. Writes nothing."""
    conf = conf.resolve()
    c = Converter(conf.parent)
    c.base = conf.parent
    c.convert(conf, conf.with_suffix(".lua").name)
    return c.result
