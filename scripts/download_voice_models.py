"""Download the local speech models once, so the first call doesn't wait for them.

    python scripts/download_voice_models.py

- Kokoro text-to-speech (about 120 MB) into models/, from the kokoro-onnx
  project's GitHub release.
- The faster-whisper speech-to-text model named by STT_MODEL (base.en is about
  150 MB) into the Hugging Face cache.

Then fetch LiveKit's voice and turn detectors with:  sn-agent voice download-files
All of these are free, open models that run on this machine.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from sn_agent.config import get_settings  # noqa: E402

KOKORO_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"


def fetch(url: str, target: Path) -> None:
    if target.exists() and target.stat().st_size > 0:
        print(f"  {target} already present")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"  downloading {url}")
    import ssl

    try:
        import truststore

        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:  # pragma: no cover
        context = ssl.create_default_context()
    partial = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(url, context=context) as response, partial.open("wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while chunk := response.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r    {done * 100 // total:3d}%", end="", flush=True)
    partial.replace(target)
    print(f"\r  saved {target} ({done / 1e6:.0f} MB)")


def main() -> int:
    # Office proxies re-sign HTTPS with their own CA, which lives in the Windows
    # certificate store; trust that store (verification stays on) so the
    # Hugging Face download works as well as the GitHub one.
    from sn_agent.intake.local_speech import trust_system_certificates

    trust_system_certificates()
    settings = get_settings()
    print("Kokoro text-to-speech:")
    model = ROOT / settings.tts_model_path if not Path(settings.tts_model_path).is_absolute() else Path(settings.tts_model_path)
    voices = ROOT / settings.tts_voices_path if not Path(settings.tts_voices_path).is_absolute() else Path(settings.tts_voices_path)
    fetch(f"{KOKORO_RELEASE}/{model.name}", model)
    fetch(f"{KOKORO_RELEASE}/{voices.name}", voices)

    print(f"faster-whisper speech-to-text ({settings.stt_model}):")
    from faster_whisper import download_model

    path = download_model(settings.stt_model)
    print(f"  ready in {path}")

    print("\nDone. Next:  sn-agent voice download-files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
