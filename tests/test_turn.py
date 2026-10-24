"""Smart Turn: features identical to Whisper's (checked against transformers to 1e-5 on 2026-10-24), and the real model
tells a finished phrase from one cut in the middle. Runs in the speech worker's virtualenv, where it runs for real
(numpy and onnxruntime live there, not in JARVIS's own)."""
import json
import subprocess
from pathlib import Path

import pytest


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


@pytest.mark.skipif(not (MODEL.exists() and STT_PY.exists()), reason="Smart Turn model not installed")
def test_real_model_finished_vs_cut_in_the_middle():
    """A fixed recording (Supertonic M4, «Готово. Я открыл папку с лабораторными и нашёл картинку шуруповёрта. Что
    сделать дальше?»). Finished phrases scored 0.85–0.98 over 5 syntheses — never held; a phrase cut mid-thought is
    caught only at some cut points (0.01–0.99), which is why only confident answers are used."""
    full, cut = run(
        "w=wave.open(sys.argv[3]);a=np.frombuffer(w.readframes(w.getnframes()),dtype=np.int16).astype(np.float32)/32768;"
        "m=t.TurnModel(sys.argv[2]);print(json.dumps([m.complete(a),m.complete(a[:int(len(a)*0.45)])]))",
        MODEL, ROOT / "tests/fixtures/phrase_ru_16k.wav")
    assert full > 0.6 and cut < 0.3, (full, cut)
