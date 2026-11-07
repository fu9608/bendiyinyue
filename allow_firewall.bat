@echo off
chcp 65001 >nul
setlocal

net session >nul 2>&1
if errorlevel 1 (
  echo 请右键此文件，选择“以管理员身份运行”。
  pause
  exit /b 1
)

netsh advfirewall firewall delete rule name="ESP32 LAN Music 5000" >nul 2>&1
netsh advfirewall firewall add rule name="ESP32 LAN Music 5000" dir=in action=allow protocol=TCP localport=5000 profile=any
if errorlevel 1 (
  echo 防火墙规则添加失败。
  pause
  exit /b 1
)

echo 已放行 TCP 5000 入站连接。
pause
