#!/usr/bin/env python3
"""
Patch main.py to set scan_limit=100 on new user registration.
Run from ~/Desktop/lister_web:
    python3 patch_register.py
"""

FILE = "main.py"

with open(FILE, "r") as f:
    c = f.read()

old = '''        res = supabase.table("businesses").insert({
            "name": business_name,
            "email": email,
            "password_hash": password_hash
        }).execute()'''

new = '''        res = supabase.table("businesses").insert({
            "name": business_name,
            "email": email,
            "password_hash": password_hash,
            "scan_limit": 100,
            "scan_count": 0,
            "onboarded": False
        }).execute()'''

n = c.count(old)
c = c.replace(old, new)
print(f"{'✓' if n else '✗'} Set scan_limit=100 on registration: {n}x")

with open(FILE, "w") as f:
    f.write(c)
