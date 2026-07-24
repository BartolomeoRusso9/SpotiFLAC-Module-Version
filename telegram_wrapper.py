import asyncio
import contextlib
import os
import re
import sys

import httpx


def is_docker():
    """Check whether the script is running inside a Docker container."""
    path = "/proc/1/cgroup"
    return os.path.exists("/.dockerenv") or (
        os.path.isfile(path) and any("docker" in line for line in open(path))
    )


# Base command:
# - Use launcher.py when running from source.
# - Use "spotiflac" if installed via pip.
cmd = ["spotiflac", *sys.argv[1:]]

if not is_docker():
    os.execvp("spotiflac", cmd)

bot_token = os.environ.get("TG_BOT_TOKEN")
chat_id = os.environ.get("TG_CHAT_ID")

# If the bot is not configured, run normally anyway.
if not bot_token or not chat_id:
    os.execvp("spotiflac", cmd)


async def main():
    # Avvia il processo in modalità asincrona
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    # Regex aggiornata: cattura sia le challenge di Zarz che quelle della nuova Community
    url_regex = re.compile(
        r"(https://(?:api\.zarz\.moe|verify\.spotbye\.qzz\.io)\S*challenge\S+)",
    )
    offset = -1

    # Usiamo httpx.AsyncClient() per mantenere viva la connessione HTTP
    async with httpx.AsyncClient() as client:
        while True:
            # Legge l'output di SpotiFLAC riga per riga senza bloccare il thread
            line_bytes = await process.stdout.readline()

            if not line_bytes:
                break  # Fine dell'output (il processo è terminato)

            line = line_bytes.decode("utf-8", errors="replace")
            sys.stdout.write(line)
            sys.stdout.flush()

            match = url_regex.search(line)
            if match:
                challenge_url = match.group(1)

                msg = (
                    "⚠️ <b>SpotiFLAC Verification Challenge</b>\n\n"
                    "Complete the verification using the link below, then send me the <code>grant</code> code in this chat:\n\n"
                    f"{challenge_url}"
                )

                with contextlib.suppress(Exception):
                    await client.post(
                        f"https://api.telegram.org/bot{bot_token}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": msg,
                            "parse_mode": "HTML",
                        },
                    )

                # Polling asincrono su Telegram
                waiting = True

                while waiting:
                    try:
                        resp = await client.get(
                            f"https://api.telegram.org/bot{bot_token}/getUpdates",
                            params={"offset": offset, "timeout": 10},
                            timeout=15.0,
                        )

                        data = resp.json()

                        if data.get("ok") and data.get("result"):
                            for update in data["result"]:
                                offset = update["update_id"] + 1

                                if "message" in update and "text" in update["message"]:
                                    # Ignora messaggi provenienti da altre chat
                                    if str(update["message"]["chat"]["id"]) != str(
                                        chat_id,
                                    ):
                                        continue

                                    text = update["message"]["text"].strip()

                                    if len(text) > 20:
                                        await client.post(
                                            f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                            json={
                                                "chat_id": chat_id,
                                                "text": "✅ Grant received! Injecting it into the process...",
                                            },
                                        )

                                        # Inietta asincronamente il grant nello stdin del terminale virtuale
                                        process.stdin.write(
                                            text.encode("utf-8") + b"\n",
                                        )
                                        await process.stdin.drain()

                                        waiting = False
                                        break

                    except Exception:
                        pass

                    if waiting:
                        await asyncio.sleep(2)

    # Attende la chiusura del processo in modo pulito
    await process.wait()
    return process.returncode


if __name__ == "__main__":
    # Avvia l'event loop di asyncio
    sys.exit(asyncio.run(main()))
