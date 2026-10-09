"""Limpeza de arquivos descartáveis com regras de segurança rígidas.

Princípios:
- Só apaga dentro de pastas de cache/temporários conhecidas (lista fechada abaixo).
- Nunca segue links simbólicos ou junções (evita "escapar" para outras pastas).
- Arquivos temporários recentes são preservados (podem pertencer a instaladores em andamento).
- Arquivos em uso são simplesmente ignorados.
- A pasta raiz de cada local nunca é removida, apenas o conteúdo.
"""
from __future__ import annotations

import ctypes
import fnmatch
import logging
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from . import winsec
from .utils import is_admin

log = logging.getLogger("DiagCompleto.cleaner")

FILE_ATTRIBUTE_REPARSE_POINT = winsec.FILE_ATTRIBUTE_REPARSE_POINT


@dataclass(frozen=True)
class Location:
    root: Path
    pattern: str = "*"
    recursive: bool = True

    @property
    def removes_dirs(self) -> bool:
        return self.recursive and self.pattern == "*"

    @property
    def single_file(self) -> bool:
        """Um nome de arquivo exato, sem curingas e sem recursão (ex.: MEMORY.DMP)."""
        return not self.recursive and not any(ch in self.pattern for ch in "*?[")


@dataclass
class ScanResult:
    size: int = 0
    files: int = 0
    error: str | None = None


@dataclass
class CleanResult:
    freed: int = 0
    deleted: int = 0
    skipped: int = 0
    error: str | None = None


@dataclass
class CleanTarget:
    id: str
    name: str
    description: str
    locations: Callable[[], list[Location]] = field(default=lambda: [])
    min_age_hours: float = 0
    requires_admin: bool = False
    default_selected: bool = True
    scan_override: Callable[[], ScanResult] | None = None
    clean_override: Callable[[], CleanResult] | None = None

    def available(self) -> bool:
        return not self.requires_admin or is_admin()


# --------------------------------------------------------------------------- segurança

def _protected_paths() -> set[str]:
    # Das APIs do Windows; variáveis de ambiente só acrescentam (nunca afrouxam) a lista.
    paths = set(winsec.protected_dirs())
    for env in ("SystemDrive", "SystemRoot", "USERPROFILE", "ProgramFiles", "ProgramFiles(x86)",
                "ProgramData", "APPDATA", "LOCALAPPDATA", "PUBLIC"):
        value = os.environ.get(env)
        if value:
            paths.add(winsec.norm(value + ("\\" if value.endswith(":") else "")))
    return paths


def _root_problem(root: Path, single_file: bool = False) -> str | None:
    """Motivo para recusar a raiz, ou None se ela for segura."""
    real = winsec.real_dir(root)
    if real is None:
        return "inexistente ou passa por junção/link"
    if not any(winsec.is_within(real, base) for base in winsec.allowed_bases()):
        return "fora do perfil do usuário, da pasta do Windows e do ProgramData"
    if single_file:
        return None
    if real in _protected_paths():
        return "pasta protegida"
    _, tail = os.path.splitdrive(real)
    if len([part for part in tail.split(os.sep) if part]) < 2:
        return "rasa demais"
    return None


def is_safe_root(root: Path) -> bool:
    """Recusa raízes perigosas (raiz do disco, Windows, perfil do usuário, junções etc.)."""
    return _root_problem(root) is None


def _is_reparse_point(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT) or stat.S_ISLNK(st.st_mode)


def _age_reference(st: os.stat_result) -> float:
    # Usa a data mais recente entre modificação e criação: arquivos recém-extraídos
    # por instaladores mantêm a data de modificação antiga, mas a criação é nova.
    created = getattr(st, "st_birthtime", st.st_ctime)
    return max(st.st_mtime, created)


