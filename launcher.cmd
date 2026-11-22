@echo off
chcp 65001 >nul 2>&1
rem ⚠️ 本文件是 UTF-8。中文必须全部排在第 2 行 chcp 之后:
rem    双击时控制台默认是 936(GBK),cmd 会按 GBK 读这个文件 ——
rem    排在 chcp 之前的 UTF-8 中文会变乱码,路径就找不着了。
rem    这就是 2026-10-13「双击一闪就没」的成因。
rem 家庭助手 · 服务启动器(双击即用)
rem
rem   直接双击        = 重启全部服务
rem   launcher.cmd status   看状态
rem   launcher.cmd logs wechat / logs wechat-mom   看某个微信账号的日志
rem   launcher.cmd stop     停止全部
rem   launcher.cmd task install   注册开机自启
rem
rem 它只是代你敲这条命令,没有隐藏逻辑:
rem   .venv\Scripts\python.exe -m la.ops.launcher <参数>
rem
rem 2026-10-13 加固:双击那条路以前不管成没成都返回 0,
rem 外面(桌面启动器)就分不出"起来了"和"没起来"。现在如实回传退出码。
setlocal
cd /d "%~dp0"

rem 控制台切到 UTF-8,否则 python 输出的中文在 GBK 代码页下是乱码
set "PYTHONIOENCODING=utf-8"

rem 双击进来(没有参数):重启 + 停一下让人看清结果
if not "%~1"=="" goto :with_args

".venv\Scripts\python.exe" -m la.ops.launcher restart
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo [!] 启动器返回 %RC% —— 上面就是原因,服务可能没起来
if "%RC%"=="0" echo 服务已在后台运行,关掉这个窗口不影响它们。
echo 看状态:launcher.cmd status    看日志:launcher.cmd logs
echo.
pause
exit /b %RC%

:with_args
".venv\Scripts\python.exe" -m la.ops.launcher %*
exit /b %ERRORLEVEL%
