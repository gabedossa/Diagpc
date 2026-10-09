"""Interface gráfica (tkinter) do DiagCompleto."""
from __future__ import annotations

import ctypes
import json
import logging
import math
import queue
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import Any, Callable

from . import __version__, cleaner, health, maintenance, report, startup, tweaks
from .utils import app_data_dir, format_bytes, is_admin, open_file, relaunch_as_admin

log = logging.getLogger("DiagCompleto.gui")

PALETTES: dict[str, dict[str, str]] = {
    "dark": {
        "bg": "#0e1014", "sidebar": "#121419", "surface": "#16191f", "surface2": "#1d2129",
        "hover": "#242934", "pressed": "#2b313d", "border": "#23272f", "border_strong": "#363c48",
        "text": "#e6e8ec", "muted": "#8d95a3", "faint": "#5a616d",
        "accent": "#5b8def", "accent_hover": "#74a0f5", "accent_pressed": "#4a7ad8", "on_accent": "#ffffff",
        "select": "#1f2a40", "nav_hover": "#1a1d24", "nav_active": "#1d2433", "track": "#262b35",
        "scroll": "#2c313c", "scroll_hover": "#3a404d", "console": "#0b0d11",
        "ok": "#4cc38a", "warn": "#e5a54b", "crit": "#ef6b6b", "info": "#6aa3f7", "unknown": "#8d95a3",
    },
    "light": {
        "bg": "#f4f5f7", "sidebar": "#fbfbfc", "surface": "#ffffff", "surface2": "#f1f3f6",
        "hover": "#e9ecf1", "pressed": "#dfe3ea", "border": "#e4e7ec", "border_strong": "#c9ced8",
        "text": "#14171c", "muted": "#626b7a", "faint": "#a3aab5",
        "accent": "#2f6fe4", "accent_hover": "#245fcf", "accent_pressed": "#1d52b5", "on_accent": "#ffffff",
        "select": "#e6eefc", "nav_hover": "#f0f2f5", "nav_active": "#e8effc", "track": "#e8ebf0",
        "scroll": "#d5dae2", "scroll_hover": "#bcc3ce", "console": "#f8f9fb",
        "ok": "#1a8f55", "warn": "#b36b00", "crit": "#d23c3c", "info": "#2563d8", "unknown": "#6b7280",
    },
}
DEFAULT_THEME = "dark"

STATUS_TAGS = {health.OK: "ok", health.WARN: "warn", health.CRIT: "crit",
               health.INFO: "info", health.UNKNOWN: "unknown"}
# Cor de cada tag de linha nas tabelas (chave da paleta).
TREE_TAG_COLORS = {"ok": "ok", "warn": "warn", "crit": "crit", "info": "info", "unknown": "unknown",
                   "applied": "ok", "off": "faint", "disabled": "faint", "normal": "text"}

# Glifos da fonte Segoe Fluent Icons / Segoe MDL2 Assets.
ICONS = {"health": "", "clean": "", "tweaks": "", "startup": "",
         "maintenance": "", "sun": "", "moon": "", "admin": ""}

_SCALE = 1.0


def px(value: float) -> int:
    """Converte medidas pensadas para 96 DPI na escala real da tela."""
    return max(1, round(value * _SCALE))


def score_color(score: int) -> str:
    return "ok" if score >= 80 else "warn" if score >= 50 else "crit"


def _colorref(color: str) -> int:
    r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    return r | (g << 8) | (b << 16)


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _mix(a: tuple[int, ...], b: tuple[int, ...], t: float) -> tuple[int, ...]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _segment_distance(x: float, y: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - a[0] - t * dx, y - a[1] - t * dy)


def paint_checkbox(image: tk.PhotoImage, size: int, gap: int, bg: str, border: str, fill: str,
                   mark: str | None = None) -> None:
    """Desenha uma caixa arredondada (com visto opcional), suavizando as bordas por
    superamostragem contra a cor de fundo conhecida. O espaço `gap` à direita separa o texto."""
    half, radius, stroke = size / 2, size * 0.24, max(1.0, size / 16)
    points = [(0.27 * size, 0.52 * size), (0.44 * size, 0.69 * size), (0.75 * size, 0.34 * size)]
    thickness = size * 0.13
    background, edge, inside = _rgb(bg), _rgb(border), _rgb(fill)
    check = _rgb(mark) if mark else None

    def box_distance(x: float, y: float, shrink: float) -> float:
        extent, r = half - 0.5 - shrink, max(radius - shrink, 0.5)
        qx, qy = abs(x - half) - (extent - r), abs(y - half) - (extent - r)
        return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - r

    samples = 3
    total = samples * samples
    rows = []
    for y in range(size):
        row = []
        for x in range(size + gap):
            outer = inner = marked = 0
            for sy in range(samples):
                for sx in range(samples):
                    fx, fy = x + (sx + 0.5) / samples, y + (sy + 0.5) / samples
                    outer += box_distance(fx, fy, 0) <= 0
                    inner += box_distance(fx, fy, stroke) <= 0
                    if check and min(_segment_distance(fx, fy, points[0], points[1]),
                                     _segment_distance(fx, fy, points[1], points[2])) <= thickness / 2:
                        marked += 1
            color = _mix(_mix(background, edge, outer / total), inside, inner / total)
            if check:
                color = _mix(color, check, marked / total)
            row.append("#%02x%02x%02x" % color)
        rows.append("{" + " ".join(row) + "}")
    image.put(" ".join(rows), to=(0, 0))


# --------------------------------------------------------------------------- preferências

def _settings_path():
    return app_data_dir() / "settings.json"


def load_settings() -> dict[str, Any]:
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict[str, Any]) -> None:
    try:
        _settings_path().write_text(json.dumps(settings), encoding="utf-8")
    except OSError:
        log.warning("Não foi possível salvar as preferências", exc_info=True)


# --------------------------------------------------------------------------- componentes

