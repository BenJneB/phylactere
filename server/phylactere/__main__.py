"""Point d'entrée : ``python -m phylactere``.

Lit le port dans la configuration et écoute toujours sur 127.0.0.1.
"""

import argparse
import sys

import uvicorn

from phylactere import __version__
from phylactere.config import ConfigError, load_settings, override_path, user_config_path

HOST = "127.0.0.1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="phylactere", description="Serveur local Phylactère")
    parser.add_argument("--port", type=int, help="remplace le port de la configuration")
    parser.add_argument(
        "--reload", action="store_true", help="redémarre à chaque modification du code (dev)"
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Erreur de configuration : {exc}", file=sys.stderr)
        return 1

    port = args.port or settings.server.port
    print(f"Phylactère {__version__}")
    print(f"Configuration : {user_config_path()}")
    if (extra := override_path()) is not None:
        print(f"Surcharges    : {extra}")
    print(f"Adresse       : http://{HOST}:{port}")
    print(f"Jeton         : {settings.server.token}")
    print("(à saisir dans le popup de l'extension)", flush=True)

    uvicorn.run(
        "phylactere.main:create_app",
        factory=True,
        host=HOST,
        port=port,
        reload=args.reload,
        log_level=settings.server.log_level,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
