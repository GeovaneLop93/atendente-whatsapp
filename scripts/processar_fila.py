#!/usr/bin/env python3
"""Drena a fila uma vez (processa mensagens pendentes). Útil para rodar o worker
manualmente sem o serviço contínuo."""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import worker  # noqa: E402

if __name__ == "__main__":
    n = worker.drain()
    print(f"processadas={n}")
