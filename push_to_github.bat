@echo off
cd /d "%~dp0"
echo ========================================================
echo Pushing to GitHub: IceAkaradach/PDFTODXF ...
echo ========================================================
echo.
git push -u origin main
echo.
echo ========================================================
echo Finished! If it says success, you can now deploy on Vercel.
echo ========================================================
pause
