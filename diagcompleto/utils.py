"""Utilitários compartilhados: privilégios, execução de comandos, formatação e logging."""
from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from . import winsec

APP_NAME = "DiagCompleto"
CREATE_NO_WINDOW = 0x08000000

log = logging.getLogger(APP_NAME)


class CommandError(RuntimeError):
    """Falha ao executar um comando externo."""


def is_windows() -> bool:
    return sys.platform == "win32"


def user_data_dir() -> Path:
    """Pasta de dados do usuário (%LOCALAPPDATA%\\DiagCompleto), obtida pela API do Windows."""
    path = Path(winsec.local_appdata()) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def app_data_dir() -> Path:
    """Pasta de dados do programa.

    Elevado, o programa não escreve em pastas que um processo comum consegue manipular
    (uma junção ali levaria a escrita de administrador para qualquer lugar). Usa então
    %ProgramData%\\DiagCompleto, com permissões que só administradores alteram.
    """
    if is_admin():
        return winsec.locked_admin_dir(APP_NAME)
    return user_data_dir()


def setup_logging() -> Path:
    log_dir = app_data_dir() / "logs"
    log_dir.mkdir(exist_ok=True)
    if not winsec.real_dir(log_dir):
        raise PermissionError(f"Pasta de logs inválida (junção ou link): {log_dir}")
    log_file = log_dir / "diagcompleto.log"
    logger = logging.getLogger(APP_NAME)
    if not logger.handlers:
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return log_file


# --------------------------------------------------------------------------- privilégios

def is_admin() -> bool:
    if not is_windows():
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def relaunch_as_admin() -> bool:
    """Reabre o programa pedindo elevação (UAC). Retorna True se o novo processo foi iniciado."""
    if getattr(sys, "frozen", False):
        params = subprocess.list2cmdline(sys.argv[1:])
    else:
        params = subprocess.list2cmdline([os.path.abspath(sys.argv[0]), *sys.argv[1:]])
    # Pasta de trabalho no System32: o processo elevado não procura executáveis e DLLs
    # na pasta do usuário de onde foi chamado.
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, winsec.system_dir(), 1)
    return result > 32


def open_file(path: Path) -> None:
    """Abre um arquivo no programa padrão. Elevado, delega ao Explorador para que o
    navegador não rode como administrador."""
    if is_admin():
        subprocess.Popen([os.path.join(winsec.windows_dir(), "explorer.exe"), str(path)],
                         env=winsec.safe_env(), cwd=winsec.system_dir())
    else:
        os.startfile(path)


# --------------------------------------------------------------------------- comandos

def _oem_codepage() -> str:
    try:
        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except (AttributeError, OSError):
        return "utf-8"


def decode_output(data: bytes) -> str:
    """Decodifica saída de ferramentas do Windows (sfc usa UTF-16, DISM usa a página OEM)."""
    if not data:
        return ""
    if b"\x00" in data:
        try:
            return data.decode("utf-16-le", errors="replace")
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(_oem_codepage(), errors="replace")


def _decode_line(line: bytes) -> str:
    # Linhas de saída UTF-16 quebradas em \n ficam desalinhadas; remover os
    # bytes nulos e ler como latin-1 recupera corretamente o texto em português.
    if b"\x00" in line:
        return line.replace(b"\x00", b"").decode("latin-1", errors="replace")
    return decode_output(line)


def system_tool(name: str) -> str:
    """Caminho de uma ferramenta do System32, contornando o redirecionamento WOW64.

    A pasta do Windows vem da API, não de %SystemRoot% (que o usuário pode alterar).
    """
    root = winsec.windows_dir()
    sysnative = os.path.join(root, "Sysnative")
    folder = sysnative if os.path.isdir(sysnative) else os.path.join(root, "System32")
    return os.path.join(folder, name)


def run_command(args: list[str], timeout: int = 300) -> tuple[int, str]:
    completed = subprocess.run(
        args,
        capture_output=True,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW if is_windows() else 0,
        env=winsec.safe_env(),
        cwd=winsec.system_dir(),
    )
    return completed.returncode, decode_output(completed.stdout) + decode_output(completed.stderr)


def run_command_stream(args: list[str], on_line: Callable[[str], None]) -> int:
    """Executa um comando enviando cada linha (inclusive progresso com \\r) para on_line."""
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=CREATE_NO_WINDOW if is_windows() else 0,
        env=winsec.safe_env(),
        cwd=winsec.system_dir(),
    )
    assert proc.stdout is not None
    buffer = b""
    last = None
    while True:
        chunk = proc.stdout.read1(4096)
        if not chunk:
            break
        buffer += chunk
        parts = re.split(rb"[\r\n]+", buffer)
        buffer = parts.pop()
        for part in parts:
            text = _decode_line(part).strip()
            if text and text != last:
                on_line(text)
                last = text
    if buffer.strip():
        on_line(_decode_line(buffer).strip())
    return proc.wait()


def _clean_clixml(text: str) -> str:
    """Extrai o texto legível de erros que o PowerShell envia em formato CLIXML."""
    if "#< CLIXML" not in text:
        return text.strip()
    parts = re.findall(r"<S S=""Error"">(.*?)</S>", text, flags=re.S)
    return "".join(parts).replace("_x000D__x000A_", "\n").strip()


# Fixa os módulos nas pastas do sistema antes de qualquer cmdlet: o PowerShell carrega
# módulos automaticamente e a pasta Documentos do usuário vem primeiro; um módulo plantado
# ali com o nome de um cmdlet (ex.: Get-PhysicalDisk) rodaria como administrador.
_PS_PREFIX = (
    r"$env:PSModulePath=[IO.Path]::Combine([Environment]::SystemDirectory,'WindowsPowerShell\v1.0\Modules');"
    "$ProgressPreference='SilentlyContinue';[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
)


def powershell_args(script: str) -> list[str]:
    """Linha de comando do PowerShell do sistema, sem perfil nem módulos do usuário."""
    encoded = base64.b64encode((_PS_PREFIX + script).encode("utf-16-le")).decode("ascii")
    return [system_tool(r"WindowsPowerShell\v1.0\powershell.exe"), "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]


def run_powershell(script: str, timeout: int = 180) -> str:
    completed = subprocess.run(
        powershell_args(script),
        capture_output=True,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW if is_windows() else 0,
        env=winsec.safe_env(),
        cwd=winsec.system_dir(),
    )
    stdout = completed.stdout.decode("utf-8", errors="replace")
    if completed.returncode != 0:
        stderr = _clean_clixml(decode_output(completed.stderr))
        raise CommandError(stderr or f"PowerShell terminou com código {completed.returncode}")
    return stdout


def run_powershell_json(script: str, timeout: int = 180) -> Any:
    output = run_powershell(f"{script.strip()} | ConvertTo-Json -Depth 6 -Compress", timeout=timeout).strip()
    if not output:
        return None
    return json.loads(output)


def as_list(value: Any) -> list:
    """ConvertTo-Json devolve objeto único em vez de lista; normaliza para lista."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


# --------------------------------------------------------------------------- formatação

def format_bytes(size: float | int | None) -> str:
    if size is None:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}".replace(".", ",")
        value /= 1024
    return f"{value:.1f} TB"
