"""Proxy SOCKS5 local a rotation d'adresse source IPv6.

Objectif
--------
Playwright/Chromium n'offre AUCUNE option native pour choisir l'adresse IP
source d'une connexion sortante (il n'existe pas de `local_address` dans
l'API `browser.launch()` / `browser.new_context()`). La seule chose que
Playwright sait faire, c'est parler a un proxy HTTP/SOCKS5 via l'option
`proxy`.

Ce script comble ce manque: il expose un proxy SOCKS5 en local
(127.0.0.1:PORT) qui, pour chaque connexion sortante, choisit une adresse
IPv6 aleatoire dans le bloc /64 route vers ce serveur (ex: bloc gratuit
OVHcloud), s'y "bind" via une socket dediee, puis relaie le trafic vers la
destination (TikTok, Facebook, etc.).

Cote code de scraping, RIEN ne change: on branche simplement ce proxy via
la configuration existante (`TIKTOK_PROXY_SERVER=socks5://127.0.0.1:1080`
dans `.env`), qui est deja lue par `platform/tiktok scraper/scraper.py` et
peut donc etre reutilisee telle quelle pour n'importe quelle autre
plateforme suivant le meme pattern (`_build_proxy_config` / `TIKTOK_PROXY_*`).

Pre-requis systeme (a executer une fois sur le serveur, en root):
1) Le bloc IPv6 /64 doit etre routable localement (AnyIP):
     sudo ip -6 route add local <PREFIX>/64 dev lo
2) Le noyau doit autoriser le bind sur des adresses non locales:
     sudo sysctl -w net.ipv6.ip_nonlocal_bind=1

Modes de rotation (IPV6_PROXY_ROTATE_MODE):
- manual (defaut, recommande pour ce projet): une adresse fixe utilisee pour
  toutes les connexions, jusqu'a ce qu'un appel HTTP explicite sur
  `http://127.0.0.1:<control-port>/rotate` en demande une nouvelle. C'est ce
  mode que `scraper.py` pilote lui-meme pour appliquer la regle "une nouvelle
  IPv6 toutes les N videos scrapees" (voir TIKTOK_IPV6_ROTATE_EVERY_N_POSTS).
- per_connection: une nouvelle adresse aleatoire a CHAQUE connexion sortante
  (chaque CONNECT SOCKS5, donc potentiellement plusieurs par page). Utile
  pour un simple test de dispersion d'IP, moins pour un usage "scraping".
- per_host: une adresse par (destination, adresse aleatoire) reutilisee
  pendant IPV6_PROXY_HOST_TTL_SECONDS.
- sticky: comme manual, mais sans le port de controle (une seule adresse
  pour toute la duree de vie du process, non modifiable a chaud).

Limites connues:
- Ne fonctionne que si la destination possede un enregistrement AAAA
  (TikTok en a un pour la plupart de ses domaines, mais pas garanti a 100%).
  Sans AAAA, le proxy retombe automatiquement sur une connexion IPv4
  "normale" (pas de rotation) plutot que d'echouer.
- Toutes les adresses partagent le meme bloc/AS (celui de votre hebergeur).
  Cela repartit la charge de requetes mais ne remplace pas une vraie
  diversite d'IP residentielles: combinez avec les autres mesures
  (profil persistant, delais humains, etc.) deja en place dans le scraper.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import os
import random
import socket
import struct
import time

LOGGER = logging.getLogger("ipv6_rotating_proxy")

# Sur Linux, IPV6_FREEBIND (niveau IPPROTO_IPV6) n'est pas toujours expose
# comme constante nommee par le module `socket` de Python, meme quand le
# noyau le supporte (kernel >= 4.20 / Ubuntu 20.04+). On le tente en best
# effort en plus du sysctl global `net.ipv6.ip_nonlocal_bind=1`, qui reste
# la methode principale et la plus fiable (voir README).
_IPV6_FREEBIND = 78


def _maybe_set_freebind(sock_obj: socket.socket) -> None:
    for level, optname in ((socket.IPPROTO_IPV6, _IPV6_FREEBIND), (socket.SOL_IP, 15)):
        try:
            sock_obj.setsockopt(level, optname, 1)
        except OSError:
            continue


class Ipv6Pool:
    """Genere/choisit des adresses IPv6 aleatoires dans un prefixe donne."""

    def __init__(self, prefix: str, mode: str, host_ttl_seconds: float):
        self.network = ipaddress.IPv6Network(prefix, strict=False)
        self.mode = mode
        self.host_ttl_seconds = host_ttl_seconds
        self._sticky_address: str | None = None
        self._per_host_cache: dict[str, tuple[str, float]] = {}

    def _random_address(self) -> str:
        host_bits = self.network.max_prefixlen - self.network.prefixlen
        offset = random.getrandbits(host_bits)
        address = ipaddress.IPv6Address(int(self.network.network_address) + offset)
        return str(address)

    def get_address(self, dest_host: str) -> str:
        if self.mode in ("sticky", "manual"):
            # "sticky": une seule adresse pour toute la duree de vie du process.
            # "manual": pareil, MAIS un appel externe a force_rotate() (via le
            # port de controle HTTP) peut la changer a la demande. C'est ce
            # mode que le scraper utilise pour la rotation "toutes les N
            # videos": lui seul decide QUAND changer d'adresse, le proxy se
            # contente d'appliquer la derniere adresse choisie a toute
            # nouvelle connexion sortante.
            if self._sticky_address is None:
                self._sticky_address = self._random_address()
                LOGGER.info("Initial IPv6 source address selected: %s", self._sticky_address)
            return self._sticky_address

        if self.mode == "per_host":
            now = time.monotonic()
            cached = self._per_host_cache.get(dest_host)
            if cached and (now - cached[1]) < self.host_ttl_seconds:
                return cached[0]
            address = self._random_address()
            self._per_host_cache[dest_host] = (address, now)
            return address

        # per_connection (defaut)
        return self._random_address()

    def force_rotate(self) -> str:
        """Choisit immediatement une nouvelle adresse aleatoire (mode manual/sticky).

        Utilise par le controleur HTTP (`/rotate`) appele depuis scraper.py.
        Retourne la nouvelle adresse pour pouvoir la logger/afficher.
        """
        self._sticky_address = self._random_address()
        LOGGER.info("IPv6 source address rotated manually -> %s", self._sticky_address)
        return self._sticky_address


async def _socks5_handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> tuple[str, int] | None:
    """Negocie une session SOCKS5 minimale (sans auth, commande CONNECT)."""
    greeting = await reader.readexactly(2)
    version, nmethods = greeting
    if version != 0x05:
        return None
    await reader.readexactly(nmethods)
    writer.write(bytes([0x05, 0x00]))  # no auth required
    await writer.drain()

    header = await reader.readexactly(4)
    _ver, cmd, _rsv, atyp = header
    if cmd != 0x01:  # seul CONNECT est supporte
        writer.write(bytes([0x05, 0x07, 0x00, 0x01]) + b"\x00" * 6)
        await writer.drain()
        return None

    if atyp == 0x01:  # IPv4
        raw = await reader.readexactly(4)
        dest_host = socket.inet_ntoa(raw)
    elif atyp == 0x03:  # domaine
        length = (await reader.readexactly(1))[0]
        dest_host = (await reader.readexactly(length)).decode("idna", errors="ignore")
    elif atyp == 0x04:  # IPv6
        raw = await reader.readexactly(16)
        dest_host = socket.inet_ntop(socket.AF_INET6, raw)
    else:
        writer.write(bytes([0x05, 0x08, 0x00, 0x01]) + b"\x00" * 6)
        await writer.drain()
        return None

    port_raw = await reader.readexactly(2)
    dest_port = struct.unpack("!H", port_raw)[0]
    return dest_host, dest_port


async def _resolve_target(loop: asyncio.AbstractEventLoop, host: str, port: int) -> tuple[int, str]:
    """Resout `host` et retourne (famille, adresse) en preferant l'IPv6."""
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ConnectionError(f"DNS resolution failed for {host}: {exc}") from exc

    ipv6_candidates = [info for info in infos if info[0] == socket.AF_INET6]
    if ipv6_candidates:
        return socket.AF_INET6, ipv6_candidates[0][4][0]

    ipv4_candidates = [info for info in infos if info[0] == socket.AF_INET]
    if ipv4_candidates:
        return socket.AF_INET, ipv4_candidates[0][4][0]

    raise ConnectionError(f"No usable DNS record for {host}")


