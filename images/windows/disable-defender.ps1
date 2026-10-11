# OPT-IN, PER-VM. Run this inside a single throwaway Windows VM when Windows Defender
# would quarantine the .exe/.dll you want to study. It is deliberately NOT part of the
# clean base image. Only use it in a VM on Strata's isolated network.
#
#   strata upload <vm> images/windows/disable-defender.ps1 C:\disable-defender.ps1
#   strata exec <vm> "C:\disable-defender.ps1"
#
# Note: on current Windows, Tamper Protection may refuse the real-time toggle until it is
# turned off by hand in the Security app. The folder exclusion below still applies.
$ErrorActionPreference = "Continue"
try { Set-MpPreference -DisableRealtimeMonitoring $true } catch { Write-Host "real-time toggle blocked (Tamper Protection): $_" }
try { Add-MpPreference -ExclusionPath "C:\" } catch {}
try { Add-MpPreference -ExclusionExtension @("exe","dll","sys","ps1","bat","cmd","scr") } catch {}
Write-Host "Defender real-time scanning disabled where allowed; C:\ excluded."
