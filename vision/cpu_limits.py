"""Islemci kullanim siniri.

Yuz tanima (SCRFD + ArcFace) ve plaka okuma (YOLOv9 + CCT) islemcide (onnxruntime) calisir.
onnxruntime varsayilan olarak TUM cekirdekleri kullanir; birden fazla kisi/plaka gorununce bu
isler islemciyi doldurup ana analiz dongusunu bekletiyordu (gecikme 1000 ms'ye cikiyordu).
Her oturum en fazla 2 cekirdek kullanir.
"""

from __future__ import annotations


def session_options(threads: int = 2):
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    so.inter_op_num_threads = 1
    so.log_severity_level = 3
    return so
