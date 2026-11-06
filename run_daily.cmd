@echo off
rem 每日无人值守运行：由 Windows 计划任务调用，不要手动双击（会重复签到）
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist logs mkdir logs
echo.>> logs\scheduler.log
echo [%date% %time%] ===== 开始 =====>> logs\scheduler.log
"C:\Users\Admin\AppData\Local\Programs\Python\Python313\python.exe" -u run_local.py --auto >> logs\scheduler.log 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] ===== 结束 退出码=%RC% =====>> logs\scheduler.log
exit /b %RC%
