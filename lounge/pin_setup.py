"""Upgrade the installed lounge to invitation + PIN access. Run on Mina as faure."""
import argparse
from contextlib import closing
from datetime import datetime
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit, parse_qs
from urllib.request import Request, urlopen

from pin_access import Access, COOKIE_NAME, digest, pin_record, read_auth
from setup_mina import write_private, run
from health_check import health, print_summary

SOURCE = Path(__file__).resolve().parent
APP = Path.home()/"catcam-lounge"
CONFIG = Path.home()/".config/catcam-lounge"
STATE = Path.home()/".local/share/catcam-lounge"
AUTH = CONFIG/"pin-access.json"
INVITE = CONFIG/"invite.url"
CADDYFILE = CONFIG/"Caddyfile"
UNIT_DIR = Path("/etc/systemd/system")
SERVICES = ("catcam-lounge", "catcam-lounge-gateway")
FILES = ("lounge.py", "pin_access.py", "pin_lounge.py", "lounge.html", "lounge.css", "lounge.js",
         "gate.html", "gate.css", "gate.js", "favicon.svg", "pin_setup.py", "setup_mina.py", "health_check.py", "storage.html")
PATHS = ("/", "/gate.css", "/gate.js", "/favicon.svg", "/lounge.css", "/lounge.js", "/frame.jpg", "/status.json")
STREAM = "/perch.mjpg"  # live view; streams end after 10 minutes and the viewer reconnects


def gateway_config(gateway_port=8087, backend_port=8088):
    return f'''{{
    admin off
    auto_https off
    persist_config off
    servers {{
        timeouts {{
            read_header 5s
            read_body 5s
            write 11m
            idle 30s
        }}
        max_header_size 16KB
    }}
}}
http://:{gateway_port} {{
    bind 127.0.0.1
    route {{
        header {{
            -Server
            Cache-Control "no-store, max-age=0"
            X-Content-Type-Options nosniff
            X-Frame-Options DENY
            Referrer-Policy no-referrer
            X-Robots-Tag "noindex, nofollow, noarchive"
        }}
        @stream {{
            method GET HEAD
            path {STREAM}
        }}
        handle @stream {{
            reverse_proxy 127.0.0.1:{backend_port} {{
                header_up -Authorization
                flush_interval -1
            }}
        }}
        @read {{
            method GET HEAD
            path {' '.join(PATHS)}
        }}
        handle @read {{
            reverse_proxy 127.0.0.1:{backend_port} {{
                header_up -Authorization
            }}
        }}
        @login {{
            method POST
            path /api/login /api/logout
        }}
        handle @login {{
            request_body {{
                max_size 2KB
            }}
            reverse_proxy 127.0.0.1:{backend_port} {{
                header_up -Authorization
            }}
        }}
        respond "Not found" 404
    }}
}}
'''


def unit(release, binary, gateway=False):
    import pwd
    username = pwd.getpwuid(os.getuid()).pw_name
    command = (f"{binary} run --config {CADDYFILE} --adapter caddyfile" if gateway else
               f"{sys.executable} {release}/pin_lounge.py --config {CONFIG}/settings.json --auth {AUTH} --database {STATE}/access.sqlite3")
    if any(char in command for char in "\n\r\"%") or " " in str(release)+str(CONFIG)+str(STATE)+str(binary)+str(sys.executable):
        raise ValueError("Install paths must not contain spaces, quotes, newlines or percent signs")
    writable = "" if gateway else f"ReadWritePaths={STATE}\n"
    return f'''[Unit]
Description=Cat Lounge {'gateway' if gateway else 'PIN viewer'}
After=network-online.target catcam-recorder.service
Wants=network-online.target

[Service]
Type=simple
User={username}
WorkingDirectory={release}
ExecStart={command}
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
RestrictSUIDSGID=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
{writable}UMask=0077
MemoryMax={'160M' if gateway else '256M'}
TasksMax=96
CPUQuota={'45%' if gateway else '60%'}

[Install]
WantedBy=multi-user.target
'''


