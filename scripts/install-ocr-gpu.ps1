$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'resolve-python.ps1')
$pythonExe = Get-ProjectPython

& $pythonExe -m pip install -e "$repoRoot\backend[ocr]"
& $pythonExe -m pip install 'onnxruntime-gpu==1.26.0'
& $pythonExe -c "import manga_ocr, onnxruntime, paddleocr, torch; print('PyTorch CUDA', torch.cuda.is_available()); print('ONNX providers', onnxruntime.get_available_providers())"
