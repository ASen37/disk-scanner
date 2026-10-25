@echo off
chcp 65001 >nul
title 磁盘扫描启动器
setlocal enabledelayedexpansion

echo ============================================
echo     1. 图形化界面启动
echo     2. 命令行参数启动
echo ============================================

set source_file="./disk_scan.py"
set /p id=选择启动方式：

if %id%==1 (
	if not exist "%source_file%" (
		echo [错误] 源文件不存在：%source_file%
		goto :end
	) else (
		:: 用 pythonw + start 独立启动 GUI：无黑窗，且不随 cmd 窗口关闭而关闭
		start "" pythonw %source_file%
		goto :eof
	)
) else if %id%==2 (
	echo [提示] 请输入文件夹路径（支持带或不带引号）
	set /p target_dir=输入文件夹路径：
	
	:: 去除所有引号
	set "target_dir=!target_dir:"=!"
	
	:: 处理路径：如果以反斜杠结尾，先去掉
	if "!target_dir:~-1!"=="\" set "target_dir=!target_dir:~0,-1!"
	
	:: 如果路径为空，提示错误
	if "!target_dir!"=="" (
		echo [错误] 路径不能为空！
		goto :end
	)
	
	:: 检查是否为盘符（如 D:）
	set "is_drive="
	echo !target_dir!|findstr /r "^[A-Za-z]:$" >nul
	if not errorlevel 1 set "is_drive=1"
	
	:: 如果是盘符，添加反斜杠以便于检查
	if defined is_drive (
		set "check_dir=!target_dir!\"
	) else (
		set "check_dir=!target_dir!"
	)
	
	echo [调试] 检查路径: !check_dir!
	
	if not exist "!check_dir!" (
		echo [错误] 目标路径不存在：!check_dir!
		goto :end
	) else (
		echo [信息] 正在扫描: !check_dir!
		::python %source_file% --list "!target_dir!"
		python %source_file% --list "!check_dir!"
		goto :end
	)
) else (
	echo [错误] 无效选项！
	goto :end
)

:end
pause
exit /b