"""Ponto de entrada do DiagCompleto.

    python main.py                 abre a interface gráfica
    python main.py saude --html    diagnóstico pela linha de comando
    python main.py --help          lista todos os comandos
"""
import sys


def main() -> int:
    if sys.platform != "win32":
        print("O DiagCompleto funciona apenas no Windows.")
        return 1
    from diagcompleto.utils import setup_logging

    setup_logging()
    if len(sys.argv) > 1:
        from diagcompleto.cli import main as cli_main

        return cli_main(sys.argv[1:])
    from diagcompleto.gui import run

    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
