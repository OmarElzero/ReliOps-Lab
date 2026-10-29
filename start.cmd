@echo off
rem Double-click to start ReliOps Lab inside WSL (git credentials and Docker live there).
rem Edit the folder below if you cloned the Lab somewhere else inside WSL.
wsl.exe -e bash -lc "cd ~/reliops-lab && bash start.sh"
pause
