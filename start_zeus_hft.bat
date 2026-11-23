@echo off
REM Lance l'interface Zeus HFT (necessite install.bat au prealable)
cd /d "%~dp0"
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
python -m zeus_hft gui
