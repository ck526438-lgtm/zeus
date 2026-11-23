"""Point d'entrée PyInstaller : ZeusHFT.exe (console)."""
import sys

from zeus_hft.__main__ import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["gui"]))
