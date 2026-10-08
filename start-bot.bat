@echo off
rem Starts the WhatsApp listener and the watcher in two windows.
rem Edit SELLER below to the dealer's WhatsApp name or number (leave empty for anyone posting photos).
set SELLER=

cd /d "%~dp0"
start "watchbot listener (WhatsApp)" cmd /k "cd listener && node index.mjs"
if "%SELLER%"=="" (
  start "watchbot saver" cmd /k "python -m watchbot watch"
) else (
  start "watchbot saver" cmd /k "python -m watchbot watch --seller "%SELLER%""
)
