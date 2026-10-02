@echo off
REM ===================================================================
REM  polylag launcher
REM
REM  Double-click for a menu, or pass a command:
REM     run.bat setup       create the venv and install dependencies
REM     run.bat selftest    unit tests + full offline simulation
REM     run.bat simulate    drive the engine with scripted markets
REM     run.bat doctor      check connectivity, markets, credentials
REM     run.bat scan fed    find market slugs for config.yaml
REM     run.bat watch       read-only: log signals, place nothing
REM     run.bat paper       simulated fills against the live book
REM     run.bat live        REAL MONEY
REM     run.bat report      performance metrics
REM     run.bat status      persisted risk state
REM     run.bat kill        latch the kill switch NOW
REM     run.bat resume      clear the latch after review
REM ===================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "VENV=.venv"
set "PY=%VENV%\Scripts\python.exe"

REM ---- find a usable Python for bootstrapping -----------------------
set "BOOTPY="
where py >nul 2>&1 && set "BOOTPY=py -3"
if "!BOOTPY!"=="" (
    where python >nul 2>&1 && set "BOOTPY=python"
)
if "!BOOTPY!"=="" (
    echo.
    echo   Python was not found on your PATH.
    echo   Install Python 3.11 or newer from https://python.org
    echo   and tick "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)

REM INTERACTIVE tracks how we were started. `shift` below rewrites %1, so we
REM cannot re-test it later to decide whether to pause and show the menu again.
set "INTERACTIVE="
set "RC=0"
if "%~1"=="" (
    set "INTERACTIVE=1"
    goto MENU
)
set "CMD=%~1"
shift
goto DISPATCH

REM ===================================================================
:MENU
cls
echo.
echo   ============================================================
echo      polylag  --  Polymarket news-lag trading system
echo   ============================================================
echo.
if not exist "%PY%" (
    echo      [!] Not set up yet. Choose 1 first.
    echo.
)
echo      SAFE ^(no network, no money^)
echo        1.  Setup          create venv, install dependencies
echo        2.  Self-test      unit tests + offline simulation
echo        3.  Simulate       watch the engine trade a fake market
echo.
echo      READ-ONLY ^(network, no money^)
echo        4.  Doctor         check connectivity and configuration
echo        5.  Scan           find market slugs for config.yaml
echo        6.  Watch          log real signals, place nothing
echo.
echo      TRADING
echo        7.  Paper          simulated fills on live books
echo        8.  LIVE           REAL MONEY -- confirmation required
echo.
echo      TOOLS
echo        9.  Report         expectancy, drawdown, fee drag
echo       10.  Status         persisted risk state
echo       11.  KILL SWITCH    stop everything right now
echo       12.  Resume         clear the kill latch
echo        0.  Exit
echo.
set "CHOICE="
set /p "CHOICE=   Choose: "

REM Do NOT write `if cond set X & goto Y` here. cmd parses `&` at the top
REM level, so the goto would run whether or not the condition held -- which
REM dispatched an empty command and produced a syntax error. One statement
REM per line, then a single guarded jump.
set "CMD="
if "!CHOICE!"=="0"  exit /b 0
if "!CHOICE!"=="1"  set "CMD=setup"
if "!CHOICE!"=="2"  set "CMD=selftest"
if "!CHOICE!"=="3"  set "CMD=simulate"
if "!CHOICE!"=="4"  set "CMD=doctor"
if "!CHOICE!"=="5"  goto ASK_SCAN
if "!CHOICE!"=="6"  set "CMD=watch"
if "!CHOICE!"=="7"  set "CMD=paper"
if "!CHOICE!"=="8"  set "CMD=live"
if "!CHOICE!"=="9"  set "CMD=report"
if "!CHOICE!"=="10" set "CMD=status"
if "!CHOICE!"=="11" set "CMD=kill"
if "!CHOICE!"=="12" set "CMD=resume"
if defined CMD goto DISPATCH
echo   Not a valid choice.
timeout /t 2 /nobreak >nul 2>&1
goto MENU

:ASK_SCAN
set "QUERY="
set /p "QUERY=   Search markets for: "
if "!QUERY!"=="" goto MENU
set "CMD=scan"
set "ARGS=!QUERY!"
goto DISPATCH

REM ===================================================================
:DISPATCH
if /i "!CMD!"=="setup" goto SETUP

if not exist "%PY%" (
    echo.
    echo   No virtual environment yet -- running setup first.
    echo.
    call :DO_SETUP
    if errorlevel 1 goto FAILED
)

if /i "!CMD!"=="live" goto LIVE
goto RUN

REM ---- setup --------------------------------------------------------
:SETUP
call :DO_SETUP
if errorlevel 1 goto FAILED
echo.
echo   Setup complete. Next: run.bat selftest
echo.
goto DONE

:DO_SETUP
echo.
echo   Creating virtual environment in %VENV% ...
if not exist "%VENV%" (
    %BOOTPY% -m venv "%VENV%"
    if errorlevel 1 (
        echo   Failed to create the virtual environment.
        exit /b 1
    )
)
echo   Upgrading pip ...
"%PY%" -m pip install --upgrade pip --quiet
echo   Installing dependencies ^(this can take a minute^) ...
"%PY%" -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo.
    echo   Dependency install failed.
    echo   If py-clob-client is the problem you can still run everything
    echo   except live mode: pip install httpx websockets feedparser PyYAML python-dotenv pytest
    exit /b 1
)
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo   Created .env from the template ^(empty -- paper mode needs nothing^).
    )
)
echo   Dependencies installed.
exit /b 0

REM ---- live mode gets an extra speed bump ---------------------------
:LIVE
echo.
echo   ============================================================
echo      LIVE TRADING -- THIS SPENDS REAL MONEY
echo   ============================================================
echo      This strategy can and often does lose money. News-lag
echo      edges decay fast, and nothing in this software predicts
echo      a profit.
echo.
echo      Before continuing you should have:
echo        - run selftest and doctor with no failures
echo        - run watch for days and paper for 30+ trades
echo        - set execution.fee_bps from the venue's real schedule
echo        - funded a wallet holding ONLY your trading bankroll
echo   ============================================================
echo.
set "SURE="
set /p "SURE=   Type LIVE to continue: "
if /i not "!SURE!"=="LIVE" (
    echo   Cancelled.
    goto DONE
)
goto RUN

REM ---- run ----------------------------------------------------------
:RUN
echo.
if "!ARGS!"=="" (
    "%PY%" run.py !CMD! %1 %2 %3 %4
) else (
    "%PY%" run.py !CMD! "!ARGS!"
)
set "RC=!ERRORLEVEL!"
echo.
if not "!RC!"=="0" (
    echo   [exit code !RC!]
)
goto DONE

:FAILED
echo.
echo   Something went wrong. Scroll up for the error.
echo.
set "RC=1"

:DONE
if defined INTERACTIVE (
    echo.
    pause
    set "ARGS="
    goto MENU
)
endlocal & exit /b %RC%
