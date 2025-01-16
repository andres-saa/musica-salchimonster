import random
from typing import List, Optional, Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import requests
from urllib.parse import urlparse, parse_qs
import json
import asyncio
import isodate

app = FastAPI(
    title="Salchimonster Backend (Multi-Sede)",
    description="API con biblioteca única y múltiples colas (una por sede) + WebSockets para sincronización en tiempo real",
    version="0.3.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class Song(BaseModel):
    id: str
    title: str
    thumbnail: str
    duration: float  # Duración en segundos
    requestedBy: Optional[str] = "Sistema"

class PlaybackStatus(BaseModel):
    currentIndex: int
    currentTime: float
    totalDuration: float
    isPlaying: bool
    currentSong: Optional[Song] = None

# Biblioteca global (compartida entre todas las sedes)
availableSongs: List[Song] = []

# Diccionario que guarda el estado de cada sede
# Estructura:
# sede_data = {
#   "sede1": {
#       "queue": [],
#       "currentIndex": 0,
#       "currentTime": 0.0,
#       "totalDuration": 0.0,
#       "isPlaying": False
#   },
#   "sede2": {
#       ...
#   }
# }
sede_data: Dict[str, Dict] = {}

# Lock global para no solapar la actualización de reproducción
playback_lock = asyncio.Lock()

# Reemplaza con tu propia clave de API de YouTube
YT_API_KEY = "AIzaSyD1D4di1V3GaXtM5EL-4LGldvvz-fH0bPI"
PLAYLIST_LINK = "https://youtube.com/playlist?list=PLzeOgQjW3-PsxvQS-DGut16T-dpVK2Hf5&si=c8vOGdVME92ciMC3"

def get_playlist_id_from_url(url: str) -> Optional[str]:
    """
    Extrae el playlistId de una URL de playlist de YouTube.
    """
    try:
        parsed_url = urlparse(url)
        query_params = parse_qs(parsed_url.query)
        playlist_ids = query_params.get("list")
        if playlist_ids:
            return playlist_ids[0]
    except Exception as e:
        print("Error extrayendo playlistId:", e)
    return None

def parse_duration(duration_iso: str) -> float:
    """
    Convierte una duración en formato ISO 8601 en segundos.
    """
    return isodate.parse_duration(duration_iso).total_seconds()

def fetch_playlist_items(playlist_id: str) -> List[Song]:
    """
    Obtiene los videos de una playlist y filtra aquellos que tengan
    restricciones (no embeddables, bloqueados por región o con contentRating).
    """
    base_url = "https://www.googleapis.com/youtube/v3/playlistItems"
    video_base_url = "https://www.googleapis.com/youtube/v3/videos"

    # 1) Obtener la lista de items (IDs de video) de la playlist
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

    # 2) Obtener información detallada de los videos en un solo request
    video_params = {
        "part": "contentDetails,status",
        "id": ",".join(video_ids),
        "key": YT_API_KEY
    }
    video_resp = requests.get(video_base_url, params=video_params)
    video_resp.raise_for_status()
    video_data = video_resp.json().get("items", [])

    # 3) Construir diccionarios de duración, embeddable, rating y restricciones de región
    duration_dict = {
        video["id"]: parse_duration(video["contentDetails"]["duration"])
        for video in video_data
    }
    embeddable_dict = {
        video["id"]: video["status"].get("embeddable", False)
        for video in video_data
    }
    content_rating_dict = {
        video["id"]: video["contentDetails"].get("contentRating", {})
        for video in video_data
    }
    region_restriction_dict = {
        video["id"]: video["contentDetails"].get("regionRestriction", {})
        for video in video_data
    }

    results = []
    for item in items:
        snippet = item["snippet"]
        video_id = snippet["resourceId"]["videoId"]
        title = snippet["title"]
        thumbnail = snippet.get("thumbnails", {}).get("high", {}).get("url", "")
        
        duration = duration_dict.get(video_id, 0)
        is_embeddable = embeddable_dict.get(video_id, False)
        content_rating = content_rating_dict.get(video_id, {})
        region_restriction = region_restriction_dict.get(video_id, {})

        # Verifica si el video está bloqueado por región
        blocked_regions = region_restriction.get("blocked", [])
        # Con esto, se excluye si:
        #  - no es embeddable
        #  - tiene un contentRating no vacío (ej. restricciones de edad)
        #  - está bloqueado en alguna región (si se quiere ser estricto, basta con que exista)

        if (
            duration > 0 
            and is_embeddable 
            and not content_rating
            and not blocked_regions
        ):
            results.append(
                Song(
                    id=video_id,
                    title=title,
                    thumbnail=thumbnail,
                    duration=duration
                )
            )
        else:
            print(f"Excluyendo video por restricciones: {title} (ID: {video_id})")

    return results

@app.on_event("startup")
async def load_available_songs():
    """
    Al iniciar la aplicación, carga la lista de canciones disponibles
    desde la playlist configurada en PLAYLIST_LINK.
    """
    global availableSongs
    playlist_id = get_playlist_id_from_url(PLAYLIST_LINK)
    if not playlist_id:
        print("No se pudo extraer 'list=' de la URL de la playlist.")
        return
    try:
        loaded = fetch_playlist_items(playlist_id)
        availableSongs = loaded
        print(f"Biblioteca cargada con {len(availableSongs)} canciones.")
    except Exception as e:
        print("Error cargando la playlist:", e)

#########################
# Manejo de Conexiones  #
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
            for connection in self.active_connections:
                try:
                    await connection.send_text(message)
                except Exception as e:
                    print(f"Error enviando mensaje: {e}")

# Un diccionario de managers, uno por sede
managers: Dict[str, ConnectionManager] = {}

def get_manager_for_sede(sede_id: str) -> ConnectionManager:
    """
    Devuelve (o crea) el ConnectionManager correspondiente a la sede dada.
    """
    if sede_id not in managers:
        managers[sede_id] = ConnectionManager()
    return managers[sede_id]

#####################
# Funciones Helpers #
#####################

def get_sede_data(sede_id: str) -> Dict:
    """
    Devuelve (o inicializa) la estructura de datos de la sede indicada.
    """
    if sede_id not in sede_data:
        sede_data[sede_id] = {
            "queue": [],
            "currentIndex": 0,
            "currentTime": 0.0,
            "totalDuration": 0.0,
            "isPlaying": False
        }
    return sede_data[sede_id]

def get_queue_status_json(sede_id: str) -> str:
    """
    Construye y devuelve un JSON con el estado de la cola y la canción actual para la sede dada.
    """
    data = get_sede_data(sede_id)
    queue = data["queue"]
    current_index = data["currentIndex"]
    current_song = queue[current_index] if 0 <= current_index < len(queue) else None

    status = PlaybackStatus(
        currentIndex=current_index,
        currentTime=data["currentTime"],
        totalDuration=data["totalDuration"],
        isPlaying=data["isPlaying"],
        currentSong=current_song
    )

    return json.dumps({
        "type": "queue_update",
        "queue": [song.dict() for song in queue],
        "status": status.dict(),
    })

async def broadcast_queue(sede_id: str):
    """
    Envía a todos los clientes (de la sede dada) la actualización de la cola en formato JSON.
    """
    manager = get_manager_for_sede(sede_id)
    await manager.broadcast(get_queue_status_json(sede_id))

async def next_song_internal(sede_id: str):
    """
    Avanza a la siguiente canción de la cola de la sede indicada. 
    Si no hay más, elige una canción aleatoria de la biblioteca y la agrega. 
    Si no hay canciones en la biblioteca, detiene la reproducción.
    """
    data = get_sede_data(sede_id)
    queue = data["queue"]
    current_index = data["currentIndex"]
    current_time = data["currentTime"]
    total_duration = data["totalDuration"]
    is_playing = data["isPlaying"]

    # Si no hemos llegado al final de la cola, simplemente avanzamos
    if current_index < len(queue) - 1:
        data["currentIndex"] = current_index + 1
        data["currentTime"] = 0.0
        data["totalDuration"] = queue[data["currentIndex"]].duration
        data["isPlaying"] = True
    else:
        # Hemos llegado al final de la cola. Tomamos una canción de la biblioteca
        # al azar y la ponemos en la cola (si hay disponibles).
        if availableSongs:
            chosen = random.choice(availableSongs)
            # Opcional: evitar repetir la misma canción si hay más de una disponible
            while len(availableSongs) > 1 and chosen.id == (queue[current_index].id if queue else None):
                chosen = random.choice(availableSongs)

            # Agrega la canción seleccionada al final de la cola
            queue.append(
                Song(**chosen.dict(exclude={"requestedBy"}), requestedBy="Salchimonster")
            )
            data["currentIndex"] = len(queue) - 1
            data["currentTime"] = 0.0
            data["totalDuration"] = chosen.duration
            data["isPlaying"] = True
        else:
            # No hay canciones disponibles
            data["isPlaying"] = False
            data["totalDuration"] = 0.0

    await broadcast_queue(sede_id)

async def handle_message(message: str, websocket: WebSocket, sede_id: str):
    """
    Procesa los mensajes recibidos a través del WebSocket (para la sede dada).
    """
    data = get_sede_data(sede_id)
    queue = data["queue"]
    current_index = data["currentIndex"]

    global playback_lock

    try:
        parsed = json.loads(message)
        msg_type = parsed.get("type")
        
        if msg_type == "update_time":
            time_val = parsed.get("time")
            if isinstance(time_val, (int, float)):
                async with playback_lock:
                    data["currentTime"] = float(time_val)
                    # Verificar si ya se acabó la canción
                    if data["currentTime"] >= data["totalDuration"] and data["totalDuration"] > 0:
                        await next_song_internal(sede_id)
                await broadcast_queue(sede_id)

        elif msg_type == "add_song":
            song_id = parsed.get("song_id")
            requested_by = parsed.get("requestedBy", "Sistema")
            if song_id:
                async with playback_lock:
                    found = next((s for s in availableSongs if s.id == song_id), None)
                    if found:
                        # Creamos una nueva instancia de Song con el "requestedBy"
                        song_data = found.dict()
                        song_data["requestedBy"] = requested_by
                        new_song = Song(**song_data)

                        queue.append(new_song)
                        # Si la cola estaba vacía, iniciamos reproducción
                        if len(queue) == 1:
                            data["currentIndex"] = 0
                            data["currentTime"] = 0.0
                            data["isPlaying"] = True
                            data["totalDuration"] = new_song.duration

                        await broadcast_queue(sede_id)
                    else:
                        # Si no se encontró la canción en la biblioteca
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "message": "Canción no encontrada"
                        }))
                # end with lock
            else:
                print("song_id no proporcionado en 'add_song'")

        else:
            print(f"Tipo de mensaje desconocido: {msg_type}")

    except json.JSONDecodeError:
        print("Error decodificando JSON")
    except Exception as e:
        print(f"Error general en WebSocket: {e}")

