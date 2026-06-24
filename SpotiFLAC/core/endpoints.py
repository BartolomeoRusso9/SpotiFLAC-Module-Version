import json
import hashlib
import base64
import os
import time
from ..core.http import httpx
import logging
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

_SEED_PARTS = [b"spotif", b"lac:co", b"mmunity:url:v1"]
_AAD = b"spotiflac|community|url|v1"

_CLOUD_URL = "https://gist.githubusercontent.com/BartolomeoRusso9/ef9fdbbc894818aea89d25a8d99f8c77/raw/"

_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".endpoints_cache.txt")


def _decrypt_base64_payload(b64_string: str) -> dict:
    """Decripta la stringa unificata di GitHub."""
    
    # 1. Rimuove tutti gli spazi, newline e ritorni a capo (non solo quelli ai bordi)
    clean_b64 = "".join(b64_string.split())
    
    # 2. Converte Base64 URL-Safe in Standard
    clean_b64 = clean_b64.replace('-', '+').replace('_', '/')
    
    # 3. FIX FONDAMENTALE: Forza in formato ASCII puro, spazzando via caratteri invisibili come il BOM
    clean_b64 = clean_b64.encode('ascii', 'ignore').decode('ascii')
    
    # 4. Padding di sicurezza
    padding_needed = len(clean_b64) % 4
    if padding_needed:
        clean_b64 += '=' * (4 - padding_needed)

    # Ora la stringa è perfettamente pulita e pronta per il decoding
    raw_bytes = base64.b64decode(clean_b64)
    
    # Separiamo i pezzi come li avevamo uniti
    nonce = raw_bytes[:12]
    encrypted_payload = raw_bytes[12:]
    
    hasher = hashlib.sha256()
    for part in _SEED_PARTS:
        hasher.update(part)
    key = hasher.digest()
    
    aesgcm = AESGCM(key)
    decrypted_bytes = aesgcm.decrypt(nonce, encrypted_payload, _AAD)
    
    return json.loads(decrypted_bytes.decode('utf-8'))

def _load_registry() -> dict:
    """Scarica il JSON crittografato da GitHub, o usa il backup locale."""
    try:
        req = httpx.get(_CLOUD_URL, headers={'User-Agent': 'SpotiFLAC-Agent'}, timeout=3.0)
        req.raise_for_status()
        cloud_string = req.text

        registry = _decrypt_base64_payload(cloud_string)
        
        try:
            with open(_CACHE_FILE, "w") as f:
                f.write(cloud_string)
        except Exception:
            pass
            
        return registry

    except Exception as e:
        logger.warning(f"Unable to contact Cloud servers ({e}). Falling back to local cache...")
        
        # 2. Se fallisce, prova a leggere l'ultima cache salvata
        try:
            if os.path.exists(_CACHE_FILE):
                with open(_CACHE_FILE, "r") as f:
                    cached_string = f.read()
                return _decrypt_base64_payload(cached_string)
        except Exception as cache_e:
            logger.error(f"Unable to read local cache: {cache_e}")
            
        return {}

# Cache in memoria con TTL: il Gist viene ricontrollato dopo _TTL_SECONDS secondi.
# Aumenta il valore per ridurre le chiamate di rete in processi a lunga esecuzione.
_TTL_SECONDS: int = 30
_registry_cache: dict = {}
_registry_fetched_at: float = 0.0

def _get_registry() -> dict:
    """Ritorna il registro, ricaricandolo dal Gist se il TTL è scaduto."""
    global _registry_cache, _registry_fetched_at
    if not _registry_cache or (time.time() - _registry_fetched_at) >= _TTL_SECONDS:
        _registry_cache = _load_registry()
        _registry_fetched_at = time.time()
    return _registry_cache


# ─── FUNZIONI HELPER PER I PROVIDER ──────────────────────

def get_qobuz_endpoints(category: str) -> list[str]:
    return _get_registry().get("qobuz", {}).get(category, [])

def get_tidal_post_endpoints() -> list[str]:
    return _get_registry().get("tidal", {}).get("post", [])

def get_deezer_endpoint(key: str) -> str:
    """Chiavi valide: 'resolver', 'flacdownloader_prepare', 'flacdownloader_asset'"""
    return _get_registry().get("deezer", {}).get(key, "")

def get_amazon_endpoint(key: str) -> str:
    """
    Chiavi valide:
    - Download: 'musicdl', 'spotbye1', 'spotbye2', 'zarz', 'community'
    - S: 's', 's_home', 's_challenge', 's_verify', 's_stream', 's_queue'
    - Resolver: 'resolver_songstats', 'resolver_songlink_api', 'resolver_songlink_html', 'resolver_spotify', 'resolver_deezer'
    - Base: 'amazon_music_base'
    """
    return _get_registry().get("amazon", {}).get(key, "")

def get_apple_music_endpoint(key: str) -> str:
    """Chiavi: 'proxy_direct', 'proxy_queued'"""
    return _get_registry().get("apple_music", {}).get(key, "")

