import os
import asyncio
import pickle
import json
import requests
import isodate
import random
from typing import List, Optional, Dict
from urllib.parse import urlparse, parse_qs

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ========= Google OAuth imports =========
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# ========= Carga de variables desde .env =========
from dotenv import load_dotenv

# Cargar variables de entorno definidas en el archivo .env
load_dotenv()

#####################
# Modelos y Clases  #
#####################

class Song(BaseModel):
    id: str
    title: str
    thumbnail: str
    duration: float
    requestedBy: Optional[str] = "Sistema"

class PlaybackStatus(BaseModel):
    currentIndex: int
    currentTime: float
    totalDuration: float
    isPlaying: bool
    currentSong: Optional[Song] = None

class AddExternalSongRequest(BaseModel):
    youtubeId: str
    title: str
    thumbnail: str
    requestedBy: Optional[str] = "Sistema"


##########################
# Configuración Principal
##########################

app = FastAPI(
    title="Salchimonster Backend (Multi-Sede)",
    description="API con biblioteca única y múltiples colas + WebSockets para sincronización en tiempo real",
    version="0.4.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Variables globales cargadas desde .env
YT_API_KEY = os.getenv("YT_API_KEY")  # Obtenido de tu .env
PLAYLIST_LINK = os.getenv("PLAYLIST_LINK")
CLIENT_SECRETS_FILE = os.getenv("CLIENT_SECRETS_FILE")
TOKEN_FILE = os.getenv("TOKEN_FILE")
YOUTUBE_PLAYLIST_ID = os.getenv("YOUTUBE_PLAYLIST_ID")

# Biblioteca global y sedes
availableSongs: List[Song] = []
sede_data: Dict[str, Dict] = {}

# Lock global de reproducción
playback_lock = asyncio.Lock()

# Alcances necesarios para Google OAuth
SCOPES = ["https://www.googleapis.com/auth/youtube"]
##########################
# Configuración Principal
##########################

app = FastAPI(
    title="Salchimonster Backend (Multi-Sede)",
    description="API con biblioteca única y múltiples colas (una por sede) + WebSockets para sincronización en tiempo real, con inserción en playlist de YouTube",
    version="0.4.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Biblioteca global y sedes
availableSongs: List[Song] = []
sede_data: Dict[str, Dict] = {}

# Lock global de reproducción
playback_lock = asyncio.Lock()

# Clave de API para operaciones de lectura (buscar, obtener playlist, etc.)
YT_API_KEY = "AIzaSyAFA-ABzqdSBbla_GLSkxhquCBzSURmPWc"  # <-- REEMPLAZA

# Playlist pública para leer las canciones iniciales
PLAYLIST_LINK = "https://www.youtube.com/playlist?list=PLzeOgQjW3-PsxvQS-DGut16T-dpVK2Hf5"  # <-- REEMPLAZA

# Alcance que necesitamos para poder editar playlists
SCOPES = ["https://www.googleapis.com/auth/youtube"]

# Nombre del archivo local con credenciales de OAuth (el que bajaste de Google Cloud)
CLIENT_SECRETS_FILE = "./client.json"

# Nombre del archivo donde guardaremos los tokens (access/refresh)
TOKEN_FILE = "./token.pickle"

# ID de la playlist donde quieras insertar videos nuevos 
YOUTUBE_PLAYLIST_ID = "PLzeOgQjW3-PsxvQS-DGut16T-dpVK2Hf5"  # <-- REEMPLAZA con tu playlist real


##########################
# Autenticación a YouTube
##########################

_youtube_creds: Optional[Credentials] = None

def init_youtube_auth():
    """
    Carga (o solicita) las credenciales OAuth para YouTube y
    las guarda en _youtube_creds (variable global).
    Si no existe token.pickle, inicia un flujo local para obtenerlo.
    """
    global _youtube_creds

    if os.path.exists(TOKEN_FILE):
        # Cargar tokens previos
        with open(TOKEN_FILE, "rb") as f:
            _youtube_creds = pickle.load(f)
    else:
        _youtube_creds = None

    # Si no hay credenciales o están expiradas sin refresh token
    if not _youtube_creds or not _youtube_creds.valid:
        if _youtube_creds and _youtube_creds.expired and _youtube_creds.refresh_token:
            # Renovar token
            _youtube_creds.refresh(Request())
        else:
            # Iniciar flujo OAuth (versión "local server" que abre navegador)
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
            _youtube_creds = flow.run_local_server(port=8080)

        # Guardar en token.pickle
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(_youtube_creds, f)

def get_youtube_access_token() -> str:
    """
    Devuelve el access_token actual.
    Si está expirado y tenemos refresh_token, lo renueva.
    """
    global _youtube_creds
    if not _youtube_creds:
        raise ValueError("Credenciales de YouTube no inicializadas. Llama a init_youtube_auth().")

    if _youtube_creds.expired and _youtube_creds.refresh_token:
        _youtube_creds.refresh(Request())
        # Guardar cambios si se renueva
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(_youtube_creds, f)

    return _youtube_creds.token


#########################
# Insertar video en YT  #
#########################

def add_video_to_youtube_playlist(video_id: str, playlist_id: str):
    """
    Inserta un video a la playlist dada. Requiere un token OAuth válido.
    """
    access_token = get_youtube_access_token()

    url = "https://www.googleapis.com/youtube/v3/playlistItems?part=snippet"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    body = {
        "snippet": {
            "playlistId": playlist_id,
            "resourceId": {
                "kind": "youtube#video",
                "videoId": video_id
            }
        }
    }
    resp = requests.post(url, headers=headers, json=body)
    if not resp.ok:
        raise ValueError(f"Error inserting into playlist: {resp.text}")
    return resp.json()


###################
# Funciones Helper
###################

def parse_duration(duration_iso: str) -> float:
    return isodate.parse_duration(duration_iso).total_seconds()

def get_playlist_id_from_url(url: str) -> Optional[str]:
    try:
        from urllib.parse import urlparse, parse_qs
        parsed_url = urlparse(url)
        query_params = parse_qs(parsed_url.query)
        playlist_ids = query_params.get("list")
        if playlist_ids:
            return playlist_ids[0]
    except:
        pass
    return None

def fetch_playlist_items(playlist_id: str) -> List[Song]:
    """
    Descarga la playlist de YouTube (con YT_API_KEY),
    y retorna solo videos embeddables y sin bloqueo.
    """
    base_url = "https://www.googleapis.com/youtube/v3/playlistItems"
    video_base_url = "https://www.googleapis.com/youtube/v3/videos"

    params = {
        "part": "snippet",
        "playlistId": playlist_id,
        "maxResults": 50,
        "key": YT_API_KEY
    }
    resp = requests.get(base_url, params=params)
    resp.raise_for_status()
    items = resp.json().get("items", [])
    video_ids = [item["snippet"]["resourceId"]["videoId"] for item in items]

    # Obtener detalles (duración, embeddable, etc.)
    video_params = {
        "part": "contentDetails,status",
        "id": ",".join(video_ids),
        "key": YT_API_KEY
    }
    video_resp = requests.get(video_base_url, params=video_params)
    video_resp.raise_for_status()
    video_data = video_resp.json().get("items", [])

    duration_dict = {
        v["id"]: parse_duration(v["contentDetails"]["duration"])
        for v in video_data
    }
    embeddable_dict = {
        v["id"]: v["status"].get("embeddable", False)
        for v in video_data
    }
    content_rating_dict = {
        v["id"]: v["contentDetails"].get("contentRating", {})
        for v in video_data
    }
    region_restriction_dict = {
        v["id"]: v["contentDetails"].get("regionRestriction", {})
        for v in video_data
    }

    results = []
    for item in items:
        snippet = item["snippet"]
        vid = snippet["resourceId"]["videoId"]
        title = snippet["title"]
        thumbnail = snippet.get("thumbnails", {}).get("high", {}).get("url", "")
        duration = duration_dict.get(vid, 0)
        is_embeddable = embeddable_dict.get(vid, False)
        content_rating = content_rating_dict.get(vid, {})
        region_restriction = region_restriction_dict.get(vid, {})
        blocked = region_restriction.get("blocked", [])

        if duration > 0 and is_embeddable and not content_rating and not blocked:
            results.append(Song(id=vid, title=title, thumbnail=thumbnail, duration=duration))
        else:
            print(f"Excluyendo {title} (ID: {vid}) por restricciones o no embeddable.")

    return results

def get_sede_data(sede_id: str) -> Dict:
    if sede_id not in sede_data:
        sede_data[sede_id] = {
            "queue": [],
            "currentIndex": 0,
            "currentTime": 0.0,
            "totalDuration": 0.0,
            "isPlaying": False
        }
    return sede_data[sede_id]


########################
# Arranque de la App
########################

@app.on_event("startup")
async def on_startup():
    # 1) Iniciar OAuth (si no hay token, pedirá login en el navegador)
    init_youtube_auth()

    # 2) Cargar la playlist local
    global availableSongs
    pl_id = get_playlist_id_from_url(PLAYLIST_LINK)
    if pl_id:
        try:
            availableSongs = fetch_playlist_items(pl_id)
            print(f"Biblioteca cargada con {len(availableSongs)} canciones.")
        except Exception as e:
            print("Error cargando la playlist:", e)
    else:
        print("No se pudo extraer playlistId de PLAYLIST_LINK.")

    # 3) Iniciar la tarea de playback en segundo plano
    asyncio.create_task(playback_updater())


#########################
# Conexiones WebSocket
#########################

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.connection_lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self.connection_lock:
            self.active_connections.append(websocket)
        print(f"Cliente conectado. Total conexiones: {len(self.active_connections)}")

    async def disconnect(self, websocket: WebSocket):
        async with self.connection_lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)
        print(f"Cliente desconectado. Total conexiones: {len(self.active_connections)}")

    async def broadcast(self, message: str):
        async with self.connection_lock:
            for conn in self.active_connections:
                try:
                    await conn.send_text(message)
                except Exception as e:
                    print(f"Error enviando mensaje: {e}")

managers: Dict[str, ConnectionManager] = {}

def get_manager_for_sede(sid: str) -> ConnectionManager:
    if sid not in managers:
        managers[sid] = ConnectionManager()
    return managers[sid]

def get_queue_status_json(sede_id: str) -> str:
    data = get_sede_data(sede_id)
    q = data["queue"]
    i = data["currentIndex"]
    current = q[i] if 0 <= i < len(q) else None
    status = PlaybackStatus(
        currentIndex=i,
        currentTime=data["currentTime"],
        totalDuration=data["totalDuration"],
        isPlaying=data["isPlaying"],
        currentSong=current
    )
    return json.dumps({
        "type": "queue_update",
        "queue": [s.dict() for s in q],
        "status": status.dict()
    })

async def broadcast_queue(sede_id: str):
    mgr = get_manager_for_sede(sede_id)
    await mgr.broadcast(get_queue_status_json(sede_id))


###################
# Tarea de Playback
###################

async def next_song_internal(sede_id: str):
    data = get_sede_data(sede_id)
    q = data["queue"]
    i = data["currentIndex"]

    if i < len(q) - 1:
        data["currentIndex"] += 1
        data["currentTime"] = 0.0
        data["totalDuration"] = q[data["currentIndex"]].duration
        data["isPlaying"] = True
    else:
        # Se acabó la cola
        if availableSongs:
            chosen = random.choice(availableSongs)
            while len(availableSongs) > 1 and chosen.id == (q[i].id if q else None):
                chosen = random.choice(availableSongs)
            q.append(Song(**chosen.dict(exclude={"requestedBy"}), requestedBy="Salchimonster"))
            data["currentIndex"] = len(q) - 1
            data["currentTime"] = 0.0
            data["totalDuration"] = chosen.duration
            data["isPlaying"] = True
        else:
            data["isPlaying"] = False
            data["totalDuration"] = 0.0

    await broadcast_queue(sede_id)

async def playback_updater():
    while True:
        await asyncio.sleep(1)
        async with playback_lock:
            for sid, data in sede_data.items():
                if data["isPlaying"] and 0 <= data["currentIndex"] < len(data["queue"]):
                    data["currentTime"] += 1.0
                    if data["currentTime"] >= data["totalDuration"] and data["totalDuration"] > 0:
                        await next_song_internal(sid)
                    else:
                        await broadcast_queue(sid)


###################
# Manejo WebSocket
###################

async def handle_message(msg: str, ws: WebSocket, sede_id: str):
    d = get_sede_data(sede_id)
    q = d["queue"]

    try:
        obj = json.loads(msg)
        msg_type = obj.get("type")

        if msg_type == "update_time":
            time_val = obj.get("time")
            if isinstance(time_val, (int, float)):
                async with playback_lock:
                    d["currentTime"] = float(time_val)
                    if d["currentTime"] >= d["totalDuration"] and d["totalDuration"] > 0:
                        await next_song_internal(sede_id)
                await broadcast_queue(sede_id)

        elif msg_type == "add_song":
            song_id = obj.get("song_id")
            req_by = obj.get("requestedBy", "Sistema")
            if song_id:
                async with playback_lock:
                    found = next((s for s in availableSongs if s.id == song_id), None)
                    if found:
                        new_song = Song(**found.dict(), requestedBy=req_by)
                        q.append(new_song)
                        if len(q) == 1:
                            d["currentIndex"] = 0
                            d["currentTime"] = 0.0
                            d["isPlaying"] = True
                            d["totalDuration"] = new_song.duration
                        await broadcast_queue(sede_id)
                    else:
                        await ws.send_text(json.dumps({
                            "type": "error",
                            "message": "Canción no encontrada en la biblioteca local"
                        }))

        elif msg_type == "add_song_external":
            yid = obj.get("youtubeId")
            title = obj.get("title")
            thumb = obj.get("thumbnail")
            req_by = obj.get("requestedBy", "Sistema")

            if yid and title and thumb:
                async with playback_lock:
                    # Verificar la duración real
                    details = fetch_video_details([yid])
                    if details:
                        dur = details[0]["duration"]
                        new_song = Song(
                            id=yid,
                            title=title,
                            thumbnail=thumb,
                            duration=dur,
                            requestedBy=req_by
                        )
                        q.append(new_song)
                        if len(q) == 1:
                            d["currentIndex"] = 0
                            d["currentTime"] = 0.0
                            d["isPlaying"] = True
                            d["totalDuration"] = dur

                        # Insertar en playlist de YouTube
                        try:
                            add_video_to_youtube_playlist(yid, YOUTUBE_PLAYLIST_ID)
                            print(f"[INFO] {yid} insertado en playlist YT.")
                        except Exception as e:
                            print("[WARN] No se pudo insertar en playlist de YT:", e)

                        await broadcast_queue(sede_id)
                    else:
                        await ws.send_text(json.dumps({
                            "type": "error",
                            "message": "No se pudo obtener detalles del video de YouTube"
                        }))
            else:
                await ws.send_text(json.dumps({
                    "type": "error",
                    "message": "Datos incompletos para agregar canción externa"
                }))

        else:
            print(f"Tipo de mensaje desconocido: {msg_type}")

    except json.JSONDecodeError:
        print("Error decodificando JSON")
    except Exception as e:
        print("Error general en WebSocket:", e)


@app.websocket("/ws/{sede_id}")
async def websocket_endpoint(websocket: WebSocket, sede_id: str):
    mgr = get_manager_for_sede(sede_id)
    await mgr.connect(websocket)
    try:
        await websocket.send_text(get_queue_status_json(sede_id))
        while True:
            data = await websocket.receive_text()
            await handle_message(data, websocket, sede_id)
    except WebSocketDisconnect:
        await mgr.disconnect(websocket)
    except Exception as e:
        print("WS Error:", e)
        await mgr.disconnect(websocket)


################
# Rutas REST
################

@app.get("/available", response_model=List[Song])
async def get_available_songs():
    return availableSongs

@app.get("/queue/{sede_id}", response_model=List[Song])
async def get_queue(sede_id: str):
    return get_sede_data(sede_id)["queue"]

@app.get("/current/{sede_id}", response_model=PlaybackStatus)
async def get_current_status(sede_id: str):
    d = get_sede_data(sede_id)
    if not d["queue"]:
        async with playback_lock:
            if not d["queue"]:
                await next_song_internal(sede_id)
    current_song = None
    if 0 <= d["currentIndex"] < len(d["queue"]):
        current_song = d["queue"][d["currentIndex"]]

    return PlaybackStatus(
        currentIndex=d["currentIndex"],
        currentTime=d["currentTime"],
        totalDuration=d["totalDuration"],
        isPlaying=d["isPlaying"],
        currentSong=current_song
    )

@app.post("/next/{sede_id}", response_model=PlaybackStatus)
async def next_song(sede_id: str):
    async with playback_lock:
        await next_song_internal(sede_id)
    return await get_current_status(sede_id)

@app.post("/refresh-playlist")
async def refresh_playlist():
    global availableSongs
    pl_id = get_playlist_id_from_url(PLAYLIST_LINK)
    if not pl_id:
        return {"error": "No se pudo extraer playlistId"}
    try:
        availableSongs = fetch_playlist_items(pl_id)
        return {"message": f"Biblioteca refrescada con {len(availableSongs)} canciones."}
    except Exception as e:
        return {"error": f"Error refrescando la playlist: {e}"}


#######################################################
# NUEVO: Buscar videos en YouTube y agregarlos externos
#######################################################

def fetch_video_details(video_ids: List[str]) -> List[Dict]:
    if not video_ids:
        return []
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "contentDetails,status",
        "id": ",".join(video_ids),
        "key": YT_API_KEY
    }
    r = requests.get(url, params=params)
    r.raise_for_status()
    data = r.json().get("items", [])
    results = []
    for v in data:
        vid = v["id"]
        dur = parse_duration(v["contentDetails"]["duration"])
        emb = v["status"].get("embeddable", False)
        c_rating = v["contentDetails"].get("contentRating", {})
        region_r = v["contentDetails"].get("regionRestriction", {})
        blocked = region_r.get("blocked", [])
        if dur > 0 and emb and not c_rating and not blocked:
            results.append({
                "id": vid,
                "duration": dur
            })
    return results