####################
# Endpoint WebSocket
####################

@app.websocket("/ws/{sede_id}")
async def websocket_endpoint(websocket: WebSocket, sede_id: str):
    """
    Maneja las conexiones WebSocket para enviar/recibir actualizaciones en tiempo real
    de la cola de reproducción de la sede especificada.
    """
    manager = get_manager_for_sede(sede_id)
    await manager.connect(websocket)
    try:
        # Al conectar, envía el estado inicial de la cola
        await websocket.send_text(get_queue_status_json(sede_id))

        while True:
            data = await websocket.receive_text()
            await handle_message(data, websocket, sede_id)
    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except Exception as e:
        print(f"Error en WebSocket: {e}")
        await manager.disconnect(websocket)

###################
# Tarea de playback
###################

async def playback_updater():
    """
    Tarea asíncrona global que cada segundo actualiza el tiempo de reproducción 
    de TODAS las sedes y, si la canción termina, avanza a la siguiente en cada sede.
    """
    while True:
        await asyncio.sleep(1)
        async with playback_lock:
            for sid, data in sede_data.items():
                if data["isPlaying"] and 0 <= data["currentIndex"] < len(data["queue"]):
                    data["currentTime"] += 1.0
                    if data["currentTime"] >= data["totalDuration"] and data["totalDuration"] > 0:
                        await next_song_internal(sid)
                    else:
                        # Notificamos a los clientes de la sede el cambio de tiempo
                        await broadcast_queue(sid)

