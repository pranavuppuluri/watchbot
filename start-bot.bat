@echo off
rem Starts the watchdog, which runs the WhatsApp listener, the saver and (if WATCHBOT_UPLOAD=on)
rem the website uploader in this one window, and restarts any of them that stop.
rem Settings (output folder, seller, uploading, approval chat) are in .env; see .env.example.
cd /d "%~dp0"
title watchbot
python -m watchbot run
pause