def request(path, *, cookie=None, payload=None, origin=None, method=None, port=8087):
    headers = {}
    if cookie: headers["Cookie"] = cookie
    if origin: headers["Origin"] = origin
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = Request(f"http://127.0.0.1:{port}"+path, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=12) as response: return response.status, response.read(), dict(response.headers)
    except HTTPError as error: return error.code, error.read(), dict(error.headers)


def stream_check(cookie, port=8087, deadline=12):
    """Read the live stream through the gateway until two frames arrive, proving nothing buffers it."""
    req = Request(f"http://127.0.0.1:{port}{STREAM}", headers={"Cookie": cookie})
    try:
        with urlopen(req, timeout=deadline) as response:
            if not response.headers.get("Content-Type", "").startswith("multipart/x-mixed-replace"):
                raise RuntimeError("The live stream did not answer as a stream")
            start, seen = time.monotonic(), b""
            while time.monotonic() - start < deadline:
                chunk = response.read1(65536)
                if not chunk:
                    break
                seen = (seen + chunk)[-(4 << 20):]
                if seen.count(b"--frame\r\n") >= 3:
                    return response.status
        raise RuntimeError("Live frames were held back between the lounge and the gateway")
    except HTTPError as error:
        return error.code


def verify():
    config = read_auth(AUTH)
    status, data, _ = request("/")
    if status != 200 or b'id="pin"' not in data or b'Live cropped view' in data:
        raise RuntimeError("PIN landing page check failed")
    for path in ("/frame.jpg", "/status.json", "/lounge.js", STREAM):
        if request(path)[0] != 401: raise RuntimeError("Unauthenticated media was not blocked")
    for origin in (config["origin"], "https://untrusted.invalid"):
        if request("/api/login", payload={"invite":"x"*32, "pin":"0000"}, origin=origin)[0] != 403:
            raise RuntimeError("Invitation/origin check failed")
    # This temporary installation-only session checks route isolation without storing the PIN.
    token = secrets.token_urlsafe(32)
    access = Access(AUTH, STATE/"access.sqlite3")
    with closing(sqlite3.connect(access.database, timeout=10)) as db:
        db.execute("INSERT INTO sessions VALUES (?,?,?)", (digest(token), config["generation"], time.time()+60))
        db.commit()
    cookie = COOKIE_NAME+"="+token
    try:
        if request("/status.json", cookie=cookie)[0] != 200: raise RuntimeError("Valid session was not accepted")
        live = stream_check(cookie)
        if live not in (200, 503): raise RuntimeError("The live stream route is not working")
        if live == 503: print("Note: the live stream is ready, but the camera had no picture during this check.")
        for path in ("/clips/", "/storage/", "/storage.html", "/api/day", "/api/retention", "/frame", "/live", "/pin-access.json", "/%2e%2e/clips/"):
            if request(path, cookie=cookie)[0] not in (400,404): raise RuntimeError("Private route isolation failed")
        if request("/api/retention", cookie=cookie, payload={"days":1}, origin=config["origin"])[0] != 404:
            raise RuntimeError("Management request was not blocked")
        if request("/api/logout", cookie=cookie, payload={}, origin=config["origin"])[0] != 200:
            raise RuntimeError("Sign-out failed")
        if request("/status.json", cookie=cookie)[0] != 401: raise RuntimeError("Revoked session was accepted")
    finally:
        access.logout(cookie)
    print("Passed: PIN page, required invitation/session, protected images, blocked archive/management, and sign-out.")


def make_auth(origin, preset):
    token = secrets.token_urlsafe(24)
    config = {"version":1, "origin":origin, "invite_hash":digest(token), "generation":secrets.token_hex(16), **preset}
    return config, origin+"/#invite="+token


def save_access_pair(config, invitation):
    # Stage the printable link before switching the verifier. After a power
    # cut the matching link can be recovered on either side of the switch.
    staged = INVITE.with_suffix(".pending")
    write_private(staged, invitation+"\n")
    write_private(AUTH, json.dumps(config, indent=2))
    os.replace(staged, INVITE)


