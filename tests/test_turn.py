"""Smart Turn: features identical to Whisper's (checked against transformers to 1e-5 on 2026-10-24), and the real model
tells a finished phrase from one cut in the middle. Runs in the speech worker's virtualenv, where it runs for real
(numpy and onnxruntime live there, not in JARVIS's own)."""
import json
import subprocess
from pathlib import Path

import pytest

from jarvis.voice.tts import SupertonicTTS

ROOT = Path(__file__).resolve().parents[1]
TURN = ROOT / "jarvis/voice/turn.py"
MODEL = Path.home() / ".local/share/jarvis/stt/turn/smart-turn-v3.2-cpu.onnx"
STT_PY = Path.home() / ".local/share/jarvis/stt/.venv/bin/python"
LOAD = ("import importlib.util,json,sys,wave,numpy as np;s=importlib.util.spec_from_file_location('t',sys.argv[1]);"
        "t=importlib.util.module_from_spec(s);s.loader.exec_module(t);")


def run(code: str, *args) -> object:
    out = subprocess.run([str(STT_PY), "-I", "-c", LOAD + code, str(TURN), *map(str, args)], capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-500:]
    return json.loads(out.stdout)


@pytest.mark.skipif(not STT_PY.exists(), reason="the speech worker is not installed")
def test_features_shape_and_layout():
    shape, contiguous, dtype, filters_ok = run(
        "f=t.features(np.random.default_rng(0).normal(0,0.1,48000).astype(np.float32));fb=t.mel_filters();"
        "print(json.dumps([list(f.shape),bool(f.flags['C_CONTIGUOUS']),str(f.dtype),"
        "bool(fb.shape==(201,80) and (fb.sum(axis=0)>0).all())]))")
    assert shape == [80, 800] and contiguous and dtype == "float32" and filters_ok


@pytest.mark.skipif(not (MODEL.exists() and STT_PY.exists() and SupertonicTTS().available()),
                    reason="Smart Turn model / Supertonic not installed")
async def test_real_model_hears_a_phrase_cut_in_the_middle(tmp_path):
    tts = SupertonicTTS()
    try:
        audio = await tts.synth("Готово. Я открыл папку с лабораторными и нашёл картинку шуруповёрта. Что сделать "
                                "дальше?", "M4", "ru")
    finally:
        await tts.stop()
    (tmp_path / "phrase.wav").write_bytes(audio)
    full, cut = run(
        "w=wave.open(sys.argv[3]);sr=w.getframerate();"
        "a=np.frombuffer(w.readframes(w.getnframes()),dtype=np.int16).astype(np.float32)/32768;"
        "a=np.interp(np.linspace(0,len(a)-1,int(len(a)*16000/sr)),np.arange(len(a)),a).astype(np.float32);"
        "m=t.TurnModel(sys.argv[2]);print(json.dumps([m.complete(a),m.complete(a[:int(len(a)*0.55)])]))",
        MODEL, tmp_path / "phrase.wav")
    assert full > 0.6 and cut < 0.3, (full, cut)
