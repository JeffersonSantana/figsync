#!/usr/bin/env python3
"""
Helper para chamar ferramentas do figma-mcp-extras via HTTP.

Uso:
  python3 figma_call.py <command> [params_json]

Exemplos:
  python3 figma_call.py get_document_info
  python3 figma_call.py create_frame '{"name":"Teste","width":390,"height":844,"layoutMode":"VERTICAL","fillColor":{"r":0.97,"g":0.97,"b":0.96,"a":1}}'
  python3 figma_call.py create_text '{"parentId":"123:456","characters":"Olá mundo","fontSize":16,"fontColor":{"r":0,"g":0,"b":0,"a":1}}'
"""
import json
import time
import uuid
import sys
import urllib.request

PORT = 3055
BASE = f"http://localhost:{PORT}"


def call(command: str, params: dict, timeout: int = 30) -> dict:
    cmd_id = str(uuid.uuid4())

    # Enfileira o comando
    payload = json.dumps({"id": cmd_id, "command": command, "params": params}).encode()
    req = urllib.request.Request(
        f"{BASE}/enqueue",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10)

    # Aguarda resultado via /collect?id=CMD_ID (sem race condition)
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = urllib.request.urlopen(f"{BASE}/collect?id={cmd_id}", timeout=10)
        data = json.loads(r.read())
        results = data.get("results", [])
        if results:
            res = results[0]
            if res.get("error"):
                raise RuntimeError(f"Erro do plugin: {res['error']}")
            return res.get("result", {})
        time.sleep(0.05)  # 50ms entre tentativas

    raise TimeoutError(f"Timeout ({timeout}s) esperando resposta de '{command}'")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    result = call(command, params)
    print(json.dumps(result, ensure_ascii=False, indent=2))
