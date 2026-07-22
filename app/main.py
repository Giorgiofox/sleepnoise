"""SleepNoise control API.

Controls Sonos speakers directly over UPnP (via SoCo) to play the
continuous noise streams served by the local Icecast instance.
Home Assistant is optional: it can call this same API via rest_command.
"""

import logging
import os
import threading

import soco
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sleepnoise")

STREAM_BASE = os.environ.get("STREAM_BASE_URL", "http://127.0.0.1:8010").rstrip("/")
MAX_VOLUME = int(os.environ.get("MAX_VOLUME", "40"))
DEFAULT_ROOM = os.environ.get("DEFAULT_ROOM", "").strip()
SONOS_IPS = [ip.strip() for ip in os.environ.get("SONOS_IPS", "").split(",") if ip.strip()]

SOUNDS = {
    "deep": {
        "mount": "deep.mp3",
        "title": "Deep airflow",
        "desc": "Low-passed brown, soft airflow",
    },
    "brown": {
        "mount": "brown.mp3",
        "title": "Brown noise",
        "desc": "Full spectrum, deep rumble",
    },
    "pink": {
        "mount": "pink.mp3",
        "title": "Pink noise",
        "desc": "Balanced, softer highs",
    },
    "white": {
        "mount": "white.mp3",
        "title": "White noise",
        "desc": "Bright, max masking",
    },
}

app = FastAPI(title="SleepNoise")

_zones_lock = threading.Lock()
_zones: dict[str, soco.SoCo] = {}


def discover(force: bool = False) -> dict[str, soco.SoCo]:
    global _zones
    with _zones_lock:
        if _zones and not force:
            return dict(_zones)
        found: dict[str, soco.SoCo] = {}
        if SONOS_IPS:
            for ip in SONOS_IPS:
                try:
                    z = soco.SoCo(ip)
                    found[z.player_name] = z
                except Exception as e:
                    log.warning("Sonos at %s unreachable: %s", ip, e)
        else:
            try:
                for z in soco.discover(timeout=5) or set():
                    found[z.player_name] = z
            except Exception as e:
                log.warning("Discovery failed: %s", e)
        if found:
            _zones = found
        return dict(_zones)


def get_speaker(name: str | None) -> soco.SoCo:
    name = (name or DEFAULT_ROOM).strip()
    if not name:
        raise HTTPException(400, "No speaker given and DEFAULT_ROOM not configured")
    zones = discover()
    if name not in zones:
        zones = discover(force=True)
    if name not in zones:
        raise HTTPException(404, f"Speaker '{name}' not found. Known: {sorted(zones)}")
    return zones[name]


def sonos_op(speaker_name: str | None, op):
    """Run op(speaker); on failure force a re-discovery and retry once.

    Heals stale cached IPs when a speaker got a new DHCP lease.
    """
    sp = get_speaker(speaker_name)
    try:
        return sp, op(sp)
    except HTTPException:
        raise
    except Exception as first:
        log.warning("Sonos op failed on %s (%s), rediscovering", sp.ip_address, first)
        discover(force=True)
        sp = get_speaker(speaker_name)
        try:
            return sp, op(sp)
        except Exception as e:
            log.error("Sonos op failed after rediscovery: %s", e)
            raise HTTPException(502, f"Sonos error: {e}")


def sleep_timer_remaining(sp: soco.SoCo) -> str | None:
    try:
        r = sp.avTransport.GetRemainingSleepTimerDuration([("InstanceID", 0)])
        return r.get("RemainingSleepTimerDuration") or None
    except Exception:
        return None


def current_sound(uri: str) -> str | None:
    for key, s in SOUNDS.items():
        if uri.endswith("/" + s["mount"]):
            return key
    return None


class PlayRequest(BaseModel):
    speaker: str | None = None
    sound: str = "deep"
    volume: int | None = Field(default=None, ge=0, le=100)
    timer_minutes: int | None = Field(default=None, ge=1, le=1440)


class StopRequest(BaseModel):
    speaker: str | None = None


class TimerRequest(BaseModel):
    speaker: str | None = None
    minutes: int | None = Field(default=None, ge=1, le=1440)


class VolumeRequest(BaseModel):
    speaker: str | None = None
    volume: int = Field(ge=0, le=100)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/status")
def status():
    speakers = []
    for name, z in sorted(discover().items()):
        entry = {"name": name, "ip": z.ip_address}
        try:
            co = z.group.coordinator if z.group else z
            track = co.get_current_track_info()
            transport = co.get_current_transport_info()
            entry.update(
                volume=z.volume,
                state=transport.get("current_transport_state"),
                sound=current_sound(track.get("uri", "")),
                timer_remaining=sleep_timer_remaining(co),
                group_members=sorted({m.player_name for m in z.group.members}) if z.group else [name],
            )
        except Exception as e:
            entry["error"] = str(e)
        speakers.append(entry)
    if any("error" in s for s in speakers):
        threading.Thread(target=discover, kwargs={"force": True}, daemon=True).start()
    return {
        "speakers": speakers,
        "sounds": SOUNDS,
        "default_room": DEFAULT_ROOM,
        "max_volume": MAX_VOLUME,
        "stream_base": STREAM_BASE,
    }


@app.post("/api/discover")
def rediscover():
    zones = discover(force=True)
    return {"speakers": sorted(zones)}


@app.post("/api/play")
def play(req: PlayRequest):
    if req.sound not in SOUNDS:
        raise HTTPException(400, f"Unknown sound '{req.sound}'. Available: {sorted(SOUNDS)}")
    vol = min(req.volume if req.volume is not None else 15, MAX_VOLUME)
    url = f"{STREAM_BASE}/{SOUNDS[req.sound]['mount']}"

    def do_play(sp):
        co = sp.group.coordinator if sp.group else sp
        sp.volume = vol
        co.play_uri(url, title=f"SleepNoise {SOUNDS[req.sound]['title']}", force_radio=True)
        co.set_sleep_timer(req.timer_minutes * 60 if req.timer_minutes else None)

    sp, _ = sonos_op(req.speaker, do_play)
    log.info("play %s on %s vol=%d timer=%s", req.sound, sp.player_name, vol, req.timer_minutes)
    return {"ok": True, "speaker": sp.player_name, "sound": req.sound, "volume": vol,
            "timer_minutes": req.timer_minutes, "url": url}


@app.post("/api/stop")
def stop(req: StopRequest):
    def do_stop(sp):
        co = sp.group.coordinator if sp.group else sp
        co.stop()
        co.set_sleep_timer(None)

    sp, _ = sonos_op(req.speaker, do_stop)
    log.info("stop on %s", sp.player_name)
    return {"ok": True, "speaker": sp.player_name}


@app.post("/api/timer")
def timer(req: TimerRequest):
    def do_timer(sp):
        co = sp.group.coordinator if sp.group else sp
        co.set_sleep_timer(req.minutes * 60 if req.minutes else None)

    sp, _ = sonos_op(req.speaker, do_timer)
    return {"ok": True, "speaker": sp.player_name, "minutes": req.minutes}


@app.post("/api/volume")
def volume(req: VolumeRequest):
    vol = min(req.volume, MAX_VOLUME)

    def do_volume(sp):
        sp.volume = vol

    sp, _ = sonos_op(req.speaker, do_volume)
    return {"ok": True, "speaker": sp.player_name, "volume": vol}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
