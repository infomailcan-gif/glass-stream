"""Kisi yeniden tanima (ReID) modelini ekran kartinda calistirir.

Ultralytics'in resmi kisi ReID modeli (yolo26x-reid) yalnizca ONNX olarak geliyor;
bu bilgisayardaki onnxruntime ise sadece islemcide calisiyor (3 kisi icin ~150 ms,
cok yavas). Model onnx2torch ile PyTorch'a cevrilip GPU'da FP16 calistirilir.

Takipci (BoT-SORT) kisiyi kiyafet ve vucut yapisindan tanir: yuzu gorunmeyen biri
kadrajdan kisa sure cikip geri girdiginde ayni numarayi alir, yeniden sayilmaz.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import torch

_patched = False


def _patch_converter() -> None:
    """onnx2torch yeni opset surumlerini (Split 18, Shape 19 ...) tanimiyor.
    Bu islemlerin anlami degismedi; kayitli en yakin eski surumle cevrilir."""
    global _patched
    if _patched:
        return
    import onnx2torch.converter as conv
    from onnx2torch.node_converters import registry

    orig = registry.get_converter

    def get_converter(operation_type, version, domain=""):
        try:
            return orig(operation_type, version, domain) if domain else orig(operation_type, version)
        except NotImplementedError:
            olds = [d.version for d in registry._CONVERTER_REGISTRY
                    if d.operation_type == operation_type and d.domain == domain and d.version <= version]
            if not olds:
                raise
            return registry._CONVERTER_REGISTRY[registry.OperationDescription(domain, operation_type, max(olds))]

    conv.get_converter = get_converter

    # onnx2torch'ta hic olmayan islem: GlobalMaxPool
    from onnx2torch.utils.common import OnnxToTorchModule, OperationConverterResult, onnx_mapping_from_node

    class _GlobalMaxPool(torch.nn.Module, OnnxToTorchModule):
        def forward(self, x):
            return torch.amax(x, dim=tuple(range(2, x.dim())), keepdim=True)

    @registry.add_converter(operation_type="GlobalMaxPool", version=1)
    def _(node, graph):  # noqa: ARG001
        return OperationConverterResult(torch_module=_GlobalMaxPool(), onnx_mapping=onnx_mapping_from_node(node=node))

    _patched = True


class GpuReID:
    """ultralytics ReID ile ayni arayuz: (img, dets_xywh) -> [ozellik vektoru]."""

    def __init__(self, model: str, device=None, imgsz: int = 224) -> None:
        import onnx
        from onnx2torch import convert

        _patch_converter()
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.half = self.device.type == "cuda"
        net = convert(self._prepare(Path(model))).eval().to(self.device)
        self.net = net.half() if self.half else net
        self.imgsz = (imgsz, imgsz)

    @staticmethod
    def _prepare(path: Path):
        """Sabitleri katla (onnxruntime) ve Clip'in bos 'max' girisini sil."""
        import onnx
        import onnxruntime as ort

        folded = path.with_suffix(".folded.onnx")
        if not folded.exists():
            so = ort.SessionOptions()
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
            so.optimized_model_filepath = str(folded)
            so.log_severity_level = 3
            ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"])
        m = onnx.load(str(folded))
        # Son adim (L2 normalizasyon: ReduceL2 + Clip + Div) onnx2torch'ta cevrilemiyor;
        # takipci ozellikleri zaten kendisi normalize ettigi icin modeli ondan once kes.
        l2 = next((n for n in m.graph.node if n.op_type == "ReduceL2"), None)
        if l2 is not None:
            cut = path.with_suffix(".cut.onnx")
            if not cut.exists():
                onnx.utils.extract_model(str(folded), str(cut), [m.graph.input[0].name], [l2.input[0]])
            m = onnx.load(str(cut))
        return m

    @torch.no_grad()
    def __call__(self, img: np.ndarray, dets: np.ndarray) -> list[np.ndarray | None]:
        from ultralytics.utils.ops import xywh2xyxy
        from ultralytics.utils.plotting import save_one_box

        # Ultralytics ReID ile ayni kirpma (kutunun etrafinda biraz pay)
        all_crops = [save_one_box(d, img, save=False, BGR=True) for d in xywh2xyxy(torch.from_numpy(dets[:, :4]))]
        valid = [bool(c.size) for c in all_crops]
        crops = [c for c in all_crops if c.size]
        if not crops:
            return [None] * len(dets)
        batch = torch.empty(len(crops), 3, *self.imgsz, device=self.device)
        for i, c in enumerate(crops):
            t = torch.from_numpy(np.ascontiguousarray(c[..., ::-1])).to(self.device).permute(2, 0, 1)[None].float() / 255
            batch[i] = torch.nn.functional.interpolate(t, size=self.imgsz, mode="bilinear", align_corners=False)[0]
        out = self.net(batch.half() if self.half else batch).float().cpu().numpy()
        it = iter(out)
        return [next(it) if ok else None for ok in valid]


def install() -> None:
    """Takipci .onnx ReID modeli istedigini GpuReID ile karsilasin."""
    from ultralytics.trackers.utils import reid

    orig = reid.ReID

    def factory(model, *a, **k):
        if str(model).endswith(".onnx"):
            try:
                r = GpuReID(model)
                print(f"[vision] kisi ReID: {Path(model).name} ({r.device}, FP16)")
                return r
            except Exception as e:  # noqa: BLE001
                print(f"[vision] ReID GPU'ya cevrilemedi ({e!r}); islemcide calisacak")
        return orig(model, *a, **k)

    reid.ReID = factory
