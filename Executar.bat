@echo off
rem Abre o DiagCompleto pedindo privilegios de administrador (necessarios para todos os recursos).
rem O caminho vai por variavel de ambiente (nunca colado no comando) para que uma pasta com
rem aspas ou caracteres especiais no nome nao injete comandos no PowerShell elevado.
rem O pythonw.exe NAO vem do PATH (que qualquer processo do usuario altera): vem do registro
rem HKLM e precisa estar em Program Files, lugares que so administradores alteram.
setlocal
set "DIAG_MAIN=%~dp0main.py"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command ^
  "$ErrorActionPreference = 'Stop';" ^
  "$pf = @([Environment]::GetFolderPath('ProgramFiles'), [Environment]::GetFolderPath('ProgramFilesX86')) | Where-Object { $_ } | ForEach-Object { $_.TrimEnd('\') + '\' };" ^
  "$found = foreach ($root in 'HKLM:\SOFTWARE\Python\PythonCore', 'HKLM:\SOFTWARE\WOW6432Node\Python\PythonCore') {" ^
  "  foreach ($k in @(Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue)) {" ^
  "    if (-not ($k.PSChildName -match '^(\d+)\.(\d+)')) { continue }" ^
  "    $v = [version]($Matches[1] + '.' + $Matches[2]); if ($v -lt [version]'3.10') { continue }" ^
  "    $ip = Get-ItemProperty -LiteralPath ($k.PSPath + '\InstallPath') -ErrorAction SilentlyContinue; if (-not $ip) { continue }" ^
  "    $exe = $ip.WindowedExecutablePath; if (-not $exe -and $ip.'(default)') { $exe = Join-Path $ip.'(default)' 'pythonw.exe' }" ^
  "    if (-not $exe -or -not (Test-Path -LiteralPath $exe -PathType Leaf)) { continue }" ^
  "    $full = [IO.Path]::GetFullPath($exe);" ^
  "    if (-not ($pf | Where-Object { $full.StartsWith($_, [StringComparison]::OrdinalIgnoreCase) })) { continue }" ^
  "    [pscustomobject]@{ v = $v; exe = $full } } };" ^
  "$py = $found | Sort-Object v -Descending | Select-Object -First 1;" ^
  "if (-not $py) { [Console]::Error.WriteLine('Nao foi encontrado um Python 3.10+ instalado para todos os usuarios em Program Files. Por seguranca, o DiagCompleto so roda como administrador com um Python que apenas administradores podem alterar. Reinstale o Python marcando Install for all users.'); exit 1 };" ^
  "Start-Process -FilePath $py.exe -ArgumentList ('\"' + $env:DIAG_MAIN + '\"') -WorkingDirectory ([Environment]::SystemDirectory) -Verb RunAs"
if errorlevel 1 pause
endlocal
