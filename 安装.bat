@echo off
REM ============================================================================
REM  dsh-jm 一键安装（Windows）
REM
REM  用法：把整个插件文件夹放好，双击这个 bat 就行。
REM  它会：① 找到 DSH 自带的 dsh.cmd  ② 把本插件 link 进 desktop profile
REM       ③ 检查 vendor 依赖是否完整  ④ 提示你重启 DSH
REM
REM  为什么用 link 而不是 npm install：别人拿到的是源码文件夹，
REM  改一行代码立刻生效，不用重新打包。
REM ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM 去掉路径结尾的反斜杠 —— pnpm 的 link: 路径带尾斜杠会解析失败
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"

echo.
echo  ===========================================
echo   dsh-jm  安装
echo  ===========================================
echo.

REM ---- 找 DSH ----
set "DSH_CMD="
for %%D in (
  "D:\dsh\resources\runtime\cli\bin\dsh.cmd"
  "C:\Program Files\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd"
  "%LOCALAPPDATA%\Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd"
  "%ProgramFiles%\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd"
) do (
  if exist %%D set "DSH_CMD=%%~D"
)

if "%DSH_CMD%"=="" (
  echo  [!] 没找到 DSH 安装目录。
  echo.
  echo      请手动执行这一条：
  echo.
  echo      dsh plugin --profile desktop add "link:%HERE%"
  echo.
  pause
  exit /b 1
)

echo  [1/3] DSH: %DSH_CMD%
echo.
echo  [2/3] 装进 desktop profile ...
call "%DSH_CMD%" plugin --profile desktop add "link:%HERE%"
if errorlevel 1 (
  echo.
  echo  [!] 安装失败。常见原因：DSH 正开着且锁住了 profile 目录。
  echo      关掉 the agent runtime 再试一次。
  pause
  exit /b 1
)

echo.
echo  [3/3] 检查 vendor 依赖 ...
if not exist "%HERE%\vendor\jmcomic\__init__.py" (
  echo  [!] vendor 目录缺失，这份插件不完整。
  echo      从源码拷来的话先跑一次：
  echo        python -m pip install jmcomic --target "%HERE%\vendor"
  goto :vendor_done
)

REM vendor 里的 .pyd 是给某个具体 Python 版本编的（本包是 3.12）。
REM 版本不对时 Python 会报 ImportError: DLL load failed，那种错误看起来像
REM "插件坏了"，其实只是位数/版本不匹配，害人找半天。
set "PY="
for %%P in (
  "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
  "%USERPROFILE%\.dsh\dsh-runtimes\dsh-next-primary-runtime\dependencies\python\python.exe"
) do (
  if exist %%P set "PY=%%~P"
)

if "%PY%"=="" (
  echo  [!] 没找到 DSH 自带的 Python，跳过版本检查。
  echo      vendor 的 .pyd 是按 Python 3.12 编的；版本不同会报 DLL load failed。
) else (
  for /f "tokens=1,2 delims=." %%A in ('"%PY%" -c "import sys;print(str(sys.version_info[0])+chr(46)+str(sys.version_info[1]))" 2^>nul') do (
    set "PYMAJ=%%A"
    set "PYMIN=%%B"
  )
  if "!PYMAJ!"=="3" if "!PYMIN!"=="12" (
    echo       vendor 就位，Python !PYMAJ!.!PYMIN! 匹配。
  ) else (
    echo  [!] 本机 Python 是 !PYMAJ!.!PYMIN!，但 vendor 是按 3.12 编的。
    echo      Windows 下会报 ImportError: DLL load failed。
    echo      解决：装一个 3.12 的 Python，或自己重建 vendor：
    echo        python -m pip install jmcomic pillow pycryptodome curl_cffi pyyaml urllib3 --target "%HERE%\vendor"
  )
)
:vendor_done

echo.
echo  ===========================================
echo   装好了。重启 the agent runtime 生效。
echo   然后在聊天里发 jm350234 试试。
echo  ===========================================
echo.
pause
