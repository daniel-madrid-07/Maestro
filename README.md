# Maestro

Muchas sesiones completas de Claude Code corriendo en tmux, dirigidas por una
de ellas o por Claude desde VS Code. Es la versión propia y reducida de lo que
CAO hace en este equipo: solo Claude Code, solo tmux, solo lo que se usa.

~1.500 líneas de Python, sin base de datos, sin framework web, dos dependencias
(`mcp` para los servidores MCP y `pyyaml` para los perfiles).

## Piezas

| Proceso | Qué es |
|---|---|
| `maestro-server` | API HTTP en `127.0.0.1:9889`. Crea sesiones tmux, arranca Claude en cada ventana, vigila cada pantalla (estado) y entrega los mensajes en cola cuando la sesión está libre. Emite eventos por SSE para el panel. |
| `maestro-ops` | Servidor MCP para el orquestador externo (Claude en VS Code). Mismos nombres de herramientas que `cao-ops-mcp`: `launch_session`, `send_session_message`, `get_terminal_status`, `get_terminal_output`, `read_session_output`, `list_sessions`, `get_session_info`, `shutdown_session`, `list_profiles`. |
| `maestro-agent` | Servidor MCP que recibe cada sesión lanzada: `assign` (worker sin esperar), `handoff` (worker esperando la respuesta), `send_message`, `list_terminals`, `delete_terminal`, `get_terminal_status`, `get_terminal_output`. |

Los dos servidores MCP tienen además las **herramientas de control**:
`answer_prompt(terminal_id, answer)` (solo con estado `waiting_user_answer`:
una tecla, un dígito que elige esa opción, o texto), `interrupt(terminal_id)`
(Esc, para un worker dando vueltas) y `restart_terminal(terminal_id)` (mismo
id, perfil, modelo, carpeta y cola; Claude nuevo, conversación perdida).

**Worktrees:** `use_worktree=true` en `launch_session`, `assign` o `handoff`
da al worker su propio checkout en `~/.maestro/worktrees/<repo>-<id>` sobre la
rama `mx/<id>`; el worker recibe la orden de hacer commit ahí. Se fusiona con
git normal (`git -C <repo> merge mx/<id>`); `list_worktrees` (ops) lista lo que
hay por fusionar, incluidos los checkouts huérfanos de un servidor anterior
(`remove_worktree` los quita). Al borrar el terminal, lo que quedara sin
commit se commitea en la rama, se elimina el checkout, y la rama solo se borra
si está fusionada. Nunca se pierde trabajo.

**Avisos:** el servidor emite `waiting` (un worker ha hecho una pregunta),
`stuck` (lleva `MAESTRO_STUCK_AFTER` s, por defecto 600, en `processing` sin
que la pantalla cambie, descontando el spinner) y `error` (Claude salió). El
panel los muestra en el ticker y lanza notificaciones del sistema (pide
permiso al pulsar el logo).

Perfiles (Markdown con front matter YAML, mismo formato que CAO) en
`src/maestro/profiles/`: `worker`, `code_supervisor`, `reviewer`. Una copia con
el mismo nombre en `~/.maestro/profiles/` tiene prioridad.

## Cómo funciona una sesión

1. `POST /sessions` → ventana tmux (`mx-<nombre>`) con `MAESTRO_TERMINAL_ID` y
   `MAESTRO_URL` en su entorno.
2. Antes de arrancar Claude, la carpeta se marca como confiable en
   `~/.claude.json` y se activa `skipDangerousModePermissionPrompt` en
   `~/.claude/settings.json`, así no aparece ningún diálogo. Si aun así aparece
   uno, se contesta (con un segundo de margen para que el renderizador acepte
   teclas).
3. `claude --dangerously-skip-permissions --model … --effort … --append-system-prompt-file … --mcp-config … --strict-mcp-config`
   (variables `CLAUDE*` heredadas se limpian antes).
4. Cuando el banner y la caja de entrada están en pantalla dos capturas
   seguidas, la terminal está lista y se entrega el primer mensaje.
5. Cada segundo se captura la pantalla y se clasifica: `processing` (spinner),
   `waiting_user_answer` (menú de opciones), `completed` (respuesta nueva en
   pantalla con la caja de entrada visible), `idle`, `error` (Claude salió).
6. Los mensajes van a una cola por terminal y se pegan (bracketed paste +
   Enter) solo cuando la terminal está `idle`/`completed`.

Al reiniciar el servidor, `~/.maestro/state.json` permite readoptar las
sesiones tmux que sigan vivas.

## Instalación (WSL)

```bash
uv tool install --editable "/mnt/c/ALL/Coding/Claude Sessions/maestro"
# → ~/.local/bin/maestro-server, maestro-ops, maestro-agent
```

Registrar el MCP para VS Code (Windows):

```
claude mcp add-json --scope user maestro-ops '{"command":"wsl.exe","args":["-d","Ubuntu","--","bash","-lc","/home/daniel/.local/bin/maestro-ops"]}'
```

## Arranque

`Iniciar Maestro.lnk` (o `start-maestro.bat`) arranca el panel con
`PANEL_SERVER_BIN=~/.local/bin/maestro-server`; el panel es el mismo que con
CAO. CAO y Maestro usan el mismo puerto: solo uno de los dos a la vez.

## API

```
GET  /health
GET  /sessions                          POST /sessions        {agent_profile, session_name, working_directory, model, initial_message, wait}
GET  /sessions/{n}                      DELETE /sessions/{n}
GET  /sessions/{n}/terminals            POST /sessions/{n}/terminals  {agent_profile, working_directory, model, caller_id, initial_message, wait}
GET  /terminals/{id}                    DELETE /terminals/{id}
POST /terminals/{id}/input {message}    POST /terminals/{id}/inbox/messages {message, sender_id}
POST /terminals/{id}/answer {answer}    POST /terminals/{id}/interrupt        POST /terminals/{id}/restart
GET  /terminals/{id}/output?mode=full|last
GET  /events (SSE)                      GET /events/history?limit=
GET  /agents/profiles                   GET /agents/profiles/{name}
```

## Lo que no tiene (a propósito)

Otros proveedores (Kiro, Codex, Kimi, Copilot…), Kubernetes y nodos remotos,
workflows, memoria compartida, autenticación, plugins, TUI, base de datos. Si
algún día hace falta, se añade en el sitio obvio.

## Tests

```bash
cd "/mnt/c/ALL/Coding/Claude Sessions/maestro"
~/.local/share/uv/tools/maestro/bin/python -m unittest discover -s tests -v
```

Las fixtures de `tests/fixtures/` son capturas reales de tmux de sesiones
lanzadas por Maestro (Claude Code 2.1.276). Si una versión nueva de Claude
cambia la interfaz y la detección falla, captura la pantalla nueva
(`tmux capture-pane -p -t <pane> -S -60`), guárdala ahí y ajusta los patrones
de `claude.py`.

## Notas

- `mcp` está fijado a `<2`: la 2.x renombró `FastMCP` a `MCPServer`.
- El código es editable en su sitio (`uv tool install --editable`): tras un
  cambio basta con reiniciar `maestro-server`; responde a SIGTERM y sale limpio.
