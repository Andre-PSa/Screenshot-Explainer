@echo off
rem Uses the Claude Code login, so no API key is needed.
rem Pass extra flags straight through, e.g.:
rem   run.bat --corner tr --width 640 --instruction "responde em portugues"
cd /d "%~dp0"
python screen_explainer.py %*
pause
