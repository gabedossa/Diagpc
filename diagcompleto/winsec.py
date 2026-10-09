"""Primitivas de segurança para rodar como administrador sem confiar em nada que um
processo comum do usuário consiga alterar.

Ameaça considerada: um programa malicioso rodando como o usuário (sem elevação) tenta
usar o DiagCompleto, quando elevado, para agir em seu nome. Por isso:
- Pastas do sistema vêm das APIs do Windows, nunca de variáveis de ambiente.
- Processos filhos recebem um ambiente limpo (PATH e PSModulePath só do sistema).
- Arquivos só são apagados depois de abertos e de o caminho real do handle ser conferido,
  o que neutraliza a troca de pastas por junções durante a limpeza.
- Quando elevado, dados do programa ficam numa pasta que só administradores alteram.
"""
from __future__ import annotations

import ctypes
import logging
import os
import time
import uuid
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("DiagCompleto.winsec")

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_userenv = ctypes.WinDLL("userenv", use_last_error=True)
_ole32 = ctypes.WinDLL("ole32")

FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

_kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
_kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
_kernel32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
_kernel32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
_kernel32.GetSystemWindowsDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
_kernel32.GetSystemWindowsDirectoryW.restype = wintypes.UINT
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
_kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, wintypes.LPVOID]
_advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_userenv.GetUserProfileDirectoryW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_shell32.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE,
                                          ctypes.POINTER(ctypes.c_void_p)]
_shell32.SHGetKnownFolderPath.restype = ctypes.c_long
_ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
_advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), wintypes.LPVOID]
_advapi32.GetNamedSecurityInfoW.argtypes = [
    wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p)]
_advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
_advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
_advapi32.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD),
                                                   ctypes.POINTER(wintypes.DWORD)]
_advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]


# --------------------------------------------------------------------------- caminhos

def norm(path: str | os.PathLike) -> str:
    """Caminho absoluto, em minúsculas e sem barra final (exceto na raiz do disco)."""
    text = os.path.normcase(os.path.abspath(os.fspath(path)))
    return text.rstrip("\\") if len(text) > 3 else text


def is_within(child: str | os.PathLike, parent: str | os.PathLike, strict: bool = False) -> bool:
    c, p = norm(child), norm(parent)
    if c == p:
        return not strict
    return c.startswith(p if p.endswith("\\") else p + "\\")


def real_dir(path: str | os.PathLike) -> str | None:
    """Caminho real da pasta, ou None se algum trecho dele for junção/link (ou se não existir)."""
    if not os.path.isdir(path):
        return None
    real = os.path.realpath(path)
    return norm(real) if norm(real) == norm(path) else None


def _known_folder(folder_id: str) -> str | None:
    guid = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(folder_id).bytes_le)
    out = ctypes.c_void_p()
    hr = _shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0x4000, None, ctypes.byref(out))  # KF_FLAG_DONT_VERIFY
    try:
        return ctypes.wstring_at(out.value) if hr == 0 and out.value else None
    finally:
        _ole32.CoTaskMemFree(out)


@lru_cache(maxsize=None)
def windows_dir() -> str:
    buf = ctypes.create_unicode_buffer(32768)
    if not _kernel32.GetSystemWindowsDirectoryW(buf, len(buf)):
        raise ctypes.WinError(ctypes.get_last_error())
    return buf.value


def system_dir() -> str:
    return os.path.join(windows_dir(), "System32")


