import asyncio
import httpx
import json
import logging
import os
import sys

# Import core interni
from SpotiFLAC.core.endpoints import get_community_url
from SpotiFLAC.core.errors import SpotiflacError
from SpotiFLAC.core.signed_session_desktop import ensure_community_session, sign_community_request
from SpotiFLAC.core.quality import map_amazon_community_quality

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("CommunityDownloader")

async def download_amazon_track_community(asin: str, quality: str, output_dir: str):
    """
    Script standalone per download tramite Community API con gestione verifica.
    """
    community_url = get_community_url("amazon")
    if not community_url:
        print("❌ Errore: Endpoint Community non configurato.")
        return

    # 1. Verifica Sessione/Grant
    print(f"🔄 Verifica sessione Community...")
    session_data = await asyncio.to_thread(ensure_community_session)
    
    # 2. Setup Richiesta
    # N.B. rimosso "/track" che causava il 404
    url = community_url.rstrip('/')
    
    payload = {
        "id": asin,
        "quality": map_amazon_community_quality(quality),
        "country": "US",
    }
    body_bytes = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    
    # 3. Firma Richiesta 
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    sig_headers = await asyncio.to_thread(
        sign_community_request, "POST", url, body_bytes, session_data
    )
    headers.update(sig_headers)

    # 4. Esecuzione
    print(f"📥 Preparazione richiesta per la traccia {asin}...")
    
    # === STAMPA DELL'URL DELLA CHIAMATA API (quello che dava 404) ===
    print(f"📡 URL della chiamata API: {url}")
    
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, content=body_bytes, headers=headers)
        
        if resp.status_code != 200:
            print(f"❌ Errore API: {resp.status_code} - {resp.text}")
            return

        data = resp.json()
        stream_url = data.get("stream_url") or data.get("url") or data.get("streamUrl")
        
        if not stream_url:
            print("❌ Stream URL non trovato.")
            return

        # === STAMPA DELLO STREAM URL RESTITUITO DAL SERVER ===
        print(f"🔗 Stream URL ottenuto: {stream_url}")

        # 5. Download effettivo
        dest = os.path.join(output_dir, f"{asin}.enc")
        
        # Gestione captcha token se presente
        dl_headers = {}
        if captcha := data.get("captcha") or data.get("x-captcha-token"):
            dl_headers["x-captcha-token"] = str(captcha)

        print(f"⬇️ Inizio download del file...")
        with open(dest, "wb") as f:
            async with client.stream("GET", stream_url, headers=dl_headers) as dl_resp:
                async for chunk in dl_resp.aiter_bytes(65536):
                    f.write(chunk)
        
        print(f"✅ Download completato: {dest}")

if __name__ == "__main__":
    # Esempio di utilizzo
    # asin: l'ID Amazon del brano
    # quality: "16" o "24" o "atmos"
    ASIN = "B079D9D75F" 
    QUALITY = "16"
    OUTPUT_DIR = "./"

    asyncio.run(download_amazon_track_community(ASIN, QUALITY, OUTPUT_DIR))