@app.get("/search_youtube")
async def search_youtube(q: str = Query(...)):
    """
    Busca videos en YouTube usando la clave de API.
    Devuelve { videoId, title, thumbnail } para cada resultado válido.
    """
    search_url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "q": q,
        "type": "video",
        "maxResults": 10,
        "key": YT_API_KEY
    }
    resp = requests.get(search_url, params=params)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items", [])
    video_ids = [i["id"]["videoId"] for i in items]

    # Filtrar por embeddable
    details = fetch_video_details(video_ids)
    valid_ids = {d["id"] for d in details}

    results = []
    for i in items:
        vid = i["id"]["videoId"]
        if vid in valid_ids:
            s = i["snippet"]
            title = s["title"]
            thumb = s["thumbnails"]["high"]["url"] if "high" in s["thumbnails"] else ""
            results.append({"videoId": vid, "title": title, "thumbnail": thumb})
    return results

@app.post("/add_song_external/{sede_id}")
async def add_song_external(sede_id: str, payload: AddExternalSongRequest):
    """
    Endpoint REST para agregar una canción no existente en la biblioteca local,
    y además insertarla en la playlist de YouTube con el token OAuth.
    """
    d = get_sede_data(sede_id)
    q = d["queue"]

    found_details = fetch_video_details([payload.youtubeId])
    if not found_details:
        return {"error": "No se pudo obtener detalles del video. (bloqueado/no embeddable)"}
    detail = found_details[0]
    new_song = Song(
        id=payload.youtubeId,
        title=payload.title,
        thumbnail=payload.thumbnail,
        duration=detail["duration"],
        requestedBy=payload.requestedBy
    )

    async with playback_lock:
        q.append(new_song)
        if len(q) == 1:
            d["currentIndex"] = 0
            d["currentTime"] = 0.0
            d["isPlaying"] = True
            d["totalDuration"] = new_song.duration

        # Insertar en playlist
        try:
            add_video_to_youtube_playlist(payload.youtubeId, YOUTUBE_PLAYLIST_ID)
            print(f"[INFO] Video {payload.youtubeId} insertado en playlist YT.")
        except Exception as e:
            print("[ERROR] No se pudo insertar en playlist de YT:", e)

        # Notificar a la sede
        await broadcast_queue(sede_id)

    return {
        "message": f"Canción '{new_song.title}' añadida a la cola de {sede_id}.",
        "song": new_song.dict()
    }
