import httpx

r = httpx.post('https://gfd-backend.onrender.com/api/v1/auth/login', json={'email':'admin@gfd.dev','password':'admin123!'}, timeout=30)
token = r.json()['access_token']

r2 = httpx.get('https://gfd-backend.onrender.com/api/v1/feed/?feed_type=explore&limit=5', headers={'Authorization': f'Bearer {token}'}, timeout=30, follow_redirects=True)
print("Status:", r2.status_code)
print("Response:", r2.text[:1000])
