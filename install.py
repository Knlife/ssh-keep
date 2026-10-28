#!/usr/bin/env python3
"""Install a self-contained Python zipapp without pip or network access."""
from pathlib import Path
import argparse
import datetime
import os
import shutil
import sys
import tempfile
import zipapp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local")
    args = parser.parse_args()
    if sys.version_info < (3, 9):
        parser.error("Python 3.9 or newer is required")
    root = Path(__file__).resolve().parent
    destination = args.prefix.expanduser().resolve() / "bin/ssh-tunnel-watch"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="stw-build-") as temporary:
        source = Path(temporary) / "src"
        shutil.copytree(root / "src", source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
        archive = Path(temporary) / "ssh-tunnel-watch.pyz"
        zipapp.create_archive(source, archive, interpreter="/usr/bin/env python3", main="ssh_tunnel_watch.cli:main")
        stage = destination.with_name(destination.name + ".install-" + str(os.getpid()))
        try:
            shutil.copyfile(archive, stage)
            os.chmod(stage, 0o755)
            if destination.exists() or destination.is_symlink():
                stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                backup = destination.with_name(destination.name + ".before-" + stamp)
                shutil.copy2(destination, backup)
                print("Previous executable backed up:", backup)
            os.replace(stage, destination)
        finally:
            stage.unlink(missing_ok=True)
    print("Installed:", destination)
    print("Keep your JSON config outside the executable; SSH keys stay in ~/.ssh.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