class NavItem(tk.Frame):
    """Item da barra lateral: indicador, ícone e texto, com destaque ao passar o mouse."""

    def __init__(self, parent: tk.Widget, app: App, icon: str, text: str, command: Callable[[], None]) -> None:
        super().__init__(parent, cursor="hand2")
        self.app = app
        self.command = command
        self.selected = False
        self.hover = False
        self.bar = tk.Frame(self, width=px(3))
        self.bar.pack(side="left", fill="y")
        self.icon = tk.Label(self, font=app.fonts["icon"], width=2)
        if app.has_icons:
            self.icon.pack(side="left", padx=(px(10), 0), pady=px(9))
        self.label = tk.Label(self, font=app.fonts["base"], anchor="w")
        self.label.pack(side="left", fill="x", expand=True, padx=px(10), pady=px(9))
        self.set_content(icon, text)
        for widget in (self, self.bar, self.icon, self.label):
            widget.bind("<Enter>", lambda _e: self._hover(True))
            widget.bind("<Leave>", lambda _e: self._hover(False))
            widget.bind("<Button-1>", lambda _e: self.command())
        app.on_theme(self.retheme)

    def set_content(self, icon: str, text: str) -> None:
        self.icon.configure(text=icon)
        self.label.configure(text=text)

    def set_selected(self, selected: bool) -> None:
        self.selected = selected
        self.retheme(self.app.palette)

    def _hover(self, hover: bool) -> None:
        self.hover = hover
        self.retheme(self.app.palette)

    def retheme(self, p: dict[str, str]) -> None:
        bg = p["nav_active"] if self.selected else p["nav_hover"] if self.hover else p["sidebar"]
        fg = p["text"] if self.selected or self.hover else p["muted"]
        self.configure(bg=bg)
        self.bar.configure(bg=p["accent"] if self.selected else bg)
        self.icon.configure(bg=bg, fg=p["accent"] if self.selected else fg)
        self.label.configure(bg=bg, fg=fg)


class ScoreRing(tk.Canvas):
    """Pontuação de saúde desenhada como um anel de progresso."""

    def __init__(self, parent: tk.Widget, app: App, size: int = 116) -> None:
        self.size = px(size)
        super().__init__(parent, width=self.size, height=self.size, highlightthickness=0, bd=0)
        self.app = app
        self.score: int | None = None
        app.on_theme(lambda _p: self.draw())

    def set_score(self, score: int | None) -> None:
        self.score = score
        self.draw()

    def draw(self) -> None:
        p = self.app.palette
        self.delete("all")
        self.configure(bg=p["surface"])
        width = px(9)
        pad = width // 2 + px(2)
        box = (pad, pad, self.size - pad, self.size - pad)
        self.create_oval(*box, outline=p["track"], width=width)
        if self.score:
            self.create_arc(*box, start=90, extent=-359.9 * self.score / 100, style="arc",
                            outline=p[score_color(self.score)], width=width)
        center = self.size / 2
        self.create_text(center, center - px(6), text="—" if self.score is None else str(self.score),
                         fill=p["text"], font=self.app.fonts["score"])
        self.create_text(center, center + px(20), text="de 100", fill=p["muted"], font=self.app.fonts["small"])


class ScrollFrame(ttk.Frame):
    """Cartão com rolagem vertical; o conteúdo vai em .inner."""

    def __init__(self, parent: tk.Widget, app: App, height: int | None = None) -> None:
        super().__init__(parent, style="Card.TFrame", padding=1)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0)
        if height:
            self.canvas.configure(height=px(height))
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas, style="CardInner.TFrame")
        window = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))
        app.on_theme(lambda p: self.canvas.configure(bg=p["surface"]))

    def scroll(self, event: tk.Event) -> None:
        if self.inner.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")


def card(parent: tk.Widget, padding: int = 16) -> ttk.Frame:
    return ttk.Frame(parent, style="Card.TFrame", padding=px(padding))


def page_header(page: ttk.Frame, title: str, subtitle: str) -> ttk.Frame:
    """Título e subtítulo da página; devolve a área de ações à direita."""
    head = ttk.Frame(page)
    head.pack(fill="x", pady=(0, px(18)))
    actions = ttk.Frame(head)
    actions.pack(side="right", anchor="s")
    text = ttk.Frame(head)
    text.pack(side="left", fill="x", expand=True)
    ttk.Label(text, text=title, style="Title.TLabel").pack(anchor="w")
    ttk.Label(text, text=subtitle, style="Muted.TLabel", wraplength=px(560),
              justify="left").pack(anchor="w", pady=(px(2), 0))
    return actions


def tree_card(parent: tk.Widget, app: App, columns: dict[str, tuple[str, int, bool]],
              selectmode: str = "extended") -> ttk.Treeview:
    frame = ttk.Frame(parent, style="Card.TFrame", padding=1)
    frame.pack(fill="both", expand=True)
    tree = ttk.Treeview(frame, columns=list(columns), show="headings", selectmode=selectmode)
    for key, (title, width, stretch) in columns.items():
        tree.heading(key, text=title, anchor="w")
        tree.column(key, width=px(width), stretch=stretch, anchor="w")
    scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    app.register_tree(tree)
    return tree


# --------------------------------------------------------------------------- janela