@app.on_event("startup")
async def start_playback_updater():
    """
    Inicia la tarea asíncrona para actualizar el tiempo de reproducción.
    """
    asyncio.create_task(playback_updater())

###############
# Endpoints REST
###############

@app.get("/available", response_model=List[Song])
async def get_available_songs_handler():
    """
    Retorna la lista de canciones disponibles (biblioteca global).
    """
    return availableSongs

@app.get("/queue/{sede_id}", response_model=List[Song])
async def get_queue(sede_id: str):
    """
    Retorna la cola de reproducción actual de la sede dada.
    """
    data = get_sede_data(sede_id)
    return data["queue"]

@app.get("/current/{sede_id}", response_model=PlaybackStatus)
async def get_current_status(sede_id: str):
    """
    Retorna el estado actual de la reproducción (canción, tiempo, etc.) de la sede dada.
    Si la cola está vacía, se intenta avanzar a la siguiente canción 
    (para asegurar que siempre haya algo en reproducción si es posible).
    """
    data = get_sede_data(sede_id)

    # Si la cola está vacía, intenta generar la siguiente canción
    if not data["queue"]:
        async with playback_lock:
            if not data["queue"]:
                await next_song_internal(sede_id)

    # Cargar el estado
    current_song = None
    if 0 <= data["currentIndex"] < len(data["queue"]):
        current_song = data["queue"][data["currentIndex"]]

    status = PlaybackStatus(
        currentIndex=data["currentIndex"],
        currentTime=data["currentTime"],
        totalDuration=data["totalDuration"],
        isPlaying=data["isPlaying"],
        currentSong=current_song
    )
    return status

@app.post("/next/{sede_id}", response_model=PlaybackStatus)
async def next_song(sede_id: str):
    """
    Avanza manualmente a la siguiente canción de la cola de la sede dada.
    """
    async with playback_lock:
        await next_song_internal(sede_id)
    return await get_current_status(sede_id)