def get_asian_provider_endpoint(provider: str, key: str) -> str:
    """Per joox, kuwo, migu, netease"""
    return _get_registry().get(provider, {}).get(key, "")

def get_soundcloud_cobalt() -> str:
    return _get_registry().get("soundcloud", {}).get("cobalt", "")

def get_youtube_endpoints(key: str) -> list[str] | str:
    """Chiavi: 'cobalt', 'zarz_clean', 'zarz_dl'"""
    return _get_registry().get("youtube", {}).get(key, [])

def get_pandora_base_and_path() -> tuple[str, str]:
    pan = _get_registry().get("pandora", {})
    return pan.get("zarz_base", ""), pan.get("zarz_dl", "")

def get_health_zarz_url() -> str:
    return _get_registry().get("health", {}).get("zarz", "")

def get_community_url(provider: str) -> str:
    """Returns l'URL Community se esiste nel registro, altrimenti stringa vuota."""
    return _get_registry().get(provider, {}).get("community", "")

def _jwt_payload(token: str) -> dict:
    """
    Decodifica (senza verificare la firma) il payload di un JWT
    'Bearer <header>.<payload>.<signature>'. Usata solo per leggere
    campi informativi come 'exp', non per validare l'autenticità.
    """
    try:
        raw = token.removeprefix("Bearer ").strip()
        parts = raw.split(".")
        if len(parts) != 3:
            return {}
        payload_b64 = parts[1]
        # JWT usa base64url senza padding: va ripristinato prima di decodificare.
        padding = "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        return json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return {}

def get_monochrome_token() -> str:
    """
    Ritorna il token (con prefisso 'Bearer ') letto dal registro cifrato.
    Se il token è un JWT con campo 'exp', verifica la scadenza e logga un
    warning se è già scaduto o scade entro 24 ore — non viene effettuato
    alcun refresh automatico: il rinnovo resta manuale (rigenerare e
    pubblicare un nuovo Gist cifrato).
    """
    import time

    token = _get_registry().get("monochrome-token", {}).get("token", "")
    if not token:
        return token

    payload = _jwt_payload(token)
    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        now = time.time()
        if exp <= now:
            expired_since = now - exp
            logger.debug(
                "Token già scaduto da %.0f ore — "
                "il proxy Tidal probabilmente risponderà 401. "
                "Rigenerare il token e aggiornare il Gist.",
                expired_since / 3600,
            )
        elif exp - now <= 86400:
            logger.debug(
                "Token in scadenza entro 24 ore (alle %s UTC).",
                time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(exp)),
            )

    return token

if __name__ == "__main__":
    print("🚀 Avvio test del file di configurazione...\n")
    print("-" * 50)

    # --- TEST 1: Decrittografia e caricamento ---
    registry = _get_registry()
    if registry:
        print("✅ TEST 1: REGISTRY caricato e decriptato con successo!")
        print(f"   Trovati {len(registry.keys())} blocchi principali (provider).")
    else:
        print("❌ TEST 1 FALLITO: Il REGISTRY è vuoto. Controlla il link GitHub o il seed.")
        exit(1)

    print("-" * 50)
    
    # --- TEST 2: Funzioni Helper ---
    print("🔍 TEST 2: Estrazione dati dalle funzioni helper...")
    
    yt_cobalt = get_youtube_endpoints("cobalt")
    print(f"   YouTube Cobalt URLs : {len(yt_cobalt)} trovati")
    
    qobuz_dl = get_qobuz_endpoints("dl")
    print(f"   Qobuz DL URLs       : {len(qobuz_dl)} trovati")

    squid = get_amazon_endpoint("s")
    print(f"   Amazon S endpoint   : {'Presente' if squid else '�NON TROVATO'}")
    
    token = get_monochrome_token()
    if token:
        print(f"   Token Monochrome    : Presente ({len(token)} caratteri)")
    else:
        print("   Token Monochrome    : ⚠️ NON TROVATO")

    print("-" * 50)
    
    # --- TEST 3: Ping Live dei server ---
    health_url = get_health_zarz_url()
    if health_url:
        print(f"🌐 TEST 3: Ping live a {health_url}...")
        try:
            # Usiamo httpx (che hai già importato in alto) per fare la chiamata
            start_time = time.time()
            res = httpx.get(health_url, timeout=5.0)
            ping = round((time.time() - start_time) * 1000)
            
            if res.status_code == 200:
                print(f"   ✅ Server ONLINE! (Ping: {ping}ms)")
                print(f"   Risposta: {res.json()}")
            else:
                print(f"   ⚠️ Server online ma ha risposto con codice errore: {res.status_code}")
                
        except httpx.RequestError as e:
            print(f"   ❌ Impossibile connettersi al server (Offline o irraggiungibile):\n   {e}")
    else:
        print("❌ Impossibile trovare l'URL di health nel registry.")
        
    print("\n🏁 Test completati.")
