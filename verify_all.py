"""Verify all core endpoints are working."""
import httpx

BASE = "http://localhost:8000"
client = httpx.Client(timeout=30)

print("=" * 50)
print("GFD Backend Verification")
print("=" * 50)

# 1. Health
r = client.get(f"{BASE}/health")
print(f"\n1. Health Check: {'✅' if r.status_code == 200 else '❌'} ({r.status_code})")

# 2. API Docs
r = client.get(f"{BASE}/docs")
print(f"2. Swagger Docs: {'✅' if r.status_code == 200 else '❌'} ({r.status_code})")

# 3. Register
r = client.post(f"{BASE}/api/v1/auth/register", json={
    "email": "verify@gfd.dev",
    "username": "verifyuser",
    "full_name": "Verify User",
    "password": "testpass123",
    "role": "developer",
})
print(f"3. Register: {'✅' if r.status_code == 201 else '⚠️ ' + str(r.status_code)} ", end="")
if r.status_code == 201:
    tokens = r.json()
    print(f"(user_id: {tokens['user_id'][:8]}...)")
elif r.status_code == 409:
    print("(already exists — OK)")
    # Login instead
    r = client.post(f"{BASE}/api/v1/auth/login", json={"email": "verify@gfd.dev", "password": "testpass123"})
    tokens = r.json()
else:
    print(f"({r.text[:100]})")
    tokens = None

if not tokens or "access_token" not in tokens:
    print("❌ Cannot continue without auth token")
    exit(1)

TOKEN = tokens["access_token"]
headers = {"Authorization": f"Bearer {TOKEN}"}

# 4. Login
r = client.post(f"{BASE}/api/v1/auth/login", json={"email": "verify@gfd.dev", "password": "testpass123"})
print(f"4. Login: {'✅' if r.status_code == 200 else '❌'} ({r.status_code})")

# 5. Get Me
r = client.get(f"{BASE}/api/v1/users/me", headers=headers)
print(f"5. Get Profile: {'✅' if r.status_code == 200 else '❌'} ({r.status_code})", end="")
if r.status_code == 200:
    print(f" (username: {r.json().get('username')})")
else:
    print(f" ({r.text[:100]})")

# 6. Create Post
r = client.post(f"{BASE}/api/v1/feed/", json={"content": "Hello GFD! Testing the API 🚀", "post_type": "text"}, headers=headers)
print(f"6. Create Post: {'✅' if r.status_code == 201 else '❌'} ({r.status_code})", end="")
if r.status_code == 201:
    post_id = r.json()["id"]
    print(f" (post_id: {post_id[:8]}...)")
else:
    post_id = None
    print(f" ({r.text[:100]})")

# 7. Get Feed
r = client.get(f"{BASE}/api/v1/feed/", headers=headers)
print(f"7. Get Feed: {'✅' if r.status_code == 200 else '❌'} ({r.status_code})")

# 8. Like Post
if post_id:
    r = client.post(f"{BASE}/api/v1/feed/{post_id}/like", headers=headers)
    print(f"8. Like Post: {'✅' if r.status_code == 201 else '❌'} ({r.status_code})")

# 9. Get Notifications
r = client.get(f"{BASE}/api/v1/notifications/", headers=headers)
print(f"9. Notifications: {'✅' if r.status_code == 200 else '❌'} ({r.status_code})")

# 10. List Projects
r = client.get(f"{BASE}/api/v1/projects/", headers=headers)
print(f"10. List Projects: {'✅' if r.status_code == 200 else '❌'} ({r.status_code})")

# 11. Get Conversations
r = client.get(f"{BASE}/api/v1/messages/conversations", headers=headers)
print(f"11. Messaging: {'✅' if r.status_code == 200 else '❌'} ({r.status_code})")

# 12. Refresh Token
r = client.post(f"{BASE}/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
print(f"12. Refresh Token: {'✅' if r.status_code == 200 else '❌'} ({r.status_code})")

# 13. Protected route without token
r = client.get(f"{BASE}/api/v1/users/me")
print(f"13. Auth Guard (no token): {'✅' if r.status_code == 403 else '❌'} ({r.status_code})")

print("\n" + "=" * 50)
passed = sum(1 for line in open(__file__).readlines() if "✅" in line)
print("Verification complete!")
print("=" * 50)
