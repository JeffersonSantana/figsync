"""
figma-mcp-extras — servidor HTTP local, todos os comandos do Figma via MCP.

Substitui o relay WebSocket por HTTP polling direto.
O plugin faz GET /pending a cada 200ms e POST /result ao concluir.
Scripts externos usam POST /enqueue + GET /collect.

Porta padrão: 3055
"""

import asyncio
import json
import logging
import os
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

# ── Config ────────────────────────────────────────────────────────────────────

PORT    = int(os.environ.get("FIGMA_HTTP_PORT", 3055))
TIMEOUT = float(os.environ.get("FIGMA_TIMEOUT", 30))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("figma-http")

# ── Estado compartilhado (thread-safe via GIL para dicts simples) ─────────────

_pending_commands:  dict[str, dict] = {}   # aguardando plugin fazer GET /pending
_collected_results: dict[str, dict] = {}   # respostas do plugin via POST /result
_pending_futures:   dict[str, Any]  = {}   # futures MCP aguardando resposta
_loop: asyncio.AbstractEventLoop | None = None

# ── Servidor HTTP ─────────────────────────────────────────────────────────────

class FigmaHandler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass  # silencia logs

    def _json(self, data: Any, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/pending":
            # Plugin busca fila de comandos
            cmds = list(_pending_commands.values())
            _pending_commands.clear()
            self._json({"commands": cmds})

        elif self.path.startswith("/collect"):
            # Scripts externos coletam respostas
            # Suporta GET /collect?id=CMD_ID para buscar resultado específico (sem race condition)
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            cmd_id = qs.get("id", [None])[0]
            if cmd_id:
                result = _collected_results.pop(cmd_id, None)
                self._json({"results": [result] if result else []})
            else:
                results = list(_collected_results.values())
                _collected_results.clear()
                self._json({"results": results})

        elif self.path == "/health":
            self._json({"ok": True, "pending": len(_pending_commands)})

        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/result":
            # Plugin envia resultado de um comando
            try:
                body   = self._body()
                cmd_id = body.get("id")
                result = body.get("result")
                error  = body.get("error")
                if not cmd_id:
                    self._json({"error": "missing id"}, 400); return

                _collected_results[cmd_id] = {"id": cmd_id, "result": result, "error": error}

                fut = _pending_futures.get(cmd_id)
                if fut and _loop and not fut.done():
                    if error:
                        _loop.call_soon_threadsafe(fut.set_exception, RuntimeError(str(error)))
                    else:
                        _loop.call_soon_threadsafe(fut.set_result, result)

                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 500)

        elif self.path == "/enqueue":
            # Scripts externos enfileiram comandos
            try:
                body = self._body()
                if not body.get("id") or not body.get("command"):
                    self._json({"error": "missing id or command"}, 400); return
                _pending_commands[body["id"]] = body
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 500)

        else:
            self._json({"error": "not found"}, 404)


def _start_http():
    server = HTTPServer(("localhost", PORT), FigmaHandler)
    log.warning("HTTP server em http://localhost:%d", PORT)
    server.serve_forever()


# ── Envio de comandos via MCP ─────────────────────────────────────────────────

async def _send(command: str, params: dict) -> Any:
    global _loop
    _loop = asyncio.get_event_loop()
    cmd_id = str(uuid.uuid4())
    fut = _loop.create_future()
    _pending_futures[cmd_id] = fut
    _pending_commands[cmd_id] = {"id": cmd_id, "command": command, "params": params}
    try:
        return await asyncio.wait_for(asyncio.shield(fut), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        _pending_commands.pop(cmd_id, None)
        _pending_futures.pop(cmd_id, None)
        raise TimeoutError(
            f"Timeout ({TIMEOUT}s) — plugin não respondeu.\n"
            "Verifique se o plugin está aberto e clicou em Conectar."
        )
    finally:
        _pending_futures.pop(cmd_id, None)


def _fmt(r: Any) -> str:
    if r is None: return ""
    return json.dumps(r, ensure_ascii=False, indent=2) if isinstance(r, (dict, list)) else str(r)


def _text(content: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=content)]


# ── MCP Server ────────────────────────────────────────────────────────────────

