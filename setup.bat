@echo off
chcp 65001 >nul
echo.
echo ============================================
echo  🎬 Movie Downloader - Website Setup
echo ============================================
echo.

echo [1/3] Checking dependencies...
pip install waitress >nul 2>&1
echo      waitress: OK

echo.
echo [2/3] Opening firewall for port 80...
netsh advfirewall firewall show rule name="Movie Downloader Web 80" >nul 2>&1
if %errorlevel%==0 (
    echo      Firewall rule already exists
) else (
    netsh advfirewall firewall add rule name="Movie Downloader Web 80" dir=in action=allow protocol=tcp localport=80
    echo      Firewall rule added
)

echo.
echo [3/3] Creating site_config.json if needed...
if not exist site_config.json (
    echo {"port": 80, "username": "admin", "password": "moviedl2024", "duckdns_token": "", "duckdns_domain": ""} > site_config.json
    echo      Created config (edit to change password)
) else (
    echo      Config exists
)

echo.
echo ============================================
echo  ✅ Setup complete!
echo.
echo  TO START:  python server_production.py
echo  TO STOP:   Press CTRL+C
echo.
echo  CONFIG:    site_config.json
echo    - Change password before sharing!
echo    - Get DuckDNS token from duckdns.org
echo ============================================
echo.
pause
