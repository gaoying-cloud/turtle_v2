import urllib.request
import json

def api(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/vnd.github.v3+json"})
    return json.loads(urllib.request.urlopen(req).read())

# Get repo info
repo = api("https://api.github.com/repos/esengine/DeepSeek-Reasonix")
print(f"Repo: {repo['full_name']}")
print(f"Language: {repo.get('language')}")
print(f"Description: {repo.get('description')}")
print(f"Topics: {repo.get('topics')}")
print(f"Default branch: {repo['default_branch']}")
print(f"Size: {repo['size']} KB")
print()
# Check if releases exist
try:
    releases = api("https://api.github.com/repos/esengine/DeepSeek-Reasonix/releases?per_page=3")
    print(f"Releases: {len(releases)}")
    for r in releases[:3]:
        print(f"  - {r['tag_name']}: {r.get('name','')} ({len(r.get('assets',[]))} assets)")
except:
    print("No releases or rate limited")