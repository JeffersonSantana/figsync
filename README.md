# FigSync

Servidor MCP + HTTP local que conecta o Claude Code ao plugin Figma, permitindo criar e editar designs via IA.

## Como funciona

```
Claude Code ↔ server_http.py (MCP + HTTP :3055) ↔ Plugin FigSync (Figma)
```

## Instalação

### macOS (recomendado)

```bash
brew install pipx
pipx install git+https://github.com/JeffersonSantana/figsync.git
```

### Windows / Linux

```bash
pip install git+https://github.com/JeffersonSantana/figsync.git
```

### Atualizar

```bash
pipx upgrade figsync
# ou
pip install --upgrade git+https://github.com/JeffersonSantana/figsync.git
```

## Uso

### Modo MCP (Claude Code)

Adicione ao seu `claude_desktop_config.json` ou configuração de MCP:

```json
{
  "mcpServers": {
    "figsync": {
      "command": "figsync"
    }
  }
}
```

### Modo standalone (só o servidor HTTP)

```bash
figsync --http-only
```

O servidor sobe em `http://localhost:3055`.

## Requisitos

- Python 3.11+
- Plugin FigSync instalado no Figma (aberto e conectado)
