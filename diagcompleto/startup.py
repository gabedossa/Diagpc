"""Gerenciamento de programas que iniciam com o Windows.

Usa o mesmo mecanismo do Gerenciador de Tarefas (chaves StartupApproved): desativar
não apaga a entrada, apenas a marca como desativada — e ela aparece como
"Desabilitado" no Gerenciador de Tarefas, podendo ser reativada a qualquer momento.
"""
from __future__ import annotations

import logging
import time
import winreg
from dataclasses import dataclass
from pathlib import Path

from . import winsec
from .utils import is_admin

log = logging.getLogger("DiagCompleto.startup")

APPROVED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"
RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN32 = r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"


@dataclass
class StartupEntry:
    name: str
    command: str
    source: str
    enabled: bool
    hive: int
    approved_subkey: str
    approved_name: str

    @property
    def requires_admin(self) -> bool:
        return self.hive == winreg.HKEY_LOCAL_MACHINE

    def available(self) -> bool:
        return not self.requires_admin or is_admin()


def _read_run_key(hive: int, key: str) -> list[tuple[str, str]]:
    try:
        with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as handle:
            values = []
            index = 0
            while True:
                try:
                    name, data, _ = winreg.EnumValue(handle, index)
                except OSError:
                    break
                values.append((name, str(data)))
                index += 1
            return values
    except FileNotFoundError:
        return []


def _is_enabled(hive: int, subkey: str, name: str) -> bool:
    try:
        with winreg.OpenKey(hive, f"{APPROVED}\\{subkey}", 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as handle:
            data, _ = winreg.QueryValueEx(handle, name)
    except FileNotFoundError:
        return True
    # Primeiro byte par (0x02, 0x06) = habilitado; ímpar (0x03, 0x07) = desabilitado.
    return not (isinstance(data, bytes) and data and data[0] & 1)


def _startup_folders() -> list[tuple[int, Path, str]]:
    return [
        (winreg.HKEY_CURRENT_USER, Path(winsec.roaming_appdata()) / r"Microsoft\Windows\Start Menu\Programs\Startup",
         "Pasta Inicializar (usuário)"),
        (winreg.HKEY_LOCAL_MACHINE, Path(winsec.program_data()) / r"Microsoft\Windows\Start Menu\Programs\StartUp",
         "Pasta Inicializar (todos)"),
    ]


def list_entries() -> list[StartupEntry]:
    entries: list[StartupEntry] = []
    run_sources = [
        (winreg.HKEY_CURRENT_USER, RUN, "Run", "Registro (usuário)"),
        (winreg.HKEY_LOCAL_MACHINE, RUN, "Run", "Registro (todos os usuários)"),
        (winreg.HKEY_LOCAL_MACHINE, RUN32, "Run32", "Registro 32 bits (todos os usuários)"),
    ]
    for hive, key, approved, label in run_sources:
        for name, command in _read_run_key(hive, key):
            if not name:
                continue
            entries.append(StartupEntry(name, command, label, _is_enabled(hive, approved, name),
                                        hive, approved, name))
    for hive, folder, label in _startup_folders():
        if not folder.is_dir():
            continue
        for item in folder.iterdir():
            if item.name.lower() == "desktop.ini" or not item.is_file():
                continue
            entries.append(StartupEntry(item.stem, str(item), label,
                                        _is_enabled(hive, "StartupFolder", item.name),
                                        hive, "StartupFolder", item.name))
    return sorted(entries, key=lambda e: e.name.lower())


def _filetime_now() -> bytes:
    return int((time.time() + 11644473600) * 10_000_000).to_bytes(8, "little")


def set_enabled(entry: StartupEntry, enabled: bool) -> None:
    if not entry.available():
        raise PermissionError("Requer executar como administrador.")
    data = bytes([0x02]) + bytes(11) if enabled else bytes([0x03, 0, 0, 0]) + _filetime_now()
    key = f"{APPROVED}\\{entry.approved_subkey}"
    with winreg.CreateKeyEx(entry.hive, key, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as handle:
        winreg.SetValueEx(handle, entry.approved_name, 0, winreg.REG_BINARY, data)
    entry.enabled = enabled
    log.info("Inicialização %s: %s", "ativada" if enabled else "desativada", entry.name)
