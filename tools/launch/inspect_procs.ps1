$procs = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -match 'watchdog\.ps1' }
foreach ($p in $procs) {
    $start = (Get-Process -Id $p.ProcessId).StartTime
    Write-Output ("pid=" + $p.ProcessId + " start=" + $start + " cmd=" + $p.CommandLine)
}
Write-Output "--- python processes:"
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | ForEach-Object {
    $start = (Get-Process -Id $_.ProcessId).StartTime
    Write-Output ("pid=" + $_.ProcessId + " start=" + $start + " cmd=" + $_.CommandLine)
}