def current_link(config=None):
    config = read_auth(AUTH) if config is None else config
    for path in (INVITE, INVITE.with_suffix(".pending")):
        try:
            url = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        token = parse_qs(urlsplit(url).fragment).get("invite", [""])[0]
        if (re.fullmatch(r"[A-Za-z0-9_-]{32}", token) and url == config["origin"]+"/#invite="+token
                and secrets.compare_digest(digest(token), config["invite_hash"])):
            if path != INVITE: os.replace(path, INVITE)
            return url
    raise RuntimeError("The invitation file could not be recovered. Run this script with --rotate-invite.")


PROXY = "http://127.0.0.1:8087"


def route_state(config, hostport, *, foreground=False):
    """Recognize only our single Tailscale HTTPS mount. Other ports are independent."""
    if config is None:
        return "unused"
    if not isinstance(config, dict):
        raise RuntimeError("Could not verify Tailscale routes")
    if config and not set(config).intersection({"TCP", "Web", "AllowFunnel", "Foreground", "Services"}):
        raise RuntimeError("Unsupported Tailscale route format. Publication was stopped.")
    tcp, web, public, temporary = (config.get(key, {}) for key in ("TCP", "Web", "AllowFunnel", "Foreground"))
    if not all(isinstance(value, dict) for value in (tcp, web, public, temporary)):
        raise RuntimeError("Unexpected Tailscale route format")
    if public.get(hostport.rsplit(":", 1)[0]+":8443"):
        raise RuntimeError("The private dashboard's port 8443 is marked public in Tailscale. Review it before sharing.")
    for child in temporary.values():
        if route_state(child, hostport, foreground=True) != "unused":
            raise RuntimeError("A temporary Tailscale listener already uses the lounge port")
    on_port = {key for key in (*web, *public) if key.endswith(":10000")}
    if not on_port and "10000" not in tcp:
        return "unused"
    if foreground or on_port != {hostport} or tcp.get("10000") != {"HTTPS": True}:
        raise RuntimeError("Port 10000 has a different Tailscale listener. Its settings were left in place.")
    entry = web.get(hostport)
    acceptable = [{"Handlers": {"/": {"Proxy": proxy}}} for proxy in (PROXY, PROXY+"/")]
    if entry not in acceptable or type(public.get(hostport, False)) is not bool:
        raise RuntimeError("Port 10000 must have just the Cat Lounge root proxy before it can be shared.")
    return "public" if public.get(hostport) else "private"


def inspect_route(origin):
    output = run(["tailscale", "serve", "status", "--json"], capture_output=True, text=True)
    return route_state(json.loads(output.stdout), urlsplit(origin).netloc)


def use_private_route(origin):
    run(["sudo", "tailscale", "serve", "--bg", "--https=10000", PROXY])
    if inspect_route(origin) != "private":
        raise RuntimeError("Could not confirm that the lounge is private in Tailscale")


def wait_for_pin_backend():
    for _ in range(25):
        try:
            if request("/frame.jpg", port=8088)[0] == 401 and b'id="pin"' in request("/", port=8088)[1]:
                return
        except OSError:
            pass
        time.sleep(.4)
    raise RuntimeError("The PIN viewer did not protect its direct camera route")