async def _open_rotated_connection(
    loop: asyncio.AbstractEventLoop, host: str, port: int, pool: Ipv6Pool
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
    family, resolved_addr = await _resolve_target(loop, host, port)

    if family != socket.AF_INET6:
        # Pas d'AAAA pour cette cible: on retombe sur une connexion IPv4
        # classique (pas de rotation possible) plutot que d'echouer.
        reader, writer = await asyncio.open_connection(resolved_addr, port)
        return reader, writer, "ipv4-fallback (no AAAA record)"

    source_address = pool.get_address(host)
    sock_obj = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock_obj.setblocking(False)
    _maybe_set_freebind(sock_obj)
    try:
        sock_obj.bind((source_address, 0))
    except OSError as exc:
        sock_obj.close()
        raise ConnectionError(
            f"Impossible de bind sur {source_address}: {exc}. "
            "Verifiez `ip -6 route add local <prefix>/64 dev lo` et "
            "`sysctl net.ipv6.ip_nonlocal_bind=1`."
        ) from exc

    await loop.sock_connect(sock_obj, (resolved_addr, port))
    reader, writer = await asyncio.open_connection(sock=sock_obj)
    return reader, writer, source_address


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    pool: Ipv6Pool,
) -> None:
    peer = client_writer.get_extra_info("peername")
    loop = asyncio.get_running_loop()
    try:
        target = await _socks5_handshake(client_reader, client_writer)
        if target is None:
            return
        dest_host, dest_port = target

        try:
            remote_reader, remote_writer, used_source = await _open_rotated_connection(
                loop, dest_host, dest_port, pool
            )
        except Exception as exc:
            LOGGER.warning("Connect failed to %s:%s (%s)", dest_host, dest_port, exc)
            client_writer.write(bytes([0x05, 0x01, 0x00, 0x01]) + b"\x00" * 6)
            await client_writer.drain()
            return

        LOGGER.info("CONNECT %s -> %s:%s via %s", peer, dest_host, dest_port, used_source)

        client_writer.write(bytes([0x05, 0x00, 0x00, 0x01]) + b"\x00\x00\x00\x00" + b"\x00\x00")
        await client_writer.drain()

        await asyncio.gather(
            _pump(client_reader, remote_writer),
            _pump(remote_reader, client_writer),
        )
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    except Exception:
        LOGGER.exception("Unexpected error handling client %s", peer)
    finally:
        try:
            client_writer.close()
        except Exception:
            pass