@lru_cache(maxsize=None)
def profile_dir() -> str:
    """Perfil do usuário segundo o token do processo (vem do HKLM, o usuário não altera)."""
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if not _userenv.GetUserProfileDirectoryW(token, buf, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buf.value
    finally:
        _kernel32.CloseHandle(token)


def _profile_folder(folder_id: str, *default: str) -> str:
    # Pastas do perfil podem ser redirecionadas pelo próprio usuário (HKCU); só aceitamos
    # o redirecionamento se continuar dentro do perfil.
    found = _known_folder(folder_id)
    if found and is_within(found, profile_dir(), strict=True):
        return found
    return os.path.join(profile_dir(), *default)


@lru_cache(maxsize=None)
def local_appdata() -> str:
    return _profile_folder("F1B32785-6FBA-4FCF-9D55-7B8E7F157091", "AppData", "Local")


@lru_cache(maxsize=None)
def roaming_appdata() -> str:
    return _profile_folder("3EB685DB-65F9-4CF6-A03A-E3EF65729F3D", "AppData", "Roaming")


@lru_cache(maxsize=None)
def program_data() -> str:
    return _known_folder("62AB5D82-FDC1-4DC3-A9DD-070D1D495D97") or os.path.join(
        os.path.splitdrive(windows_dir())[0] + "\\", "ProgramData")


@lru_cache(maxsize=None)
def protected_dirs() -> frozenset[str]:
    """Pastas que nunca podem ser raiz de uma limpeza (obtidas das APIs do Windows)."""
    win = windows_dir()
    paths = {os.path.splitdrive(win)[0] + "\\", win, system_dir(), os.path.join(win, "SysWOW64"),
             profile_dir(), os.path.join(profile_dir(), "AppData"), local_appdata(), roaming_appdata(),
             program_data()}
    for folder_id in ("905E63B6-C1BF-494E-B29C-65B732D3D21A",   # Program Files
                      "7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E",   # Program Files (x86)
                      "DFDF76A2-C82A-4D63-906A-5644AC457385",   # Public
                      "FDD39AD0-238F-46AF-ADB4-6C85480369C7",   # Documentos
                      "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",   # Área de trabalho
                      "374DE290-123F-4565-9164-39C4925E467B"):  # Downloads
        found = _known_folder(folder_id)
        if found:
            paths.add(found)
    return frozenset(norm(p) for p in paths)


def allowed_bases() -> list[str]:
    """Únicas árvores onde a limpeza pode agir."""
    return [norm(profile_dir()), norm(windows_dir()), norm(program_data())]


def safe_env() -> dict[str, str]:
    """Ambiente para processos filhos: nada de PATH/módulos vindos de pastas do usuário."""
    win, sys32 = windows_dir(), system_dir()
    env = {k: v for k, v in os.environ.items() if k.upper() not in ("PATH", "PSMODULEPATH", "PATHEXT",
                                                                    "PSEXECUTIONPOLICYPREFERENCE", "COMSPEC")}
    env.update({
        "SystemRoot": win, "windir": win, "ComSpec": os.path.join(sys32, "cmd.exe"),
        "PATH": ";".join([sys32, win, os.path.join(sys32, "Wbem"),
                          os.path.join(sys32, "WindowsPowerShell", "v1.0")]),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "PSModulePath": os.path.join(sys32, "WindowsPowerShell", "v1.0", "Modules"),
    })
    return env


# --------------------------------------------------------------------------- exclusão segura

class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [("dwFileAttributes", wintypes.DWORD), ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME), ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD), ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD), ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD), ("nFileIndexLow", wintypes.DWORD)]


def final_path(handle: int) -> str | None:
    buf = ctypes.create_unicode_buffer(32768)
    n = _kernel32.GetFinalPathNameByHandleW(handle, buf, len(buf), 0)
    if not n or n >= len(buf):
        return None
    path = buf.value
    if path.startswith("\\\\?\\UNC\\"):
        return None  # rede: nunca é alvo legítimo
    return path[4:] if path.startswith("\\\\?\\") else path


def _mark_for_deletion(handle: int) -> bool:
    # FileDispositionInfoEx: DELETE | POSIX_SEMANTICS | IGNORE_READONLY_ATTRIBUTE
    flags = wintypes.DWORD(0x1 | 0x2 | 0x10)
    if _kernel32.SetFileInformationByHandle(handle, 21, ctypes.byref(flags), ctypes.sizeof(flags)):
        return True
    legacy = wintypes.BOOLEAN(True)  # Windows antigo: FileDispositionInfo
    return bool(_kernel32.SetFileInformationByHandle(handle, 4, ctypes.byref(legacy), ctypes.sizeof(legacy)))


def delete_within(path: str | os.PathLike, root: str, is_dir: bool = False) -> bool:
    """Apaga path somente se o caminho real (após abrir) estiver estritamente dentro de root.

    Abrir primeiro e conferir o handle elimina a corrida em que alguém troca uma pasta
    intermediária por uma junção entre a varredura e a exclusão.
    """
    flags = 0x00200000 | 0x02000000  # OPEN_REPARSE_POINT | BACKUP_SEMANTICS
    access = 0x00010000 | 0x80 | 0x00100000  # DELETE | FILE_READ_ATTRIBUTES | SYNCHRONIZE
    handle = _kernel32.CreateFileW(os.fspath(path), access, 0x7, None, 3, flags, None)  # OPEN_EXISTING
    if handle == INVALID_HANDLE_VALUE or handle is None:
        return ctypes.get_last_error() in (2, 3)  # já não existe
    try:
        info = _ByHandleFileInformation()
        if not _kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            return False
        attrs = info.dwFileAttributes
        if attrs & FILE_ATTRIBUTE_REPARSE_POINT or bool(attrs & FILE_ATTRIBUTE_DIRECTORY) != is_dir:
            return False
        real = final_path(handle)
        if real is None or not is_within(real, root, strict=True):
            log.warning("Exclusão recusada: %s aponta para fora de %s (%s)", path, root, real)
            return False
        return _mark_for_deletion(handle)
    finally:
        _kernel32.CloseHandle(handle)


