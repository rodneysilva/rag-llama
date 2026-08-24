# ═══════════════════════════════════════════════════════════════
# RagAroy — instalação em UM comando (DevOps)
# Uso: powershell -File setup.ps1
#      powershell -File setup.ps1 -Modelos chat,embed   (baixa modelos)
#      powershell -File setup.ps1 -Modelos tudo
# ═══════════════════════════════════════════════════════════════
# Sobe: Qdrant + RabbitMQ (management) + Redis + API (container).
# Os MODELOS rodam no host: python servicos_llm.py (llama-server GPU).
param([string]$Modelos = "")
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

Write-Output "== RagAroy setup =="

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker nao encontrado — instale o Docker Desktop primeiro."
}

# 1) .env (na primeira vez, a partir do exemplo)
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Output "!! .env criado do exemplo — EDITE: AUTH_ADMIN_USER/AUTH_ADMIN_PASS"
    Write-Output "   (o usuario inicial nasce no primeiro boot da API)"
}

# 2) arquivos de estado que o compose monta como arquivo (devem existir)
if (-not (Test-Path "users.json")) {
    Set-Content -LiteralPath "users.json" -Value '{"usuarios": {}}' -Encoding UTF8
}
if (-not (Test-Path "usuarios_permitidos.txt")) {
    Set-Content -LiteralPath "usuarios_permitidos.txt" -Value "# Nomes permitidos a criar login (um por linha - adicione os seus)" -Encoding UTF8
}
foreach ($d in @("sessions", "saidas", "logs", "datasets")) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
}

# 3) build + up
Write-Output "== docker compose up -d --build =="
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { throw "compose falhou" }

# 4) espera a API ficar saudavel (healthcheck do container)
Write-Output "== aguardando a API (healthcheck) =="
$ok = $false
foreach ($i in 1..30) {
    Start-Sleep 4
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:8000/api/status" -UseBasicParsing -TimeoutSec 4
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch {}
    Write-Host -NoNewline "."
}
Write-Output ""
if (-not $ok) { Write-Output "!! API ainda subindo — confira: docker compose ps / docker compose logs api" }

Write-Output ""
if ($Modelos) {
    Write-Output "== baixando modelos ($Modelos) =="
    $vp = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
    & $vp -X utf8 scripts\baixar_modelos.py --tipos $Modelos
}

Write-Output "== RagAroy no ar =="
Write-Output "   app           : http://localhost:8000  (ou https://<sub>.<dominio>)"
Write-Output "   qdrant        : http://localhost:6333/dashboard"
Write-Output "   rabbit mgmt   : http://localhost:15672  (usuario/senha: RABBIT_USER/RABBIT_PASS do .env; dev default 'ragaroy')"
Write-Output ""
Write-Output "   MODELOS (host): python servicos_llm.py  <- chat/embedding para o container usar"
Write-Output "   Estudio/troca de modelo: modo host (python -m uvicorn api.app:app --host 0.0.0.0 --port 8000)"
