"""TEST-ONLY fake of the `tailscale` command-line client (never used by the application itself).

State lives in the JSON file named by FAKE_TAILSCALE_STATE:
  {"logged_in": bool, "https": bool, "dns": "pc.tailxxxx.ts.net", "serve": {port or null}, "funnel": bool}
It imitates the documented output shapes of `status --json`, `serve --bg`, `serve status --json`
and `serve --https=443 off` closely enough to test the engine's handling of them.
"""
import json
import os
import sys
import time

f = os.environ["FAKE_TAILSCALE_STATE"]
st = json.load(open(f, encoding="utf-8"))
a = sys.argv[1:]


def save():
    json.dump(st, open(f, "w", encoding="utf-8"))


if a[:2] == ["status", "--json"]:
    if st.get("stopped"):
        print("failed to connect to local Tailscale service; is Tailscale running?")
        sys.exit(1)
    print(json.dumps({"Version": "1.90.1-tabc", "BackendState": "Running" if st["logged_in"] else "NeedsLogin",
                      "AuthURL": "" if st["logged_in"] else "https://login.tailscale.com/a/abc123",
                      "Self": {"DNSName": (st["dns"] + ".") if st["logged_in"] else "", "Online": st["logged_in"]},
                      "CurrentTailnet": {"Name": "owner@example.test"} if st["logged_in"] else None,
                      "CertDomains": [st["dns"]] if st["https"] and st["logged_in"] else None}))
elif a[:3] == ["serve", "status", "--json"]:
    if st.get("serve"):
        hp = st["dns"] + ":443"
        print(json.dumps({"TCP": {"443": {"HTTPS": True}},
                          "Web": {hp: {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{st['serve']}"}}}},
                          "AllowFunnel": {hp: True} if st.get("funnel") else {}}))
    else:
        print("{}")
elif a and a[0] == "serve" and "off" in a:
    st["serve"] = None
    save()
elif a and a[0] == "serve" and "--bg" in a:
    if not st["https"]:
        print("Serve is not enabled on your tailnet.\nTo enable, visit:\n\n         "
              "https://login.tailscale.com/f/serve?node=n123CNTRL\n")
        sys.stdout.flush()
        time.sleep(60)          # the real client waits here; the engine must not hang with it
        sys.exit(1)
    st["serve"] = int(a[-1])
    save()
    print(f"Available within your tailnet:\n\nhttps://{st['dns']}/\n|-- proxy http://127.0.0.1:{a[-1]}\n\n"
          "Serve started and running in the background.")
else:
    print("unknown command", a)
    sys.exit(2)
