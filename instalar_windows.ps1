# Instala "Consumo de Red" en Windows: menú Inicio, escritorio y registro
# automático en segundo plano al iniciar sesión. No necesita administrador.
$ErrorActionPreference = "Stop"
$dest = Join-Path $env:LOCALAPPDATA "ConsumoRed"

if (Get-Command py -ErrorAction SilentlyContinue) {
    $pyExe = (& py -3 -c "import sys; print(sys.executable)").Trim()
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pyExe = (& python -c "import sys; print(sys.executable)").Trim()
} else {
    Write-Host "Falta Python 3. Instálalo desde https://www.python.org (marca 'Add python.exe to PATH')."
    exit 1
}
$pyw = Join-Path (Split-Path $pyExe) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $pyExe }

# psutil lee el consumo por adaptador; sin él la app usa PowerShell (más lento)
& $pyExe -m pip install --user --quiet --disable-pip-version-check psutil

New-Item -ItemType Directory -Force $dest | Out-Null
Copy-Item (Join-Path $PSScriptRoot "consumo_red.py") $dest -Force
$script = Join-Path $dest "consumo_red.py"

$shell = New-Object -ComObject WScript.Shell
function Acceso($ruta, $argumentos) {
    $l = $shell.CreateShortcut($ruta)
    $l.TargetPath = $pyw
    $l.Arguments = $argumentos
    $l.WorkingDirectory = $dest
    $l.Save()
}
Acceso (Join-Path ([Environment]::GetFolderPath("Programs")) "Consumo de Red.lnk") "`"$script`""
Acceso (Join-Path ([Environment]::GetFolderPath("Desktop")) "Consumo de Red.lnk") "`"$script`""
Acceso (Join-Path ([Environment]::GetFolderPath("Startup")) "Consumo de Red (registro).lnk") "`"$script`" --registrar"

# Reinicia el registro con la versión recién copiada
Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" |
    Where-Object { $_.CommandLine -like "*consumo_red.py*--registrar*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Process $pyw -ArgumentList "`"$script`"", "--registrar" -WindowStyle Hidden

Write-Host "Listo. Busca 'Consumo de Red' en el menú Inicio o en tu escritorio."
