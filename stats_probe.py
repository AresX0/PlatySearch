import sqlite3, os, json, sys
p = '/home/data/platysearch.db'
out = {'exists': os.path.exists(p)}
if out['exists']:
    out['size_mb'] = round(os.path.getsize(p)/1048576, 1)
    c = sqlite3.connect(p)
    out['pages'] = c.execute('SELECT COUNT(*) FROM pages').fetchone()[0]
    out['queue_total'] = c.execute('SELECT COUNT(*) FROM crawl_queue').fetchone()[0]
    try:
        out['queue_pending'] = c.execute("SELECT COUNT(*) FROM crawl_queue WHERE status=0").fetchone()[0]
    except Exception as e:
        out['queue_pending_err'] = str(e)
    try:
        out['queue_status_breakdown'] = c.execute("SELECT status, COUNT(*) FROM crawl_queue GROUP BY status").fetchall()
    except Exception as e:
        out['queue_status_err'] = str(e)
    out['domains'] = c.execute('SELECT COUNT(DISTINCT domain) FROM pages').fetchone()[0]
    try:
        out['postings'] = c.execute('SELECT COUNT(*) FROM postings').fetchone()[0]
    except Exception as e:
        out['postings_err'] = str(e)
    out['top_domains'] = c.execute('SELECT domain, COUNT(*) AS n FROM pages GROUP BY domain ORDER BY n DESC LIMIT 15').fetchall()
    try:
        out['pages_24h'] = c.execute("SELECT COUNT(*) FROM pages WHERE crawled_at > datetime('now','-1 day')").fetchone()[0]
        out['pages_1h'] = c.execute("SELECT COUNT(*) FROM pages WHERE crawled_at > datetime('now','-1 hour')").fetchone()[0]
        out['pages_by_hour_24h'] = c.execute("SELECT strftime('%Y-%m-%d %H:00',crawled_at) h, COUNT(*) FROM pages WHERE crawled_at > datetime('now','-1 day') GROUP BY h ORDER BY h").fetchall()
        out['latest_crawl'] = c.execute("SELECT MAX(crawled_at) FROM pages").fetchone()[0]
    except Exception as e:
        out['recent_err'] = str(e)
    try:
        out['ai_dist'] = c.execute("SELECT CASE WHEN ai_score IS NULL THEN 'null' WHEN ai_score < 0.3 THEN 'human' WHEN ai_score < 0.6 THEN 'mixed' ELSE 'ai' END AS bucket, COUNT(*) FROM pages GROUP BY bucket").fetchall()
    except Exception as e:
        out['ai_err'] = str(e)
print(json.dumps(out, default=str, indent=2))
