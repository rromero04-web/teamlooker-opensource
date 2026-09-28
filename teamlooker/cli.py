"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys

from teamlooker import __version__, relay
from teamlooker.config import Config
from teamlooker.ids import format_connection_code, format_id, parse_target


def _config(args) -> Config:
    config = Config()
    if getattr(args, "relay", None):
        from teamlooker.relay_client import RelayAddress
        config.data["relay"] = str(RelayAddress.parse(args.relay))
    return config


async def _run_host(args) -> None:
    from teamlooker.host import HostService

    config = _config(args)
    if args.permanent_password:
        config.set_permanent_password(args.permanent_password)

    def on_event(event: dict) -> None:
        kind = event["type"]
        if kind == "status":
            print(f"[estado] {event['status']} {event.get('detail', '')}".rstrip(), flush=True)
        elif kind == "session_started":
            s = event["session"]
            print(f"[sesión] {s['viewer']} conectado desde {s['peer']}", flush=True)
        elif kind == "session_ended":
            print("[sesión] finalizada", flush=True)
        elif kind == "chat":
            print(f"[chat] {event['sender']}: {event['text']}", flush=True)
        elif kind == "auth_failed":
            print(f"[aviso] contraseña incorrecta ({event['failures']} fallos)", flush=True)
        elif kind == "file_received":
            print(f"[archivo] recibido {event['path']}", flush=True)

    if getattr(args, "relay_mode", False):
        config.mode = "relay"
    host = HostService(config, on_event=on_event)
    if args.no_session_password:
        host._session_verifier = None
        host.session_password = ""
    if not host.verifiers():
        print("Error: sin contraseña de sesión ni permanente nadie podría conectarse.", file=sys.stderr)
        return
    print(f"TeamLooker {__version__} (modo anfitrión sin interfaz)")
    task = host.start()
    if config.mode == "direct":
        await asyncio.sleep(0.5)
        addr = host.direct_address
        if host.session_password:
            print(f"  Contraseña:  {host.session_password}")
        if config.permanent_verifier:
            print("  Acceso desatendido: contraseña permanente activa")
        if addr.code:
            print(f"  Código:      {format_connection_code(addr.code)}")
        for ip in addr.local_ips:
            print(f"  Dirección:   {ip}:{addr.port}  (misma red local)")
        print("  Esperando el mapeo del router (UPnP/NAT-PMP)…", flush=True)
        for _ in range(15):
            await asyncio.sleep(1)
            if host.direct_address.public_ip:
                pub = host.direct_address
                print(f"  Público:     {pub.public_ip}:{pub.public_port}  "
                      f"(vía {pub.mapping_method})  código {format_connection_code(pub.code)}",
                      flush=True)
                break
        else:
            print("  (sin mapeo automático: entre redes distintas, abre el puerto en tu router)",
                  flush=True)
    else:
        print(f"  ID:          {format_id(host.device_id)}")
        if host.session_password:
            print(f"  Contraseña:  {host.session_password}")
        print(f"  Servidor:    {config.relay}", flush=True)
    await task


