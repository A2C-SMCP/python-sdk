"""Check the built distribution without development dependencies (#224).

Run with ``python tests/packaging/check_wheel_install.py`` (requires uv).
The temporary environment and artifacts are removed on success or failure.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="a2c-wheel-check-") as directory:
        work = Path(directory)

        def run(*args: str) -> None:
            subprocess.run(args, cwd=work, check=True, timeout=300)

        run("uv", "build", "--wheel", "--out-dir", str(work / "dist"), str(root))
        (wheel,) = (work / "dist").glob("*.whl")
        environment = work / "venv"
        run("uv", "venv", "--python", sys.executable, str(environment))
        bin_dir = environment / ("Scripts" if os.name == "nt" else "bin")
        python = str(bin_dir / ("python.exe" if os.name == "nt" else "python"))

        # YAML is a core runtime dependency, not a side effect of CLI/dev extras.
        run("uv", "pip", "install", "--python", python, str(wheel))
        run(python, "-I", "-c", "import yaml; assert yaml.safe_load('name: sample') == {'name': 'sample'}")

        # Computer currently imports CLI dependencies, so check it with the extra.
        # Never install dev/test dependency groups: poethepoet would mask #224.
        run("uv", "pip", "install", "--python", python, f"{wheel}[cli]")
        run(
            python,
            "-I",
            "-c",
            "from a2c_smcp.computer import Computer; "
            "from a2c_smcp.computer.skills.staging import parse_skill_frontmatter; "
            "assert parse_skill_frontmatter('---\\nname: sample\\n---\\nBody') == {'name': 'sample'}",
        )

        run(str(bin_dir / ("a2c-computer.exe" if os.name == "nt" else "a2c-computer")), "--help")


if __name__ == "__main__":
    main()
