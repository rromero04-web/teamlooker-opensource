# Arquitectura y seguridad de TeamLooker

## Componentes

| Componente | Módulo | Qué hace |
|---|---|---|
| Transporte directo | `teamlooker/direct.py` | El anfitrión escucha en un puerto TCP; el visor conecta directamente. **Modo por defecto, sin servidor.** |
| Traversía NAT | `teamlooker/nat.py` | Abre el puerto en el router del anfitrión con UPnP/NAT-PMP (solo habla con el gateway local). |
| Relay (opcional) | `teamlooker/relay.py` | Servidor propio que empareja por ID y reenvía bytes a ciegas, para redes donde la conexión directa no es posible. |
| Anfitrión | `teamlooker/host.py` | Escucha (directo) o se registra (relay), autentica visores y sirve sesiones (pantalla, entrada, archivos, chat, portapapeles). |
| Visor | `teamlooker/viewer.py` | Abre la conexión (directa o por relay), autentica y ofrece una API asíncrona a la interfaz. |
| Interfaz | `teamlooker/ui/` | Servidor web local (aiohttp) en `127.0.0.1` y páginas HTML/JS. |

La aplicación de escritorio (`teamlooker app`) ejecuta a la vez un anfitrión (para que el equipo sea accesible) y la interfaz, desde la que se abren sesiones de visor.

## Conexión directa (por defecto)

En modo directo no interviene ningún servidor:

1. El anfitrión abre un servidor TCP en `direct_port` (7570 por defecto) e intenta, en segundo plano, abrir ese puerto en su router con **NAT-PMP** (UDP al gateway) y, si falla, **UPnP IGD** (descubrimiento SSDP + SOAP `AddPortMapping`). Ninguna de las dos cosa contacta con servidores externos: solo con el router local.
2. La interfaz muestra un **código de conexión** de 10 caracteres (Crockford base32) que empaqueta la IP y el puerto (`ids.encode_connection_code`), además de las direcciones locales y la pública si el mapeo tuvo éxito.
3. El visor introduce el código (o `IP:puerto`), abre una conexión TCP directa al anfitrión y ejecuta exactamente el mismo handshake SRP + canal AES-256-GCM que en modo relay.

Si la traversía NAT no es posible (CGNAT, UPnP desactivado, red corporativa), el usuario abre el puerto manualmente o usa el modo relay. La seguridad no depende de la red: aunque el puerto esté abierto a internet, sin la contraseña no se establece la sesión, y hay bloqueo exponencial ante intentos fallidos.

## Protocolo con el relay

Todas las tramas llevan un prefijo de 4 bytes (longitud, big-endian). Antes del emparejamiento son JSON.

**Registro del anfitrión** (conexión de control persistente):

```
anfitrión → {"type":"register","pubkey":<Ed25519 pública, base64>}
relay     → {"type":"challenge","nonce":<32 bytes aleatorios>}
anfitrión → {"type":"proof","sig":firma("teamlooker-register:" + nonce)}
relay     → {"type":"registered","id":"123456789"}
```

El ID son 9 dígitos derivados de `SHA-256("teamlooker-device-id:" + clave pública)`. Solo quien tiene la clave privada puede registrar ese ID. El relay envía `ping` cada 30 s y el anfitrión responde `pong`; si pasan 90 s sin nada, se descarta.

**Conexión de un visor**:

```
visor     → {"type":"connect","target":"123456789"}           (conexión nueva)
relay     → anfitrión: {"type":"incoming","session":<secreto aleatorio>,"peer":<IP>}
anfitrión → {"type":"accept","session":<secreto>}              (conexión nueva)
relay     → ambos: {"type":"paired"}
```

A partir de aquí el relay copia bytes entre los dos sockets sin interpretarlos. El relay limita los intentos de conexión por IP (30/min por defecto).

## Handshake de extremo a extremo (SRP-6a)

Parámetros: grupo de 2048 bits de RFC 5054, `g = 2`, SHA-256, identidad fija `teamlooker`.

El anfitrión puede aceptar dos contraseñas: la **de sesión** (aleatoria, 6 caracteres, cambia cuando el usuario pulsa «Nueva») y la **permanente** (acceso desatendido). De ninguna guarda la contraseña: solo `(sal, verificador)`.

```
visor     → {"type":"hello","proto":1,"A":…}
anfitrión → {"type":"challenge","offers":[{"salt","B"}, …]}      (una oferta por contraseña aceptada)
visor     → {"type":"proof","M1":[…]}                           (una prueba por oferta, con el mismo A)
anfitrión → {"type":"auth_ok","which":i,"M2":…}  |  {"type":"auth_fail","reason":…}
```

