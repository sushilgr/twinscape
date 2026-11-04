"""Runs automatically when the TwinscapeUE project opens (PythonScriptPlugin convention).

Puts the repository's Unreal scripts on sys.path, points them at the repository (so the HDA
and the cached sample areas are found), and adds the Twinscape menu to the main menu bar.
"""
import os
import sys

import unreal

_proj = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir())
_repo = os.path.normpath(os.path.join(_proj, "..", ".."))          # <repo>/unreal/TwinscapeUE
_scripts = os.path.join(_repo, "unreal", "scripts")

if os.path.isdir(_scripts):
    if _scripts not in sys.path:
        sys.path.append(_scripts)
    os.environ.setdefault("TWINSCAPE", _repo)
    try:
        import earthtwin_unreal as et
        et.register_menu()
        unreal.log("[Twinscape] ready - repository: %s" % _repo)
    except Exception as e:  # never block the editor from opening
        unreal.log_warning("[Twinscape] could not load scripts: %s" % e)
else:
    unreal.log_warning("[Twinscape] unreal/scripts not found next to this project (%s)" % _scripts)
