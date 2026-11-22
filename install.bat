@echo off
echo === Installation de ZEUS ===
python -m venv .venv
call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
echo.
echo Installation terminee. Ouvre MetaTrader 5, connecte ton compte DEMO, puis lance start_zeus.bat
pause
