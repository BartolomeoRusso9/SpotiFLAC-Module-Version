import sys
import os
import time
import httpx
import subprocess
import re

def is_docker():
    """Controlla se stiamo girando dentro un container Docker."""
    path = '/proc/1/cgroup'
    return os.path.exists('/.dockerenv') or (os.path.isfile(path) and any('docker' in line for line in open(path)))

# Comando base: se sei nei sorgenti usa launcher.py, se hai installato via pip usa spotiflac
# Cambia 'launcher.py' con 'spotiflac' a seconda di come avvii di solito
cmd = ["python", "launcher.py"] + sys.argv[1:] 

if not is_docker():
    # FUORI DA DOCKER: Esegui il comando normalmente senza Telegram
    os.execvp("python", cmd)

# DENTRO DOCKER: Attiva la logica Telegram
bot_token = os.environ.get("TG_BOT_TOKEN")
chat_id = os.environ.get("TG_CHAT_ID")

# Se non c'è il bot configurato, esegui comunque normalmente
if not bot_token or not chat_id:
    os.execvp("python", cmd)

print("🤖 [Docker Wrapper] Ambiente rilevato: Attivazione Listener Telegram...")

# (Il resto della logica rimane identica a quella che abbiamo visto prima)
p = subprocess.Popen(
    cmd,
    stdout=subprocess.PIPE,
    stdin=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=1
)

url_regex = re.compile(r"(https://api\.zarz\.moe/v2/challenge\S+)")
offset = -1

while True:
    line = p.stdout.readline()
    if not line and p.poll() is not None: break
    sys.stdout.write(line)
    sys.stdout.flush()
    
    match = url_regex.search(line)
    if match:
        challenge_url = match.group(1)
        msg = (
            "⚠️ *SpotiFLAC Turnstile Challenge*\n\n"
            "Risolvi il captcha dal link qui sotto, poi incollami in chat il codice `grant`:\n\n"
            f"{challenge_url}"
        )
        
        try:
            httpx.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}
            )
            print("🤖 [Docker Wrapper] Notifica Telegram inviata! In attesa della tua risposta in chat...")
        except Exception as e:
            print(f"🤖 [Docker Wrapper] Errore invio Telegram: {e}")
        
        # Polling continuo da Telegram
        waiting = True
        while waiting:
            try:
                resp = httpx.get(
                    f"https://api.telegram.org/bot{bot_token}/getUpdates",
                    params={"offset": offset, "timeout": 10},
                    timeout=15.0
                )
                data = resp.json()
                
                if data.get("ok") and data.get("result"):
                    for update in data["result"]:
                        offset = update["update_id"] + 1
                        
                        if "message" in update and "text" in update["message"]:
                            # Ignora messaggi da estranei
                            if str(update["message"]["chat"]["id"]) != str(chat_id):
                                continue 
                            
                            testo = update["message"]["text"].strip()
                            if len(testo) > 20: 
                                httpx.post(
                                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                    json={"chat_id": chat_id, "text": "✅ Grant ricevuto! Iniezione nel processo in corso..."}
                                )
                                p.stdin.write(testo + "\n")
                                p.stdin.flush()
                                waiting = False
                                break
            except Exception:
                pass
            
            if waiting:
                time.sleep(2)

p.wait()
sys.exit(p.returncode)