def install():
    if not (CONFIG/"installed.json").exists(): raise RuntimeError("The original lounge must be installed first")
    binary = APP/"bin/caddy"
    if not binary.is_file(): raise RuntimeError("The existing Caddy binary is missing")
    if not run([str(binary), "version"], capture_output=True, text=True).stdout.startswith("v2."):
        raise RuntimeError("This lounge requires Caddy 2")
    status = json.loads(run(["tailscale", "status", "--json"], capture_output=True, text=True).stdout)
    hostname = status["Self"]["DNSName"].rstrip(".")
    if not hostname or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in hostname):
        raise ValueError("Unexpected Tailscale hostname")
    origin = f"https://{hostname}:10000"
    fingerprint = hashlib.sha256(b"".join((SOURCE/name).read_bytes() for name in FILES)).hexdigest()[:16]
    release = APP/"pin-releases"/fingerprint
    release.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not release.exists():
        with tempfile.TemporaryDirectory(dir=release.parent) as staging:
            complete = Path(staging)/"complete"
            complete.mkdir(mode=0o700)
            for name in FILES:
                shutil.copy2(SOURCE/name, complete/name)
            complete.rename(release)
    elif any((release/name).read_bytes() != (SOURCE/name).read_bytes() for name in FILES):
        raise RuntimeError("An existing release has changed on disk")
    run([sys.executable, "-m", "py_compile", *[str(release/name) for name in FILES if name.endswith('.py')]])
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    candidate = CONFIG/"Caddyfile.pin-candidate"
    write_private(candidate, gateway_config())
    run([str(binary), "validate", "--config", str(candidate), "--adapter", "caddyfile"], stdout=subprocess.DEVNULL)
    new_auth = not AUTH.exists()
    if new_auth:
        preset = json.loads((SOURCE/"pin-preset.json").read_text(encoding="utf-8"))
        if set(preset) != {"pin_salt", "pin_hash", "pin_iterations"}: raise ValueError("Invalid PIN preset")
        config, invite_url = make_auth(origin, preset)
    else:
        config = read_auth(AUTH)
        if config["origin"] != origin: raise RuntimeError("Hostname changed; rotate the invitation before continuing")
        invite_url = current_link(config)
    backup = CONFIG/("before-pin-"+datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    backup.mkdir(mode=0o700)
    previous = {CADDYFILE:CADDYFILE.read_text(encoding="utf-8")}
    for service in SERVICES:
        target = UNIT_DIR/(service+".service")
        previous[target] = target.read_text(encoding="utf-8")
    for target, data in previous.items(): write_private(backup/target.name, data)
    if AUTH.exists(): shutil.copy2(AUTH, backup/AUTH.name)
    run(["sudo", "-v"])
    if new_auth and inspect_route(origin) == "public":
        use_private_route(origin)
    try:
        run(["sudo", "systemctl", "stop", "catcam-lounge-gateway", "catcam-lounge"])
        if new_auth:
            save_access_pair(config, invite_url)
        read_auth(AUTH)
        Access(AUTH, STATE/"access.sqlite3")
        for service, gateway in zip(SERVICES, (False, True)):
            if gateway:
                # Keep the legacy gateway in place on disk until the new
                # backend rejects unsigned media, including across a reboot.
                write_private(CADDYFILE, gateway_config())
            local = CONFIG/(service+".pin.service")
            write_private(local, unit(release, binary, gateway))
            run(["sudo", "install", "-m", "644", str(local), str(UNIT_DIR/(service+".service"))])
            run(["sudo", "systemctl", "daemon-reload"])
            run(["sudo", "systemctl", "enable", "--now", service])
            if not gateway:
                wait_for_pin_backend()
        for _ in range(25):
            try:
                if request("/")[0] == 200: break
            except OSError: pass
            time.sleep(.4)
        verify()
    except BaseException:
        subprocess.run(["sudo", "systemctl", "stop", *SERVICES], check=False)
        for target, data in previous.items():
            if target == CADDYFILE: write_private(target, data)
            else: run(["sudo", "install", "-m", "644", str(backup/target.name), str(target)])
        if new_auth:
            AUTH.unlink(missing_ok=True)
            INVITE.unlink(missing_ok=True)
            INVITE.with_suffix(".pending").unlink(missing_ok=True)
        run(["sudo", "systemctl", "daemon-reload"])
        run(["sudo", "systemctl", "start", *SERVICES])
        raise
    for name in ("pin_setup.py", "pin_access.py", "health_check.py", "setup_mina.py", "storage.html"):
        if (SOURCE/name).resolve() != (APP/name).resolve():
            write_private(APP/name, (release/name).read_text(encoding="utf-8"))
    write_private(CONFIG/"pin-installed.json", json.dumps({"release":str(release), "installed_at":time.time()}, indent=2))
    print("The lounge now uses the requested four-digit PIN. Existing username/password logins are no longer used.")


def share_link():
    print("\nPRIVATE INVITATION — share this complete link with your mom and friend:")
    print(current_link())
    print("They open the link and enter the PIN. No username or Tailscale app is needed after Funnel is enabled.")


def publish():
    verify()
    origin = read_auth(AUTH)["origin"]
    if inspect_route(origin) != "public":
        try:
            run(["sudo", "tailscale", "funnel", "--bg", "--https=10000", PROXY])
            if inspect_route(origin) != "public":
                raise RuntimeError("Funnel did not confirm the expected guest route")
        except (Exception, KeyboardInterrupt):
            use_private_route(origin)
            raise
    print("Tailscale confirms public access to the protected family lounge on port 10000.")


def update_recorder():
    target = Path.home()/"catcam.py"
    source = SOURCE/"CatCam-Next.py"
    if not target.is_file() or not source.is_file():
        raise RuntimeError("The private recorder update or existing recorder is missing")
    if source.read_bytes() != target.read_bytes():
        # This installer backs up the previous script, restarts only the
        # recorder, and restores it if the private application checks fail.
        run([sys.executable, str(source), "--install-sightings"])
    status, body, _ = request("/storage/", port=8086)
    if status != 200 or b"<title>Storage " not in body:
        raise RuntimeError("The owner's storage page did not start")
    print("The owner's Storage page and recorder protection checks are ready.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("install", "publish", "private", "link", "health", "rotate-invite", "set-pin", "upgrade-recorder", "client-info"):
        parser.add_argument("--"+flag, action="store_true")
    args = parser.parse_args()
    if not any(vars(args).values()): parser.error("Choose an action")
    if args.private and args.publish: parser.error("Choose private or public access")
    if sys.platform != "linux" or os.getuid() == 0: parser.error("Run on Mina as faure, not root")
    os.umask(0o077)
    if args.client_info:
        conf = read_auth(AUTH)
        preset = json.loads((SOURCE/"pin-preset.json").read_text(encoding="utf-8"))
        print(json.dumps({"url": current_link(conf), "original_pin": all(conf.get(k)==v for k,v in preset.items())}))
        return
    if args.install: install()
    if args.rotate_invite or args.set_pin:
        config = read_auth(AUTH)
        if args.set_pin:
            value = getpass.getpass("New four-digit PIN: ")
            if getpass.getpass("Repeat PIN: ") != value: raise ValueError("PINs did not match")
            config.update(pin_record(value))
        token = secrets.token_urlsafe(24)
        config.update(invite_hash=digest(token), generation=secrets.token_hex(16))
        save_access_pair(config, config["origin"]+"/#invite="+token)
        print("Invitation replaced. Previous links and sessions are revoked.")
    if args.install:
        print("PIN access is ready on your tailnet. Your old lounge password is retired.")
        print("Your invitation:", current_link(), flush=True)
    recorder_error = None
    if args.upgrade_recorder:
        try:
            update_recorder()
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            recorder_error = error
            print("Private recorder update needs attention:", error, file=sys.stderr)
    if args.install or args.health:
        result = health()
        print_summary(result)
        try:
            write_private(STATE/"health.json", json.dumps(result, indent=2))
        except OSError:
            print("Could not save the health report on Mina.", file=sys.stderr)
    if args.private:
        origin = read_auth(AUTH)["origin"]
        inspect_route(origin)
        use_private_route(origin)
    if args.publish: publish()
    if args.install or args.link or args.publish or args.rotate_invite or args.set_pin: share_link()
    if recorder_error:
        raise RuntimeError("The lounge upgrade succeeded. The private recorder check reported an issue above.")


if __name__ == "__main__":
    try:
        if sys.platform != "linux" or os.getuid() == 0:
            raise SystemExit("Run on Mina under your regular Linux account.")
        import fcntl
        os.umask(0o077)
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (STATE/"owner-command.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            main()
    except (OSError, ValueError, KeyError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
        print("PIN setup stopped: "+str(error), file=sys.stderr)
        print("Your private Cat Cam remains at port 8443.", file=sys.stderr)
        sys.exit(1)
