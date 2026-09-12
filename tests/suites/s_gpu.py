"""GPU 兜底状态机（不加载真模型，全部用假对象驱动）。

覆盖"用户明确要求"的三类：拿不到 GPU、GPU 用不了（加载期就失败）、运行期 CUDA 错，
以及"探测不许反复死磕"与"非设备故障不许被兜底掩盖"。
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "suites"))
from _helpers import ENGINE                                            # noqa: E402

SUITE = {"id": "gpu", "title": "GPU 兜底与探测节流（假模型）", "tags": ["fast"]}


def fresh(env=None):
    import os
    for k in ("KB_DEVICE", "KB_EMBED_MODEL", "CUDA_VISIBLE_DEVICES"):
        os.environ.pop(k, None)
    if env:
        os.environ.update(env)
    spec = importlib.util.spec_from_file_location("kbe_gpu_%d" % id(env), ENGINE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeTorchNoCuda:
    __version__ = "0.0.0+fake"

    class cuda:
        calls = 0

        @staticmethod
        def is_available():
            FakeTorchNoCuda.cuda.calls += 1
            return False

        @staticmethod
        def empty_cache():
            pass


class FakeModel:
    device = "cuda:0"

    def to(self, d):
        self.device = d
        return self


class WrapsModel:
    """CrossEncoder 形态：自己没有 .to()，内部 model 有。"""
    def __init__(self):
        self.model = FakeModel()
        self.device = "cuda:0"


def run(ctx):
    real_torch = sys.modules.get("torch")

    # —— 1. 没有 GPU：直接 CPU，且探测只发生一次 ——
    k = fresh()
    sys.modules["torch"] = FakeTorchNoCuda
    FakeTorchNoCuda.cuda.calls = 0
    k._CUDA = None
    k._GPU_OK = None
    ctx.check("cuda 不可用时 _cuda_available() 为 False", k._cuda_available() is False)
    ctx.check("gpu_usable() 为 False", k._gpu_usable() is False)
    for _ in range(5):
        k._cuda_available()
        k._gpu_usable()
        k._batch_size("KB_EMBED_BATCH", 32, 128)
    ctx.check("设备探测不被反复死磕（15 次调用只问一次）", FakeTorchNoCuda.cuda.calls == 1,
              FakeTorchNoCuda.cuda.calls)
    ctx.check("批大小退回 CPU 默认值", k._batch_size("KB_EMBED_BATCH", 32, 128) == 32)

    # —— 2. 显式 cuda 但没有 GPU：不传 cuda，直接 CPU ——
    k2 = fresh({"KB_DEVICE": "cuda"})
    sys.modules["torch"] = FakeTorchNoCuda
    k2._CUDA = None
    k2._GPU_OK = None
    ctx.check("KB_DEVICE=cuda 且无 GPU → device_kwargs 给 cpu", k2._device_kwargs() == {"device": "cpu"},
              k2._device_kwargs())

    # —— 3. 加载期 CUDA 错：退 CPU 一次 + 粘性关闭 + 之后不再尝试 GPU ——
    sys.modules["torch"] = real_torch if real_torch else FakeTorchNoCuda
    k3 = fresh()
    k3._CUDA = None
    k3._GPU_OK = None
    tries = []

    def build_broken(**dkw):
        tries.append(dict(dkw))
        if not dkw or dkw.get("device") == "cuda":
            raise RuntimeError("CUDA error: no kernel image is available for execution on the device")
        return "MODEL_ON_CPU"

    model, fell_back = k3._load_with_cpu_fallback("embed", build_broken)
    ctx.check("加载期 CUDA 错 → 返回 CPU 上的模型", model == "MODEL_ON_CPU" and fell_back is True)
    ctx.check("两次尝试：先按策略、再显式 CPU", tries[:2] == [{}, {"device": "cpu"}], tries)
    ctx.check("粘性关闭：gpu_usable() 变 False", k3._gpu_usable() is False)
    ctx.check("关闭后只尝试一次且直接 CPU",
              (lambda: (tries.clear(), k3._load_with_cpu_fallback("rerank", lambda **d: (tries.append(d), "X")[1]),
                        tries == [{"device": "cpu"}]))()[2])

    # —— 4. 非 CUDA 故障不被兜底掩盖 ——
    k4 = fresh()
    seen = []

    def build_missing(**dkw):
        seen.append(dict(dkw))
        raise FileNotFoundError("no such model directory")

    try:
        k4._load_with_cpu_fallback("embed", build_missing)
        ctx.check("FileNotFoundError 原样上抛", False, "被兜底吞掉了")
    except FileNotFoundError:
        ctx.check("FileNotFoundError 原样上抛（没有偷偷退 CPU）", len(seen) == 1, seen)

    # —— 5. 运行期 CUDA 错（OOM 与非 OOM）：缩批 → 移到 CPU → 跑通 ——
    for label, err in (("OOM", RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")),
                       ("非 OOM 的 CUDA 错", RuntimeError("CUDA error: device-side assert triggered"))):
        k5 = fresh()
        k5._CUDA = True
        k5._GPU_OK = True
        seq = []

        class M(FakeModel):
            pass

        m = M()

        def run_once(bs):
            seq.append(bs)
            if len(seq) <= 2:
                raise err
            return ["ok@%d" % bs]

        out = k5._with_device_retry("embed", m, run_once)
        ctx.check("%s：最终跑通且批大小为 4" % label, out == ["ok@4"], out)
        ctx.check("%s：调用序列 [默认批, 4, 4]" % label, seq == [128, 4, 4], seq)
        ctx.check("%s：模型已移到 CPU" % label, str(m.device) == "cpu", m.device)

    # —— 6. 设备报告字段 ——
    k6 = fresh()
    k6._set_device_note("embed", "CUDA OOM → 已回退 CPU")
    rep = k6.device_report()
    ctx.check("device_report 带 note", "note" in rep and "已回退 CPU" in rep["note"], rep.get("note"))
    k6._set_device_note("embed", None)
    ctx.check("正常加载后 note 被清除", k6._device_note_text() is None)

    # —— 7. CrossEncoder 形态的 CPU 回退 ——
    k7 = fresh()
    w = WrapsModel()
    ctx.check("无 .to() 时退到内部 model.to()", k7._move_model_to_cpu(w) is True and str(w.device) == "cpu",
              w.device)
