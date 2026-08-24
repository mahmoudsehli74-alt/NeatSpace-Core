"""Token envelope encryption — WP6 contract (implemented next).

AES-256-GCM envelope encryption for Pinterest OAuth refresh tokens at rest:
Mongo stores only {ciphertext, iv, auth_tag, key_version, algo}. The master
key lives exclusively in environment/GitHub Secrets (TOKEN_MASTER_KEY, 32-byte
hex). Key rotation = new key_version + re-encrypt sweep. Wrong-key or
tampered-ciphertext reads must raise — never return garbage credentials.
"""
