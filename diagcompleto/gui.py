"""Interface gráfica (tkinter) do DiagCompleto."""
from __future__ import annotations

import ctypes
import logging
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, Callable

from . import __version__, cleaner, health, maintenance, report, startup, tweaks
from .utils import format_bytes, is_admin, open_file, relaunch_as_admin

log = logging.getLogger("DiagCompleto.gui")

STATUS_TAGS = {
    health.OK: ("ok", "#1e8e3e"),
    health.WARN: ("warn", "#b26a00"),
    health.CRIT: ("crit", "#c5221f"),
    health.INFO: ("info", "#1a6dd4"),
    health.UNKNOWN: ("unknown", "#6b7280"),
}
FONT = ("Segoe UI", 10)
MUTED = "#5f6b7a"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"DiagCompleto {__version__} — Otimizador e Diagnóstico do Windows")
        self.geometry("1120x720")
        self.minsize(900, 600)
        self.option_add("*Font", FONT)
        self._events: queue.Queue = queue.Queue()
        self._busy = 0

        style = ttk.Style(self)
        style.configure("Treeview", rowheight=26)
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 13))
        style.configure("Score.TLabel", font=("Segoe UI Semibold", 30))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 10))

        if not is_admin():
            self._build_admin_banner()

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=(6, 0))
        self.health_tab = HealthTab(notebook, self)
        self.cleaner_tab = CleanerTab(notebook, self)
        self.tweaks_tab = TweaksTab(notebook, self)
        self.startup_tab = StartupTab(notebook, self)
        self.maintenance_tab = MaintenanceTab(notebook, self)
        notebook.add(self.health_tab, text="  Saúde do PC  ")
        notebook.add(self.cleaner_tab, text="  Limpeza  ")
        notebook.add(self.tweaks_tab, text="  Otimizações  ")
        notebook.add(self.startup_tab, text="  Inicialização  ")
        notebook.add(self.maintenance_tab, text="  Manutenção  ")

        status = ttk.Frame(self)
        status.pack(fill="x", padx=10, pady=6)
        self.status_var = tk.StringVar(value="Pronto.")
        ttk.Label(status, textvariable=self.status_var, style="Muted.TLabel").pack(side="left")
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=160)
        self.progress.pack(side="right")

        self.after(100, self._process_events)
        self.after(300, self.health_tab.analyze)

    def _build_admin_banner(self) -> None:
        banner = tk.Frame(self, background="#fff4ce")
        banner.pack(fill="x")
        tk.Label(banner, background="#fff4ce", foreground="#5c4400",
                 text="Executando sem privilégios de administrador: alguns sensores, limpezas e otimizações "
                      "ficam indisponíveis.").pack(side="left", padx=10, pady=6)
        ttk.Button(banner, text="Reiniciar como administrador", command=self._elevate).pack(side="right", padx=10)

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
            self.progress.start(12)
        else:
            self.progress.stop()
            self.status_var.set("Pronto.")


def _toolbar(parent: tk.Widget) -> ttk.Frame:
    frame = ttk.Frame(parent)
    frame.pack(fill="x", pady=(10, 8))
    return frame


def _scrolled_tree(parent: tk.Widget, columns: dict[str, tuple[str, int, bool]],
                   selectmode: str = "extended") -> ttk.Treeview:
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True)
    tree = ttk.Treeview(frame, columns=list(columns), show="headings", selectmode=selectmode)
    for key, (title, width, stretch) in columns.items():
        tree.heading(key, text=title, anchor="w")
        tree.column(key, width=width, stretch=stretch, anchor="w")
    scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    for _, (tag, color) in STATUS_TAGS.items():
        tree.tag_configure(tag, foreground=color)
    return tree


# ====================================================================== Saúde

