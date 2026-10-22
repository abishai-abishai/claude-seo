$ErrorActionPreference = "Stop"
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw "Python 3 required" }
$SkillDir = Join-Path $HOME ".claude/skills"
$AgentsDir = Join-Path $HOME ".claude/agents"
$ConfigJson = Join-Path $HOME ".config/claude-seo/naver.json"
if (-not (Test-Path (Join-Path $SkillDir "seo"))) { throw "claude-seo not installed" }
$SourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SkillTarget = Join-Path $SkillDir "seo-naver"
New-Item -ItemType Directory -Path $SkillTarget -Force | Out-Null
Copy-Item (Join-Path $SourceDir "skills/seo-naver/SKILL.md") (Join-Path $SkillTarget "SKILL.md") -Force
New-Item -ItemType Directory -Path $AgentsDir -Force | Out-Null
Copy-Item (Join-Path $SourceDir "agents/seo-naver.md") (Join-Path $AgentsDir "seo-naver.md") -Force
# naver.json holds no secret. It is written only when it does not exist yet,
# so questions and brands already in it are never overwritten.
if (Test-Path $ConfigJson) {
    Write-Host "Kept existing $ConfigJson"
} else {
    $Site = Read-Host "Site URL (e.g. https://www.example.com)"
    if (-not $Site) { throw "Site URL required" }
    $py = @"
import json, os, sys, tempfile
path = sys.argv[1]
site = os.environ['NAVER_SITE']
data = {
    'site': site,
    'ai_briefing': {'devices': ['mo', 'pc'], 'queries': [], 'brands': []},
    'keywords': {'list': [], 'candidates': [], 'max': 20,
                 'product_patterns': ['goods_view', 'goodsNo=', '/product']},
    'search_advisor': {'export_dir': '~/Downloads', 'max_age_days': 7},
    'crawl_check': {'sample_size': 50},
}
directory = os.path.dirname(path)
os.makedirs(directory, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=directory, prefix='.naver.', suffix='.json')
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
print('Wrote ' + path)
"@
    $env:NAVER_SITE = $Site
    try {
        $py | python - $ConfigJson
        if ($LASTEXITCODE -ne 0) { throw "Nothing was saved (python exited $LASTEXITCODE). See the message above." }
    } finally { Remove-Item Env:NAVER_SITE -ErrorAction SilentlyContinue }
}
Write-Host "Done. Add AI Briefing questions and brands under ai_briefing in $ConfigJson (docs/NAVER.md section 10)."
