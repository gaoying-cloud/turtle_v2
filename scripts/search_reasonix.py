import urllib.request, json

url = "https://api.github.com/search/repositories?q=reasonix&sort=stars&order=desc"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
data = json.loads(urllib.request.urlopen(req).read())
items = data.get("items", [])

print(f"Found {len(items)} repositories:\n")
for i, r in enumerate(items[:15]):
    desc = (r.get("description") or "")[:80]
    print(f'{i+1}. {r["full_name"]}')
    print(f'   URL: {r["html_url"]}')
    print(f'   Stars: {r["stargazers_count"]}  Language: {r.get("language","N/A")}')
    print(f'   Desc: {desc}')
    print()