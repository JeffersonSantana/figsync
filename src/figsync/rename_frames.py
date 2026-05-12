#!/usr/bin/env python3
"""
rename_frames.py — Encontra frames/groups genéricos e os renomeia via HTTP.

Uso:
  python3 rename_frames.py <fileKey> <nodeId>
  python3 rename_frames.py <fileKey> <nodeId> '[{"id":"1:2","new_name":"header"}]'

Requer:
  - FIGMA_TOKEN no ambiente ou em ../.env
  - Plugin "Figma MCP Extras" aberto no Figma com server_http.py rodando
"""

import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path

import httpx

# ── Configuração ──────────────────────────────────────────────────────────────

HTTP_PORT = int(os.environ.get("FIGMA_HTTP_PORT", 3055))
BASE_URL  = f"http://localhost:{HTTP_PORT}"
TIMEOUT   = float(os.environ.get("FIGMA_TIMEOUT", 20))


def load_token() -> str:
    token = os.environ.get("FIGMA_TOKEN", "")
    if token:
        return token
    env = Path(__file__).parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("FIGMA_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("❌  FIGMA_TOKEN não encontrado. Adicione ao .env ou exporte no terminal.")


# ── Figma REST API ─────────────────────────────────────────────────────────────

async def fetch_node(file_key: str, node_id: str, token: str) -> dict:
    api_id = node_id.replace(":", "-")
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"https://api.figma.com/v1/files/{file_key}/nodes?ids={api_id}",
            headers={"X-Figma-Token": token},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        return next(iter(data["nodes"].values()))["document"]


# ── Análise de frames genéricos ───────────────────────────────────────────────

GENERIC = re.compile(r"^(Frame|Group|frame|group)\s+\d+$")


def find_generic_frames(node: dict, path: str = "") -> list[dict]:
    """Retorna todos os frames/groups com nome genérico recursivamente."""
    found   = []
    name    = node.get("name", "")
    current = f"{path} › {name}" if path else name

    if GENERIC.match(name):
        found.append({
            "id":   node["id"],
            "name": name,
            "type": node.get("type", ""),
            "path": current,
        })

    for child in node.get("children", []):
        found.extend(find_generic_frames(child, current))

    return found


# ── Comunicação HTTP com o plugin ─────────────────────────────────────────────

async def http_rename(renames: list[dict]) -> list[dict]:
    """
    Enfileira comandos no servidor HTTP e coleta respostas do plugin.
    Cada rename: {"id": "nodeId", "new_name": "nome"}
    """
    results = []

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=5) as client:
        # Verifica conectividade
        try:
            await client.get("/pending", timeout=3)
        except Exception:
            print(f"\n❌  Servidor HTTP indisponível em {BASE_URL}")
            print("    Inicie com: python3 figma-mcp-extras/server_http.py\n")
            for r in renames:
                results.append({**r, "ok": False, "error": "servidor indisponível"})
            return results

        # Enfileira todos os comandos
        pending: dict[str, dict] = {}
        for r in renames:
            cmd_id = str(uuid.uuid4())
            pending[cmd_id] = r
            await client.post("/enqueue", json={
                "id":      cmd_id,
                "command": "rename_node",
                "params":  {"nodeId": r["id"], "name": r["new_name"]},
            })

        # Coleta respostas com polling
        deadline = asyncio.get_event_loop().time() + TIMEOUT
        while pending:
            if asyncio.get_event_loop().time() >= deadline:
                for r in pending.values():
                    results.append({**r, "ok": False, "error": "timeout"})
                break
            await asyncio.sleep(0.25)
            try:
                resp = await client.get("/collect")
                for item in resp.json().get("results", []):
                    mid = item.get("id")
                    if mid in pending:
                        r = pending.pop(mid)
                        results.append({**r, "ok": not item.get("error"), "error": item.get("error")})
            except Exception:
                pass

    return results


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    if len(sys.argv) < 3:
        print("Uso: python3 rename_frames.py <fileKey> <nodeId> [renames_json]")
        sys.exit(1)

    file_key = sys.argv[1]
    node_id  = sys.argv[2].replace("-", ":")
    token    = load_token()

    print(f"🔍 Buscando estrutura de {node_id}...")
    root     = await fetch_node(file_key, node_id, token)
    generics = find_generic_frames(root)

    if not generics:
        print("✅  Nenhum frame/group genérico encontrado.")
        return

    print(f"\n📋 {len(generics)} frame(s) genérico(s) encontrado(s):\n")
    for g in generics:
        print(f"  [{g['id']}] {g['type']:<10} '{g['name']}'")
        print(f"           {g['path']}\n")

    if len(sys.argv) < 4:
        print("💡 Para renomear, passe os nomes como JSON:")
        print(f'   Exemplo: python3 rename_frames.py <fileKey> <nodeId> \'[{{"id":"{generics[0]["id"]}","new_name":"header"}}]\'')
        return

    renames = json.loads(sys.argv[3])
    if not renames:
        print("Nenhum rename para aplicar.")
        return

    print(f"🔄 Renomeando {len(renames)} frame(s)...")
    collected = await http_rename(renames)

    print()
    ok_list  = [r for r in collected if r["ok"]]
    err_list = [r for r in collected if not r["ok"]]
    for r in ok_list:
        print(f"  ✅ [{r['id']}] → '{r['new_name']}'")
    for r in err_list:
        print(f"  ❌ [{r['id']}] {r.get('error', '?')}")
    print(f"\n{len(ok_list)} renomeado(s), {len(err_list)} erro(s).")


if __name__ == "__main__":
    asyncio.run(main())