app = Server("figma-mcp-extras")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    def tool(name, desc, props, required=None):
        return types.Tool(
            name=name,
            description=desc,
            inputSchema={
                "type": "object",
                "properties": props,
                "required": required or [],
            },
        )

    str_  = {"type": "string"}
    num_  = {"type": "number"}
    bool_ = {"type": "boolean"}
    arr_  = lambda item: {"type": "array", "items": item}
    obj_  = {"type": "object"}

    return [
        # ── Leitura ───────────────────────────────────────────────────────────
        tool("get_document_info",
             "Retorna informações da página atual do Figma (nome, filhos, etc).",
             {}),

        tool("get_selection",
             "Retorna os nós atualmente selecionados no Figma.",
             {}),

        tool("get_node_info",
             "Retorna informações detalhadas de um nó pelo ID.",
             {"nodeId": {**str_, "description": "ID do nó (ex: '928:27067')"}},
             ["nodeId"]),

        tool("get_nodes_info",
             "Retorna informações de múltiplos nós em paralelo.",
             {"nodeIds": {**arr_(str_), "description": "Lista de IDs"}},
             ["nodeIds"]),

        tool("read_my_design",
             "Exporta JSON dos nós selecionados (útil para IA analisar o design).",
             {}),

        tool("get_styles",
             "Lista todos os estilos locais (cores, textos, efeitos).",
             {}),

        tool("get_local_components",
             "Lista todos os componentes locais do arquivo.",
             {
                 "includeDetails": {**bool_, "description": "Incluir detalhes dos componentes"},
                 "searchTerm":     {**str_,  "description": "Filtrar por nome"},
             }),

        tool("get_annotations",
             "Retorna anotações/comentários de um nó.",
             {"nodeId": str_}),

        tool("get_reactions",
             "Retorna interações de prototype de uma lista de nós.",
             {"nodeIds": arr_(str_)},
             ["nodeIds"]),

        tool("get_instance_overrides",
             "Retorna overrides de uma instância de componente.",
             {"instanceNodeId": {**str_, "description": "ID da instância (opcional)"}}),

        tool("scan_text_nodes",
             "Busca todos os nós de texto em um nó raiz.",
             {
                 "nodeId":      str_,
                 "searchTerm":  str_,
                 "caseSensitive": bool_,
             }),

        tool("scan_nodes_by_types",
             "Busca nós por tipo (FRAME, TEXT, COMPONENT, etc).",
             {
                 "nodeId": str_,
                 "types":  {**arr_(str_), "description": "Ex: ['FRAME', 'TEXT']"},
             },
             ["types"]),

        # ── Criação ───────────────────────────────────────────────────────────
        tool("create_frame",
             "Cria um novo frame. Suporta layout inline — passe layoutMode, padding, sizing e fillColor para evitar chamadas extras.",
             {
                 "x":          num_,
                 "y":          num_,
                 "width":      num_,
                 "height":     num_,
                 "name":       str_,
                 "parentId":   str_,
                 "fillColor":  {**obj_, "description": "{r,g,b,a} 0-1"},
                 "strokeColor":    obj_,
                 "strokeWeight":   num_,
                 "cornerRadius":   num_,
                 "layoutMode":     {**str_, "description": "NONE | HORIZONTAL | VERTICAL"},
                 "paddingTop":     num_,
                 "paddingRight":   num_,
                 "paddingBottom":  num_,
                 "paddingLeft":    num_,
                 "primaryAxisAlignItems":  {**str_, "description": "MIN|CENTER|MAX|SPACE_BETWEEN"},
                 "counterAxisAlignItems":  {**str_, "description": "MIN|CENTER|MAX|BASELINE"},
                 "layoutSizingHorizontal": {**str_, "description": "FIXED|HUG|FILL"},
                 "layoutSizingVertical":   {**str_, "description": "FIXED|HUG|FILL"},
                 "itemSpacing":    num_,
             }),

        tool("create_rectangle",
             "Cria um retângulo.",
             {
                 "x": num_, "y": num_, "width": num_, "height": num_,
                 "name": str_,
                 "fillColor": obj_,
                 "parentId":  str_,
             }),

        tool("create_text",
             "Cria um nó de texto. Use 'characters' para definir o conteúdo inline, sem precisar de set_text_content depois.",
             {
                 "x": num_, "y": num_,
                 "characters": {**str_, "description": "Conteúdo do texto (preferir sobre 'text')"},
                 "text":       {**str_, "description": "Alias de characters (legado)"},
                 "fontSize":   num_,
                 "fontWeight": num_,
                 "fontColor":  {**obj_, "description": "{r,g,b,a} 0-1 — cor do texto"},
                 "fillColor":  {**obj_, "description": "Alias de fontColor (legado)"},
                 "parentId":   str_,
                 "name":       str_,
             }),

        tool("create_component_instance",
             "Cria uma instância de um componente local pelo ID.",
             {
                 "componentId": {**str_, "description": "ID do componente master"},
                 "x": num_, "y": num_,
                 "parentId": str_,
             },
             ["componentId"]),

        tool("clone_node",
             "Clona um nó existente.",
             {
                 "nodeId":   str_,
                 "x":        num_,
                 "y":        num_,
                 "parentId": str_,
             },
             ["nodeId"]),

        # ── Edição de propriedades ─────────────────────────────────────────────
        tool("set_fill_color",
             "Define a cor de preenchimento de um nó.",
             {
                 "nodeId": str_,
                 "color":  {**obj_, "description": "{r,g,b,a} com valores 0-1"},
             },
             ["nodeId", "color"]),

        tool("set_stroke_color",
             "Define a cor de borda de um nó.",
             {
                 "nodeId":       str_,
                 "color":        obj_,
                 "strokeWeight": num_,
             },
             ["nodeId", "color"]),

        tool("set_corner_radius",
             "Define o raio de canto de um nó.",
             {
                 "nodeId": str_,
                 "radius": num_,
                 "corners": {**arr_(num_), "description": "[TL, TR, BR, BL]"},
             },
             ["nodeId"]),

        tool("set_text_content",
             "Altera o texto de um nó TEXT.",
             {"nodeId": str_, "text": str_},
             ["nodeId", "text"]),

        tool("set_multiple_text_contents",
             "Altera múltiplos nós de texto de uma vez.",
             {
                 "textUpdates": {
                     "type": "array",
                     "items": {"type": "object", "properties": {"nodeId": str_, "text": str_}},
                 }
             },
             ["textUpdates"]),

        tool("set_annotation",
             "Cria ou atualiza uma anotação em um nó.",
             {
                 "nodeId":  str_,
                 "label":   str_,
                 "properties": arr_(obj_),
             },
             ["nodeId"]),

        tool("set_multiple_annotations",
             "Cria múltiplas anotações de uma vez.",
             {"annotations": arr_(obj_)},
             ["annotations"]),

        tool("set_instance_overrides",
             "Aplica overrides de uma instância fonte em instâncias alvo.",
             {
                 "sourceInstanceId": str_,
                 "targetNodeIds":    arr_(str_),
             },
             ["sourceInstanceId", "targetNodeIds"]),

        # ── Layout ─────────────────────────────────────────────────────────────
        tool("set_layout_mode",
             "Define o modo de layout auto (NONE | HORIZONTAL | VERTICAL | GRID).",
             {
                 "nodeId":     str_,
                 "layoutMode": {**str_, "description": "NONE | HORIZONTAL | VERTICAL"},
             },
             ["nodeId", "layoutMode"]),

        tool("set_padding",
             "Define o padding interno de um frame com auto-layout.",
             {
                 "nodeId":         str_,
                 "top":            num_,
                 "right":          num_,
                 "bottom":         num_,
                 "left":           num_,
                 "vertical":       num_,
                 "horizontal":     num_,
             },
             ["nodeId"]),

        tool("set_axis_align",
             "Define o alinhamento dos itens no eixo principal e secundário.",
             {
                 "nodeId":              str_,
                 "primaryAxisAlignItems":   {**str_, "description": "MIN|CENTER|MAX|SPACE_BETWEEN"},
                 "counterAxisAlignItems":   {**str_, "description": "MIN|CENTER|MAX|BASELINE"},
             },
             ["nodeId"]),

        tool("set_layout_sizing",
             "Define o sizing do frame (FIXED | HUG | FILL).",
             {
                 "nodeId":              str_,
                 "primaryAxisSizingMode":   {**str_, "description": "FIXED | AUTO (HUG)"},
                 "counterAxisSizingMode":   {**str_, "description": "FIXED | AUTO (HUG)"},
             },
             ["nodeId"]),

        tool("set_item_spacing",
             "Define o espaço entre itens de um auto-layout.",
             {"nodeId": str_, "spacing": num_},
             ["nodeId", "spacing"]),

        # ── Movimento e tamanho ─────────────────────────────────────────────────
        tool("move_node",
             "Move um nó para uma posição (x, y).",
             {"nodeId": str_, "x": num_, "y": num_},
             ["nodeId", "x", "y"]),

        tool("resize_node",
             "Redimensiona um nó.",
             {"nodeId": str_, "width": num_, "height": num_},
             ["nodeId", "width", "height"]),

        tool("set_focus",
             "Seleciona e centraliza a viewport em um nó.",
             {"nodeId": str_},
             ["nodeId"]),

        tool("set_selections",
             "Seleciona múltiplos nós na página atual.",
             {"nodeIds": arr_(str_)},
             ["nodeIds"]),

        # ── Remoção ────────────────────────────────────────────────────────────
        tool("delete_node",
             "Remove um nó do Figma.",
             {"nodeId": str_},
             ["nodeId"]),

        tool("delete_multiple_nodes",
             "Remove múltiplos nós de uma vez.",
             {"nodeIds": arr_(str_)},
             ["nodeIds"]),

        # ── Renomeação / visibilidade / travamento ─────────────────────────────
        tool("rename_node",
             "Renomeia um layer/frame/componente pelo nodeId.",
             {"nodeId": str_, "name": str_},
             ["nodeId", "name"]),

        tool("rename_multiple_nodes",
             "Renomeia múltiplos layers de uma vez.",
             {
                 "renames": {
                     "type": "array",
                     "items": {"type": "object", "properties": {"nodeId": str_, "name": str_}},
                 }
             },
             ["renames"]),

        tool("set_node_visible",
             "Altera visibilidade de um nó.",
             {"nodeId": str_, "visible": bool_},
             ["nodeId", "visible"]),

        tool("set_node_locked",
             "Trava ou destrava um nó para edição.",
             {"nodeId": str_, "locked": bool_},
             ["nodeId", "locked"]),

        # ── Export ─────────────────────────────────────────────────────────────
        tool("export_node_as_image",
             "Exporta um nó como imagem (PNG/SVG/PDF/JPEG).",
             {
                 "nodeId": str_,
                 "format": {**str_, "description": "PNG | SVG | PDF | JPEG"},
                 "scale":  num_,
             },
             ["nodeId"]),

        # ── Protótipo ──────────────────────────────────────────────────────────
        tool("set_default_connector",
             "Define o conector padrão para conexões de protótipo.",
             {"connectorId": str_},
             ["connectorId"]),

        tool("create_connections",
             "Cria conexões de protótipo entre nós.",
             {"connections": arr_(obj_)},
             ["connections"]),

        # ── Efeitos visuais avançados ──────────────────────────────────────────
        tool("set_gradient_fill",
             "Define um fill gradiente (LINEAR, RADIAL ou ANGULAR) em um nó.",
             {
                 "nodeId": str_,
                 "type":   {**str_, "description": "LINEAR | RADIAL | ANGULAR"},
                 "angle":  {**num_, "description": "Ângulo em graus (0-360), padrão 135"},
                 "stops":  {
                     "type": "array",
                     "description": "Lista de paradas do gradiente",
                     "items": {
                         "type": "object",
                         "properties": {
                             "color":    {"type": "object", "description": "{r,g,b,a} 0-1"},
                             "position": {"type": "number", "description": "0.0 (início) a 1.0 (fim)"},
                         },
                     },
                 },
             },
             ["nodeId", "stops"]),

        tool("set_effects",
             "Define efeitos visuais (sombra, blur) em um nó. Suporta DROP_SHADOW, INNER_SHADOW, LAYER_BLUR, BACKGROUND_BLUR.",
             {
                 "nodeId":  str_,
                 "effects": {
                     "type": "array",
                     "description": "Lista de efeitos",
                     "items": {
                         "type": "object",
                         "properties": {
                             "type":    {"type": "string", "description": "DROP_SHADOW | INNER_SHADOW | LAYER_BLUR | BACKGROUND_BLUR"},
                             "color":   {"type": "object", "description": "{r,g,b,a} — para sombras"},
                             "offset":  {"type": "object", "description": "{x,y} em px — para sombras"},
                             "radius":  {"type": "number", "description": "Blur radius em px"},
                             "spread":  {"type": "number", "description": "Spread em px — para sombras"},
                             "visible": {"type": "boolean"},
                         },
                     },
                 },
             },
             ["nodeId", "effects"]),

        tool("set_opacity",
             "Define a opacidade de layer de um nó (0 = invisível, 1 = opaco).",
             {
                 "nodeId":  str_,
                 "opacity": {**num_, "description": "0.0 a 1.0"},
             },
             ["nodeId", "opacity"]),

        tool("set_text_style",
             "Define propriedades tipográficas avançadas em um nó TEXT: letterSpacing, lineHeight, textCase, textDecoration, textAlignHorizontal.",
             {
                 "nodeId":              str_,
                 "letterSpacing":       {**num_, "description": "Espaçamento entre letras em px (negativo = comprimido)"},
                 "lineHeight":          {**num_, "description": "Altura de linha em px"},
                 "textCase":            {**str_, "description": "ORIGINAL | UPPER | LOWER | TITLE"},
                 "textDecoration":      {**str_, "description": "NONE | UNDERLINE | STRIKETHROUGH"},
                 "textAlignHorizontal": {**str_, "description": "LEFT | CENTER | RIGHT | JUSTIFIED"},
             },
             ["nodeId"]),

        tool("create_ellipse",
             "Cria uma elipse ou círculo. Para círculo: width == height.",
             {
                 "x": num_, "y": num_, "width": num_, "height": num_,
                 "name":         str_,
                 "parentId":     str_,
                 "fillColor":    {**obj_, "description": "{r,g,b,a} 0-1"},
                 "strokeColor":  obj_,
                 "strokeWeight": num_,
                 "opacity":      {**num_, "description": "0.0 a 1.0"},
             }),

        tool("set_clip_content",
             "Define se um frame recorta o conteúdo que ultrapassa seus limites (clipsContent).",
             {
                 "nodeId": str_,
                 "clip":   {"type": "boolean", "description": "true = recortar, false = mostrar além das bordas"},
             },
             ["nodeId", "clip"]),

        tool("batch_execute",
             "⚡ Executa N comandos Figma em PARALELO. "
             "SEMPRE usar este tool em vez de tools individuais ao criar ou editar nós. "
             "Retorna array de resultados na mesma ordem dos comandos.",
             {
                 "commands": {
                     "type": "array",
                     "description": "Lista de objetos {command: string, params: object}",
                     "items": {
                         "type": "object",
                         "properties": {
                             "command": {"type": "string"},
                             "params":  {"type": "object"},
                         },
                         "required": ["command", "params"],
                     },
                 }
             },
             ["commands"]),
    ]


