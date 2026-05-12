#!/usr/bin/env python3
"""
figma_batch.py — Execução paralela de comandos Figma via HTTP.

Enfileira N comandos de uma vez e coleta todos os resultados em paralelo,
usando o fato de que o plugin processa um array inteiro de comandos por ciclo
de polling (10ms adaptativo). Isso elimina o overhead de roundtrip serial.

Performance:
  Serial (figma_call.py):  N × ~333ms = 35 comandos → ~11s
  Batch  (este módulo):    3-5 waves  × ~150ms       → ~0.5s  (≈ 14×)

Uso:
  from figma_batch import batch_call, call

  # Múltiplos comandos em paralelo:
  results = batch_call([
      {"command": "create_frame", "params": {"name": "header", "width": 390, "height": 80}},
      {"command": "create_frame", "params": {"name": "body",   "width": 390, "height": 600}},
  ])
  header_id = results[0]["id"]
  body_id   = results[1]["id"]

  # Comando único (compatível com figma_call.py):
  doc = call("get_document_info", {})
"""

import json
import time
import uuid
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

PORT    = 3055
BASE    = f"http://localhost:{PORT}"
TIMEOUT = 30  # segundos por comando


# ── Primitivos HTTP ────────────────────────────────────────────────────────────

def _enqueue(cmd_id: str, command: str, params: dict) -> None:
    """Enfileira um único comando no servidor HTTP."""
    payload = json.dumps({"id": cmd_id, "command": command, "params": params}).encode()
    req = urllib.request.Request(
        f"{BASE}/enqueue",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10)


def _collect(cmd_id: str, timeout: float = TIMEOUT) -> dict:
    """Aguarda e retorna o resultado de um comando já enfileirado."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = urllib.request.urlopen(f"{BASE}/collect?id={cmd_id}", timeout=10)
        data = json.loads(r.read())
        results = data.get("results", [])
        if results:
            res = results[0]
            if res.get("error"):
                raise RuntimeError(f"Erro do plugin [{cmd_id[:8]}]: {res['error']}")
            return res.get("result", {})
        time.sleep(0.02)  # 20ms entre polls
    raise TimeoutError(f"Timeout ({timeout}s) aguardando resposta de '{cmd_id[:8]}'")


# ── API principal ──────────────────────────────────────────────────────────────

def batch_call(
    commands: list[dict],
    timeout_per_command: float = TIMEOUT,
    max_workers: int = 32,
) -> list[dict]:
    """
    Executa N comandos em paralelo e retorna os resultados na mesma ordem.

    Parâmetros:
      commands: lista de dicts com {"command": str, "params": dict}
      timeout_per_command: segundos máximos por comando (default 30s)
      max_workers: máximo de threads simultâneas (default 32)

    Retorna:
      lista de resultados na mesma ordem dos comandos de entrada.
      Lança RuntimeError se qualquer comando falhar.

    Estratégia:
      1. Enfileirar todos os comandos em paralelo (threads) → plugin os vê
         no mesmo ciclo de polling e processa em lote.
      2. Coletar todos os resultados em paralelo (threads).
    """
    if not commands:
        return []

    n = len(commands)
    ids = [str(uuid.uuid4()) for _ in range(n)]
    workers = min(n, max_workers)

    # Fase 1: enfileirar tudo em paralelo
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_enqueue, ids[i], commands[i]["command"], commands[i].get("params", {}))
            for i in range(n)
        ]
        for f in as_completed(futures):
            f.result()  # propaga exceções de enqueue

    # Fase 2: coletar tudo em paralelo, preservando ordem
    results = [None] * n
    errors = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_idx = {
            pool.submit(_collect, ids[i], timeout_per_command): i
            for i in range(n)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                errors.append(f"  [{idx}] {commands[idx]['command']}: {e}")

    if errors:
        raise RuntimeError("batch_call falhou em {} de {} comandos:\n{}".format(
            len(errors), n, "\n".join(errors)
        ))

    return results


def call(command: str, params: dict = None) -> dict:
    """
    Executa um único comando. Compatível com figma_call.py.
    Usa batch_call internamente para consistência.
    """
    results = batch_call([{"command": command, "params": params or {}}])
    return results[0]


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    result = call(command, params)
    print(json.dumps(result, ensure_ascii=False, indent=2))
