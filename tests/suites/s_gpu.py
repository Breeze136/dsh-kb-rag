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
    for k in ("KB_DEVICE", "KB_EMBED_MODEL", "CUDA_VISIBLE_DEVICES", "KB_GPU_PROBE",
              "KB_EMBED_BATCH", "KB_RERANK_BATCH"):
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


class FakeTensor:
    def __matmul__(self, other):
        return self

    def sum(self):
        return self

    def item(self):
        return 0.0


class FakeTorchProbeFail:
    """驱动报告有卡，但**真算一次**就炸（驱动/内核不匹配、容器、独占模式都长这样）。"""
    __version__ = "0.0.0+fake"
    zeros_calls = 0

    class cuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def empty_cache():
            pass

        @staticmethod
        def get_device_properties(i):
            raise RuntimeError("no device properties")

    @staticmethod
    def zeros(*a, **kw):
        FakeTorchProbeFail.zeros_calls += 1
        raise RuntimeError("CUDA error: unknown error")


class FakeTorchOK:
    """驱动有卡且真能算。"""
    __version__ = "0.0.0+fake"
    zeros_calls = 0

    class cuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def empty_cache():
            pass

        @staticmethod
        def get_device_properties(i):
            class P:
                total_memory = 8 * 1024 ** 3
            return P()

    @staticmethod
    def zeros(*a, **kw):
        FakeTorchOK.zeros_calls += 1
        return FakeTensor()


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

    # —— 5. 运行期 CUDA 错（OOM 与非 OOM）：折半 → 4 → 跑通，并记住批大小 ——
    for label, err in (("OOM", RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")),
                       ("非 OOM 的 CUDA 错", RuntimeError("CUDA error: device-side assert triggered"))):
        k5 = fresh({"KB_GPU_PROBE": "0"})
        k5._CUDA = True
        k5._GPU_OK = True
        k5._VRAM_GB = 24.0      # 锁死显存分级：24GB → 嵌入批 128（否则会跟着本机显存变）
        seq = []
        m = FakeModel()

        def run_once(bs):
            seq.append(bs)
            if len(seq) <= 2:
                raise err
            return ["ok@%d" % bs]

        out = k5._with_device_retry("embed", m, run_once)
        ctx.check("%s：折半到 4 后跑通" % label, out == ["ok@4"], out)
        ctx.check("%s：调用序列 [128, 64, 4]（不是一步掉到 4）" % label, seq == [128, 64, 4], seq)
        ctx.check("%s：模型仍在 GPU（没白退 CPU）" % label, str(m.device) == "cuda:0", m.device)
        ctx.check("%s：记住跑通的批大小" % label,
                  k5._BATCH_EFFECTIVE.get(("embed", "gpu")) == 4, dict(k5._BATCH_EFFECTIVE))

    # —— 5b. 梯次全失败 → 移到 CPU，且批大小回到 **CPU 默认** ——
    k5b = fresh({"KB_GPU_PROBE": "0"})
    k5b._CUDA = True
    k5b._GPU_OK = True
    k5b._VRAM_GB = 24.0
    seq2 = []
    m2 = FakeModel()

    def oom_until_cpu(bs):
        seq2.append(bs)
        if str(m2.device) != "cpu":
            raise RuntimeError("CUDA out of memory")
        return ["ok-cpu@%d" % bs]

    out2 = k5b._with_device_retry("embed", m2, oom_until_cpu)
    ctx.check("梯次全失败 → 模型移到 CPU 并用 CPU 默认批 32 跑完", out2 == ["ok-cpu@32"], out2)
    ctx.check("调用序列 [128, 64, 4, 32]", seq2 == [128, 64, 4, 32], seq2)
    ctx.check("CPU 侧的批大小被记住", k5b._BATCH_EFFECTIVE.get(("embed", "cpu")) == 32,
              dict(k5b._BATCH_EFFECTIVE))

    # —— 5c. 记忆生效：第二次调用不再从 128 撞一遍 ——
    k5c = fresh({"KB_GPU_PROBE": "0"})
    k5c._CUDA = True
    k5c._GPU_OK = True
    k5c._VRAM_GB = 24.0
    m3 = FakeModel()
    calls = []

    def shrink_once(bs):
        calls.append(bs)
        if bs >= 128:
            raise RuntimeError("CUDA out of memory")
        return ["ok@%d" % bs]

    k5c._with_device_retry("embed", m3, shrink_once)
    k5c._with_device_retry("embed", m3, shrink_once)
    ctx.check("第二次调用直接从记忆的 64 开始（不再撞 128）", calls == [128, 64, 64], calls)

    # —— 6. 设备报告字段 ——
    k6 = fresh()
    k6._set_device_note("embed", "CUDA OOM → 已回退 CPU")
    rep = k6.device_report()
    ctx.check("device_report 带 note", "note" in rep and "已回退 CPU" in rep["note"], rep.get("note"))
    k6._set_device_note("embed", None)
    ctx.check("正常加载后 note 被清除", k6._device_note_text() is None)
    sys.modules["torch"] = FakeTorchOK
    k6._CUDA = None
    k6._GPU_OK = None
    k6._PROBE = None
    k6._VRAM_GB = None
    k6._gpu_usable()                     # 真实探测一次（体检本身不触发探测，见 6b）
    rep2 = k6.device_report()
    ctx.check("device_report 带 torch/显存/批大小/探测结果",
              rep2.get("probe") == "ok" and rep2.get("vram_gb") == 8.0
              and isinstance(rep2.get("batch"), dict)
              and rep2["batch"]["embed"]["gpu_tier"] == 128,   # 8GB 不降级（与历史默认一致）
              {k: rep2.get(k) for k in ("probe", "vram_gb", "batch")})

    # —— 6b. 只读体检不许有副作用：不触发探测、也不因此把 GPU 关掉 ——
    k6b = fresh()
    sys.modules["torch"] = FakeTorchProbeFail
    FakeTorchProbeFail.zeros_calls = 0
    k6b._CUDA = None
    k6b._GPU_OK = None
    k6b._PROBE = None
    rep6b = k6b.device_report()
    ctx.check("只读体检不触发探测、不关闭 GPU",
              FakeTorchProbeFail.zeros_calls == 0 and k6b._PROBE is None and k6b._GPU_OK is None
              and rep6b.get("gpu_usable") is True,
              {k: rep6b.get(k) for k in ("gpu_usable", "probe")})

    # —— 7. CrossEncoder 形态的 CPU 回退 ——
    sys.modules["torch"] = real_torch if real_torch else FakeTorchNoCuda
    k7 = fresh()
    w = WrapsModel()
    ctx.check("无 .to() 时退到内部 model.to()", k7._move_model_to_cpu(w) is True and str(w.device) == "cpu",
              w.device)

    # —— 8. 真实可用性探测：is_available=True 但算不了 → 粘性关闭 + 原因可读 ——
    k8 = fresh()
    sys.modules["torch"] = FakeTorchProbeFail
    FakeTorchProbeFail.zeros_calls = 0
    k8._CUDA = None
    k8._GPU_OK = None
    k8._PROBE = None
    ctx.check("探测失败 → gpu_usable() 为 False", k8._gpu_usable() is False)
    ctx.check("探测失败 → device_kwargs 直接给 cpu", k8._device_kwargs() == {"device": "cpu"},
              k8._device_kwargs())
    ctx.check("探测失败的原因进报告", "unknown error" in (k8.device_report().get("gpu_disabled_reason") or ""),
              k8.device_report().get("gpu_disabled_reason"))
    ctx.check("报告标 probe=failed", k8.device_report().get("probe") == "failed")
    for _ in range(4):
        k8._gpu_usable()
    ctx.check("探测只做一次（结果缓存）", FakeTorchProbeFail.zeros_calls == 1, FakeTorchProbeFail.zeros_calls)

    # —— 9. 探测成功：真算得动才算可用 ——
    k9 = fresh()
    sys.modules["torch"] = FakeTorchOK
    k9._CUDA = None
    k9._GPU_OK = None
    k9._PROBE = None
    ctx.check("探测成功 → gpu_usable() 为 True", k9._gpu_usable() is True)
    ctx.check("探测成功 → 报告标 probe=ok", k9.device_report().get("probe") == "ok")

    # —— 10. KB_GPU_PROBE=0：跳过探测，保留旧行为（想强行试 GPU 的逃生口） ——
    k10 = fresh({"KB_GPU_PROBE": "0"})
    sys.modules["torch"] = FakeTorchProbeFail
    FakeTorchProbeFail.zeros_calls = 0
    k10._CUDA = None
    k10._GPU_OK = None
    k10._PROBE = None
    ctx.check("KB_GPU_PROBE=0 → 不做真实计算，直接按 is_available 走", k10._gpu_usable() is True)
    ctx.check("KB_GPU_PROBE=0 → 一次都没真算", FakeTorchProbeFail.zeros_calls == 0,
              FakeTorchProbeFail.zeros_calls)

    # —— 11. 显存分级批大小 ——
    k11 = fresh({"KB_GPU_PROBE": "0"})
    for vram, emb, rer in ((2.0, 16, 8), (6.0, 64, 32), (8.0, 128, 64), (16.0, 128, 64), (24.0, 128, 64)):
        k11._VRAM_GB = vram
        ctx.check("显存 %.1fGB → 嵌入批 %d / 精排批 %d" % (vram, emb, rer),
                  k11._gpu_batch("embed", 128) == emb and k11._gpu_batch("rerank", 64) == rer,
                  (k11._gpu_batch("embed", 128), k11._gpu_batch("rerank", 64)))
    k11._VRAM_GB = None
    ctx.check("显存查不到 → 用历史默认（不改变未知环境）", k11._gpu_batch("embed", 128) == 128)

    # —— 12. 批大小跟着模型**实际**所在设备走 ——
    k12 = fresh({"KB_GPU_PROBE": "0"})
    cpu_model = FakeModel()
    cpu_model.device = "cpu"
    k12._GPU_OK = True                        # 策略上"能用 GPU"，但模型已经在 CPU 上
    ctx.check("模型在 CPU → 视为非 GPU（回退后不再用 128）", k12._model_on_gpu(cpu_model) is False)
    ctx.check("模型在 cuda → 视为 GPU", k12._model_on_gpu(FakeModel()) is True)

    # —— 13. 设备类错误识别扩到 MPS / ROCm；非设备错误不误伤 ——
    ctx.check("MPS OOM 算设备错", k12._is_cuda_error(RuntimeError("MPS backend out of memory")))
    ctx.check("ROCm 报错算设备错", k12._is_cuda_error(RuntimeError("hipErrorNoBinaryForGpu (rocm)")))
    ctx.check("普通文件错误不算设备错", not k12._is_cuda_error(FileNotFoundError("no such file")))
    ctx.check("含 chip 的普通错误不误伤（不引入 hip/metal 这类短词）",
              not k12._is_cuda_error(RuntimeError("unsupported chip format")))

    # —— 14. reload 重置设备判定（修好驱动/CUDA 版 torch 后不必重启 DSH） ——
    k14 = fresh({"KB_GPU_PROBE": "0"})
    sys.modules["torch"] = FakeTorchOK
    k14._GPU_OK = False
    k14._GPU_WHY = "旧故障"
    k14._PROBE = False
    k14._VRAM_GB = 4.0
    k14._BATCH_EFFECTIVE[("embed", "gpu")] = 4
    k14._set_device_note("embed", "旧说明")
    k14.get_embedder = lambda: None
    k14.get_reranker = lambda: None
    rep14 = k14.cmd_reload({})
    ctx.check("reload 把旧的设备判定清掉（GPU 不可用不再是粘性的）",
              k14._GPU_OK is None and k14._PROBE is None, (k14._GPU_OK, k14._PROBE))
    ctx.check("reload 之后重新探测就能用上 GPU —— 修好驱动/CUDA 版 torch 不必重启 DSH",
              k14._gpu_usable() is True and k14._PROBE is True, k14.device_report())
    ctx.check("reload 后批大小记忆与说明被清空",
              k14._BATCH_EFFECTIVE == {} and k14._device_note_text() is None,
              (dict(k14._BATCH_EFFECTIVE), k14._device_note_text()))
    ctx.check("reload 响应带 device 报告", isinstance(rep14.get("device"), dict), rep14.get("device"))

    sys.modules["torch"] = real_torch if real_torch else FakeTorchNoCuda