# ── Dispatcher ────────────────────────────────────────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        if name == "batch_execute":
            commands = arguments.get("commands", [])
            tasks = [_send(cmd["command"], cmd.get("params", {})) for cmd in commands]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            output = [
                {
                    "index": i,
                    "command": cmd["command"],
                    "error": str(res) if isinstance(res, Exception) else None,
                    "result": None if isinstance(res, Exception) else res,
                }
                for i, (cmd, res) in enumerate(zip(commands, results))
            ]
            return _text(f"✅ batch_execute ({len(commands)} comandos)\n{_fmt(output)}")
        result = await _send(name, arguments)
        return _text(f"✅ {name}\n{_fmt(result)}")
    except TimeoutError as e:
        return _text(f"⏱ {e}")
    except Exception as e:
        return _text(f"❌ {name}: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────

def _start_http():
    HTTPServer(("localhost", PORT), FigmaHandler).serve_forever()


async def main():
    Thread(target=_start_http, daemon=True).start()
    async with mcp.server.stdio.stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())


def cli():
    """Entry point para `figma-mcp-extras` instalado via pip."""
    import sys
    if "--http-only" in sys.argv:
        print(f"Servidor HTTP rodando em http://localhost:{PORT}")
        print("Aguardando conexão do plugin Figma... (Ctrl+C para parar)")
        _start_http()
    else:
        asyncio.run(main())


if __name__ == "__main__":
    cli()
