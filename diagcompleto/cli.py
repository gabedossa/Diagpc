"""Interface de linha de comando (útil para automação e diagnóstico rápido)."""
from __future__ import annotations

import argparse
import os

from . import cleaner, health, report, startup, tweaks
from .utils import format_bytes, is_admin


def cmd_health(args: argparse.Namespace) -> int:
    print("Analisando componentes...")
    result = health.run_health_check()
    for key, value in result.summary.items():
        print(f"{key:>12}: {value}")
    print(f"\nPontuação de saúde: {result.score}/100\n")
    for item in result.items:
        print(f"[{item.status:^12}] {item.component} — {item.name}\n               {item.detail}")
    if args.html:
        path = report.save_report(result)
        print(f"\nRelatório salvo em {path}")
        os.startfile(path)
    return 0


def cmd_scan(_args: argparse.Namespace) -> int:
    total = 0
    for target in cleaner.build_targets():
        result = cleaner.scan_target(target)
        size = result.error or format_bytes(result.size)
        mark = "*" if target.default_selected else " "
        print(f" {mark} {target.id:<18} {size:>22}  {target.name}")
        total += result.size
    print(f"\nTotal recuperável: {format_bytes(total)}  (* = selecionado por padrão)")
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    targets = {t.id: t for t in cleaner.build_targets()}
    ids = args.targets or [t.id for t in targets.values() if t.default_selected]
    unknown = [i for i in ids if i not in targets]
    if unknown:
        print("Alvos desconhecidos: " + ", ".join(unknown))
        return 2
    if not args.yes:
        answer = input(f"Limpar {', '.join(ids)}? [s/N] ").strip().lower()
        if answer not in ("s", "sim", "y"):
            return 1
    freed = 0
    for target_id in ids:
        result = cleaner.clean_target(targets[target_id])
        freed += result.freed
        status = result.error or f"{format_bytes(result.freed)} ({result.deleted} arquivos, {result.skipped} em uso)"
        print(f"{target_id:<18} {status}")
    print(f"\nTotal liberado: {format_bytes(freed)}")
    return 0


def cmd_tweaks(_args: argparse.Namespace) -> int:
    for tweak in tweaks.build_tweaks():
        applied = tweak.is_applied()
        state = "aplicado" if applied else "não aplicado" if applied is False else "indisponível"
        print(f"{tweak.id:<22} {state:<14} risco {tweak.risk:<6} {tweak.name}")
    return 0


def cmd_startup(_args: argparse.Namespace) -> int:
    for entry in startup.list_entries():
        print(f"{'ON ' if entry.enabled else 'OFF'}  {entry.name:<30} {entry.source}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="DiagCompleto", description="Otimizador e diagnóstico do Windows.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("saude", help="diagnóstico de saúde dos componentes")
    p.add_argument("--html", action="store_true", help="gera e abre relatório HTML")
    p.set_defaults(func=cmd_health)
    sub.add_parser("analisar", help="mostra o espaço recuperável").set_defaults(func=cmd_scan)
    p = sub.add_parser("limpar", help="limpa arquivos descartáveis")
    p.add_argument("targets", nargs="*", help="ids dos alvos (padrão: os recomendados)")
    p.add_argument("-y", "--yes", action="store_true", help="não pedir confirmação")
    p.set_defaults(func=cmd_clean)
    sub.add_parser("ajustes", help="lista o estado das otimizações").set_defaults(func=cmd_tweaks)
    sub.add_parser("inicializacao", help="lista programas de inicialização").set_defaults(func=cmd_startup)
    args = parser.parse_args(argv)
    if not is_admin():
        print("(sem privilégios de administrador — algumas informações e ações ficam limitadas)\n")
    return args.func(args)
