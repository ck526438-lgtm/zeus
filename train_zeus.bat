@echo off
call .venv\Scripts\activate
python main.py download --symbol XAUUSD --days 90
python main.py train --csv data\XAUUSD_M1.csv
python main.py backtest --csv data\XAUUSD_M1.csv
pause
