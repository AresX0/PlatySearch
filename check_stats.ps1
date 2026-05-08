$ErrorActionPreference = "Stop"
$tok = az account get-access-token --resource https://management.azure.com/ --query accessToken -o tsv
$pyCmd = @'
import sqlite3, os, json
p = '/home/data/platysearch.db'
out = {'exists': os.path.exists(p)}
if out['exists']:
    out['size_mb'] = round(os.path.getsize(p)/1048576, 1)
    c = sqlite3.connect(p)
    out['pages'] = c.execute('SELECT COUNT(*) FROM pages').fetchone()[0]
    out['queue_total'] = c.execute('SELECT COUNT(*) FROM crawl_queue').fetchone()[0]
    try:
        out['queue_pending'] = c.execute("SELECT COUNT(*) FROM crawl_queue WHERE status='pending' OR status=0").fetchone()[0]
    except Exception as e:
        out['queue_pending_err'] = str(e)
    out['domains'] = c.execute('SELECT COUNT(DISTINCT domain) FROM pages').fetchone()[0]
    try:
        out['postings'] = c.execute('SELECT COUNT(*) FROM postings').fetchone()[0]
    except Exception as e:
        out['postings_err'] = str(e)
    out['top_domains'] = c.execute('SELECT domain, COUNT(*) AS n FROM pages GROUP BY domain ORDER BY n DESC LIMIT 15').fetchall()
    try:
        out['recent_pages'] = c.execute("SELECT COUNT(*) FROM pages WHERE crawled_at > datetime('now','-1 day')").fetchone()[0]
        out['last_24h_by_hour'] = c.execute("SELECT strftime('%Y-%m-%d %H',crawled_at) h, COUNT(*) FROM pages WHERE crawled_at > datetime('now','-1 day') GROUP BY h ORDER BY h").fetchall()
    except Exception as e:
        out['recent_err'] = str(e)
print(json.dumps(out, default=str))
'@
$body = @{ command = "python3 -c `"$($pyCmd -replace '"','\"' -replace "`r`n",' ' -replace "`n",' ')`""; dir = "/home" } | ConvertTo-Json -Depth 5
$r = Invoke-RestMethod -Uri "https://platysearch-app.scm.azurewebsites.net/api/command" -Method Post -Headers @{Authorization="Bearer $tok"} -ContentType "application/json" -Body $body -TimeoutSec 600
$r | ConvertTo-Json -Depth 5