# --------------------------------------------------------------------------- pasta protegida (modo administrador)

# Dono: Administradores. Sem herança. SYSTEM e Administradores: controle total.
# Usuários: leitura (para o navegador abrir os relatórios). OWNER RIGHTS: só leitura,
# para que o dono de um arquivo não ganhe direito implícito de alterar permissões.
_LOCKED_SDDL = "O:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)(A;OICI;0x1200a9;;;OW)"
_TRUSTED_OWNERS = {"S-1-5-32-544", "S-1-5-18"}
_TRUSTED_WRITERS = {"S-1-5-32-544", "S-1-5-18"}
_WRITE_RIGHTS = 0x2 | 0x4 | 0x10 | 0x40 | 0x100 | 0x10000 | 0x40000 | 0x80000 | 0x40000000 | 0x10000000


def _sid_string(psid: int) -> str | None:
    out = ctypes.c_void_p()
    if not _advapi32.ConvertSidToStringSidW(psid, ctypes.byref(out)):
        return None
    try:
        return ctypes.wstring_at(out.value)
    finally:
        _kernel32.LocalFree(out)


def _is_locked(path: str) -> bool:
    """Confere dono e DACL: só administradores/SYSTEM podem escrever, sem herança."""
    owner, dacl, sd = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    # SE_FILE_OBJECT; OWNER | DACL
    if _advapi32.GetNamedSecurityInfoW(path, 1, 0x1 | 0x4, ctypes.byref(owner), None,
                                       ctypes.byref(dacl), None, ctypes.byref(sd)) != 0:
        return False
    try:
        if _sid_string(owner.value) not in _TRUSTED_OWNERS or not dacl.value:
            return False
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not _advapi32.GetSecurityDescriptorControl(sd, ctypes.byref(control), ctypes.byref(revision)):
            return False
        if not control.value & 0x1000:  # SE_DACL_PROTECTED
            return False
        ace_count = ctypes.cast(dacl, ctypes.POINTER(wintypes.WORD))[2]  # ACL.AceCount
        for index in range(ace_count):
            ace = ctypes.c_void_p()
            if not _advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                return False
            ace_type = ctypes.cast(ace, ctypes.POINTER(ctypes.c_ubyte))[0]
            if ace_type == 1:  # ACCESS_DENIED: só restringe
                continue
            if ace_type != 0:  # tipo inesperado
                return False
            mask = ctypes.cast(ace.value + 4, ctypes.POINTER(wintypes.DWORD))[0]
            sid = _sid_string(ace.value + 8)
            if mask & _WRITE_RIGHTS and sid not in _TRUSTED_WRITERS:
                return False
        return True
    finally:
        _kernel32.LocalFree(sd)


def _create_locked_dir(path: str) -> None:
    sd = ctypes.c_void_p()
    if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(_LOCKED_SDDL, 1, ctypes.byref(sd), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        class SecurityAttributes(ctypes.Structure):
            _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                        ("bInheritHandle", wintypes.BOOL)]
        attrs = SecurityAttributes(ctypes.sizeof(SecurityAttributes), sd, False)
        # A pasta já nasce protegida: não há janela entre criar e aplicar as permissões.
        if not _kernel32.CreateDirectoryW(path, ctypes.byref(attrs)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _kernel32.LocalFree(sd)


def _is_plain_dir(path: str) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(st.st_file_attributes & FILE_ATTRIBUTE_DIRECTORY) and not (
        st.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def locked_admin_dir(name: str) -> Path:
    """%ProgramData%\\<name>, que só administradores podem alterar.

    Se a pasta existir mas não estiver protegida (por exemplo, criada antes por um
    processo comum para plantar links), ela é renomeada e recriada do zero.
    """
    path = os.path.join(program_data(), name)
    for _ in range(3):
        if _is_plain_dir(path) and _is_locked(path):
            return Path(path)
        if os.path.lexists(path):
            quarantine = f"{path}.suspeito-{time.strftime('%Y%m%d%H%M%S')}"
            log.warning("Pasta de dados sem proteção; movida para %s", quarantine)
            os.rename(path, quarantine)
        try:
            _create_locked_dir(path)
        except FileExistsError:
            continue
    if _is_plain_dir(path) and _is_locked(path):
        return Path(path)
    raise PermissionError(f"Não foi possível criar uma pasta protegida em {path}")
