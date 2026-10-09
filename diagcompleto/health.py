"""Diagnóstico de saúde dos componentes (somente leitura — nada é alterado no sistema)."""
from __future__ import annotations

import ctypes
import logging
import shutil
import string
import winreg
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .utils import as_list, format_bytes, is_admin, run_powershell_json

log = logging.getLogger("DiagCompleto.health")

OK = "OK"
WARN = "Atenção"
CRIT = "Crítico"
INFO = "Info"
UNKNOWN = "Desconhecido"

# Códigos do Gerenciador de Dispositivos que não indicam defeito
# (22 = desativado pelo usuário, 45 = dispositivo desconectado).
IGNORED_DEVICE_CODES = {22, 45}
# 32 = driver do dispositivo desativado de propósito (comum em dispositivos virtuais de fabricantes).
BENIGN_DEVICE_CODES = {32}

HEALTH_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
function D($d) { if ($d) { $d.ToString('o') } else { $null } }
function CountEvents($filter) { @(Get-WinEvent -FilterHashtable $filter -MaxEvents 500).Count }
$r = [ordered]@{}
$os = Get-CimInstance Win32_OperatingSystem
$r.os = [ordered]@{ caption = $os.Caption; version = $os.Version; build = $os.BuildNumber; arch = $os.OSArchitecture;
  lastBoot = D $os.LastBootUpTime; installDate = D $os.InstallDate }
$cs = Get-CimInstance Win32_ComputerSystem
$r.computer = [ordered]@{ manufacturer = $cs.Manufacturer; model = $cs.Model; name = $cs.Name }
$bb = Get-CimInstance Win32_BaseBoard
$bios = Get-CimInstance Win32_BIOS
$r.board = [ordered]@{ manufacturer = $bb.Manufacturer; product = $bb.Product; bios = $bios.SMBIOSBIOSVersion; biosDate = D $bios.ReleaseDate }
$r.cpu = @(Get-CimInstance Win32_Processor | ForEach-Object { [ordered]@{ name = "$($_.Name)".Trim();
  cores = $_.NumberOfCores; threads = $_.NumberOfLogicalProcessors; load = $_.LoadPercentage; maxClock = $_.MaxClockSpeed; status = $_.Status } })
$r.memoryModules = @(Get-CimInstance Win32_PhysicalMemory | ForEach-Object { [ordered]@{ capacity = $_.Capacity;
  speed = $_.ConfiguredClockSpeed; manufacturer = "$($_.Manufacturer)".Trim(); part = "$($_.PartNumber)".Trim(); slot = $_.DeviceLocator } })
$r.gpu = @(Get-CimInstance Win32_VideoController | ForEach-Object { [ordered]@{ name = $_.Name; driver = $_.DriverVersion;
  driverDate = D $_.DriverDate; status = $_.Status; errorCode = $_.ConfigManagerErrorCode } })
$r.thermal = @(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature | ForEach-Object {
  [math]::Round($_.CurrentTemperature / 10 - 273.15, 1) })
$r.disks = @(Get-PhysicalDisk | ForEach-Object {
  $c = $_ | Get-StorageReliabilityCounter
  [ordered]@{ name = $_.FriendlyName; media = "$($_.MediaType)"; bus = "$($_.BusType)"; size = $_.Size; health = "$($_.HealthStatus)";
    operational = (@($_.OperationalStatus) | ForEach-Object { "$_" }) -join ', '; wear = $c.Wear; temperature = $c.Temperature;
    temperatureMax = $c.TemperatureMax; readErrors = $c.ReadErrorsTotal; writeErrors = $c.WriteErrorsTotal; powerOnHours = $c.PowerOnHours } })
$r.smart = @(Get-CimInstance -Namespace root/wmi -ClassName MSStorageDriver_FailurePredictStatus | ForEach-Object {
  [ordered]@{ instance = $_.InstanceName; predictFailure = [bool]$_.PredictFailure } })