class App(tk.Tk):
    PAGES = [
        ("health", "Saúde do PC"),
        ("clean", "Limpeza"),
        ("tweaks", "Otimizações"),
        ("startup", "Inicialização"),
        ("maintenance", "Manutenção"),
    ]

    def __init__(self) -> None:
        super().__init__()
        global _SCALE
        _SCALE = max(1.0, self.winfo_fpixels("1i") / 96)
        self.title(f"DiagCompleto {__version__}")
        width = min(px(1200), self.winfo_screenwidth() - px(40))
        height = min(px(780), self.winfo_screenheight() - px(80))
        self.geometry(f"{width}x{height}")
        self.minsize(min(px(980), width), min(px(620), height))

        self._events: queue.Queue = queue.Queue()
        self._busy = 0
        self._theme_callbacks: list[Callable[[dict[str, str]], None]] = []
        self._trees: list[ttk.Treeview] = []
        self.settings = load_settings()
        theme = self.settings.get("theme")
        self.theme_name = theme if theme in PALETTES else DEFAULT_THEME
        self.palette = PALETTES[self.theme_name]

        self._init_fonts()
        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self._configure_styles(self.palette)
        self._build_layout()
        self.apply_theme(self.theme_name)
        self.show_page("health")

        self.bind_all("<MouseWheel>", self._on_wheel, add="+")
        self.after(100, self._process_events)
        self.after(300, self.health_tab.analyze)

    # ------------------------------------------------------------------ aparência

    def _init_fonts(self) -> None:
        families = set(tkfont.families(self))
        icon = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in families), None)
        mono = next((f for f in ("Cascadia Mono", "Consolas") if f in families), "Courier New")
        self.has_icons = icon is not None
        self.fonts = {
            "base": ("Segoe UI", 10),
            "small": ("Segoe UI", 9),
            "bold": ("Segoe UI Semibold", 10),
            "small_bold": ("Segoe UI Semibold", 9),
            "card_title": ("Segoe UI Semibold", 12),
            "title": ("Segoe UI Semibold", 20),
            "brand": ("Segoe UI Semibold", 14),
            "score": ("Segoe UI Semibold", 24),
            "metric": ("Segoe UI Semibold", 12),
            "icon": (icon or "Segoe UI", 12),
            "mono": (mono, 9),
        }
        size, gap = px(16), px(10)
        self._check_size = (size, gap)
        self._checks = {variant: {state: tk.PhotoImage(master=self, width=size + gap, height=size)
                                  for state in ("off", "on", "dis_off", "dis_on")}
                        for variant in ("card", "plain")}
        self._checks_ready = False

    def _paint_checks(self, p: dict[str, str]) -> None:
        """Caixas de seleção com visto, redesenhadas no lugar a cada troca de tema."""
        size, gap = self._check_size
        for variant, bg in (("card", p["surface"]), ("plain", p["bg"])):
            images = self._checks[variant]
            paint_checkbox(images["off"], size, gap, bg, p["border_strong"], p["surface"])
            paint_checkbox(images["on"], size, gap, bg, p["accent"], p["accent"], p["on_accent"])
            paint_checkbox(images["dis_off"], size, gap, bg, p["border"], p["surface2"])
            paint_checkbox(images["dis_on"], size, gap, bg, p["faint"], p["faint"], p["surface"])
            if not self._checks_ready:
                self.style.element_create(f"{variant}.check", "image", images["off"],
                                          ("disabled", "selected", images["dis_on"]),
                                          ("disabled", images["dis_off"]), ("selected", images["on"]), sticky="")
        self._checks_ready = True

    def _configure_styles(self, p: dict[str, str]) -> None:
        s, f = self.style, self.fonts
        s.configure(".", background=p["bg"], foreground=p["text"], font=f["base"], bordercolor=p["border"],
                    lightcolor=p["bg"], darkcolor=p["bg"], troughcolor=p["surface2"], focuscolor=p["accent"],
                    selectbackground=p["select"], selectforeground=p["text"], insertcolor=p["text"])

        s.configure("TFrame", background=p["bg"])
        s.configure("Sidebar.TFrame", background=p["sidebar"])
        s.configure("Card.TFrame", background=p["surface"], bordercolor=p["border"], lightcolor=p["border"],
                    darkcolor=p["border"], relief="solid", borderwidth=1)
        s.configure("CardInner.TFrame", background=p["surface"], relief="flat", borderwidth=0)

        s.configure("TLabel", background=p["bg"], foreground=p["text"])
        s.configure("Muted.TLabel", background=p["bg"], foreground=p["muted"])
        s.configure("Title.TLabel", background=p["bg"], font=f["title"])
        s.configure("Metric.TLabel", background=p["bg"], foreground=p["text"], font=f["metric"])
        s.configure("Brand.TLabel", background=p["sidebar"], foreground=p["text"], font=f["brand"])
        s.configure("SidebarMuted.TLabel", background=p["sidebar"], foreground=p["muted"], font=f["small"])
        s.configure("SidebarOk.TLabel", background=p["sidebar"], foreground=p["ok"], font=f["small"])
        s.configure("SidebarWarn.TLabel", background=p["sidebar"], foreground=p["warn"], font=f["small"])
        s.configure("Card.TLabel", background=p["surface"], foreground=p["text"])
        s.configure("CardMuted.TLabel", background=p["surface"], foreground=p["muted"], font=f["small"])
        s.configure("CardKey.TLabel", background=p["surface"], foreground=p["muted"], font=f["small"])
        s.configure("CardValue.TLabel", background=p["surface"], foreground=p["text"], font=f["bold"])
        s.configure("CardTitle.TLabel", background=p["surface"], foreground=p["text"], font=f["card_title"])

        # Botões planos, sem o contorno pontilhado de foco.
        s.layout("TButton", [("Button.border", {"sticky": "nswe", "border": "1", "children": [
            ("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})]})])
        s.configure("TButton", background=p["surface2"], foreground=p["text"], bordercolor=p["border"],
                    lightcolor=p["surface2"], darkcolor=p["surface2"], relief="flat",
                    padding=(px(16), px(7)), anchor="center", font=f["base"])
        state_bg = [("disabled", p["surface2"]), ("pressed", p["pressed"]), ("active", p["hover"])]
        s.map("TButton", background=state_bg, lightcolor=state_bg, darkcolor=state_bg,
              foreground=[("disabled", p["faint"])],
              bordercolor=[("disabled", p["border"]), ("active", p["border_strong"])])
        s.configure("Accent.TButton", background=p["accent"], foreground=p["on_accent"], bordercolor=p["accent"],
                    lightcolor=p["accent"], darkcolor=p["accent"], font=f["bold"])
        accent_bg = [("disabled", p["surface2"]), ("pressed", p["accent_pressed"]), ("active", p["accent_hover"])]
        s.map("Accent.TButton", background=accent_bg, lightcolor=accent_bg, darkcolor=accent_bg,
              bordercolor=[("disabled", p["border"]), ("pressed", p["accent_pressed"]),
                           ("active", p["accent_hover"])],
              foreground=[("disabled", p["faint"])])
        s.configure("Small.TButton", padding=(px(12), px(4)), font=f["small"])

        self._paint_checks(p)
        for style, variant, bg, font in (("TCheckbutton", "card", p["surface"], f["bold"]),
                                         ("Plain.TCheckbutton", "plain", p["bg"], f["base"])):
            s.layout(style, [("Checkbutton.padding", {"sticky": "nswe", "children": [
                (f"{variant}.check", {"side": "left", "sticky": ""}),
                ("Checkbutton.label", {"side": "left", "sticky": "nswe"})]})])
            s.configure(style, background=bg, foreground=p["text"], font=font, padding=(0, px(2)))
            s.map(style, background=[("active", bg)], foreground=[("disabled", p["faint"])])

        s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        s.configure("Treeview", background=p["surface"], fieldbackground=p["surface"], foreground=p["text"],
                    bordercolor=p["surface"], lightcolor=p["surface"], darkcolor=p["surface"],
                    rowheight=px(34), font=f["base"])
        s.map("Treeview", background=[("selected", p["select"])], foreground=[("selected", p["text"])])
        s.configure("Treeview.Heading", background=p["surface"], foreground=p["muted"], font=f["small_bold"],
                    bordercolor=p["border"], lightcolor=p["surface"], darkcolor=p["surface"], relief="flat",
                    padding=(px(10), px(8)))
        s.map("Treeview.Heading", background=[("active", p["surface2"])], lightcolor=[("active", p["surface2"])],
              darkcolor=[("active", p["surface2"])])

        s.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        s.configure("Vertical.TScrollbar", troughcolor=p["surface"], background=p["scroll"],
                    bordercolor=p["surface"], lightcolor=p["scroll"], darkcolor=p["scroll"],
                    arrowsize=px(10), gripcount=0)
        thumb = [("active", p["scroll_hover"])]
        s.map("Vertical.TScrollbar", background=thumb, lightcolor=thumb, darkcolor=thumb)

        s.configure("Horizontal.TProgressbar", troughcolor=p["surface2"], background=p["accent"],
                    bordercolor=p["surface2"], lightcolor=p["accent"], darkcolor=p["accent"], thickness=px(4))
        s.configure("TSeparator", background=p["border"])

    def on_theme(self, callback: Callable[[dict[str, str]], None]) -> None:
        """Registra um widget tk (não-ttk) que precisa ser recolorido ao trocar o tema."""
        self._theme_callbacks.append(callback)
        callback(self.palette)

    def register_tree(self, tree: ttk.Treeview) -> None:
        self._trees.append(tree)
        self._theme_tree(tree)

    def _theme_tree(self, tree: ttk.Treeview) -> None:
        for tag, key in TREE_TAG_COLORS.items():
            tree.tag_configure(tag, foreground=self.palette[key])

    def apply_theme(self, name: str) -> None:
        self.theme_name = name
        self.palette = p = PALETTES[name]
        self._configure_styles(p)
        self.configure(bg=p["bg"])
        for callback in self._theme_callbacks:
            callback(p)
        for tree in self._trees:
            self._theme_tree(tree)
        if name == "dark":
            self.theme_item.set_content(ICONS["sun"], "Tema claro")
        else:
            self.theme_item.set_content(ICONS["moon"], "Tema escuro")
        self._style_titlebar(p)

    def toggle_theme(self) -> None:
        self.apply_theme("light" if self.theme_name == "dark" else "dark")
        self.settings["theme"] = self.theme_name
        save_settings(self.settings)

    def _style_titlebar(self, p: dict[str, str]) -> None:
        """Barra de título do Windows no mesmo tema (Windows 10 20H1+ / 11)."""
        try:
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            dwm = ctypes.windll.dwmapi

            def set_attribute(attribute: int, value: int) -> None:
                data = ctypes.c_int(value)
                dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))

            set_attribute(20, 1 if self.theme_name == "dark" else 0)  # DWMWA_USE_IMMERSIVE_DARK_MODE
            set_attribute(35, _colorref(p["sidebar"]))  # DWMWA_CAPTION_COLOR (Windows 11)
            set_attribute(36, _colorref(p["text"]))     # DWMWA_TEXT_COLOR
            set_attribute(34, _colorref(p["border"]))   # DWMWA_BORDER_COLOR
        except (AttributeError, OSError):
            pass

    # ------------------------------------------------------------------ estrutura

    def _build_layout(self) -> None:
        self.columnconfigure(2, weight=1)
        self.rowconfigure(0, weight=1)

        sidebar = ttk.Frame(self, style="Sidebar.TFrame", width=px(252))
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.pack_propagate(False)
        divider = tk.Frame(self, width=1)
        divider.grid(row=0, column=1, sticky="ns")
        self.on_theme(lambda p: divider.configure(bg=p["border"]))

        brand = ttk.Frame(sidebar, style="Sidebar.TFrame", padding=(px(20), px(24), px(20), px(20)))
        brand.pack(fill="x")
        ttk.Label(brand, text="DiagCompleto", style="Brand.TLabel").pack(anchor="w")
        ttk.Label(brand, text=f"Diagnóstico e otimização · v{__version__}",
                  style="SidebarMuted.TLabel").pack(anchor="w")

        nav = ttk.Frame(sidebar, style="Sidebar.TFrame", padding=(px(10), 0))
        nav.pack(fill="x")
        self.nav_items: dict[str, NavItem] = {}
        for key, label in self.PAGES:
            item = NavItem(nav, self, ICONS[key], label, lambda k=key: self.show_page(k))
            item.pack(fill="x", pady=1)
            self.nav_items[key] = item

        bottom = ttk.Frame(sidebar, style="Sidebar.TFrame", padding=(px(10), px(10), px(10), px(18)))
        bottom.pack(side="bottom", fill="x")
        self.theme_item = NavItem(bottom, self, ICONS["sun"], "Tema claro", self.toggle_theme)
        self.theme_item.pack(fill="x", pady=1)
        if not is_admin():
            NavItem(bottom, self, ICONS["admin"], "Executar como admin", self._elevate).pack(fill="x", pady=1)
        ttk.Separator(bottom).pack(fill="x", pady=(px(10), px(10)))
        if is_admin():
            ttk.Label(bottom, text="●  Modo administrador", style="SidebarOk.TLabel").pack(anchor="w", padx=px(12))
        else:
            ttk.Label(bottom, text="●  Acesso limitado", style="SidebarWarn.TLabel").pack(anchor="w", padx=px(12))
            ttk.Label(bottom, style="SidebarMuted.TLabel", wraplength=px(212), justify="left",
                      text="Alguns sensores, limpezas e otimizações exigem administrador."
                      ).pack(anchor="w", padx=px(12), pady=(px(2), 0))

        main = ttk.Frame(self, padding=(px(32), px(26), px(32), px(14)))
        main.grid(row=0, column=2, sticky="nsew")
        main.rowconfigure(0, weight=1)
        main.columnconfigure(0, weight=1)
        container = ttk.Frame(main)
        container.grid(row=0, column=0, sticky="nsew")
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        self.health_tab = HealthTab(container, self)
        self.cleaner_tab = CleanerTab(container, self)
        self.tweaks_tab = TweaksTab(container, self)
        self.startup_tab = StartupTab(container, self)
        self.maintenance_tab = MaintenanceTab(container, self)
        self.pages = {"health": self.health_tab, "clean": self.cleaner_tab, "tweaks": self.tweaks_tab,
                      "startup": self.startup_tab, "maintenance": self.maintenance_tab}
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")

        status = ttk.Frame(main)
        status.grid(row=1, column=0, sticky="ew", pady=(px(12), 0))
        self.status_var = tk.StringVar(value="Pronto.")
        ttk.Label(status, textvariable=self.status_var, style="Muted.TLabel").pack(side="left")
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=px(160))

    def show_page(self, key: str) -> None:
        self.pages[key].tkraise()
        for name, item in self.nav_items.items():
            item.set_selected(name == key)

    def _on_wheel(self, event: tk.Event) -> None:
        widget = self.winfo_containing(event.x_root, event.y_root)
        while widget is not None and not isinstance(widget, ScrollFrame):
            widget = widget.master
        if widget is not None:
            widget.scroll(event)

    def _elevate(self) -> None:
        if relaunch_as_admin():
            self.destroy()

    # ------------------------------------------------------------------ tarefas em segundo plano

    def run_task(self, label: str, func: Callable[[], Any],
                 on_done: Callable[[Any], None] | None = None) -> None:
        """Executa func numa thread e chama on_done(resultado) na thread da interface."""
        self._set_busy(1, label)

        def worker() -> None:
            try:
                self._events.put(("done", on_done, func(), None))
            except Exception as exc:  # erro mostrado ao usuário em vez de matar a thread silenciosamente
                log.exception("Falha na tarefa: %s", label)
                self._events.put(("done", on_done, None, exc))

        threading.Thread(target=worker, daemon=True).start()

    def call_in_ui(self, func: Callable, *args: Any) -> None:
        self._events.put(("call", func, args, None))

    def _process_events(self) -> None:
        try:
            while True:
                kind, func, payload, error = self._events.get_nowait()
                if kind == "call":
                    func(*payload)
                    continue
                self._set_busy(-1)
                if error is not None:
                    messagebox.showerror("Erro", f"{error}", parent=self)
                elif func is not None:
                    func(payload)
        except queue.Empty:
            pass
        self.after(100, self._process_events)

    def _set_busy(self, delta: int, label: str | None = None) -> None:
        self._busy = max(0, self._busy + delta)
        if self._busy:
            if label:
                self.status_var.set(label + "…")
            if not self.progress.winfo_ismapped():
                self.progress.pack(side="right")
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()
            self.status_var.set("Pronto.")


# ====================================================================== Saúde

class HealthTab(ttk.Frame):
    def __init__(self, parent: tk.Widget, app: App) -> None:
        super().__init__(parent)
        self.app = app
        self.report: health.HealthReport | None = None

        actions = page_header(self, "Saúde do PC", "Discos, memória, processador, vídeo, bateria, estabilidade "
                              "e segurança. Somente leitura: nada é alterado.")
        self.analyze_btn = ttk.Button(actions, text="Analisar", style="Accent.TButton", command=self.analyze)
        self.analyze_btn.pack(side="right")
        self.export_btn = ttk.Button(actions, text="Exportar relatório", command=self.export, state="disabled")
        self.export_btn.pack(side="right", padx=(0, px(8)))

        overview = card(self, padding=20)
        overview.pack(fill="x")
        self.ring = ScoreRing(overview, app)
        self.ring.pack(side="left", anchor="n")
        info = ttk.Frame(overview, style="CardInner.TFrame")
        info.pack(side="left", fill="both", expand=True, padx=(px(24), 0))
        self.headline_var = tk.StringVar(value="Ainda não analisado")
        self.counts_var = tk.StringVar(value="Clique em “Analisar” para examinar o computador.")
        ttk.Label(info, textvariable=self.headline_var, style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(info, textvariable=self.counts_var, style="CardMuted.TLabel").pack(anchor="w", pady=(px(2), 0))
        self.summary = ttk.Frame(info, style="CardInner.TFrame")
        self.summary.pack(fill="x", pady=(px(14), 0))
        for column in range(3):
            self.summary.columnconfigure(column, weight=1, uniform="summary")

        spacer = ttk.Frame(self, height=px(16))
        spacer.pack(fill="x")
        self.tree = tree_card(self, app, {
            "component": ("Componente", 130, False),
            "name": ("Item", 300, False),
            "status": ("Status", 120, False),
            "detail": ("Detalhes", 520, True),
        }, selectmode="browse")
        self.tree.bind("<<TreeviewSelect>>", self._show_detail)
        self.detail_var = tk.StringVar()
        ttk.Label(self, textvariable=self.detail_var, wraplength=px(900), justify="left",
                  style="Muted.TLabel").pack(fill="x", pady=(px(10), 0))

    def analyze(self) -> None:
        self.analyze_btn.configure(state="disabled")
        self.app.run_task("Analisando componentes (pode levar até 1 minuto)", health.run_health_check, self._show)

    def _show(self, result: health.HealthReport | None) -> None:
        self.analyze_btn.configure(state="normal")
        if result is None:
            return
        self.report = result
        self.export_btn.configure(state="normal")
        self.ring.set_score(result.score)
        crit, warn = result.count(health.CRIT), result.count(health.WARN)
        if crit:
            headline = "Problemas sérios encontrados"
        elif warn:
            headline = "Alguns pontos pedem atenção"
        else:
            headline = "Tudo em ordem"
        self.headline_var.set(headline)
        self.counts_var.set(f"{crit} crítico(s) · {warn} alerta(s) · {len(result.items)} itens verificados")

        for child in self.summary.winfo_children():
            child.destroy()
        for index, (key, value) in enumerate(result.summary.items()):
            cell = ttk.Frame(self.summary, style="CardInner.TFrame")
            cell.grid(row=index // 3, column=index % 3, sticky="nw", padx=(0, px(16)), pady=(0, px(10)))
            ttk.Label(cell, text=key.upper(), style="CardKey.TLabel").pack(anchor="w")
            ttk.Label(cell, text=value, style="CardValue.TLabel", wraplength=px(250),
                      justify="left").pack(anchor="w")

        self.tree.delete(*self.tree.get_children())
        for item in result.items:
            tag = "normal" if item.status == health.OK else STATUS_TAGS.get(item.status, "unknown")
            self.tree.insert("", "end", values=(item.component, item.name, f"●  {item.status}", item.detail),
                             tags=(tag,))

    def _show_detail(self, _event: object) -> None:
        selection = self.tree.selection()
        if selection:
            values = self.tree.item(selection[0], "values")
            self.detail_var.set(f"{values[1]}: {values[3]}")

    def export(self) -> None:
        if not self.report:
            return
        path = report.save_report(self.report)
        open_file(path)  # abre no navegador padrão, sem elevação
        self.app.status_var.set(f"Relatório salvo em {path}")


# ====================================================================== Limpeza

class CleanerTab(ttk.Frame):
    def __init__(self, parent: tk.Widget, app: App) -> None:
        super().__init__(parent)
        self.app = app
        self.targets = cleaner.build_targets()
        self.vars: dict[str, tk.BooleanVar] = {}
        self.size_vars: dict[str, tk.StringVar] = {}
        self.scan: dict[str, cleaner.ScanResult] = {}

        actions = page_header(self, "Limpeza", "Apenas caches e temporários conhecidos. Documentos, senhas, "
                              "cookies e programas nunca são tocados. Arquivos em uso são ignorados.")
        self.clean_btn = ttk.Button(actions, text="Limpar selecionados", style="Accent.TButton",
                                    command=self.clean, state="disabled")
        self.clean_btn.pack(side="right")
        self.scan_btn = ttk.Button(actions, text="Analisar", command=self.analyze)
        self.scan_btn.pack(side="right", padx=(0, px(8)))
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, px(10)))
        ttk.Label(bar, text="Marque o que deseja limpar. Itens com efeito colateral vêm desmarcados.",
                  style="Muted.TLabel").pack(side="left")
        self.total_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.total_var, style="Metric.TLabel").pack(side="right")

        listing = ScrollFrame(self, app)
        listing.pack(fill="both", expand=True)
        rows = listing.inner
        rows.columnconfigure(0, weight=1)
        for index, target in enumerate(self.targets):
            if index:
                ttk.Separator(rows).grid(row=index * 2 - 1, column=0, columnspan=2, sticky="ew", padx=px(18))
            row = ttk.Frame(rows, style="CardInner.TFrame", padding=(px(18), px(12)))
            row.grid(row=index * 2, column=0, columnspan=2, sticky="ew")
            row.columnconfigure(0, weight=1)
            var = tk.BooleanVar(value=target.default_selected and target.available())
            var.trace_add("write", lambda *_: self._update_total())
            self.vars[target.id] = var
            check = ttk.Checkbutton(row, variable=var, text=target.name)
            check.grid(row=0, column=0, sticky="w")
            if not target.available():
                check.configure(state="disabled", text=f"{target.name}  ·  requer administrador")
            size_var = tk.StringVar(value="")
            self.size_vars[target.id] = size_var
            ttk.Label(row, textvariable=size_var, style="CardValue.TLabel").grid(row=0, column=1, sticky="e")
            ttk.Label(row, text=target.description, style="CardMuted.TLabel", wraplength=px(700),
                      justify="left").grid(row=1, column=0, columnspan=2, sticky="w", padx=(px(24), 0),
                                           pady=(px(2), 0))

    def analyze(self) -> None:
        self.scan_btn.configure(state="disabled")
        self.clean_btn.configure(state="disabled")
        for var in self.size_vars.values():
            var.set("…")
        self.app.run_task("Calculando espaço recuperável",
                          lambda: {t.id: cleaner.scan_target(t) for t in self.targets}, self._show_scan)

    def _show_scan(self, results: dict[str, cleaner.ScanResult] | None) -> None:
        self.scan_btn.configure(state="normal")
        if results is None:
            return
        self.scan = results
        for target_id, result in results.items():
            text = result.error if result.error else f"{format_bytes(result.size)}  ·  {result.files} itens"
            self.size_vars[target_id].set(text)
        self.clean_btn.configure(state="normal")
        self._update_total()

    def _selected(self) -> list[cleaner.CleanTarget]:
        return [t for t in self.targets if self.vars[t.id].get() and t.available()]

    def _update_total(self) -> None:
        if not self.scan:
            return
        total = sum(self.scan[t.id].size for t in self._selected() if t.id in self.scan)
        self.total_var.set(f"{format_bytes(total)} selecionados")

    def clean(self) -> None:
        selected = self._selected()
        if not selected:
            messagebox.showinfo("Limpeza", "Nenhum item selecionado.", parent=self)
            return
        names = "\n".join(f"• {t.name}" for t in selected)
        if not messagebox.askyesno("Confirmar limpeza", f"Os seguintes itens serão limpos:\n\n{names}\n\nContinuar?",
                                   parent=self):
            return
        self.clean_btn.configure(state="disabled")
        self.scan_btn.configure(state="disabled")
        self.app.run_task("Limpando", lambda: [cleaner.clean_target(t) for t in selected], self._done)

    def _done(self, results: list[cleaner.CleanResult] | None) -> None:
        self.scan_btn.configure(state="normal")
        if results is None:
            return
        freed = sum(r.freed for r in results)
        deleted = sum(r.deleted for r in results)
        skipped = sum(r.skipped for r in results)
        message = f"{format_bytes(freed)} liberados ({deleted} arquivos removidos)."
        if skipped:
            message += f"\n{skipped} arquivos em uso foram mantidos — é normal."
        messagebox.showinfo("Limpeza concluída", message, parent=self)
        self.analyze()


# ====================================================================== Otimizações

class TweaksTab(ttk.Frame):
    def __init__(self, parent: tk.Widget, app: App) -> None:
        super().__init__(parent)
        self.app = app
        self.store = tweaks.BackupStore()
        self.tweaks = {t.id: t for t in tweaks.build_tweaks()}

        actions = page_header(self, "Otimizações", "O valor original de cada ajuste é salvo antes de aplicar; "
                              "“Reverter” restaura exatamente o estado anterior.")
        ttk.Button(actions, text="Aplicar recomendadas", style="Accent.TButton",
                   command=self.apply_recommended).pack(side="right")

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, px(12)))
        ttk.Button(bar, text="Aplicar selecionadas", command=lambda: self._run(self._selected(), apply=True)
                   ).pack(side="left")
        ttk.Button(bar, text="Reverter selecionadas", command=lambda: self._run(self._selected(), apply=False)
                   ).pack(side="left", padx=px(8))
        ttk.Button(bar, text="Atualizar", command=self.refresh).pack(side="left")
        self.restore_var = tk.BooleanVar(value=is_admin())
        restore = ttk.Checkbutton(bar, text="Criar ponto de restauração antes", variable=self.restore_var,
                                  style="Plain.TCheckbutton")
        restore.pack(side="right")
        if not is_admin():
            restore.configure(state="disabled")

        self.tree = tree_card(self, app, {
            "name": ("Otimização", 380, True),
            "category": ("Categoria", 130, False),
            "risk": ("Risco", 80, False),
            "state": ("Estado", 130, False),
            "note": ("Observação", 240, False),
        })
        self.tree.bind("<<TreeviewSelect>>", self._show_description)
        self.desc_var = tk.StringVar(value="Selecione uma otimização para ver a descrição.")
        ttk.Label(self, textvariable=self.desc_var, wraplength=px(900), justify="left",
                  style="Muted.TLabel").pack(fill="x", pady=(px(10), 0))
        self.after(500, self.refresh)

    def refresh(self) -> None:
        def states() -> dict[str, bool | None]:
            result = {}
            for tweak in self.tweaks.values():
                try:
                    result[tweak.id] = tweak.is_applied()
                except OSError:
                    result[tweak.id] = None
            return result

        self.app.run_task("Lendo estado das otimizações", states, self._fill)

    def _fill(self, states: dict[str, bool | None] | None) -> None:
        if states is None:
            return
        selection = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for tweak in self.tweaks.values():
            applied = states.get(tweak.id)
            state = "●  Aplicado" if applied else "○  Não aplicado" if applied is False else "Indisponível"
            note = tweak.note
            if not tweak.available():
                note = "Requer administrador" + (f"; {note}" if note else "")
            tag = "disabled" if not tweak.available() else "normal"
            self.tree.insert("", "end", iid=tweak.id, tags=(tag,),
                             values=(tweak.name, tweak.category, tweak.risk, state, note))
        self.tree.selection_set([iid for iid in selection if self.tree.exists(iid)])

    def _selected(self) -> list[tweaks.Tweak]:
        return [self.tweaks[iid] for iid in self.tree.selection()]

    def _show_description(self, _event: object) -> None:
        selected = self._selected()
        if selected:
            tweak = selected[0]
            self.desc_var.set(f"{tweak.name} — {tweak.description}")

    def apply_recommended(self) -> None:
        chosen = [t for t in self.tweaks.values() if t.recommended and t.available()]
        names = "\n".join(f"• {t.name}" for t in chosen)
        if messagebox.askyesno("Aplicar recomendadas",
                               f"Serão aplicadas as otimizações de baixo risco:\n\n{names}\n\nContinuar?",
                               parent=self):
            self._run(chosen, apply=True, confirm=False)

    def _run(self, chosen: list[tweaks.Tweak], apply: bool, confirm: bool = True) -> None:
        chosen = [t for t in chosen if t.available()]
        if not chosen:
            messagebox.showinfo("Otimizações", "Selecione ao menos uma otimização disponível.", parent=self)
            return
        verb = "aplicar" if apply else "reverter"
        if confirm:
            names = "\n".join(f"• {t.name} (risco {t.risk.lower()})" for t in chosen)
            if not messagebox.askyesno("Confirmar", f"Deseja {verb}:\n\n{names}", parent=self):
                return
        create_restore = apply and self.restore_var.get()

        def work() -> list[str]:
            messages = []
            if create_restore:
                _, message = maintenance.create_restore_point()
                messages.append(message)
            for tweak in chosen:
                try:
                    tweak.apply(self.store) if apply else tweak.revert(self.store)
                    messages.append(f"✔ {tweak.name}")
                except (tweaks.TweakError, OSError) as exc:
                    messages.append(f"✖ {tweak.name}: {exc}")
            return messages

        label = "Criando ponto de restauração e aplicando" if create_restore else verb.capitalize()
        self.app.run_task(label, work, self._done)

    def _done(self, messages: list[str] | None) -> None:
        if messages:
            messagebox.showinfo("Resultado", "\n".join(messages) +
                                "\n\nAlguns ajustes só têm efeito após logoff ou reinício.", parent=self)
        self.refresh()


