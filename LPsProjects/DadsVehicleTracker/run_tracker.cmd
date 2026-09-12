@echo off
REM Launcher for the Windows Scheduled Task "Dads Vehicle Tracker" (runs at logon).
REM Supervises the Flask app: if it exits for any reason, wait 10 s and start it
REM again. Appends to data\app.log. Stop it via Task Scheduler (End task).
cd /d "%~dp0"
if not exist data mkdir data
:loop
echo [%date% %time%] starting app.py >> data\app.log
".venv\Scripts\python.exe" app.py >> data\app.log 2>&1
echo [%date% %time%] app.py exited with %errorlevel%; restarting in 10 s >> data\app.log
timeout /t 10 /nobreak > nul
goto loop
