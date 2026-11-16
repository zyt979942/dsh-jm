@echo off
REM ============================================================================
REM  dsh-jm 发布到 npm（别人就能在市场里搜到 / 一条命令装）
REM
REM  为什么是 npm：DSH 的插件市场（dshmarket）安装插件走的就是 npm registry，
REM  它的 catalog-npm.js 里写得很清楚 —— 插件经由 npm 到达用户机器。
REM  所以「别人能搜到并一键装」= 这个包必须在 npm 上。
REM
REM  第一次要先登录（会开浏览器）：
REM      npm adduser
REM  或者用 token：
REM      npm config set //registry.npmjs.org/:_authToken=你的token
REM ============================================================================
setlocal
cd /d "%~dp0"

echo.
echo  ===========================================
echo   dsh-jm  发布到 npm
echo  ===========================================
echo.

REM ---- 检查登录 ----
for /f "delims=" %%u in ('npm whoami 2^>nul') do set "NPMUSER=%%u"
if "%NPMUSER%"=="" (
  echo  [!] 还没登录 npm。
  echo.
  echo      先跑一次：  npm adduser
  echo      （没有账号先去 https://www.npmjs.com/signup 注册，免费）
  echo.
  pause
  exit /b 1
)
echo  [1/4] 已登录为: %NPMUSER%

REM ---- 检查 package.json 里的占位 ----
findstr /C:"USERNAME" package.json >nul 2>&1
if not errorlevel 1 (
  echo.
  echo  [!] package.json 里的 repository/homepage 还是占位 USERNAME。
  echo      改成你自己的 GitHub 地址（没有仓库也可以先删掉这两段），再跑一次。
  echo.
  pause
  exit /b 1
)
echo  [2/4] package.json 检查通过

REM ---- 检查 vendor ----
if not exist "vendor\jmcomic\__init__.py" (
  echo.
  echo  [!] vendor 缺失，这份包不完整（别人装了会跑不起来）。
  echo      先补上：
  echo        python -m pip install jmcomic --target "%~dp0vendor"
  echo.
  pause
  exit /b 1
)
echo  [3/4] vendor 就位

REM ---- 发布 ----
echo  [4/4] 发布 ...
echo.
call npm publish
if errorlevel 1 (
  echo.
  echo  [!] 发布失败。常见原因：
  echo      - 版本号已存在（改 package.json 的 version 再试）
  echo      - 包名被占用（改 name，比如 dsh-jm-download）
  echo      - 没登录 / token 过期
  echo.
  pause
  exit /b 1
)

echo.
echo  ===========================================
echo   发布成功。
echo   别人现在可以：
echo     dsh plugin --profile desktop add dsh-jm
echo   或者等市场收录后直接在插件市场里搜。
echo  ===========================================
echo.
pause
