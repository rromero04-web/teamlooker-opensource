<p align="center">
  <img src="docs/img/logo.png" width="120" alt="TeamLooker">
</p>

<h1 align="center">TeamLooker</h1>

<p align="center"><b>Escritorio remoto de código abierto — conexión directa entre PCs, sin servidores.</b></p>

TeamLooker reúne las funciones principales de TeamViewer: conectarte a otro equipo con un **código y una contraseña**, ver y controlar su pantalla, transferir archivos, chatear y compartir el portapapeles. La conexión es **directa de PC a PC** (no hay ningún servidor intermediario) y todo el tráfico va **cifrado de extremo a extremo**.

![Ventana principal](docs/img/ventana-principal.png)

| Sesión remota (control real) | Transferencia de archivos |
|---|---|
| ![Sesión remota](docs/img/sesion-remota.png) | ![Archivos](docs/img/transferencia-archivos.png) |

## Descargar y usar (Windows, macOS, Linux)

En cada versión publicada hay un ejecutable autocontenido en la página de **[Releases](../../releases)** — no necesita instalación ni Python:

- **Windows**: `TeamLooker-windows.exe` → doble clic. Se abre la aplicación en tu navegador.
- **macOS**: `TeamLooker-macos` → `chmod +x` y ejecutar (la primera vez, permite *Grabación de pantalla* y *Accesibilidad*).
- **Linux**: `TeamLooker-linux` → `chmod +x` y ejecutar (sesión X11).

