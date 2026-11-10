"""Arma dist/IslaDinamica-Setup.exe:  python build.py   (necesita: pip install pyinstaller)
1. IslaDinamica.exe en una carpeta (arranca rápido: importa, no descomprime; importa porque cada evento
   de Claude Code lanza el hook).
2. Esa carpeta comprimida dentro de un instalador de un solo archivo."""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
DIST, BUILD = HERE / "dist", HERE / "build"


def pyinstaller(*args):
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--distpath", str(DIST),
                    "--workpath", str(BUILD), "--specpath", str(BUILD), *args], check=True, cwd=HERE)


def main():
    shutil.rmtree(DIST, ignore_errors=True)
    import island
    island.robot_image().save(HERE / "icono.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                                                         (128, 128), (256, 256)])
    pyinstaller("--name", "IslaDinamica", "--icon", str(HERE / "icono.ico"),
                "--collect-submodules", "winrt", "--collect-all", "screen_brightness_control",
                "--hidden-import", "pystray._win32", "--hidden-import", "claude_hook",
                "--hidden-import", "install_hooks", str(HERE / "app.py"))
    zip_base = BUILD / "IslaDinamica"
    shutil.make_archive(str(zip_base), "zip", DIST / "IslaDinamica")
    pyinstaller("--onefile", "--name", "IslaDinamica-Setup", "--icon", str(HERE / "icono.ico"),
                "--add-data", f"{zip_base}.zip;.", str(HERE / "instalador.py"))
    print("Listo:", DIST / "IslaDinamica-Setup.exe")


if __name__ == "__main__":
    main()
