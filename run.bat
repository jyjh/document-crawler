@echo off
cd /d "%~dp0"
python -m doc_crawler --config "%~dp0config.yaml" %*
exit /b %ERRORLEVEL%

