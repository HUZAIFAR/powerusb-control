# Runs the PowerUSB server in this window (Ctrl-C to stop).
# For unattended running use install-autostart.ps1 instead.
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
python -m powerusb.server
