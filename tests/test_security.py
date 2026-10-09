"""Ataques de um processo comum contra o DiagCompleto elevado."""
import os
import subprocess
import winreg
from pathlib import Path

import pytest

from diagcompleto import cleaner, tweaks, utils, winsec
from diagcompleto.tweaks import BackupStore, RegistryTweak, RegValue

TEST_KEY = r"Software\DiagCompletoTest"
OTHER_KEY = r"Software\DiagCompletoTestAlvo"


def junction(link: Path, target: Path) -> None:
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    if result.returncode != 0:
        pytest.skip("Não foi possível criar junção neste ambiente")


@pytest.fixture(autouse=True)
def clean_registry():
    yield
    for key in (TEST_KEY, OTHER_KEY):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------- limpeza

def test_delete_refuses_path_that_escapes_root_through_swapped_junction(tmp_path: Path):
    # Simula a troca de uma subpasta por junção entre a varredura e a exclusão.
    root = tmp_path / "cache"
    root.mkdir()
    outside = tmp_path / "documentos"
    outside.mkdir()
    precious = outside / "importante.docx"
    precious.write_bytes(b"x")
    junction(root / "sub", outside)

    assert winsec.delete_within(root / "sub" / "importante.docx", winsec.norm(root)) is False
    assert precious.exists()


def test_delete_inside_root_works_and_keeps_root(tmp_path: Path):
    root = tmp_path / "cache"
    (root / "sub").mkdir(parents=True)
    victim = root / "sub" / "a.tmp"
    victim.write_bytes(b"x")
    os.chmod(victim, 0o444)  # somente leitura também deve ser apagado
    assert winsec.delete_within(victim, winsec.norm(root)) is True
    assert not victim.exists()
    assert winsec.delete_within(root / "sub", winsec.norm(root), is_dir=True) is True
    assert root.is_dir()
    assert winsec.delete_within(root, winsec.norm(root), is_dir=True) is False  # a raiz nunca


def test_root_reached_through_junction_is_refused(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.tmp").write_bytes(b"x")
    junction(tmp_path / "link", real)
    assert not cleaner.is_safe_root(tmp_path / "link")
    target = cleaner.CleanTarget("t", "t", "", lambda: [cleaner.Location(tmp_path / "link")])
    cleaner.clean_target(target)
    assert (real / "a.tmp").exists()


def test_temp_variable_cannot_redirect_cleaning(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    roots = {winsec.norm(loc.root) for t in cleaner.build_targets() for loc in t.locations()}
    assert not any(winsec.is_within(r, tmp_path) for r in roots)
    assert winsec.norm(Path(winsec.local_appdata()) / "Temp") in roots


def test_roots_outside_allowed_trees_are_refused(tmp_path: Path):
    assert not cleaner.is_safe_root(Path(winsec.windows_dir()) / "System32")
    assert not cleaner.is_safe_root(Path(winsec.profile_dir()) / "Documents")
    if os.path.isdir(r"C:\Program Files\Common Files"):
        assert not cleaner.is_safe_root(Path(r"C:\Program Files\Common Files"))


# --------------------------------------------------------------------------- otimizações

def make_tweak():
    return RegistryTweak("teste", "Teste", "", "Teste", values=(RegValue("HKCU", TEST_KEY, "Valor", 1),))


def test_tampered_backup_cannot_write_other_registry_keys(tmp_path: Path):
    store = BackupStore(tmp_path / "backup.json")
    store.save_if_absent("teste", [
        {"hive": "HKCU", "key": OTHER_KEY, "name": "Debugger", "existed": True, "value": "malware.exe",
         "kind": winreg.REG_SZ}])
    with pytest.raises(tweaks.TweakError):
        make_tweak().revert(store)
    assert tweaks.read_value("HKCU", OTHER_KEY, "Debugger")[0] is False


@pytest.mark.parametrize("item", [
    {"hive": "HKCU", "key": TEST_KEY, "name": "Valor", "existed": True, "value": "texto", "kind": winreg.REG_DWORD},
    {"hive": "HKCU", "key": TEST_KEY, "name": "Valor", "existed": True, "value": 1, "kind": 999},
    {"hive": "HKCU", "key": TEST_KEY, "name": "Valor", "existed": "sim"},
    "lixo",
])
def test_malformed_backup_is_rejected(tmp_path: Path, item):
    store = BackupStore(tmp_path / "backup.json")
    store.save_if_absent("teste", [item])
    with pytest.raises(tweaks.TweakError):
        make_tweak().revert(store)


def test_backup_from_fallback_is_used_but_never_written(tmp_path: Path):
    fallback = tmp_path / "usuario.json"
    BackupStore(fallback).save_if_absent("teste", [
        {"hive": "HKCU", "key": TEST_KEY, "name": "Valor", "existed": False, "value": None, "kind": None}])
    before = fallback.read_bytes()
    store = BackupStore(tmp_path / "admin.json", fallback=fallback)
    make_tweak().apply(BackupStore(tmp_path / "outro.json"))
    make_tweak().revert(store)
    assert tweaks.read_value("HKCU", TEST_KEY, "Valor")[0] is False
    assert fallback.read_bytes() == before


# --------------------------------------------------------------------------- processos filhos

def test_child_processes_get_system_only_environment(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PSModulePath", str(tmp_path))
    env = winsec.safe_env()
    for name in ("PATH", "PSModulePath"):
        assert all(winsec.is_within(p, winsec.windows_dir()) for p in env[name].split(";"))
    assert os.path.isabs(utils.powershell_args("1")[0])


def test_powershell_ignores_user_module_path(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("PSModulePath", str(tmp_path))
    output = utils.run_powershell("Write-Output $env:PSModulePath").strip()
    assert output and all(winsec.is_within(p, winsec.windows_dir()) for p in output.split(";"))


def test_user_created_folder_is_not_trusted_as_locked(tmp_path: Path):
    assert winsec._is_locked(str(tmp_path)) is False


def test_locked_folder_acl_blocks_owner_and_detects_weak_acls(monkeypatch, tmp_path: Path):
    me = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
                        text=True).stdout.strip().split(",")[1].strip('"')
    monkeypatch.setattr(winsec, "_TRUSTED_OWNERS", winsec._TRUSTED_OWNERS | {me})
    # Mesmo DACL da pasta real; sem "O:BA", pois o teste não roda elevado.
    monkeypatch.setattr(winsec, "_LOCKED_SDDL", winsec._LOCKED_SDDL.replace("O:BA", ""))
    locked = tmp_path / "locked"
    winsec._create_locked_dir(str(locked))
    assert winsec._is_locked(str(locked))
    grant = subprocess.run(["icacls", str(locked), "/grant", f"*{me}:(M)"], capture_output=True)
    assert grant.returncode != 0  # OWNER RIGHTS: o dono não altera as permissões

    monkeypatch.setattr(winsec, "_LOCKED_SDDL", f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1301bf;;;{me})")
    weak = tmp_path / "weak"
    winsec._create_locked_dir(str(weak))
    assert not winsec._is_locked(str(weak))