# ====================================================================== Inicialização

class StartupTab(ttk.Frame):
    def __init__(self, parent: tk.Widget, app: App) -> None:
        super().__init__(parent)
        self.app = app
        self.entries: dict[str, startup.StartupEntry] = {}

        actions = page_header(self, "Inicialização", "Desativar não desinstala nem apaga nada — é o mesmo "
                              "mecanismo do Gerenciador de Tarefas e pode ser revertido a qualquer momento.")
        self.count_var = tk.StringVar()
        ttk.Label(actions, textvariable=self.count_var, style="Metric.TLabel").pack(side="right")

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, px(12)))
        ttk.Button(bar, text="Desativar", command=lambda: self._toggle(False)).pack(side="left")
        ttk.Button(bar, text="Ativar", command=lambda: self._toggle(True)).pack(side="left", padx=px(8))
        ttk.Button(bar, text="Atualizar", command=self.refresh).pack(side="left")

        self.tree = tree_card(self, app, {
            "name": ("Nome", 220, False),
            "state": ("Estado", 120, False),
            "source": ("Origem", 240, False),
            "command": ("Comando", 500, True),
        })
        self.after(700, self.refresh)

    def refresh(self) -> None:
        self.app.run_task("Lendo programas de inicialização", startup.list_entries, self._fill)

    def _fill(self, entries: list[startup.StartupEntry] | None) -> None:
        if entries is None:
            return
        self.tree.delete(*self.tree.get_children())
        self.entries.clear()
        for index, entry in enumerate(entries):
            iid = str(index)
            self.entries[iid] = entry
            self.tree.insert("", "end", iid=iid, tags=("normal" if entry.enabled else "off",),
                             values=(entry.name, "●  Ativado" if entry.enabled else "○  Desativado",
                                     entry.source, entry.command))
        enabled = sum(e.enabled for e in entries)
        self.count_var.set(f"{enabled} de {len(entries)} ativados")

    def _toggle(self, enable: bool) -> None:
        selected = [self.entries[iid] for iid in self.tree.selection()]
        if not selected:
            messagebox.showinfo("Inicialização", "Selecione um ou mais programas.", parent=self)
            return
        blocked = [e.name for e in selected if not e.available()]
        if blocked:
            messagebox.showwarning("Inicialização", "Requer administrador para:\n" + "\n".join(blocked), parent=self)
        for entry in selected:
            if entry.available():
                try:
                    startup.set_enabled(entry, enable)
                except OSError as exc:
                    messagebox.showerror("Erro", f"{entry.name}: {exc}", parent=self)
        self.refresh()


