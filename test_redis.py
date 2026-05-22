import redis

r = redis.from_url("rediss://default:gQAAAAAAAgveAAIgcDJiNjE1YmQzYmE1YWM0MjRlYmQyYmExNzg0OWQ4ZDFjNw@adapted-jaybird-134110.upstash.io:6379")

# Test set/get
r.set("gfd_test", "hello from GFD backend!")
value = r.get("gfd_test")
print(f"Redis connected! Value: {value.decode()}")
r.delete("gfd_test")
print("Redis test passed!")
