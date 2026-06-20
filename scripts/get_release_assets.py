import urllib.request
import json

req = urllib.request.Request(
    "https://api.github.com/repos/esengine/DeepSeek-Reasonix/releases/tags/v1.9.1",
    headers={"User-Agent": "Mozilla/5.0", "Accept": "application/vnd.github.v3+json"}
)
release = json.loads(urllib.request.urlopen(req).read())
print(f"Tag: {release['tag_name']}")
print(f"Name: {release.get('name','')}")
print(f"Assets ({len(release.get('assets',[]))}):\n")
for a in release.get("assets", []):
    size_mb = a['size'] / (1024*1024)
    print(f"  - {a['name']} ({size_mb:.1f} MB)")
    print(f"    URL: {a['browser_download_url']}")
    print()
