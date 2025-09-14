import requests

resp = requests.get(
    "http://127.0.0.1:8000/search",
    params={"q": "BLACK NIKE SHOES", "limit": 5}
)

print(resp.status_code)
print(resp.json())
