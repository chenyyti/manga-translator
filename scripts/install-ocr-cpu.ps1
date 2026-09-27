$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'resolve-python.ps1')
$pythonExe = Get-ProjectPython

& $pythonExe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
& $pythonExe -m pip install -e "$repoRoot\backend[ocr]"
& $pythonExe -m pip install onnxruntime
& $pythonExe -c "import manga_ocr, onnxruntime, paddleocr, torch; print('PyTorch', torch.__version__); print('ONNX providers', onnxruntime.get_available_providers())"
