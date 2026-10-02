@echo off
setlocal

rem Keep the bilingual report response within the production output limit.
rem This is runtime capacity only; content selection and quality rules stay unchanged.
set "MAX_BATCH_ARTICLES=6"
set "MAX_NEWS_INPUT_CHARS_PER_ARTICLE=1200"
set "MAX_NEWS_OUTPUT_TOKENS=7200"

py.exe -3 "%~dp0..\run_daily_delivery.py" --scheduled
set "RADAR_EXIT_CODE=%ERRORLEVEL%"
rem Preserve Python's result across environment cleanup and later commands.
endlocal & exit /b %RADAR_EXIT_CODE%
