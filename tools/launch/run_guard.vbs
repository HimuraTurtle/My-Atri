' ATRI 守卫无窗宿主：计划任务与手动启动共用。
' 2026-11-21 复查发现：任务动作直接用 powershell.exe -File 运行守卫时，
' InteractiveToken 任务会在用户会话挂一个控制台窗口，窗口被关闭即
' CTRL_CLOSE（LastResult 0xC000013A），守卫带着监护能力一起死。
' wscript 宿主没有控制台可关，停止守卫只能走 watchdog.ps1 -Stop。
' 路径由脚本自身位置推导，仓库内无硬编码绝对路径。

Set fso = CreateObject("Scripting.FileSystemObject")
launchDir = fso.GetParentFolderName(WScript.ScriptFullName)
root = fso.GetParentFolderName(fso.GetParentFolderName(launchDir))
watchdog = launchDir & "\watchdog.ps1"

Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = root
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & watchdog & """ -Guard", 0, False
