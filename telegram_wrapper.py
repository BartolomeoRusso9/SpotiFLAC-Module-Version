import sys
import os
import time
import httpx
import subprocess
import re


def is_docker():
    """Check whether the script is running inside a Docker container."""
    path = "/proc/1/cgroup"
    return os.path.exists("/.dockerenv") or (
        os.path.isfile(path) and any("docker" in line for line in open(path))
    )


# Base command:
# - Use launcher.py when running from source.
# - Use "spotiflac" if installed via pip.
cmd = ["python", "spotiflac"] + sys.argv[1:]

if not is_docker():
    # OUTSIDE DOCKER: Run normally without Telegram integration.
    os.execvp("python", cmd)

# INSIDE DOCKER: Enable Telegram listener.
bot_token = os.environ.get("TG_BOT_TOKEN")
chat_id = os.environ.get("TG_CHAT_ID")

# If the bot is not configured, run normally anyway.
if not bot_token or not chat_id:
    os.execvp("python", cmd)

print("🤖 [Docker Wrapper] Docker environment detected: Starting Telegram listener...")

p = subprocess.Popen(
    cmd,
    stdout=subprocess.PIPE,
    stdin=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=1,
)

url_regex = re.compile(r"(https://api\.zarz\.moe/v2/challenge\S+)")
offset = -1

while True:
    line = p.stdout.readline()
    if not line and p.poll() is not None:
        break

    sys.stdout.write(line)
    sys.stdout.flush()

    match = url_regex.search(line)
    if match:
        challenge_url = match.group(1)

        msg = (
            "⚠️ *SpotiFLAC Turnstile Challenge*\n\n"
            "Complete the CAPTCHA using the link below, then send me the `grant` code in this chat:\n\n"
            f"{challenge_url}"
        )

        try:
            httpx.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": msg,
                    "parse_mode": "Markdown",
                },
            )
            print(
                "🤖 [Docker Wrapper] Telegram notification sent! Waiting for your reply..."
            )
        except Exception as e:
            print(f"🤖 [Docker Wrapper] Failed to send Telegram notification: {e}")

        # Continuously poll Telegram until a valid grant is received.
        waiting = True

        while waiting:
            try:
                resp = httpx.get(
                    f"https://api.telegram.org/bot{bot_token}/getUpdates",
                    params={"offset": offset, "timeout": 10},
                    timeout=15.0,
                )

                data = resp.json()

                if data.get("ok") and data.get("result"):
                    for update in data["result"]:
                        offset = update["update_id"] + 1

                        if "message" in update and "text" in update["message"]:

                            # Ignore messages from other chats.
                            if str(update["message"]["chat"]["id"]) != str(chat_id):
                                continue

                            text = update["message"]["text"].strip()

                            if len(text) > 20:
                                httpx.post(
                                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                    json={
                                        "chat_id": chat_id,
                                        "text": "✅ Grant received! Injecting it into the process...",
                                    },
                                )

                                p.stdin.write(text + "\n")
                                p.stdin.flush()
                                waiting = False
                                break

            except Exception:
                pass

            if waiting:
                time.sleep(2)

p.wait()
sys.exit(p.returncode)
