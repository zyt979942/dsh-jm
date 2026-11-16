@echo off
chcp 65001 >nul 2>&1
REM ============================================================================
REM  dsh-jm 一键安装（Windows）
REM
REM  为什么不用 `dsh plugin add`：桌面版（Electron）不带 dsh CLI，
REM  这个命令只存在于 npm 全局安装的版本里。老版本这个 bat 找的就是 CLI，
REM  在桌面版上必然报「没找到 DSH」—— 现在改成直接改 profile 配置。
REM
REM  具体逻辑在 install.mjs 里（带备份、带校验、能自愈）。
REM ============================================================================
setlocal
cd /d "%~dp0"

echo.
echo  ===========================================
echo   dsh-jm  安装
echo  ===========================================
echo.

REM ---- 找 Node：优先用 DSH 自带的，其次系统 PATH ----
set "NODE="
for %%P in (
  "%USERPROFILE%\.dsh\dsh-runtimes\dsh-next-primary-runtime\dependencies\node\bin\node.exe"
  "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"
) do (
  if exist %%P if not defined NODE set "NODE=%%~P"
)
if not defined NODE (
  where node >nul 2>&1 && set "NODE=node"
)

if not defined NODE (
  echo  [!] 没找到 Node.js。
  echo.
  echo      DSH 桌面版自带一份，通常在：
  echo        %%USERPROFILE%%\.dsh\dsh-runtimes\dsh-next-primary-runtime\dependencies\node\bin\node.exe
  echo.
  echo      找不到的话，装一个 Node.js 22+ 再来：https://nodejs.org/
  pause
  exit /b 1
)

echo   用 Node: %NODE%
echo.
call "%NODE%" install.mjs
if errorlevel 1 (
  echo.
  echo  [!] 安装没成功。上面有具体原因。
  pause
  exit /b 1
)

pause