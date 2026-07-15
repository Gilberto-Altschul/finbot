import hashlib

client_id = "2e691bbe-5ffa-48a0-b545-6775b4645b82"
client_secret = "_Z5eO9R8RyApzN-XeeSDWNMvOW7vpWvP2TsoCs3J2jc"

print(f"clientId len={len(client_id)} sha256={hashlib.sha256(client_id.encode()).hexdigest()}")
print(f"clientSecret len={len(client_secret)} sha256={hashlib.sha256(client_secret.encode()).hexdigest()}")