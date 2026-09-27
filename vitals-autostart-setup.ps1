# Registers a per-user logon task that boots the WSL distro, which in turn
# starts docker.service (already enabled) and the vitals container.
# Re-running this script is safe: it replaces any previous version.
$ErrorActionPreference = 'Stop'

$TaskName = 'Vitals Monitor autostart (WSL)'
$Distro   = 'Ubuntu-26.04'
$Script   = '/home/kobi/vitalsigns/autostart.sh'
$Me       = "$env:USERDOMAIN\$env:USERNAME"

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction -Execute 'wsl.exe' `
          -Argument "-d $Distro -u root $Script"

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $Me
# WSL and the network stack are not necessarily ready the instant the desktop
# appears; the script itself also waits for dockerd.
$trigger.Delay = 'PT20S'

$settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -Hidden -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit ([TimeSpan]::FromMinutes(10)) `
            -RestartCount 2 -RestartInterval ([TimeSpan]::FromMinutes(1))

$principal = New-ScheduledTaskPrincipal -UserId $Me -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description 'Boots the Ubuntu WSL distro at logon so the Vitals Monitor container (localhost:8080) comes up automatically.' | Out-Null

Write-Output "REGISTERED: $TaskName"
Get-ScheduledTask -TaskName $TaskName |
    Select-Object TaskName, State, @{n='Action';e={$_.Actions[0].Execute + ' ' + $_.Actions[0].Arguments}} |
    Format-List
