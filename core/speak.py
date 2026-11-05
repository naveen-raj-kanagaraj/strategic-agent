"""Voice notes: text to speech (Microsoft Edge's free voices through edge-tts), sent as a Telegram voice message.

Used for urgent alerts: trending stories and breaking news. VOICE_NAME picks the voice (default en-IN-PrabhatNeural).
"""
import asyncio
import os
import re
import subprocess
import tempfile

import requests

VOICE = os.getenv("VOICE_NAME", "en-IN-PrabhatNeural")
VOICE_ALERTS = os.getenv("VOICE_ALERTS", "true").lower() == "true"


def _clean(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text or "")
    text = re.sub(r"[\[\]#*_>|]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:1500]


def synth(text: str) -> str:
    """Path to an OGG/Opus voice file (or MP3 if ffmpeg is missing). Caller deletes it."""
    import edge_tts
    mp3 = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False).name
    asyncio.run(edge_tts.Communicate(_clean(text), VOICE).save(mp3))
    ogg = mp3[:-4] + ".ogg"
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp3, "-c:a", "libopus", "-b:a", "32k", ogg],
                       check=True, timeout=60)
        os.remove(mp3)
        return ogg
    except Exception:
        return mp3


def send_voice(text: str, chat_id: str, token: str = None) -> bool:
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    if not (VOICE_ALERTS and token and chat_id and _clean(text)):
        return False
    path = None
    try:
        path = synth(text)
        kind = "sendVoice" if path.endswith(".ogg") else "sendAudio"
        with open(path, "rb") as f:
            r = requests.post(f"https://api.telegram.org/bot{token}/{kind}", data={"chat_id": chat_id},
                              files={"voice" if kind == "sendVoice" else "audio": f}, timeout=60)
        return r.ok
    except Exception as e:
        print(f"Voice note failed: {str(e)[:120]}")
        return False
    finally:
        if path and os.path.exists(path):
            os.remove(path)