def iter_files(location: Location, min_age_hours: float = 0,
               now: float | None = None, dirs_out: list[Path] | None = None) -> Iterator[tuple[Path, int]]:
    """Percorre os arquivos elegíveis de um local sem seguir links/junções."""
    if not location.root.is_dir():
        return
    problem = _root_problem(location.root, location.single_file)
    if problem:
        log.warning("Local recusado por segurança (%s): %s", problem, location.root)
        return
    now = now or time.time()
    min_age = min_age_hours * 3600
    pattern = location.pattern.lower()
    stack = [location.root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if _is_reparse_point(st):
                continue
            if stat.S_ISDIR(st.st_mode):
                if location.recursive:
                    stack.append(Path(entry.path))
                    if dirs_out is not None:
                        dirs_out.append(Path(entry.path))
                continue
            if not fnmatch.fnmatch(entry.name.lower(), pattern):
                continue
            if min_age and now - _age_reference(st) < min_age:
                continue
            yield Path(entry.path), st.st_size


def _delete_file(path: Path, root: Path) -> bool | None:
    # Exclusão pelo handle, conferindo o caminho real: se uma pasta intermediária for trocada
    # por junção depois da varredura, o arquivo de fora da raiz não é apagado.
    return winsec.delete_within(path, winsec.norm(root))


def scan_target(target: CleanTarget) -> ScanResult:
    if not target.available():
        return ScanResult(error="Requer administrador")
    if target.scan_override:
        try:
            return target.scan_override()
        except OSError as exc:
            return ScanResult(error=str(exc))
    result = ScanResult()
    for location in target.locations():
        for _, size in iter_files(location, target.min_age_hours):
            result.size += size
            result.files += 1
    return result


def clean_target(target: CleanTarget) -> CleanResult:
    if not target.available():
        return CleanResult(error="Requer administrador")
    if target.clean_override:
        try:
            return target.clean_override()
        except OSError as exc:
            return CleanResult(error=str(exc))
    result = CleanResult()
    now = time.time()
    for location in target.locations():
        dirs: list[Path] = []
        for path, size in list(iter_files(location, target.min_age_hours, now, dirs)):
            outcome = _delete_file(path, location.root)
            if outcome:
                result.freed += size
                result.deleted += 1
            elif outcome is False:
                result.skipped += 1
            # None: o próprio programa dono já apagou; não conta como liberado nem como ignorado.
        if location.removes_dirs:
            _remove_empty_dirs(dirs, location.root, target.min_age_hours, now)
    log.info("Limpeza %s: %d arquivos, %d bytes, %d ignorados",
             target.id, result.deleted, result.freed, result.skipped)
    return result


def _remove_empty_dirs(dirs: list[Path], root: Path, min_age_hours: float, now: float) -> None:
    # Do mais profundo para o mais raso; a raiz do local não está na lista.
    real_root = winsec.norm(root)
    for directory in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            if min_age_hours and now - _age_reference(os.lstat(directory)) < min_age_hours * 3600:
                continue
        except OSError:
            continue
        winsec.delete_within(directory, real_root, is_dir=True)  # só funciona se estiver vazia


# --------------------------------------------------------------------------- Lixeira

class _RecycleBinInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]


def _recycle_bin_scan() -> ScanResult:
    info = _RecycleBinInfo()
    info.cbSize = ctypes.sizeof(_RecycleBinInfo)
    hr = ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
    if hr != 0:
        return ScanResult(error=f"Falha ao consultar a Lixeira (0x{hr & 0xFFFFFFFF:08X})")
    return ScanResult(size=info.i64Size, files=info.i64NumItems)


def _recycle_bin_clean() -> CleanResult:
    before = _recycle_bin_scan()
    if not before.files:
        return CleanResult()
    flags = 0x1 | 0x2 | 0x4  # sem confirmação, sem barra de progresso, sem som
    ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, flags)
    after = _recycle_bin_scan()
    return CleanResult(freed=max(0, before.size - after.size), deleted=max(0, before.files - after.files))


# --------------------------------------------------------------------------- alvos

def _chromium_caches(base: Path | None) -> list[Location]:
    if not base or not base.is_dir():
        return []
    locations = []
    for profile in base.iterdir():
        if not profile.is_dir():
            continue
        for sub in ("Cache", "Code Cache", "GPUCache", "Service Worker\\CacheStorage"):
            candidate = profile / sub
            if candidate.is_dir():
                locations.append(Location(candidate))
    for shared in ("ShaderCache", "GrShaderCache"):
        if (base / shared).is_dir():
            locations.append(Location(base / shared))
    return locations


def _browser_locations() -> list[Location]:
    local = Path(winsec.local_appdata())
    locations = []
    for vendor in (r"Google\Chrome", r"Microsoft\Edge", r"BraveSoftware\Brave-Browser", r"Vivaldi"):
        locations += _chromium_caches(local / vendor / "User Data")
    for name in ("Opera Stable", "Opera GX Stable"):
        folder = local / "Opera Software" / name
        for sub in ("Cache", "Code Cache", "GPUCache"):
            if (folder / sub).is_dir():
                locations.append(Location(folder / sub))
    firefox = local / "Mozilla" / "Firefox" / "Profiles"
    if firefox.is_dir():
        for profile in firefox.iterdir():
            for sub in ("cache2", "startupCache", "thumbnails"):
                if (profile / sub).is_dir():
                    locations.append(Location(profile / sub))
    return locations