$bat = Get-CimInstance Win32_Battery | Select-Object -First 1
if ($bat) {
  $r.battery = [ordered]@{ present = $true; charge = $bat.EstimatedChargeRemaining; status = $bat.BatteryStatus;
    design = (Get-CimInstance -Namespace root/wmi -ClassName BatteryStaticData | Select-Object -First 1).DesignedCapacity;
    full = (Get-CimInstance -Namespace root/wmi -ClassName BatteryFullChargedCapacity | Select-Object -First 1).FullChargedCapacity;
    cycles = (Get-CimInstance -Namespace root/wmi -ClassName BatteryCycleCount | Select-Object -First 1).CycleCount }
} else { $r.battery = [ordered]@{ present = $false } }
$since = (Get-Date).AddDays(-30)
$r.events = [ordered]@{
  whea = CountEvents @{ LogName = 'System'; ProviderName = 'Microsoft-Windows-WHEA-Logger'; StartTime = $since }
  unexpectedShutdown = CountEvents @{ LogName = 'System'; ProviderName = 'Microsoft-Windows-Kernel-Power'; Id = 41; StartTime = $since }
  bugcheck = CountEvents @{ LogName = 'System'; ProviderName = 'Microsoft-Windows-WER-SystemErrorReporting'; Id = 1001; StartTime = $since }
  diskSevere = CountEvents @{ LogName = 'System'; ProviderName = 'disk'; Id = 7,11,15,51,52; StartTime = $since }
  diskRetry = CountEvents @{ LogName = 'System'; ProviderName = 'disk'; Id = 153; StartTime = $since }
}
$r.problemDevices = @(Get-CimInstance Win32_PnPEntity -Filter 'ConfigManagerErrorCode <> 0' | ForEach-Object {
  [ordered]@{ name = "$($_.Name)"; code = $_.ConfigManagerErrorCode } })
$r.antivirus = @(Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct | ForEach-Object { "$($_.displayName)" })
$mp = Get-MpComputerStatus
$r.defender = if ($mp) { [ordered]@{ realtime = [bool]$mp.RealTimeProtectionEnabled; enabled = [bool]$mp.AntivirusEnabled;
  signatureAge = $mp.AntivirusSignatureAge } } else { $null }
