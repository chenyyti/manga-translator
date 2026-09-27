$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'resolve-python.ps1')
$pythonExe = Get-ProjectPython
& $pythonExe -m pip install -e "$repoRoot\backend[rendering]"
& $pythonExe -m pip install torch --index-url https://download.pytorch.org/whl/cu124
& $pythonExe -c "import cv2, torch; print('OpenCV', cv2.__version__, 'Torch', torch.__version__, 'CUDA', torch.cuda.is_available())"