def _existing(*paths: Path | None, pattern: str = "*", recursive: bool = True) -> list[Location]:
    return [Location(p, pattern, recursive) for p in paths if p and p.is_dir()]


def _windir() -> Path:
    return Path(winsec.windows_dir())


def _program_data() -> Path:
    return Path(winsec.program_data())


def build_targets() -> list[CleanTarget]:
    # Caminhos das APIs do Windows: %TEMP% e afins podem ser apontados pelo usuário para
    # qualquer pasta, e a limpeza roda como administrador.
    local = Path(winsec.local_appdata())
    temp = local / "Temp"
    return [
        CleanTarget(
            "user_temp", "Arquivos temporários do usuário",
            "Pasta Temp do usuário. Arquivos com menos de 24 h são preservados.",
            lambda: _existing(temp), min_age_hours=24),
        CleanTarget(
            "windows_temp", "Arquivos temporários do Windows",
            "C:\\Windows\\Temp. Arquivos com menos de 24 h são preservados.",
            lambda: _existing(_windir() / "Temp"), min_age_hours=24, requires_admin=True),
        CleanTarget(
            "thumbnails", "Cache de miniaturas",
            "Miniaturas de imagens/vídeos do Explorador. São recriadas automaticamente.",
            lambda: _existing(local / "Microsoft" / "Windows" / "Explorer",
                              pattern="thumbcache_*.db", recursive=False)),
        CleanTarget(
            "browser_cache", "Cache dos navegadores",
            "Somente cache (Chrome, Edge, Brave, Opera, Vivaldi, Firefox). Senhas, cookies e histórico NÃO "
            "são tocados. Feche os navegadores para liberar mais espaço.",
            _browser_locations),
        CleanTarget(
            "error_reports", "Relatórios de erro do Windows",
            "Relatórios de falhas já enviados/arquivados (WER).",
            lambda: _existing(
                local / "Microsoft" / "Windows" / "WER",
                _program_data() / "Microsoft" / "Windows" / "WER" / "ReportArchive",
                _program_data() / "Microsoft" / "Windows" / "WER" / "ReportQueue")),
        CleanTarget(
            "app_crash_dumps", "Despejos de memória de aplicativos",
            "Arquivos .dmp gerados quando programas travam (%LOCALAPPDATA%\\CrashDumps).",
            lambda: _existing(local / "CrashDumps")),
        CleanTarget(
            "delivery_logs", "Logs antigos do Windows",
            "Arquivos .log/.etl com mais de 30 dias em C:\\Windows\\Logs e Panther.",
            lambda: _existing(_windir() / "Logs", pattern="*.log") + _existing(_windir() / "Logs", pattern="*.etl")
            + _existing(_windir() / "Panther", pattern="*.log"),
            min_age_hours=24 * 30, requires_admin=True),
        CleanTarget(
            "shader_cache", "Cache de shaders (DirectX/NVIDIA/AMD)",
            "Recriado automaticamente; jogos podem engasgar levemente na primeira execução após limpar.",
            lambda: _existing(local / "D3DSCache", local / "NVIDIA" / "DXCache", local / "NVIDIA" / "GLCache",
                              local / "AMD" / "DxCache", local / "AMD" / "GLCache"),
            default_selected=False),
        CleanTarget(
            "windows_update", "Cache de downloads do Windows Update",
            "Instaladores de atualizações já baixados (mais de 3 dias). O Windows baixa novamente se precisar.",
            lambda: _existing(_windir() / "SoftwareDistribution" / "Download"),
            min_age_hours=72, requires_admin=True, default_selected=False),
        CleanTarget(
            "system_dumps", "Despejos de memória do sistema (tela azul)",
            "Minidumps de telas azuis. Mantenha se estiver investigando travamentos.",
            lambda: _existing(_windir() / "Minidump") + [
                Location(_windir(), "MEMORY.DMP", recursive=False)] if _windir().is_dir() else [],
            requires_admin=True, default_selected=False),
        CleanTarget(
            "recycle_bin", "Lixeira",
            "Esvazia a Lixeira de todas as unidades. Os arquivos não poderão ser recuperados.",
            default_selected=False, scan_override=_recycle_bin_scan, clean_override=_recycle_bin_clean),
    ]