async def _handle_control_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, pool: Ipv6Pool
) -> None:
    """Repond a un mini-serveur HTTP "fait main" (une seule route utile: /rotate).

    On evite volontairement une vraie librairie HTTP: le proxy doit rester
    sans dependance externe. On lit juste la ligne de requete
    ("GET /rotate HTTP/1.1"), on ignore les en-tetes, et on repond en texte
    brut. Suffisant pour un usage 100% local (127.0.0.1) depuis scraper.py.
    """
    try:
        request_line = await reader.readline()
        # On consomme et jette le reste des en-tetes HTTP jusqu'a la ligne vide.
        while True:
            line = await reader.readline()
            if not line or line in (b"\r\n", b"\n"):
                break

        path = request_line.decode("latin-1", errors="ignore").split(" ")[1] if b" " in request_line else "/"

        if path.rstrip("/") == "/rotate":
            new_address = pool.force_rotate()
            body = f"OK {new_address}\n".encode()
        elif path.rstrip("/") in ("", "/status"):
            body = f"OK mode={pool.mode}\n".encode()
        else:
            body = b"NOT_FOUND\n"

        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        writer.write(response)
        await writer.drain()
    except Exception:
        LOGGER.warning("Control request failed", exc_info=True)
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def main_async(args: argparse.Namespace) -> None:
    pool = Ipv6Pool(args.prefix, args.rotate_mode, args.host_ttl_seconds)
    server = await asyncio.start_server(
        lambda r, w: _handle_client(r, w, pool),
        host=args.listen_host,
        port=args.listen_port,
    )
    LOGGER.info(
        "IPv6 rotating SOCKS5 proxy listening on %s:%s (prefix=%s, mode=%s)",
        args.listen_host,
        args.listen_port,
        args.prefix,
        args.rotate_mode,
    )

    control_server = None
    if args.control_port:
        control_server = await asyncio.start_server(
            lambda r, w: _handle_control_client(r, w, pool),
            host=args.listen_host,
            port=args.control_port,
        )
        LOGGER.info(
            "Control endpoint listening on http://%s:%s/rotate", args.listen_host, args.control_port
        )

    async with server:
        if control_server is not None:
            async with control_server:
                await asyncio.gather(server.serve_forever(), control_server.serve_forever())
        else:
            await server.serve_forever()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prefix",
        default=os.getenv("IPV6_PROXY_PREFIX", ""),
        help="Bloc IPv6 route localement, ex: 2001:41d0:2:9434::/64",
    )
    parser.add_argument(
        "--listen-host",
        default=os.getenv("IPV6_PROXY_LISTEN_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=int(os.getenv("IPV6_PROXY_LISTEN_PORT", "1080")),
    )
    parser.add_argument(
        "--rotate-mode",
        choices=["per_connection", "per_host", "sticky", "manual"],
        default=os.getenv("IPV6_PROXY_ROTATE_MODE", "manual"),
        help=(
            "manual: une adresse fixe jusqu'a un appel explicite a /rotate "
            "(c'est ce que scraper.py utilise pour la rotation toutes les N videos)."
        ),
    )
    parser.add_argument(
        "--host-ttl-seconds",
        type=float,
        default=float(os.getenv("IPV6_PROXY_HOST_TTL_SECONDS", "300")),
    )
    parser.add_argument(
        "--control-port",
        type=int,
        default=int(os.getenv("IPV6_PROXY_CONTROL_PORT", "8091")),
        help="Port HTTP local exposant /rotate. Mettre a 0 pour desactiver.",
    )
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = build_arg_parser()
    args = parser.parse_args()

    if not args.prefix:
        parser.error("Prefixe IPv6 manquant (--prefix ou variable d'env IPV6_PROXY_PREFIX)")

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        LOGGER.info("Proxy interrupted by user")


if __name__ == "__main__":
    main()
