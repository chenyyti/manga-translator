$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'resolve-python.ps1')
$pythonExe = Get-ProjectPython

& $pythonExe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
& $pythonExe -m pip install -e "$repoRoot\backend[detection]"
& $pythonExe -c "import torch, ultralytics; print('Ultralytics', ultralytics.__version__); print('CUDA', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '不可用')"
