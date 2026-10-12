# Strata provisioning for Windows 11. Runs once at first logon (FirstLogonCommands, as the
# auto-logged-in strata admin), then powers off.
# Goal: the guest agent running at every boot (Strata depends on it), SSH and RDP
# reachable, and no automatic disk encryption (so states can be compared later).
# Windows Defender is left ON in the clean image; turn it off per-VM when needed
# (see images/windows/disable-defender.ps1), not in the shared base.
$ErrorActionPreference = "Continue"
Start-Transcript -Path C:\strata-provision.log -Append | Out-Null

function Find-VirtioDrive {
  foreach ($d in (Get-CimInstance Win32_LogicalDisk | Where-Object { $_.DriveType -eq 5 })) {
    if (Test-Path "$($d.DeviceID)\virtio-win-gt-x64.msi") { return $d.DeviceID }
    if (Test-Path "$($d.DeviceID)\guest-agent\qemu-ga-x86_64.msi") { return $d.DeviceID }
  }
  return $null
}

$virtio = Find-VirtioDrive
if ($virtio) {
  Write-Host "virtio media at $virtio"
  # Drivers + QEMU guest agent (installed as an automatic service)
  if (Test-Path "$virtio\virtio-win-gt-x64.msi") {
    Start-Process msiexec -ArgumentList "/i `"$virtio\virtio-win-gt-x64.msi`" /qn /norestart ADDLOCAL=ALL" -Wait
  }
  if (Test-Path "$virtio\guest-agent\qemu-ga-x86_64.msi") {
    Start-Process msiexec -ArgumentList "/i `"$virtio\guest-agent\qemu-ga-x86_64.msi`" /qn /norestart" -Wait
  }
} else {
  Write-Host "WARNING: virtio media not found; guest agent may be missing"
}

# Make sure the guest agent runs now and at every boot (Strata depends on it)
foreach ($svc in @("QEMU-GA", "QEMU Guest Agent", "qemu-ga")) {
  if (Get-Service -Name $svc -ErrorAction SilentlyContinue) {
    Set-Service -Name $svc -StartupType Automatic
    Start-Service -Name $svc -ErrorAction SilentlyContinue
  }
}

# OpenSSH server
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction SilentlyContinue
if (Get-Service sshd -ErrorAction SilentlyContinue) {
  Set-Service -Name sshd -StartupType Automatic
  Start-Service sshd
  New-NetFirewallRule -Name strata-ssh -DisplayName "Strata SSH" -Enabled True `
    -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -ErrorAction SilentlyContinue
}

# RDP
Set-ItemProperty "HKLM:\System\CurrentControlSet\Control\Terminal Server" -Name fDenyTSConnections -Value 0
Enable-NetFirewallRule -DisplayGroup "Remote Desktop" -ErrorAction SilentlyContinue

# No automatic encryption; decrypt C: if it already started, so disk states stay comparable
reg add "HKLM\SYSTEM\CurrentControlSet\Control\BitLocker" /v PreventDeviceEncryption /t REG_DWORD /d 1 /f | Out-Null
try {
  if ((Get-BitLockerVolume -MountPoint C: -ErrorAction Stop).VolumeStatus -ne "FullyDecrypted") {
    Disable-BitLocker -MountPoint C:
  }
} catch {}

# Disable VBS/HVCI (a GUEST security feature — does not affect host isolation). It runs a nested
# hypervisor inside the guest, which is very slow on an already nested VM; off makes runtime VMs
# much snappier. Also set in the answer file so it applies from first boot.
reg add "HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard" /v EnableVirtualizationBasedSecurity /t REG_DWORD /d 0 /f | Out-Null
reg add "HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity" /v Enabled /t REG_DWORD /d 0 /f | Out-Null
bcdedit /set hypervisorlaunchtype off | Out-Null

# Never sleep; show the console at the login screen
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0

# Clear any automatic logon so the finished image shows a login screen and does not try to
# auto-log-in with a build-time password (Strata resets the account password per VM).
$winlogon = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
Set-ItemProperty $winlogon -Name AutoAdminLogon -Value "0" -ErrorAction SilentlyContinue
Remove-ItemProperty $winlogon -Name DefaultPassword -ErrorAction SilentlyContinue

Stop-Transcript | Out-Null
# Hand back a clean, powered-off image
Stop-Computer -Force
