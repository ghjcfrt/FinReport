param([int]$Port = 8765)
$health = Invoke-RestMethod "http://127.0.0.1:$Port/health"
$health | ConvertTo-Json -Depth 6
if ($health.status -ne "ready") { exit 1 }
if ($health.model -ne "baidu/Unlimited-OCR" -or $health.backend -ne "transformers-cuda-nf4" -or $health.model_revision -ne "8249d20b2822a7d8a4c22c8d8c9069b5ac36bda3" -or $health.model_sha256 -ne "2bc48a7a110061ea58fff65d3169367eebe3aee371ca6968dc2219c1b2855fc6") { throw "OCR health identity mismatch" }
