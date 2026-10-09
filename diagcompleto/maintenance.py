"""Ferramentas de manutenção oficiais do Windows (DISM, SFC, otimização de unidades) e
ponto de restauração do sistema."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from .utils import CommandError, is_admin, powershell_args, run_command_stream, run_powershell, system_tool

log = logging.getLogger("DiagCompleto.maintenance")


@dataclass(frozen=True)
class MaintenanceAction:
    id: str
    name: str
    description: str
    duration: str
    command: Callable[[], list[str]]
    requires_admin: bool = True


ACTIONS = [
    MaintenanceAction(
        "component_cleanup", "Limpar componentes do Windows (WinSxS)",
        "Remove versões antigas de componentes substituídas por atualizações. Usa o DISM oficial, sem "
        "/ResetBase — as atualizações continuam podendo ser desinstaladas.",
        "5 a 20 min",
        lambda: [system_tool("Dism.exe"), "/Online", "/Cleanup-Image", "/StartComponentCleanup"]),
    MaintenanceAction(
        "analyze_store", "Analisar repositório de componentes",
        "Mostra quanto espaço a limpeza de componentes pode liberar (somente leitura).",
        "1 a 3 min",
        lambda: [system_tool("Dism.exe"), "/Online", "/Cleanup-Image", "/AnalyzeComponentStore"]),
    MaintenanceAction(
        "dism_restore", "Reparar imagem do Windows (DISM)",
        "Verifica e corrige a imagem do sistema usando o Windows Update como fonte. Execute antes do SFC.",
        "10 a 30 min",
        lambda: [system_tool("Dism.exe"), "/Online", "/Cleanup-Image", "/RestoreHealth"]),
    MaintenanceAction(
        "sfc", "Verificar arquivos do sistema (SFC)",
        "Verifica a integridade dos arquivos protegidos do Windows e repara os corrompidos.",
        "10 a 20 min",
        lambda: [system_tool("sfc.exe"), "/scannow"]),
    MaintenanceAction(
        "optimize_drives", "Otimizar unidades",
        "TRIM em SSDs e desfragmentação em HDs — o Windows escolhe o adequado para cada unidade.",
        "1 min (SSD) a 1 h (HD)",
        lambda: powershell_args(
            "Get-Volume | Where-Object { $_.DriveType -eq 'Fixed' -and $_.DriveLetter } | "
            "ForEach-Object { Write-Output \"Unidade $($_.DriveLetter):\"; "
            "Optimize-Volume -DriveLetter $_.DriveLetter -Verbose 4>&1 }")),
    MaintenanceAction(
        "flush_dns", "Limpar cache DNS",
        "Resolve problemas de sites que não abrem após mudanças de rede.",
        "instantâneo",
        lambda: [system_tool("ipconfig.exe"), "/flushdns"], requires_admin=False),
]


def run_action(action: MaintenanceAction, on_line: Callable[[str], None]) -> int:
    if action.requires_admin and not is_admin():
        raise PermissionError("Esta ação requer executar como administrador.")
    log.info("Manutenção iniciada: %s", action.id)
    code = run_command_stream(action.command(), on_line)
    log.info("Manutenção %s terminou com código %s", action.id, code)
    return code


def create_restore_point(description: str = "DiagCompleto - antes das otimizações") -> tuple[bool, str]:
    """Cria um ponto de restauração. O Windows limita a um por 24 h e exige a Proteção
    do Sistema ativa; nesses casos retorna (False, motivo) sem alterar nada."""
    if not is_admin():
        return False, "Requer executar como administrador."
    safe_description = description.replace("'", "")
    script = (
        "$ErrorActionPreference='Stop';"
        "$before=@(Get-ComputerRestorePoint -ErrorAction SilentlyContinue).Count;"
        f"try {{ Checkpoint-Computer -Description '{safe_description}' -RestorePointType MODIFY_SETTINGS "
        "-WarningAction SilentlyContinue } catch { Write-Output \"ERRO:$($_.Exception.Message)\"; exit 0 };"
        "$after=@(Get-ComputerRestorePoint -ErrorAction SilentlyContinue).Count;"
        "Write-Output \"$before|$after\""
    )
    try:
        output = run_powershell(script, timeout=600).strip()
    except (CommandError, OSError) as exc:
        return False, f"Falha ao criar ponto de restauração: {exc}"
    if output.startswith("ERRO:"):
        return False, ("Não foi possível criar o ponto de restauração. Verifique se a Proteção do Sistema "
                       f"está ativada para a unidade C:. Detalhe: {output[5:]}")
    try:
        before, after = (int(x) for x in output.splitlines()[-1].split("|"))
    except ValueError:
        return False, f"Resposta inesperada: {output}"
    if after > before:
        return True, "Ponto de restauração criado."
    return False, ("O Windows não criou um novo ponto (ele permite apenas um a cada 24 h). "
                   "Já existe um ponto de restauração recente que pode ser usado.")
