param(
    [string]$TaskName = "DocumentCrawler",
    [string]$ConfigPath = "$PSScriptRoot\config.yaml",
    [string]$At = "02:00",
    [string]$User,
    [securestring]$Password
)

$ErrorActionPreference = "Stop"

$installDir = Split-Path -Parent $PSCommandPath
$resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).Path

$argument = "-m doc_crawler --config `"$resolvedConfig`""
$action = New-ScheduledTaskAction -Execute "python" -Argument $argument -WorkingDirectory $installDir
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

if ($User) {
    if (-not $Password) {
        $Password = Read-Host -Prompt "Password for $User" -AsSecureString
    }
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Password)
    try {
        $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -User $User `
            -Password $plainPassword `
            -RunLevel Limited `
            -Force
    } finally {
        if ($bstr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
    }
} else {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force
}

Write-Host "Registered scheduled task '$TaskName'."
