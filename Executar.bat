@echo off
rem Abre o DiagCompleto pedindo privilegios de administrador (necessarios para todos os recursos).
rem O caminho vai por variavel de ambiente (nunca colado no comando) para que uma pasta com
rem aspas ou caracteres especiais no nome nao injete comandos no PowerShell elevado.
setlocal
set "DIAG_MAIN=%~dp0main.py"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command ^
  "Start-Process -FilePath pythonw -ArgumentList ('\"' + $env:DIAG_MAIN + '\"') -WorkingDirectory ([Environment]::SystemDirectory) -Verb RunAs"
endlocal
