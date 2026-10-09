"""Ajustes (tweaks) do Windows — todos reversíveis.

Antes de aplicar qualquer ajuste, o valor original é salvo em
%LOCALAPPDATA%\\DiagCompleto\\tweaks_backup.json. "Reverter" restaura exatamente
o estado anterior (inclusive apagando valores que não existiam).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import winreg
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .utils import app_data_dir, is_admin, run_command, system_tool

log = logging.getLogger("DiagCompleto.tweaks")

HIVES = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}
HIGH_PERFORMANCE_PLAN = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

RISK_LOW = "Baixo"
RISK_MEDIUM = "Médio"


class TweakError(RuntimeError):
    pass


# --------------------------------------------------------------------------- backup

class BackupStore:
    """Guarda o estado original de cada ajuste. Nunca sobrescreve um backup existente,
    para que reaplicar um ajuste não apague a referência do estado original."""

    def __init__(self, path: Path | None = None):
        self.path = path or app_data_dir() / "tweaks_backup.json"
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.exception("Backup de ajustes ilegível: %s", self.path)
            raise TweakError("O arquivo de backup dos ajustes está corrompido; nada foi alterado.")

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, tweak_id: str) -> Any:
        with self._lock:
            return self._load().get(tweak_id)

    def save_if_absent(self, tweak_id: str, value: Any) -> None:
        with self._lock:
            data = self._load()
            if tweak_id not in data:
                data[tweak_id] = value
                self._write(data)

    def remove(self, tweak_id: str) -> None:
        with self._lock:
            data = self._load()
            if data.pop(tweak_id, None) is not None:
                self._write(data)


# --------------------------------------------------------------------------- registro

@dataclass(frozen=True)
class RegValue:
    hive: str
    key: str
    name: str
    value: int | str
    kind: int = winreg.REG_DWORD


def _access(hive: str, write: bool) -> int:
    access = winreg.KEY_WOW64_64KEY
    access |= winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE if write else winreg.KEY_READ
    return access


def read_value(hive: str, key: str, name: str) -> tuple[bool, Any, int | None]:
    """Retorna (existe, valor, tipo)."""
    try:
        with winreg.OpenKey(HIVES[hive], key, 0, _access(hive, False)) as handle:
            value, kind = winreg.QueryValueEx(handle, name)
            return True, value, kind
    except FileNotFoundError:
        return False, None, None


def key_exists(hive: str, key: str) -> bool:
    try:
        winreg.CloseKey(winreg.OpenKey(HIVES[hive], key, 0, _access(hive, False)))
        return True
    except FileNotFoundError:
        return False


def write_value(hive: str, key: str, name: str, value: Any, kind: int) -> None:
    with winreg.CreateKeyEx(HIVES[hive], key, 0, _access(hive, True)) as handle:
        winreg.SetValueEx(handle, name, 0, kind, value)


def delete_value(hive: str, key: str, name: str) -> None:
    try:
        with winreg.OpenKey(HIVES[hive], key, 0, _access(hive, True)) as handle:
            winreg.DeleteValue(handle, name)
    except FileNotFoundError:
        pass


def _serialize(value: Any) -> Any:
    return {"hex": value.hex()} if isinstance(value, bytes) else value


def _deserialize(value: Any) -> Any:
    return bytes.fromhex(value["hex"]) if isinstance(value, dict) and "hex" in value else value


# --------------------------------------------------------------------------- tipos de ajuste

@dataclass
class Tweak:
    id: str
    name: str
    description: str
    category: str
    risk: str = RISK_LOW
    requires_admin: bool = False
    note: str = ""
    recommended: bool = True

    def available(self) -> bool:
        return not self.requires_admin or is_admin()

    def is_applied(self) -> bool | None:
        raise NotImplementedError

    def apply(self, store: BackupStore) -> None:
        raise NotImplementedError

    def revert(self, store: BackupStore) -> None:
        raise NotImplementedError

    def _check_admin(self) -> None:
        if not self.available():
            raise TweakError("Requer executar como administrador.")


@dataclass
class RegistryTweak(Tweak):
    values: tuple[RegValue, ...] = field(default_factory=tuple)

    def is_applied(self) -> bool | None:
        try:
            for v in self.values:
                exists, current, _ = read_value(v.hive, v.key, v.name)
                if not exists or current != v.value:
                    return False
            return True
        except PermissionError:
            return None

    def apply(self, store: BackupStore) -> None:
        self._check_admin()
        backup = []
        for v in self.values:
            exists, current, kind = read_value(v.hive, v.key, v.name)
            backup.append({"hive": v.hive, "key": v.key, "name": v.name, "existed": exists,
                           "value": _serialize(current), "kind": kind})
        store.save_if_absent(self.id, backup)
        for v in self.values:
            write_value(v.hive, v.key, v.name, v.value, v.kind)
        log.info("Ajuste aplicado: %s", self.id)

    def revert(self, store: BackupStore) -> None:
        self._check_admin()
        backup = store.get(self.id)
        if backup is None:
            raise TweakError("Não há backup deste ajuste (não foi aplicado por este programa).")
        for item in backup:
            if item["existed"]:
                write_value(item["hive"], item["key"], item["name"], _deserialize(item["value"]), item["kind"])
            else:
                delete_value(item["hive"], item["key"], item["name"])
        store.remove(self.id)
        log.info("Ajuste revertido: %s", self.id)


@dataclass
class ServiceTweak(RegistryTweak):
    """Altera o tipo de inicialização de um serviço (4 = desativado) e o interrompe."""
    service: str = ""

    def __post_init__(self) -> None:
        key = rf"SYSTEM\CurrentControlSet\Services\{self.service}"
        self.values = (RegValue("HKLM", key, "Start", 4),)
        self.requires_admin = True

    def is_applied(self) -> bool | None:
        if not key_exists("HKLM", self.values[0].key):
            return None
        return super().is_applied()

    def apply(self, store: BackupStore) -> None:
        if not key_exists("HKLM", self.values[0].key):
            raise TweakError(f"Serviço {self.service} não existe neste computador.")
        super().apply(store)
        run_command([system_tool("sc.exe"), "stop", self.service], timeout=60)

    def revert(self, store: BackupStore) -> None:
        super().revert(store)
        _, start, _ = read_value("HKLM", self.values[0].key, "Start")
        if start == 2:  # automático
            run_command([system_tool("sc.exe"), "start", self.service], timeout=60)


@dataclass
class HibernationTweak(Tweak):
    def is_applied(self) -> bool | None:
        exists, value, _ = read_value("HKLM", r"SYSTEM\CurrentControlSet\Control\Power", "HibernateEnabled")
        return exists and value == 0

    def apply(self, store: BackupStore) -> None:
        self._check_admin()
        store.save_if_absent(self.id, {"enabled": not self.is_applied()})
        code, out = run_command([system_tool("powercfg.exe"), "/hibernate", "off"], timeout=60)
        if code != 0:
            raise TweakError(out.strip() or "powercfg falhou")

    def revert(self, store: BackupStore) -> None:
        self._check_admin()
        backup = store.get(self.id)
        if backup is not None and not backup.get("enabled", True):
            store.remove(self.id)
            return
        code, out = run_command([system_tool("powercfg.exe"), "/hibernate", "on"], timeout=60)
        if code != 0:
            raise TweakError(out.strip() or "powercfg falhou")
        store.remove(self.id)


def active_power_plan() -> str | None:
    code, out = run_command([system_tool("powercfg.exe"), "/getactivescheme"], timeout=30)
    match = GUID_RE.search(out) if code == 0 else None
    return match.group(0).lower() if match else None


@dataclass
class PowerPlanTweak(Tweak):
    def is_applied(self) -> bool | None:
        plan = active_power_plan()
        return None if plan is None else plan == HIGH_PERFORMANCE_PLAN

    def apply(self, store: BackupStore) -> None:
        current = active_power_plan()
        if current and current != HIGH_PERFORMANCE_PLAN:
            store.save_if_absent(self.id, {"plan": current})
        code, out = run_command([system_tool("powercfg.exe"), "/setactive", HIGH_PERFORMANCE_PLAN], timeout=30)
        if code != 0:
            store.remove(self.id)
            raise TweakError("O plano Alto Desempenho não está disponível neste computador "
                             "(comum em notebooks com Modern Standby). Nada foi alterado.")

    def revert(self, store: BackupStore) -> None:
        backup = store.get(self.id)
        plan = (backup or {}).get("plan") or "381b4222-f694-41f0-9685-ff5bb260df2e"  # Equilibrado
        code, out = run_command([system_tool("powercfg.exe"), "/setactive", plan], timeout=30)
        if code != 0:
            raise TweakError(out.strip() or "powercfg falhou")
        store.remove(self.id)


# --------------------------------------------------------------------------- catálogo

CDM = r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager"

PRIVACY = "Privacidade"
PERFORMANCE = "Desempenho"
CLEAN_UI = "Interface limpa"


def build_tweaks() -> list[Tweak]:
    return [
        RegistryTweak(
            "advertising_id", "Desativar ID de publicidade",
            "Impede que aplicativos usem um identificador para anúncios personalizados.",
            PRIVACY, values=(
                RegValue("HKCU", r"Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo", "Enabled", 0),
            )),
        RegistryTweak(
            "suggestions", "Desativar sugestões, dicas e apps instalados automaticamente",
            "Remove anúncios no Menu Iniciar/Configurações e impede a instalação silenciosa de apps sugeridos "
            "(ex.: jogos promocionais).",
            CLEAN_UI, values=(
                RegValue("HKCU", CDM, "SilentInstalledAppsEnabled", 0),
                RegValue("HKCU", CDM, "SystemPaneSuggestionsEnabled", 0),
                RegValue("HKCU", CDM, "SoftLandingEnabled", 0),
                RegValue("HKCU", CDM, "SubscribedContent-338388Enabled", 0),
                RegValue("HKCU", CDM, "SubscribedContent-338389Enabled", 0),
                RegValue("HKCU", CDM, "SubscribedContent-353694Enabled", 0),
                RegValue("HKCU", CDM, "SubscribedContent-353696Enabled", 0),
            )),
        RegistryTweak(
            "tailored_experiences", "Desativar experiências personalizadas",
            "Impede que a Microsoft use dados de diagnóstico para dicas e anúncios personalizados.",
            PRIVACY, values=(
                RegValue("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Privacy",
                         "TailoredExperiencesWithDiagnosticDataEnabled", 0),
            )),
        RegistryTweak(
            "start_web_search", "Desativar pesquisa Bing no Menu Iniciar",
            "A pesquisa do Iniciar passa a mostrar só resultados locais (mais rápida).",
            CLEAN_UI, note="Requer reiniciar o Explorador ou fazer logoff", values=(
                RegValue("HKCU", r"Software\Policies\Microsoft\Windows\Explorer", "DisableSearchBoxSuggestions", 1),
            )),
        RegistryTweak(
            "telemetry", "Reduzir telemetria ao mínimo permitido",
            "Define a coleta de dados de diagnóstico no nível mínimo que a edição do Windows aceita.",
            PRIVACY, requires_admin=True, values=(
                RegValue("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\DataCollection", "AllowTelemetry", 0),
            )),
        RegistryTweak(
            "activity_history", "Desativar histórico de atividades",
            "Impede o Windows de registrar e enviar o histórico de atividades (Linha do Tempo).",
            PRIVACY, requires_admin=True, values=(
                RegValue("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\System", "PublishUserActivities", 0),
                RegValue("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\System", "UploadUserActivities", 0),
            )),
        ServiceTweak(
            "diagtrack_service", "Desativar serviço de telemetria (DiagTrack)",
            "Serviço 'Experiências do Usuário Conectado e Telemetria'. Não afeta atualizações nem o funcionamento "
            "do Windows; libera memória e uso de disco/rede em segundo plano.",
            PRIVACY, service="DiagTrack"),
        RegistryTweak(
            "game_dvr", "Desativar gravação em segundo plano (Game DVR)",
            "Desliga a captura contínua da Xbox Game Bar, que consome GPU/CPU durante jogos.",
            PERFORMANCE, values=(
                RegValue("HKCU", r"System\GameConfigStore", "GameDVR_Enabled", 0),
                RegValue("HKCU", r"Software\Microsoft\Windows\CurrentVersion\GameDVR", "AppCaptureEnabled", 0),
            )),
        RegistryTweak(
            "visual_effects", "Reduzir efeitos visuais",
            "Desativa animações de janelas/barra de tarefas e a transparência. Deixa a interface mais ágil, "
            "especialmente em PCs mais simples.",
            PERFORMANCE, note="Requer logoff", values=(
                RegValue("HKCU", r"Control Panel\Desktop\WindowMetrics", "MinAnimate", "0", winreg.REG_SZ),
                RegValue("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
                         "TaskbarAnimations", 0),
                RegValue("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                         "EnableTransparency", 0),
            )),
        RegistryTweak(
            "menu_delay", "Menus mais rápidos",
            "Reduz o atraso de abertura de submenus de 400 ms para 150 ms.",
            PERFORMANCE, note="Requer logoff", values=(
                RegValue("HKCU", r"Control Panel\Desktop", "MenuShowDelay", "150", winreg.REG_SZ),
            )),
        RegistryTweak(
            "background_apps", "Bloquear apps da Loja em segundo plano",
            "Impede que apps da Microsoft Store rodem em segundo plano. Alguns apps (ex.: Email, Vincular ao "
            "Celular) podem deixar de enviar notificações.",
            PERFORMANCE, risk=RISK_MEDIUM, recommended=False, values=(
                RegValue("HKCU", r"Software\Microsoft\Windows\CurrentVersion\BackgroundAccessApplications",
                         "GlobalUserDisabled", 1),
            )),
        HibernationTweak(
            "hibernation", "Desativar hibernação",
            "Apaga o arquivo hiberfil.sys (vários GB). Desativa também a Inicialização Rápida e a opção "
            "'Hibernar'. Em notebooks, mantenha se você usa hibernação.",
            PERFORMANCE, risk=RISK_MEDIUM, requires_admin=True, recommended=False),
        PowerPlanTweak(
            "power_plan", "Plano de energia Alto Desempenho",
            "Mantém o processador em frequências mais altas. Ideal para desktops; em notebooks reduz a "
            "duração da bateria e aumenta o calor.",
            PERFORMANCE, risk=RISK_MEDIUM, recommended=False),
    ]
