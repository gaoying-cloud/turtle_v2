import urllib.request, json

url = "https://api.github.com/repos/esengine/DeepSeek-Reasonix/releases/tags/desktop-v1.9.1"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
data = json.loads(urllib.request.urlopen(req).read())
print(f"Release: {data['tag_name']} - {data.get('name','')}")
print(f"Assets: {len(data.get('assets',[]))}\n")
for a in data.get("assets", []):
    name = a["name"].lower()
    if "windows" in name or "win" in name or "exe" in name:
        size_mb = a["size"] / (1024*1024)
        print(f"  [{size_mb:.1f} MB] {a['name']}")
        print(f"    {a['browser_download_url']}\n")