class HealthTab(ttk.Frame):
    def __init__(self, parent: tk.Widget, app: App) -> None:
        super().__init__(parent, padding=10)
        self.app = app
        self.report: health.HealthReport | None = None

        top = ttk.Frame(self)
        top.pack(fill="x")
        score_box = ttk.Frame(top)
        score_box.pack(side="left", padx=(0, 24))
        self.score_var = tk.StringVar(value="—")
        self.score_label = ttk.Label(score_box, textvariable=self.score_var, style="Score.TLabel")
        self.score_label.pack()
        ttk.Label(score_box, text="saúde geral", style="Muted.TLabel").pack()

        self.summary_var = tk.StringVar(value="Clique em “Analisar” para examinar o computador.")
        ttk.Label(top, textvariable=self.summary_var, justify="left").pack(side="left", fill="x", expand=True)

        buttons = ttk.Frame(top)
        buttons.pack(side="right")
        self.analyze_btn = ttk.Button(buttons, text="Analisar", style="Accent.TButton", command=self.analyze)
        self.analyze_btn.pack(fill="x", pady=2)
        self.export_btn = ttk.Button(buttons, text="Exportar relatório", command=self.export, state="disabled")
        self.export_btn.pack(fill="x", pady=2)

        ttk.Separator(self).pack(fill="x", pady=10)
        self.tree = _scrolled_tree(self, {
            "component": ("Componente", 120, False),
            "name": ("Item", 300, False),
            "status": ("Status", 100, False),
            "detail": ("Detalhes", 520, True),
        }, selectmode="browse")
        self.tree.bind("<<TreeviewSelect>>", self._show_detail)
        self.detail_var = tk.StringVar()
        ttk.Label(self, textvariable=self.detail_var, wraplength=1000, justify="left",
                  style="Muted.TLabel").pack(fill="x", pady=(8, 0))

    def analyze(self) -> None:
        self.analyze_btn.configure(state="disabled")
        self.app.run_task("Analisando componentes (pode levar até 1 minuto)", health.run_health_check, self._show)

    def _show(self, result: health.HealthReport | None) -> None:
        self.analyze_btn.configure(state="normal")
        if result is None:
            return
        self.report = result
        self.export_btn.configure(state="normal")
        self.score_var.set(str(result.score))
        color = "#1e8e3e" if result.score >= 80 else "#b26a00" if result.score >= 50 else "#c5221f"
        self.score_label.configure(foreground=color)
        lines = [f"{k}: {v}" for k, v in result.summary.items()]
        lines.append(f"{result.count(health.CRIT)} crítico(s), {result.count(health.WARN)} alerta(s)")
        self.summary_var.set("\n".join(lines))
        self.tree.delete(*self.tree.get_children())
        for item in result.items:
            tag = STATUS_TAGS.get(item.status, ("unknown", ""))[0]
            self.tree.insert("", "end", values=(item.component, item.name, item.status, item.detail), tags=(tag,))

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
        super().__init__(parent, padding=10)
        self.app = app
        self.targets = cleaner.build_targets()
        self.vars: dict[str, tk.BooleanVar] = {}
        self.size_vars: dict[str, tk.StringVar] = {}
        self.scan: dict[str, cleaner.ScanResult] = {}

        ttk.Label(self, text="Arquivos descartáveis", style="Title.TLabel").pack(anchor="w")
        ttk.Label(self, style="Muted.TLabel", text="Apenas caches e temporários conhecidos. Documentos, "
                  "senhas, cookies e programas nunca são tocados. Arquivos em uso são ignorados.").pack(anchor="w")

        bar = _toolbar(self)
        self.scan_btn = ttk.Button(bar, text="Analisar", command=self.analyze)
        self.scan_btn.pack(side="left")
        self.clean_btn = ttk.Button(bar, text="Limpar selecionados", style="Accent.TButton",
                                    command=self.clean, state="disabled")
        self.clean_btn.pack(side="left", padx=6)
        self.total_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.total_var, style="Title.TLabel").pack(side="right")

        grid = ttk.Frame(self)
        grid.pack(fill="both", expand=True)
        grid.columnconfigure(1, weight=1)
        for row, target in enumerate(self.targets):
            var = tk.BooleanVar(value=target.default_selected and target.available())
            var.trace_add("write", lambda *_: self._update_total())
            self.vars[target.id] = var
            check = ttk.Checkbutton(grid, variable=var, text=target.name)
            check.grid(row=row * 2, column=0, sticky="w", pady=(6, 0))
            if not target.available():
                check.configure(state="disabled", text=f"{target.name} (requer administrador)")
            size_var = tk.StringVar(value="")
            self.size_vars[target.id] = size_var
            ttk.Label(grid, textvariable=size_var, anchor="e").grid(row=row * 2, column=2, sticky="e")
            ttk.Label(grid, text=target.description, style="Muted.TLabel", wraplength=850,
                      justify="left").grid(row=row * 2 + 1, column=0, columnspan=3, sticky="w", padx=(24, 0))

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
            text = result.error if result.error else f"{format_bytes(result.size)}  ({result.files} itens)"
            self.size_vars[target_id].set(text)
        self.clean_btn.configure(state="normal")
        self._update_total()

    def _selected(self) -> list[cleaner.CleanTarget]:
        return [t for t in self.targets if self.vars[t.id].get() and t.available()]

    def _update_total(self) -> None:
        if not self.scan:
            return
        total = sum(self.scan[t.id].size for t in self._selected() if t.id in self.scan)
        self.total_var.set(f"Selecionado: {format_bytes(total)}")

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
        super().__init__(parent, padding=10)
        self.app = app
        self.store = tweaks.BackupStore()
        self.tweaks = {t.id: t for t in tweaks.build_tweaks()}

        ttk.Label(self, text="Otimizações reversíveis", style="Title.TLabel").pack(anchor="w")
        ttk.Label(self, style="Muted.TLabel", text="O valor original de cada ajuste é salvo antes de aplicar; "
                  "“Reverter” restaura exatamente o estado anterior.").pack(anchor="w")

        bar = _toolbar(self)
        ttk.Button(bar, text="Aplicar recomendadas", style="Accent.TButton",
                   command=self.apply_recommended).pack(side="left")
        ttk.Button(bar, text="Aplicar selecionadas", command=lambda: self._run(self._selected(), apply=True)
                   ).pack(side="left", padx=6)
        ttk.Button(bar, text="Reverter selecionadas", command=lambda: self._run(self._selected(), apply=False)
                   ).pack(side="left")
        ttk.Button(bar, text="Atualizar", command=self.refresh).pack(side="left", padx=6)
        self.restore_var = tk.BooleanVar(value=is_admin())
        restore = ttk.Checkbutton(bar, text="Criar ponto de restauração antes", variable=self.restore_var)
        restore.pack(side="right")
        if not is_admin():
            restore.configure(state="disabled")

        self.tree = _scrolled_tree(self, {
            "name": ("Otimização", 380, True),
            "category": ("Categoria", 130, False),
            "risk": ("Risco", 70, False),
            "state": ("Estado", 120, False),
            "note": ("Observação", 260, False),
        })
        self.tree.tag_configure("applied", foreground="#1e8e3e")
        self.tree.tag_configure("disabled", foreground="#9aa0a6")
        self.tree.bind("<<TreeviewSelect>>", self._show_description)
        self.desc_var = tk.StringVar(value="Selecione uma otimização para ver a descrição.")
        ttk.Label(self, textvariable=self.desc_var, wraplength=1000, justify="left").pack(fill="x", pady=(8, 0))
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
            state = "Aplicado" if applied else "Não aplicado" if applied is False else "Indisponível"
            note = tweak.note
            if not tweak.available():
                note = "Requer administrador" + (f"; {note}" if note else "")
            tag = "disabled" if not tweak.available() else "applied" if applied else ""
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
        super().__init__(parent, padding=10)
        self.app = app
        self.entries: dict[str, startup.StartupEntry] = {}

        ttk.Label(self, text="Programas que iniciam com o Windows", style="Title.TLabel").pack(anchor="w")
        ttk.Label(self, style="Muted.TLabel", text="Desativar não desinstala nem apaga nada — é o mesmo "
                  "mecanismo do Gerenciador de Tarefas e pode ser revertido a qualquer momento.").pack(anchor="w")
        bar = _toolbar(self)
        ttk.Button(bar, text="Desativar", command=lambda: self._toggle(False)).pack(side="left")
        ttk.Button(bar, text="Ativar", command=lambda: self._toggle(True)).pack(side="left", padx=6)
        ttk.Button(bar, text="Atualizar", command=self.refresh).pack(side="left")
        self.count_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.count_var, style="Muted.TLabel").pack(side="right")

        self.tree = _scrolled_tree(self, {
            "name": ("Nome", 220, False),
            "state": ("Estado", 90, False),
            "source": ("Origem", 230, False),
            "command": ("Comando", 500, True),
        })
        self.tree.tag_configure("off", foreground="#9aa0a6")
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
            self.tree.insert("", "end", iid=iid, tags=(() if entry.enabled else ("off",)),
                             values=(entry.name, "Ativado" if entry.enabled else "Desativado",
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
        super().__init__(parent, padding=10)
        self.app = app
        self.buttons: list[tuple[ttk.Button, bool]] = []  # (botão, permitido sem admin ou somos admin)

        ttk.Label(self, text="Ferramentas oficiais do Windows", style="Title.TLabel").pack(anchor="w")
        ttk.Label(self, style="Muted.TLabel", text="Executam os utilitários da própria Microsoft (DISM, SFC, "
                  "Otimizar Unidades). Não feche o programa durante a execução.").pack(anchor="w")

        grid = ttk.Frame(self)
        grid.pack(fill="x", pady=10)
        grid.columnconfigure(1, weight=1)
        actions = list(maintenance.ACTIONS)
        for row, action in enumerate(actions):
            button = ttk.Button(grid, text=action.name, width=40, command=lambda a=action: self.run(a))
            button.grid(row=row, column=0, sticky="w", pady=2)
            allowed = is_admin() or not action.requires_admin
            if not allowed:
                button.configure(state="disabled")
            self.buttons.append((button, allowed))
            ttk.Label(grid, text=f"{action.description}  [{action.duration}]", style="Muted.TLabel",
                      wraplength=650, justify="left").grid(row=row, column=1, sticky="w", padx=10)
        restore_btn = ttk.Button(grid, text="Criar ponto de restauração agora", width=40,
                                 command=self.restore_point)
        restore_btn.grid(row=len(actions), column=0, sticky="w", pady=2)
        if not is_admin():
            restore_btn.configure(state="disabled")
        self.buttons.append((restore_btn, is_admin()))
        ttk.Label(grid, text="Permite desfazer alterações do sistema pelo Painel de Controle > Recuperação.",
                  style="Muted.TLabel").grid(row=len(actions), column=1, sticky="w", padx=10)

        self.output = ScrolledText(self, height=12, font=("Consolas", 9), state="disabled", wrap="word")
        self.output.pack(fill="both", expand=True)

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
