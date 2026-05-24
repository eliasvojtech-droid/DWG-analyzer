@echo off
chcp 1250 >nul
echo.
echo  DWG Block Analyzer - sestaveni .exe
echo  =====================================
echo.

python -m pip install flask ezdxf pyinstaller

echo.
echo Sestavuji .exe...
python -m PyInstaller --onefile --windowed --name "DWG Block Analyzer" --add-data "index.html;." app.py

echo.
if exist "dist\DWG Block Analyzer.exe" (
    echo Hotovo! Soubor je v: dist\DWG Block Analyzer.exe
    explorer dist
) else (
    echo CHYBA: sestaveni selhalo
)
pause
