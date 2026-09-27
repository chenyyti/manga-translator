$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'resolve-python.ps1')
$pythonExe = Get-ProjectPython

& $pythonExe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
& $pythonExe -m pip install -e "$repoRoot\backend[detection]"
& $pythonExe -c "import torch, ultralytics; print('Ultralytics', ultralytics.__version__); print('PyTorch', torch.__version__, 'CPU')"