* El visor comprueba `M2`: así sabe que habla con quien conoce el verificador, y no con un relay malicioso que se hace pasar por el anfitrión.
* Quien observa o manipula el handshake (incluido el relay) no obtiene información para probar contraseñas offline; cada intento requiere hablar con el anfitrión.
* Tras 3 fallos, el anfitrión bloquea nuevos intentos durante 5 s, 10 s, 20 s… hasta 5 min (`auth_fail` con `reason: "locked"`).

## Canal cifrado

De la clave de sesión SRP `K` se derivan, con HKDF-SHA256, dos claves AES-256-GCM (una por sentido: `viewer->host` y `host->viewer`). El nonce de 96 bits es un contador que empieza en 0 y crece en cada trama; como el transporte es ordenado no se transmite. Una trama reenviada, reordenada, truncada o modificada no descifra y la sesión se cierra.

Cada mensaje de aplicación es: longitud de cabecera (4 bytes) + cabecera JSON + cuerpo binario opcional.

## Mensajes de aplicación

| Sentido | Tipo | Contenido |
|---|---|---|
| V→A | `viewer_hello` | nombre del visor |
| A→V | `session_info` | nombre, SO, monitores, permisos, capacidades |
| A→V | `frame` | `width`, `height`, `rects: [[x,y,w,h,len]…]`, `cursor` + JPEGs concatenados |
| V→A | `frame_ack` | devuelve un crédito de control de flujo |
| A→V | `cursor` | posición normalizada del cursor cuando la pantalla no cambió |
| A→V | `screen_error` | no se pudo capturar la pantalla (la sesión sigue) |
| V→A | `mouse_move`, `mouse_button`, `mouse_scroll` | coordenadas normalizadas 0..1 sobre el monitor elegido |
| V→A | `key` | `key` y `code` de `KeyboardEvent`, `down` |
| V→A | `combo`, `type_text` | combinaciones (Ctrl+Alt+Supr…) y texto |
| V→A | `settings`, `monitor`, `refresh` | calidad, fps, escala, gris; monitor; fotograma completo |
| ambos | `chat` | texto |
| ambos | `clipboard` | texto del portapapeles |
| V→A | `fs_list`, `fs_op` → A→V `fs_result` | explorador remoto (listar, mkdir, renombrar, borrar vacíos) |
| ambos | `file_begin`, `file_chunk`, `file_end`, `file_cancel`, `file_result` | transferencias en bloques de 64 KiB con SHA-256 final |
| V→A | `file_get` | pedir una descarga |
| ambos | `bye` | fin de sesión |

### Pantalla

`screen.py` captura con `mss`, reduce la resolución si se pidió y compara con el fotograma anterior (`ImageChops.difference`). Solo se codifican las teselas de 64×64 que cambiaron, fusionando las contiguas de cada fila en un único JPEG. El anfitrión tiene como mucho 3 fotogramas sin confirmar (`frame_ack`), lo que adapta la velocidad a la red y al navegador del visor sin acumular retraso.

### Entrada

El visor envía coordenadas normalizadas, así que la escala de visualización y la resolución transmitida no afectan a la precisión. Las teclas se envían con `key`/`code` del navegador y se traducen con `inputctl.map_key`; al soltar se libera exactamente lo que se pulsó, y al terminar la sesión se liberan todas las teclas y botones pendientes.

## Modelo de amenazas

| Atacante | Protección |
|---|---|
| Observador de red | Todo va cifrado con AES-256-GCM; con `tls://` también se ocultan los IDs. |
| Operador del relay | Solo ve IDs, IPs y volumen de tráfico. No puede leer, modificar ni suplantar (SRP autentica a ambos extremos). |
| Alguien que adivina contraseñas | Solo online, con bloqueo exponencial en el anfitrión y límite por IP en el relay. |
| Otro programa o web en el mismo equipo del visor | La interfaz solo escucha en `127.0.0.1` y exige un token aleatorio de 192 bits en cada petición y WebSocket; CSP restrictiva. |
| Visor autenticado | Tiene el acceso que el anfitrión concede (`control`, `files`, `clipboard`), comprobado en el anfitrión. Los nombres de archivo recibidos se reducen a un solo componente; no hay borrado recursivo remoto. |

Limitaciones conocidas: quien conoce la contraseña tiene, con los permisos por defecto, el mismo acceso que el usuario del anfitrión (igual que en TeamViewer). La contraseña de sesión es de 6 caracteres (≈30 bits): suficiente con bloqueo online, pero para acceso desatendido usa una contraseña permanente larga.