async def _screenshot(args) -> None:
    from PIL import Image

    from teamlooker.relay_client import RelayAddress
    from teamlooker.screen import apply_frame
    from teamlooker.viewer import ViewerSession

    config = _config(args)
    password = args.password or getpass.getpass("Contraseña del asociado: ")
    if config.mode == "relay":
        viewer = ViewerSession.relay(RelayAddress.parse(config.relay), args.partner, password, name="cli")
    else:
        target = parse_target(args.partner)
        if not target:
            sys.exit("Código o dirección no válidos.")
        viewer = ViewerSession.direct(target[0], target[1], password, name="cli")
    info = await viewer.connect()
    print(f"Conectado a {info['hostname']} ({info['os']})")
    image: Image.Image | None = None
    if args.monitor:
        await viewer.send({"type": "monitor", "index": args.monitor - 1})
    while True:
        msg, data = await viewer.channel.recv_msg()
        if msg["type"] == "frame" and msg.get("monitor", 0) == max(0, args.monitor - 1):
            image = apply_frame(image, msg, data)
            break
    image.save(args.output)
    print(f"Captura guardada en {args.output} ({image.width}x{image.height})")
    await viewer.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="teamlooker",
        description="TeamLooker: escritorio remoto de código abierto.")
    parser.add_argument("--version", action="version", version=f"TeamLooker {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="mostrar registros detallados")
    sub = parser.add_subparsers(dest="command")

    p_app = sub.add_parser("app", help="abrir la aplicación (por defecto)")
    p_app.add_argument("--relay", help="servidor relay (host:puerto o tls://host:puerto)")
    p_app.add_argument("--ui-port", type=int, default=0, help="puerto local de la interfaz")
    p_app.add_argument("--no-browser", action="store_true", help="no abrir el navegador")
    p_app.add_argument("--viewer-only", action="store_true",
                       help="solo controlar otros equipos (este no será accesible)")

    p_relay = sub.add_parser("relay", help="ejecutar un servidor relay")
    relay.add_arguments(p_relay)

    p_host = sub.add_parser("host", help="compartir este equipo sin interfaz gráfica (conexión directa)")
    p_host.add_argument("--relay", help="usar un servidor relay en vez de conexión directa")
    p_host.add_argument("--relay-mode", action="store_true", help="usar el modo relay guardado")
    p_host.add_argument("--permanent-password", help="establecer y guardar una contraseña permanente")
    p_host.add_argument("--no-session-password", action="store_true",
                        help="aceptar solo la contraseña permanente")

    p_pw = sub.add_parser("set-password", help="configurar la contraseña permanente (acceso desatendido)")
    p_pw.add_argument("--clear", action="store_true", help="desactivar el acceso desatendido")

    p_shot = sub.add_parser("screenshot", help="guardar una captura de un equipo remoto")
    p_shot.add_argument("partner", help="código de conexión o IP:puerto del equipo remoto")
    p_shot.add_argument("-o", "--output", default="captura.png")
    p_shot.add_argument("-p", "--password", help="contraseña (se pedirá si falta)")
    p_shot.add_argument("--monitor", type=int, default=1, help="número de monitor (1, 2, …)")
    p_shot.add_argument("--relay", help="servidor relay")

    sub.add_parser("info", help="mostrar el ID y la configuración de este equipo")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    command = args.command or "app"
    try:
        if command == "app":
            from teamlooker.ui.server import run_app
            config = _config(args) if args.command else Config()
            asyncio.run(run_app(config, port=getattr(args, "ui_port", 0),
                                open_browser=not getattr(args, "no_browser", False),
                                enable_host=not getattr(args, "viewer_only", False)))
        elif command == "relay":
            asyncio.run(relay.run_from_args(args))
        elif command == "host":
            asyncio.run(_run_host(args))
        elif command == "set-password":
            config = Config()
            if args.clear:
                config.set_permanent_password(None)
                print("Acceso desatendido desactivado.")
                return
            first = getpass.getpass("Nueva contraseña permanente (mín. 8 caracteres): ")
            if len(first) < 8 or first != getpass.getpass("Repítela: "):
                sys.exit("Las contraseñas no coinciden o son demasiado cortas.")
            config.set_permanent_password(first)
            print("Contraseña permanente guardada (solo se almacena un verificador SRP).")
        elif command == "screenshot":
            asyncio.run(_screenshot(args))
        elif command == "info":
            from teamlooker.ids import device_id_from_public_key
            from teamlooker.nat import local_ips
            from teamlooker.relay_client import public_key_bytes
            config = Config()
            print(f"Modo:      {config.mode}")
            if config.mode == "direct":
                print(f"Puerto directo: {config.direct_port}  (UPnP/NAT-PMP: "
                      f"{'sí' if config.enable_upnp else 'no'})")
                for ip in local_ips():
                    print(f"Dirección local: {ip}:{config.direct_port}")
            else:
                print(f"ID:        {format_id(device_id_from_public_key(public_key_bytes(config.identity_key)))}")
                print(f"Servidor:  {config.relay}")
            print(f"Config:    {config.path}")
            print(f"Permisos:  {config.permissions}")
            print(f"Acceso desatendido: {'sí' if config.permanent_verifier else 'no'}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
