"""Run a background task with the dependency directory from the active add-on."""
from pathlib import Path
import runpy
import sys


blender = "--" in sys.argv
args = sys.argv[sys.argv.index("--") + 1:] if blender else sys.argv[1:]
dependencies, script_name, *arguments = args
directory = Path(__file__).resolve().parent
script = directory / script_name

sys.path[:0] = [dependencies, str(directory)]
sys.argv = [str(script), "--", *arguments] if blender else [str(script), *arguments]
runpy.run_path(str(script), run_name="__main__")
