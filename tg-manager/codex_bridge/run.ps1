$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TgManagerDir = Split-Path -Parent $ScriptDir
$Python = Join-Path $TgManagerDir ".venv\Scripts\python.exe"
& $Python (Join-Path $ScriptDir "bot.py")
