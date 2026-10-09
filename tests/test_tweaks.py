import winreg

import pytest

from diagcompleto import tweaks
from diagcompleto.tweaks import BackupStore, RegistryTweak, RegValue

TEST_KEY = r"Software\DiagCompletoTest"


@pytest.fixture
def store(tmp_path):
    return BackupStore(tmp_path / "backup.json")


@pytest.fixture(autouse=True)
def clean_registry():
    yield
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, TEST_KEY)
    except FileNotFoundError:
        pass


def make_tweak():
    return RegistryTweak("teste", "Teste", "", "Teste", values=(
        RegValue("HKCU", TEST_KEY, "Novo", 1),
        RegValue("HKCU", TEST_KEY, "Existente", "depois", winreg.REG_SZ),
    ))


def test_apply_and_revert_restore_exact_original_state(store):
    tweaks.write_value("HKCU", TEST_KEY, "Existente", "antes", winreg.REG_SZ)
    tweak = make_tweak()
    assert tweak.is_applied() is False

    tweak.apply(store)
    assert tweak.is_applied() is True
    assert tweaks.read_value("HKCU", TEST_KEY, "Existente")[1] == "depois"

    tweak.revert(store)
    assert tweaks.read_value("HKCU", TEST_KEY, "Existente")[1:] == ("antes", winreg.REG_SZ)
    assert tweaks.read_value("HKCU", TEST_KEY, "Novo")[0] is False  # não existia: foi removido
    assert store.get("teste") is None


def test_reapplying_keeps_first_backup(store):
    tweaks.write_value("HKCU", TEST_KEY, "Existente", "original", winreg.REG_SZ)
    tweak = make_tweak()
    tweak.apply(store)
    tweak.apply(store)  # o segundo backup veria o valor já alterado; deve ser ignorado
    tweak.revert(store)
    assert tweaks.read_value("HKCU", TEST_KEY, "Existente")[1] == "original"


def test_revert_without_backup_fails(store):
    with pytest.raises(tweaks.TweakError):
        make_tweak().revert(store)


def test_binary_values_survive_backup(store):
    tweaks.write_value("HKCU", TEST_KEY, "Novo", b"\x01\x02", winreg.REG_BINARY)
    tweak = make_tweak()
    tweak.apply(store)
    tweak.revert(store)
    assert tweaks.read_value("HKCU", TEST_KEY, "Novo")[1:] == (b"\x01\x02", winreg.REG_BINARY)


def test_catalog_ids_are_unique_and_documented():
    catalog = tweaks.build_tweaks()
    assert len({t.id for t in catalog}) == len(catalog)
    for tweak in catalog:
        assert tweak.name and tweak.description
        if isinstance(tweak, RegistryTweak):
            assert tweak.values