$r
"""


@dataclass
class HealthItem:
    component: str
    name: str
    status: str
    detail: str


@dataclass
class HealthReport:
    items: list[HealthItem]
    score: int
    summary: dict[str, str]
    collected_at: datetime
    admin: bool
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    def count(self, status: str) -> int:
        return sum(1 for item in self.items if item.status == status)


# --------------------------------------------------------------------------- coleta

class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _memory_status() -> dict[str, int]:
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(_MemoryStatus)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return {"load": status.dwMemoryLoad, "total": status.ullTotalPhys, "available": status.ullAvailPhys}


def _fixed_volumes() -> list[dict[str, Any]]:
    volumes = []
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    for index, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << index):
            continue
        root = f"{letter}:\\"
        if ctypes.windll.kernel32.GetDriveTypeW(root) != 3:  # DRIVE_FIXED
            continue
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            continue
        volumes.append({"letter": letter, "total": usage.total, "free": usage.free})
    return volumes


def _pending_reboot() -> bool:
    keys = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
    ]
    for key in keys:
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0,
                                           winreg.KEY_READ | winreg.KEY_WOW64_64KEY))
            return True
        except OSError:
            continue
    return False


def collect_raw() -> dict[str, Any]:
    raw = run_powershell_json(HEALTH_SCRIPT, timeout=240) or {}
    raw["memory"] = _memory_status()
    raw["volumes"] = _fixed_volumes()
    raw["pendingReboot"] = _pending_reboot()
    return raw


# --------------------------------------------------------------------------- avaliação

def _worst(*statuses: str) -> str:
    order = [CRIT, WARN, UNKNOWN, INFO, OK]
    for status in order:
        if status in statuses:
            return status
    return OK


def evaluate_disks(raw: dict) -> list[HealthItem]:
    items = []
    disks = as_list(raw.get("disks"))
    if not disks:
        items.append(HealthItem("Armazenamento", "Discos físicos", UNKNOWN,
                                "Não foi possível ler os discos físicos."))
    for disk in disks:
        statuses = []
        details = []
        health = (disk.get("health") or "").strip()
        translated = {"Healthy": "Saudável", "Warning": "Alerta", "Unhealthy": "Com defeito"}.get(health, health or "?")
        details.append(f"Estado: {translated}")
        if health == "Unhealthy":
            statuses.append(CRIT)
        elif health == "Warning":
            statuses.append(WARN)
        elif health == "Healthy":
            statuses.append(OK)
        else:
            statuses.append(UNKNOWN)

        media = (disk.get("media") or "").upper()
        is_hdd = media == "HDD"
        wear = disk.get("wear")
        if wear is not None:
            details.append(f"Desgaste: {wear}%")
            if wear >= 90:
                statuses.append(CRIT)
            elif wear >= 70:
                statuses.append(WARN)

        temperature = disk.get("temperature")
        if temperature:
            details.append(f"Temp.: {temperature} °C")
            warn_at, crit_at = (50, 60) if is_hdd else (65, 75)
            if temperature >= crit_at:
                statuses.append(CRIT)
            elif temperature >= warn_at:
                statuses.append(WARN)

        errors = (disk.get("readErrors") or 0) + (disk.get("writeErrors") or 0)
        if disk.get("readErrors") is not None or disk.get("writeErrors") is not None:
            details.append(f"Erros de leitura/escrita: {errors}")
            if errors > 0:
                statuses.append(WARN)

        hours = disk.get("powerOnHours")
        if hours:
            details.append(f"Horas ligado: {hours:,}".replace(",", "."))
            if is_hdd and hours > 40000:
                statuses.append(WARN)
                details.append("idade elevada para um HD")

        kind = disk.get("media") if disk.get("media") not in (None, "", "Unspecified") else disk.get("bus", "")
        name = f"{disk.get('name', 'Disco')} ({kind}, {format_bytes(disk.get('size'))})"
        items.append(HealthItem("Armazenamento", name, _worst(*statuses), " | ".join(details)))

    failing = [s for s in as_list(raw.get("smart")) if s.get("predictFailure")]
    if failing:
        items.append(HealthItem("Armazenamento", "Previsão de falha S.M.A.R.T.", CRIT,
                                f"{len(failing)} disco(s) com falha iminente prevista. Faça backup imediatamente!"))
    elif raw.get("smart"):
        items.append(HealthItem("Armazenamento", "Previsão de falha S.M.A.R.T.", OK,
                                "Nenhum disco com falha prevista."))
    return items


def evaluate_volumes(raw: dict) -> list[HealthItem]:
    items = []
    for volume in as_list(raw.get("volumes")):
        total, free = volume["total"], volume["free"]
        pct = (free / total * 100) if total else 0
        status = CRIT if pct < 5 else WARN if pct < 10 else OK
        detail = f"{format_bytes(free)} livres de {format_bytes(total)} ({pct:.0f}%)"
        if status != OK:
            detail += " — pouco espaço deixa o Windows lento e pode impedir atualizações"
        items.append(HealthItem("Armazenamento", f"Espaço livre na unidade {volume['letter']}:", status, detail))
    return items


def evaluate_memory(raw: dict) -> list[HealthItem]:
    items = []
    memory = raw.get("memory") or {}
    total = memory.get("total")
    if total:
        load = memory.get("load", 0)
        status = WARN if load >= 90 else OK
        detail = f"{format_bytes(total)} instalada | Em uso agora: {load}% ({format_bytes(memory.get('available'))} livres)"
        if total < 4 * 1024 ** 3:
            status = _worst(status, WARN)
            detail += " | Menos de 4 GB: insuficiente para o Windows atual"
        items.append(HealthItem("Memória", "Memória RAM", status, detail))
    modules = as_list(raw.get("memoryModules"))
    if modules:
        desc = []
        for module in modules:
            speed = f" {module['speed']} MHz" if module.get("speed") else ""
            desc.append(f"{module.get('slot') or '?'}: {format_bytes(module.get('capacity'))}{speed}")
        speeds = {m.get("speed") for m in modules if m.get("speed")}
        status = INFO
        text = "; ".join(desc)
        if len(speeds) > 1:
            text += " — módulos com velocidades diferentes operam na menor delas"
        items.append(HealthItem("Memória", f"Módulos ({len(modules)})", status, text))
    return items


def evaluate_cpu(raw: dict) -> list[HealthItem]:
    items = []
    for cpu in as_list(raw.get("cpu")):
        status = OK if (cpu.get("status") or "OK") == "OK" else WARN
        detail = (f"{cpu.get('cores')} núcleos / {cpu.get('threads')} threads | "
                  f"Clock máx.: {cpu.get('maxClock')} MHz | Uso agora: {cpu.get('load') if cpu.get('load') is not None else '?'}%")
        items.append(HealthItem("Processador", cpu.get("name") or "CPU", status, detail))

    temps = [t for t in as_list(raw.get("thermal")) if isinstance(t, (int, float)) and 0 < t < 130]
    if temps:
        hottest = max(temps)
        status = CRIT if hottest >= 90 else WARN if hottest >= 80 else OK
        items.append(HealthItem("Processador", "Temperatura (zona térmica ACPI)", status,
                                f"{hottest} °C (sensor da placa-mãe; pode diferir da temperatura do núcleo)"))
    else:
        reason = "sensor não exposto pelo fabricante" if raw.get("_admin") else "execute como administrador para ler"
        items.append(HealthItem("Processador", "Temperatura", UNKNOWN, f"Indisponível ({reason})."))
    return items


def evaluate_gpu(raw: dict) -> list[HealthItem]:
    items = []
    for gpu in as_list(raw.get("gpu")):
        code = gpu.get("errorCode") or 0
        status = OK if (gpu.get("status") == "OK" and code == 0) else WARN
        date = (gpu.get("driverDate") or "")[:10]
        detail = f"Driver {gpu.get('driver') or '?'}" + (f" de {date}" if date else "")
        if code:
            detail += f" | Código de erro do dispositivo: {code}"
        if date:
            try:
                age_years = (datetime.now() - datetime.fromisoformat(date)).days / 365
                if age_years > 2:
                    detail += " | driver com mais de 2 anos — considere atualizar pelo site do fabricante"
            except ValueError:
                pass
        items.append(HealthItem("Vídeo", gpu.get("name") or "Placa de vídeo", status, detail))
    return items


def evaluate_battery(raw: dict) -> list[HealthItem]:
    battery = raw.get("battery") or {}
    if not battery.get("present"):
        return []
    design, full = battery.get("design"), battery.get("full")
    cycles = battery.get("cycles")
    charge = battery.get("charge")
    if design and full:
        pct = min(full / design * 100, 100)
        status = CRIT if pct < 50 else WARN if pct < 70 else OK
        detail = f"Capacidade atual: {pct:.0f}% da original ({full:,} de {design:,} mWh)".replace(",", ".")
    else:
        status = INFO
        detail = "Capacidade de projeto indisponível (execute como administrador)"
    if cycles:
        detail += f" | Ciclos: {cycles}"
    if charge is not None:
        detail += f" | Carga: {charge}%"
    return [HealthItem("Bateria", "Bateria", status, detail)]


def evaluate_events(raw: dict) -> list[HealthItem]:
    events = raw.get("events") or {}
    items = []

    def add(name: str, count: int | None, warn_at: int, crit_at: int, meaning: str) -> None:
        if count is None:
            items.append(HealthItem("Estabilidade", name, UNKNOWN, "Não foi possível ler o log de eventos."))
            return
        status = CRIT if count >= crit_at else WARN if count >= warn_at else OK
        detail = f"{count} ocorrência(s) nos últimos 30 dias"
        if count:
            detail += f" — {meaning}"
        items.append(HealthItem("Estabilidade", name, status, detail))

    add("Erros de hardware (WHEA)", events.get("whea"), 1, 10,
        "falhas reportadas pelo próprio hardware (CPU, RAM, PCIe); investigue")
    add("Telas azuis (BSOD)", events.get("bugcheck"), 1, 3,
        "o sistema travou com erro crítico")
    add("Desligamentos inesperados", events.get("unexpectedShutdown"), 1, 5,
        "queda de energia, travamento ou desligamento forçado")
    add("Erros graves de disco", events.get("diskSevere"), 1, 10,
        "setores defeituosos, erros de paginação ou falha prevista; faça backup")
    retries = events.get("diskRetry")
    if retries:
        status = WARN if retries >= 100 else INFO
        items.append(HealthItem("Estabilidade", "Operações de disco repetidas", status,
                                f"{retries} nos últimos 30 dias — o Windows repetiu leituras/escritas com sucesso. "
                                "Comum com economia de energia do SSD/driver de armazenamento; "
                                "fique atento se aumentar."))
    return items


def evaluate_devices(raw: dict) -> list[HealthItem]:
    problems = [d for d in as_list(raw.get("problemDevices")) if d.get("code") not in IGNORED_DEVICE_CODES]
    if not problems:
        return [HealthItem("Dispositivos", "Gerenciador de Dispositivos", OK, "Nenhum dispositivo com problema.")]
    names = ", ".join(f"{d.get('name')} (código {d.get('code')})" for d in problems[:5])
    more = f" e mais {len(problems) - 5}" if len(problems) > 5 else ""
    if all(d.get("code") in BENIGN_DEVICE_CODES for d in problems):
        return [HealthItem("Dispositivos", "Gerenciador de Dispositivos", INFO,
                           f"{len(problems)} dispositivo(s) com driver desativado (geralmente intencional): {names}{more}")]
    return [HealthItem("Dispositivos", "Gerenciador de Dispositivos", WARN,
                       f"{len(problems)} dispositivo(s) com problema: {names}{more}")]


def evaluate_security(raw: dict) -> list[HealthItem]:
    products = [p for p in as_list(raw.get("antivirus")) if p]
    defender = raw.get("defender")
    third_party = [p for p in products if "defender" not in p.lower()]
    if third_party:
        return [HealthItem("Segurança", "Antivírus", OK, "Registrado: " + ", ".join(products))]
    if defender:
        if not defender.get("realtime"):
            return [HealthItem("Segurança", "Antivírus", WARN,
                               "Proteção em tempo real do Microsoft Defender está desativada.")]
        age = defender.get("signatureAge")
        if age is not None and age > 7:
            return [HealthItem("Segurança", "Antivírus", WARN,
                               f"Definições do Defender desatualizadas há {age} dias.")]
        return [HealthItem("Segurança", "Antivírus", OK, "Microsoft Defender ativo e atualizado.")]
    if products:
        return [HealthItem("Segurança", "Antivírus", OK, "Registrado: " + ", ".join(products))]
    return [HealthItem("Segurança", "Antivírus", WARN, "Nenhum antivírus ativo foi detectado.")]


def evaluate_system(raw: dict, now: datetime | None = None) -> list[HealthItem]:
    items = []
    os_info = raw.get("os") or {}
    last_boot = os_info.get("lastBoot")
    if last_boot:
        try:
            boot = datetime.fromisoformat(last_boot)
            now = now or datetime.now(boot.tzinfo or timezone.utc)
            days = (now - boot).total_seconds() / 86400
            status = INFO if days > 7 else OK
            detail = f"Ligado há {days:.1f} dia(s)"
            if days > 7:
                detail += " — reiniciar periodicamente libera memória e conclui atualizações"
            items.append(HealthItem("Sistema", "Tempo desde a última reinicialização", status, detail))
        except ValueError:
            pass
    if raw.get("pendingReboot"):
        items.append(HealthItem("Sistema", "Reinicialização pendente", WARN,
                                "Há atualizações aguardando reinicialização para serem concluídas."))
    return items


def evaluate(raw: dict) -> list[HealthItem]:
    items: list[HealthItem] = []
    for evaluator in (evaluate_system, evaluate_cpu, evaluate_memory, evaluate_disks, evaluate_volumes,
                      evaluate_gpu, evaluate_battery, evaluate_events, evaluate_devices, evaluate_security):
        try:
            items.extend(evaluator(raw))
        except Exception:  # um avaliador com dado inesperado não derruba o diagnóstico inteiro
            log.exception("Falha no avaliador %s", evaluator.__name__)
    return items


def compute_score(items: list[HealthItem]) -> int:
    penalty = sum(30 if i.status == CRIT else 8 if i.status == WARN else 0 for i in items)
    return max(0, 100 - penalty)


def build_summary(raw: dict) -> dict[str, str]:
    os_info = raw.get("os") or {}
    computer = raw.get("computer") or {}
    board = raw.get("board") or {}
    cpus = as_list(raw.get("cpu"))
    gpus = as_list(raw.get("gpu"))
    memory = raw.get("memory") or {}
    return {
        "Sistema": f"{os_info.get('caption', '?')} (build {os_info.get('build', '?')}, {os_info.get('arch', '?')})",
        "Computador": f"{computer.get('manufacturer', '')} {computer.get('model', '')}".strip() or "?",
        "Placa-mãe": f"{board.get('manufacturer', '')} {board.get('product', '')} — BIOS {board.get('bios', '?')}".strip(),
        "Processador": ", ".join(c.get("name", "?") for c in cpus) or "?",
        "Memória": format_bytes(memory.get("total")),
        "Vídeo": ", ".join(g.get("name", "?") for g in gpus) or "?",
    }


def run_health_check() -> HealthReport:
    raw = collect_raw()
    raw["_admin"] = is_admin()
    items = evaluate(raw)
    return HealthReport(items=items, score=compute_score(items), summary=build_summary(raw),
                        collected_at=datetime.now(), admin=raw["_admin"], raw=raw)