> Los ejecutables los compila automáticamente GitHub Actions ([workflow](.github/workflows/release.yml)) en cada sistema operativo. Si prefieres compilarlo tú, mira [Compilar el ejecutable](#compilar-el-ejecutable).

## Conectar en 3 pasos (sin servidor)

1. Abre TeamLooker en los dos equipos.
2. En el equipo que quieres controlar, mira su **código de conexión** (o su IP) y su **contraseña**.
3. En el otro equipo, escribe ese código, pulsa **Conectar** e introduce la contraseña.

* **En la misma red local** funciona sin configurar nada.
* **Entre redes distintas** (cada PC detrás de su router), TeamLooker intenta abrir el puerto automáticamente con **UPnP/NAT-PMP**. Si tu router no lo permite, abre el puerto (por defecto **7570/TCP**) en el router del equipo anfitrión, o usa una VPN. No hace falta ningún servicio externo.

## Funciones

| Función | TeamLooker |
|---|---|
| Conexión directa P2P, sin servidor | ✅ por defecto |
| Traversía de NAT automática (redes distintas) | ✅ UPnP / NAT-PMP, sin servicios externos |
| Código de conexión corto + contraseña de sesión | ✅ |
| Acceso desatendido con contraseña permanente | ✅ (solo se guarda un verificador SRP) |
| Ver la pantalla remota en tiempo real | ✅ solo se envían las zonas que cambian (teselas JPEG) |
| Control remoto de ratón y teclado | ✅ Ctrl+Alt+Supr, Win, Alt+Tab, Win+L |
| Varios monitores | ✅ selector de monitor |
| Calidad, resolución y escala de grises ajustables | ✅ control de flujo adaptativo |
| Cursor remoto visible | ✅ |
| Transferencia de archivos (dos paneles) | ✅ subir/bajar, crear/renombrar/borrar; integridad SHA-256 |
| Modo «solo transferencia de archivos» | ✅ |
| Chat en ambos sentidos | ✅ |
| Portapapeles de texto sincronizado | ✅ |
| Escribir tu portapapeles en el equipo remoto | ✅ |
| Modo «solo ver» | ✅ |
| Permisos del anfitrión (control/archivos/portapapeles) | ✅ se aplican en el anfitrión |
| Ver quién está conectado y expulsarlo | ✅ |
| Conexiones recientes | ✅ |
| Cifrado de extremo a extremo | ✅ SRP-6a + AES-256-GCM |
| Protección contra fuerza bruta | ✅ bloqueo exponencial en el anfitrión |
| Ejecutable único para Windows/macOS/Linux | ✅ |
| Servidor propio opcional (relay) para casos difíciles | ✅ modo alternativo |

### Comparación honesta con TeamViewer

TeamLooker cubre el **uso principal** de TeamViewer (control remoto + archivos + chat), pero **no** es un clon completo. Todavía **no** incluye: audio/vídeo remoto y videollamada, grabación de sesiones, VPN, Wake-on-LAN, impresión remota, transferencia de carpetas completas, captura en Wayland, control de la pantalla de inicio de sesión/UAC de Windows como servicio, apps móviles, ni una infraestructura global gestionada. La diferencia de diseño más importante es intencionada: **no hay servidores** — la conexión es directa, cosa que da privacidad y control total, a cambio de necesitar UPnP o abrir un puerto para conectar entre redes muy restringidas (CGNAT, redes corporativas).

## Compilar desde el código

Requiere **Python 3.10+**.

```bash
git clone https://github.com/rromero04-web/teamlooker-opensource.git
cd teamlooker-opensource
python -m pip install .
teamlooker            # abre la aplicación (modo directo por defecto)
```

Requisitos por sistema:

* **Linux**: sesión X11 (en Wayland, inicia sesión con «Xorg»). Portapapeles: instala `xclip` o `xsel`.
* **macOS**: concede *Grabación de pantalla* y *Accesibilidad* a tu terminal/Python.
* **Windows**: nada más. Para controlar ventanas de administrador, ejecútalo como administrador.

### Compilar el ejecutable

```bash
python -m pip install ".[build]"
python assets/make_icons.py                 # genera el icono
pyinstaller packaging/teamlooker.spec --noconfirm
# el ejecutable queda en dist/TeamLooker(.exe)
```

Para publicar los ejecutables de los tres sistemas, crea un tag `vX.Y.Z` y súbelo: el workflow de GitHub Actions los compila y los adjunta a la Release.

## Uso por línea de comandos

```bash
teamlooker                       # aplicación (modo directo)
teamlooker info                  # tu código/dirección y configuración
teamlooker host                  # compartir este equipo sin interfaz (muestra código + contraseña)
teamlooker set-password          # contraseña permanente (acceso desatendido)
teamlooker screenshot 60001-047CJ -o captura.png   # captura de un equipo remoto (por código o IP)
teamlooker app --viewer-only     # solo controlar; este equipo no será accesible
```

Para dejar un equipo Linux siempre accesible, hay un servicio de ejemplo en [`contrib/teamlooker-host.service`](contrib/teamlooker-host.service).

## Modo servidor propio (opcional)

Si necesitas conectar entre redes donde la conexión directa no es posible (CGNAT en ambos extremos, redes corporativas), puedes levantar tu propio **relay** —sigue siendo tu servidor, no uno público—:

```bash
teamlooker relay --port 7575                       # en un VPS o PC accesible
docker build -t teamlooker-relay . && docker run -d -p 7575:7575 teamlooker-relay
```

Luego, en *Opciones → Modo de conexión*, elige **Servidor propio** e indica `host:puerto`. Con TLS: `teamlooker relay --certfile fullchain.pem --keyfile privkey.pem` y conéctate a `tls://host:7575`. El relay solo empareja y reenvía bytes cifrados: no puede ver la pantalla, las pulsaciones, los archivos ni las contraseñas.

## Cómo funciona

```
        Equipo A (visor)                                   Equipo B (anfitrión)
   ┌───────────────────────┐                          ┌───────────────────────┐
   │  navegador (interfaz)  │      conexión TCP        │  escucha en un puerto │
   │  app TeamLooker  ──────┼─────── directa ─────────►│  captura + entrada    │
   └───────────────────────┘   (UPnP/NAT-PMP abre     └───────────────────────┘
                                 el puerto si hace falta)
        ◄════════ cifrado de extremo a extremo (SRP-6a + AES-256-GCM) ════════►
```

* El **código de conexión** codifica la dirección y el puerto del anfitrión en 10 caracteres fáciles de leer.
* La **contraseña nunca viaja** por la red: se usa SRP-6a (un protocolo estándar de autenticación por contraseña) para acordar la clave de sesión, y el canal se cifra con AES-256-GCM.
* La interfaz es una página web local (`127.0.0.1`, protegida con un token aleatorio) que se abre en tu navegador; no necesita Electron ni Qt.

Detalles del protocolo y del modelo de seguridad en [docs/ARQUITECTURA.md](docs/ARQUITECTURA.md).

## Desarrollo

```bash
python -m pip install -e ".[dev]"
pytest -q
```

Las pruebas levantan anfitrión y visor reales (con pantalla y entrada simuladas) y cubren: conexión **directa** y por relay, autenticación SRP, bloqueo por fuerza bruta, que el intermediario solo vea texto cifrado, streaming con control de flujo, entrada remota, multi-monitor, chat, portapapeles, explorador y transferencia de archivos, códigos de conexión, traversía NAT y la interfaz local.

## Aviso legal

TeamLooker es un proyecto independiente, no afiliado ni respaldado por TeamViewer. Úsalo solo en equipos sobre los que tengas autorización. Licencia [MIT](LICENSE).
