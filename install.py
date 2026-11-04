#!/usr/bin/env python3
"""Install Twinscape into Houdini by writing a package file that points at this folder.

    python3 install.py                     # newest Houdini found, e.g. 22.0
    python3 install.py --houdini 21.0      # a specific version
    python3 install.py --prefs <dir>       # an explicit Houdini user prefs folder
    python3 install.py --uninstall

The package sets $TWINSCAPE to this repository, adds houdini/python to PYTHONPATH and
houdini/ to HOUDINI_PATH (so the HDA in houdini/otls loads). Houdini Engine inside Unreal
reads the same user prefs, so this one step covers both apps. Restart Houdini afterwards.
"""
import argparse
import glob
import json
import os
import platform
import re
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
PKG_NAME = "twinscape.json"


def prefs_roots():
    home = os.path.expanduser("~")
    system = platform.system()
    if system == "Darwin":
        return sorted(glob.glob(os.path.join(home, "Library", "Preferences", "houdini", "[0-9]*.[0-9]*")))
    if system == "Windows":
        docs = os.path.join(os.environ.get("USERPROFILE", home), "Documents")
        return sorted(glob.glob(os.path.join(docs, "houdini[0-9]*.[0-9]*")))
    return sorted(glob.glob(os.path.join(home, "houdini[0-9]*.[0-9]*")))


def version_of(path):
    m = re.search(r"(\d+)\.(\d+)$", path.rstrip("/\\"))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def default_prefs(version):
    home = os.path.expanduser("~")
    system = platform.system()
    if system == "Darwin":
        return os.path.join(home, "Library", "Preferences", "houdini", version)
    if system == "Windows":
        return os.path.join(os.environ.get("USERPROFILE", home), "Documents", "houdini" + version)
    return os.path.join(home, "houdini" + version)


def package_json():
    return {
        "env": [
            {"TWINSCAPE": REPO.replace("\\", "/")},
            {"PYTHONPATH": {"value": "$TWINSCAPE/houdini/python", "method": "prepend"}},
        ],
        "path": "$TWINSCAPE/houdini",
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--houdini", help="Houdini version folder to install into, e.g. 22.0")
    ap.add_argument("--prefs", help="explicit Houdini user prefs folder (overrides --houdini)")
    ap.add_argument("--uninstall", action="store_true", help="remove the Twinscape package file")
    a = ap.parse_args()

    if a.prefs:
        prefs = a.prefs
    elif a.houdini:
        prefs = default_prefs(a.houdini)
    else:
        found = prefs_roots()
        if not found:
            sys.exit("No Houdini preferences folder found. Start Houdini once, or pass --houdini 22.0 / --prefs <dir>.")
        prefs = max(found, key=version_of)

    pkg_dir = os.path.join(prefs, "packages")
    target = os.path.join(pkg_dir, PKG_NAME)
    if a.uninstall:
        if os.path.exists(target):
            os.remove(target)
            print("Removed", target)
        else:
            print("Nothing to remove at", target)
        return

    os.makedirs(pkg_dir, exist_ok=True)
    with open(target, "w") as f:
        json.dump(package_json(), f, indent=2)
    print("Twinscape installed")
    print("  repository : " + REPO)
    print("  package    : " + target)
    legacy = os.path.join(pkg_dir, "earthtwin.json")
    if os.path.exists(legacy):
        print("  note       : an older 'earthtwin.json' package is also installed in that folder.\n"
              "               It defines the same HDA; remove it so Houdini loads this one.")
    print("Restart Houdini, then: Tab > 'Twinscape City Builder', or open houdini/scenes/Twinscape_demo.hip")


if __name__ == "__main__":
    main()
