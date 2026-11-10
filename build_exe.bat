@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [OpenMacDisk] 正在生成可执行程序...

python -c "import PyInstaller" 2>nul || (
  echo [OpenMacDisk] 正在安装打包工具 PyInstaller...
  python -m pip install -q pyinstaller || goto :fail
)
python -c "import wx" 2>nul || (
  echo [OpenMacDisk] 正在安装界面库 wxPython...
  python -m pip install -q wxPython || goto :fail
)
python -c "import PIL" 2>nul || (
  echo [OpenMacDisk] 正在安装图标生成库 Pillow...
  python -m pip install -q Pillow || goto :fail
)
python -c "import Crypto" 2>nul || (
  echo [OpenMacDisk] 正在安装加密 DMG 支持 pycryptodome...
  python -m pip install -q pycryptodome || goto :fail
)
python "_build\make_brand.py" || goto :fail

python -m PyInstaller --noconfirm --clean --onefile --noconsole --name OpenMacDisk --distpath "%~dp0." --workpath "%~dp0_build\work" --specpath "%~dp0_build" --icon "%~dp0_build\icon.ico" --version-file "%~dp0_build\version_info.txt" --add-data "%~dp0_build\icon.ico;_build" --add-data "%~dp0_build\splash.png;_build" --add-data "%~dp0dmg_file.ico;_build" --hidden-import openmacdisk.image --hidden-import openmacdisk.udif --hidden-import openmacdisk.partition --hidden-import openmacdisk.hfs --hidden-import openmacdisk.icons --hidden-import openmacdisk.writer --hidden-import openmacdisk.inplace --hidden-import openmacdisk.shell --hidden-import openmacdisk.apfs --hidden-import openmacdisk.crypto_dmg --hidden-import openmacdisk.hfs_extents --hidden-import openmacdisk.lzfse --hidden-import Crypto.Cipher.AES --hidden-import Crypto.Cipher.DES3 desktop.py || goto :fail

echo.
echo [OpenMacDisk] 完成：OpenMacDisk.exe （双击即可运行）
exit /b 0

:fail
echo.
echo [OpenMacDisk] 打包失败，请查看上方错误信息。
pause
exit /b 1
