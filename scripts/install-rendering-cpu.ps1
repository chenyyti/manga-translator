$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'resolve-python.ps1')
$pythonExe = Get-ProjectPython
& $pythonExe -m pip install -e "$repoRoot\backend[rendering]"
& $pythonExe -m pip install torch --index-url https://download.pytorch.org/whl/cpu
& $pythonExe -c "import cv2, numpy; print('OpenCV', cv2.__version__, 'NumPy', numpy.__version__)"