# ====================================================================== Manutenção

class MaintenanceTab(ttk.Frame):
    def __init__(self, parent: tk.Widget, app: App) -> None:
        super().__init__(parent)
        self.app = app
        self.buttons: list[tuple[ttk.Button, bool]] = []  # (botão, permitido sem admin ou somos admin)

        page_header(self, "Manutenção", "Utilitários oficiais da Microsoft (DISM, SFC, Otimizar Unidades). "
                    "Não feche o programa durante a execução.")

        listing = ScrollFrame(self, app)
        listing.pack(fill="both", expand=True)
        rows = listing.inner
        rows.columnconfigure(0, weight=1)
        entries = [(a.name, f"{a.description}  ·  {a.duration}", lambda a=a: self.run(a),
                    is_admin() or not a.requires_admin) for a in maintenance.ACTIONS]
        entries.append(("Criar ponto de restauração",
                         "Permite desfazer alterações do sistema pelo Painel de Controle > Recuperação.",
                         self.restore_point, is_admin()))
        for index, (name, description, command, allowed) in enumerate(entries):
            if index:
                ttk.Separator(rows).grid(row=index * 2 - 1, column=0, sticky="ew", padx=px(18))
            row = ttk.Frame(rows, style="CardInner.TFrame", padding=(px(18), px(11)))
            row.grid(row=index * 2, column=0, sticky="ew")
            row.columnconfigure(0, weight=1)
            ttk.Label(row, text=name, style="CardValue.TLabel").grid(row=0, column=0, sticky="w")
            if not allowed:
                description += "  ·  requer administrador"
            ttk.Label(row, text=description, style="CardMuted.TLabel", wraplength=px(640),
                      justify="left").grid(row=1, column=0, sticky="w", pady=(px(2), 0))
            button = ttk.Button(row, text="Executar", style="Small.TButton", command=command,
                                state="normal" if allowed else "disabled")
            button.grid(row=0, column=1, rowspan=2, sticky="e", padx=(px(16), 0))
            self.buttons.append((button, allowed))

        ttk.Label(self, text="SAÍDA", style="Muted.TLabel", font=app.fonts["small_bold"]
                  ).pack(anchor="w", pady=(px(18), px(6)))
        console = ttk.Frame(self, style="Card.TFrame", padding=1)
        console.pack(fill="x")
        self.output = tk.Text(console, height=7, font=app.fonts["mono"], state="disabled", wrap="word",
                              relief="flat", bd=0, highlightthickness=0, padx=px(14), pady=px(10))
        scroll = ttk.Scrollbar(console, orient="vertical", command=self.output.yview)
        self.output.configure(yscrollcommand=scroll.set)
        self.output.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        app.on_theme(lambda p: self.output.configure(bg=p["console"], fg=p["text"], insertbackground=p["text"],
                                                     selectbackground=p["select"], selectforeground=p["text"]))

    def _append(self, text: str) -> None:
        self.output.configure(state="normal")
        self.output.insert("end", text + "\n")
        self.output.see("end")
        self.output.configure(state="disabled")

    def _set_buttons(self, enabled: bool) -> None:
        for button, allowed in self.buttons:
            button.configure(state="normal" if enabled and allowed else "disabled")

    def run(self, action: maintenance.MaintenanceAction) -> None:
        if not messagebox.askyesno(action.name, f"{action.description}\n\nDuração estimada: {action.duration}.\n"
                                   "Iniciar agora?", parent=self):
            return
        self._set_buttons(False)
        self._append(f"\n=== {action.name} ===")
        self.app.run_task(action.name,
                          lambda: maintenance.run_action(action, lambda line: self.app.call_in_ui(self._append, line)),
                          self._finished)

    def _finished(self, code: int | None) -> None:
        self._set_buttons(True)
        if code is not None:
            self._append("Concluído com sucesso." if code == 0 else f"Terminou com código {code}.")

    def restore_point(self) -> None:
        self._set_buttons(False)
        self._append("\n=== Ponto de restauração ===")
        self.app.run_task("Criando ponto de restauração", maintenance.create_restore_point,
                          lambda result: (self._set_buttons(True), result and self._append(result[1])))


def run() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # texto nítido em telas com escala
    except (AttributeError, OSError):
        pass
    App().mainloop()
