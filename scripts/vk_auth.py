"""One-off VK ID authorization (OAuth 2.1 + PKCE) for a user token with wall/photos rights.

Step 1:  python3 scripts/vk_auth.py url     -> prints the authorization link, saves verifier to /opt/investor/.vk_pkce.json
Step 2:  python3 scripts/vk_auth.py token   -> asks (hidden) for the `code` and `device_id` from the redirect URL,
                                              writes tokens to /opt/investor/.vk_user.env (mode 600). Tokens are never printed.
"""
import base64, getpass, hashlib, json, os, secrets, sys, urllib.parse, urllib.request

CLIENT_ID = "54886356"
REDIRECT = "https://rmigas.ru/vk_id_redirect"
SCOPE = os.environ.get("VK_SCOPE", "wall photos")
PKCE = "/opt/investor/.vk_pkce.json"
OUT = "/opt/investor/.vk_user.env"


def write_private(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def url() -> None:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    write_private(PKCE, json.dumps({"verifier": verifier, "state": state}))
    q = urllib.parse.urlencode({
        "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256", "scope": SCOPE})
    print("Открой в браузере под админом группы:\nhttps://id.vk.ru/authorize?" + q)


def token() -> None:
    saved = json.load(open(PKCE))
    code = getpass.getpass("code из адресной строки: ").strip()
    device = getpass.getpass("device_id из адресной строки: ").strip()
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code_verifier": saved["verifier"], "redirect_uri": REDIRECT,
        "code": code, "client_id": CLIENT_ID, "device_id": device, "state": saved["state"]}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request("https://id.vk.ru/oauth2/auth", data), timeout=20))
    if "access_token" not in r:
        sys.exit("VK error: " + str({k: r[k] for k in ("error", "error_description") if k in r}))
    write_private(OUT, f"VK_USER_ACCESS_TOKEN={r['access_token']}\nVK_USER_REFRESH_TOKEN={r.get('refresh_token', '')}\n"
                       f"VK_USER_DEVICE_ID={device}\nVK_USER_EXPIRES_IN={r.get('expires_in', '')}\nVK_APP_ID={CLIENT_ID}\n")
    os.remove(PKCE)
    print("OK, токены сохранены в", OUT, "scope:", r.get("scope", "?"))


if __name__ == "__main__":
    {"url": url, "token": token}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
