@echo off
rem Abre o DiagCompleto pedindo privilegios de administrador (necessarios para todos os recursos).
cd /d "%~dp0"
powershell -NoProfile -Command "Start-Process -FilePath pythonw -ArgumentList '\"%~dp0main.py\"' -WorkingDirectory '%~dp0' -Verb RunAs"
