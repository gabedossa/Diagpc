import os
import time
from pathlib import Path

import pytest

from diagcompleto import cleaner
from diagcompleto.cleaner import CleanTarget, Location


def make_file(path: Path, size: int = 10, age_hours: float = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if age_hours:
        old = time.time() - age_hours * 3600
        os.utime(path, (old, old))
    return path


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    root = tmp_path / "app" / "cache"
    root.mkdir(parents=True)
    return root


def target_for(root: Path, pattern: str = "*", recursive: bool = True, **kwargs) -> CleanTarget:
    return CleanTarget("t", "teste", "", lambda: [Location(root, pattern, recursive)], **kwargs)


def test_scan_and_clean_removes_content_but_keeps_root(cache: Path):
    make_file(cache / "a.tmp", 100)
    make_file(cache / "sub" / "deep" / "b.tmp", 50)
    target = target_for(cache)

    scan = cleaner.scan_target(target)
    assert (scan.size, scan.files) == (150, 2)

    result = cleaner.clean_target(target)
    assert (result.freed, result.deleted) == (150, 2)
    assert cache.is_dir()
    assert list(cache.iterdir()) == []


def test_recent_files_are_preserved(cache: Path):
    # A data de criação de arquivos novos é "agora", então mesmo com mtime antigo
    # eles devem ser preservados (proteção para instaladores em andamento).
    make_file(cache / "novo.tmp", age_hours=48)
    target = target_for(cache, min_age_hours=24)
    assert cleaner.scan_target(target).files == 0
    cleaner.clean_target(target)
    assert (cache / "novo.tmp").exists()


def test_pattern_and_non_recursive(cache: Path):
    make_file(cache / "thumbcache_1.db")
    make_file(cache / "outro.db")
    make_file(cache / "sub" / "thumbcache_2.db")
    target = target_for(cache, pattern="thumbcache_*.db", recursive=False)
    cleaner.clean_target(target)
    assert not (cache / "thumbcache_1.db").exists()
    assert (cache / "outro.db").exists()
    assert (cache / "sub" / "thumbcache_2.db").exists()


def test_junctions_are_never_followed(cache: Path, tmp_path: Path):
    precious = make_file(tmp_path / "documentos" / "importante.docx")
    link = cache / "link"
    try:
        os.symlink(precious.parent, link, target_is_directory=True)
    except OSError:
        import subprocess
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(precious.parent)],
                                capture_output=True)
        if result.returncode != 0:
            pytest.skip("Não foi possível criar link/junção neste ambiente")
    cleaner.clean_target(target_for(cache))
    assert precious.exists()


def test_dangerous_roots_are_refused():
    assert not cleaner.is_safe_root(Path("C:\\"))
    assert not cleaner.is_safe_root(Path(os.environ["SystemRoot"]))
    assert not cleaner.is_safe_root(Path(os.environ["USERPROFILE"]))
    assert not cleaner.is_safe_root(Path(os.environ["LOCALAPPDATA"]))
    assert cleaner.is_safe_root(Path(os.environ["LOCALAPPDATA"]) / "CrashDumps")


def test_unsafe_root_is_not_scanned(monkeypatch, cache: Path):
    make_file(cache / "a.tmp")
    monkeypatch.setattr(cleaner, "is_safe_root", lambda root: False)
    assert cleaner.scan_target(target_for(cache)).files == 0


def test_all_builtin_targets_resolve_to_safe_locations():
    for target in cleaner.build_targets():
        for location in target.locations():
            assert location.single_file or cleaner.is_safe_root(location.root), (target.id, location.root)
